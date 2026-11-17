#!/usr/bin/env python3
"""Хост-агент деплоя: читает состояние установки, доказывает её работоспособность, откатывает.

ЕДИНСТВЕННЫЙ модуль, уезжающий на хост (доставляется по stdin, вызывается подкомандой).
Второй способ читать установку означал бы, что снапшот и верификация расходятся ровно тогда,
когда меняется вывод CLI, — а расходится при этом именно ЦЕЛЬ ОТКАТА.

Принцип: каждый шаг утверждает ровно то, что проверил. «Команда сказала updated» и «скрипт не
упал» — не доказательства; за две недели именно они трижды позволили флоту разъехаться молча.

Подкоманды (аргумент — JSON, ответ — JSON в последней строке stdout):
  installed <ch,ch>   состояние каналов: installed | absent | undetermined
  verify    <spec>    отчёт со СТРУКТУРНЫМИ кодами проблем
  restore   <spec>    вернуть канал на снапшот
"""
import hashlib
import json
import re
import os
import pathlib
import pwd
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

import kimi_acceptance as ka

#: Окружение строится АЛЛОУЛИСТОМ, а не наследуется. Этот модуль доказывает, что стерильный
#: гейт работает; унаследуй он `GIT_DIR`/`GIT_INDEX_FILE`/`GIT_CONFIG_*` хоста — доказательство
#: считалось бы про другой репозиторий. Случайный `export GIT_*` на любой машине флота делал бы
#: проверку ложно красной или ложно зелёной.
#: `CODEX_HOME` — не украшение: этот репозиторий сам его уважает, и без него мы ЧИТАЛИ бы одну
#: установку (`~/.codex`), а `update()` через логин-шелл мутировал бы другую.
_ENV_ALLOW = ("PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "USER", "LOGNAME", "TMPDIR",
              "CODEX_HOME")
E = {k: v for k, v in os.environ.items() if k in _ENV_ALLOW}
E.update({"GIT_AUTHOR_NAME": "deploy-smoke", "GIT_AUTHOR_EMAIL": "smoke@deploy",
          "GIT_COMMITTER_NAME": "deploy-smoke", "GIT_COMMITTER_EMAIL": "smoke@deploy",
          "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
          "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"})

#: Единственный реестр каналов. Раньше знание о каналах лежало в четырёх местах с неявным
#: `else`, и опечатка в инвентаре молча трактовалась как codex и МУТИРОВАЛАСЬ; денилист и
#: default-fallthrough в этом репозитории проигрывали пять раз подряд.
#: `via_shim`: у Claude хуки ходят через `gates-run`, и проверять надо ИМЕННО его резолв;
#: у Codex шима нет вовсе, поэтому smoke идёт по установленному скрипту напрямую.
CHANNELS = {
    "claude": {"cli": "claude", "via_shim": True,
               "update": (["plugin", "marketplace", "update", "lenar-gates"],
                          ["plugin", "update", "gates@lenar-gates"]),
               "remove": ["plugin", "uninstall", "gates@lenar-gates"]},
    "codex": {"cli": "codex", "via_shim": False,
              "update": (["plugin", "marketplace", "upgrade", "lenar-gates"],
                         ["plugin", "add", "gates@lenar-gates"]),
              "remove": ["plugin", "remove", "gates@lenar-gates"]},
    "kimi": {"cli": "kimi", "via_shim": True, "update": (), "remove": []},
}


class InstallError(RuntimeError):
    pass


class ReleaseDigestError(InstallError):
    pass


class ReleaseArchiveError(InstallError):
    pass


_MAX_ENTRY_BYTES = 16 * 1024 * 1024
_MAX_EXTRACT_BYTES = 100 * 1024 * 1024


def _st(state, **kw):
    return {"state": state, **kw}


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, env=E, capture_output=True, text=True)


# ── состояние установки ────────────────────────────────────────────────────────────────────
def _installed_claude():
    """Правило резолва — ТО ЖЕ, что у шима `gates-run`: первый scope с АБСОЛЮТНЫМ installPath,
    где реально лежит `scripts/ladder_gate.py`. Иначе деплой верифицировал бы одну установку,
    а хуки исполняли другую."""
    p = pathlib.Path(os.path.expanduser("~/.claude/plugins/installed_plugins.json"))
    reg = str(p)                      # адрес мутации сообщается ЯВНО, см. `cmd_restore`
    try:
        d = json.loads(p.read_text())
    except (OSError, ValueError) as exc:
        # Нечитаемые ИЛИ отсутствующие метаданные — это НЕ «не установлен». Шим ловит ровно тот
        # же отказ (`json.load(open(...))` в общем `except`) и уходит в глоб-фолбэк, где
        # запускает СТАРШУЮ версию из кэша. Объявить это absent значило бы сказать «первая
        # установка» про машину, на которой прямо сейчас работает гейт.
        cached = sorted(pathlib.Path(os.path.expanduser(
            "~/.claude/plugins/cache/lenar-gates/gates")).glob("*/scripts/ladder_gate.py"))
        if cached:
            return _st("undetermined", registry=reg, why=f"метаданные недоступны ({exc}) — шим уйдёт "
                                                     f"в глоб-фолбэк и запустит "
                                                     f"{cached[-1].parent.parent}")
        return _st("absent", registry=reg, why=f"метаданных нет ({exc}) и кэш пуст")
    marketplace = p.with_name("known_marketplaces.json")
    try:
        source = json.loads(marketplace.read_text())["lenar-gates"]["source"]
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return _st("undetermined", registry=reg,
                   why=f"marketplace source не прочитан: {type(exc).__name__}: {exc}")
    if not isinstance(source, dict) or source.get("source") != "github" \
            or source.get("repo") != "shagiev/claude-gates":
        return _st("undetermined", registry=reg,
                   why="marketplace lenar-gates обязан использовать GitHub shagiev/claude-gates, "
                       f"а не {source!r}; локальная конфигурация не переписывается")
    try:
        entries = (d.get("plugins") or {}).get("gates@lenar-gates") or []
        cands = [(e.get("installPath") or "", e.get("version"), e.get("gitCommitSha"))
                 for e in entries]
    except AttributeError as exc:
        # Дрейф СХЕМЫ (список вместо словаря, строка вместо записи) вероятнее синтаксической
        # ошибки, а ловился только второй случай — первый летел мимо диагностики.
        return _st("undetermined", registry=reg,
                   why=f"схема installed_plugins.json не та, что ожидалась: {exc}")
    for ip, version, sha in cands:
        if os.path.isabs(ip) and (pathlib.Path(ip) / "scripts" / "ladder_gate.py").is_file():
            return _st("installed", version=version, sha=sha, path=ip, registry=reg)
    if cands:
        return _st("undetermined", registry=reg,
                   why="записи есть, но ни один installPath не резолвится — шим уйдёт в "
                       "глоб-фолбэк, версия не определена")
    return _st("absent", registry=reg, why="плагин не значится установленным")


def _codex_home() -> str:
    return os.environ.get("CODEX_HOME") or os.path.expanduser("~/.codex")


def _installed_codex():
    r = subprocess.run(["codex", "plugin", "list"], capture_output=True, text=True, env=E)
    if r.returncode != 0:
        return _st("undetermined", codex_home=_codex_home(),
                   why=f"codex plugin list rc={r.returncode}: {(r.stderr or '').strip()[:160]}")
    if "lenar-gates" not in r.stdout:
        # Разбор здесь заведомо хрупкий, и непрочитанный вывод обязан отличаться от «плагина
        # нет»: иначе смена формата CLI даёт «первая установка», деплой мутирует канал без
        # доказанной цели отката — ровно тот пустой вывод, неотличимый от «уже свежее».
        return _st("undetermined", codex_home=_codex_home(),
                   why="вывод `codex plugin list` не опознан — маркетплейс lenar-gates в нём "
                       "не упомянут вовсе")
    rows = []
    for line in r.stdout.splitlines():
        cols = re.split(r"\s{2,}", line.strip())
        if len(cols) < 4 or cols[0] != "gates@lenar-gates":
            continue
        # Статус разбирается ТОКЕНОМ. `"installed" in line` совпадало с `not installed`, и
        # снесённый плагин объявлялся установленным: цель отката становилась фикцией, а код
        # `not-installed` для этого канала не мог сработать никогда.
        rows.append((cols[1].split(",")[0].strip(), cols[2], cols[-1]))
    live = [r_ for r_ in rows if r_[0] == "installed"]
    if len(live) > 1:
        return _st("undetermined", why=f"плагин значится установленным {len(live)} раз — "
                                       "какая установка активна, не определить")
    if not live:
        if rows:
            return _st("absent", codex_home=_codex_home(), why=f"статус {rows[0][0]!r}")
        return _st("undetermined", codex_home=_codex_home(),
                   why="строка плагина в выводе не разобрана по колонкам")
    _status, col_version, path = live[0]
    # Версию берём из манифеста НА ДИСКЕ, а не из колонки: колоночный индекс — это парсинг
    # чужого вывода, а манифест лежит там же, куда всё равно смотрит дерево.
    man = pathlib.Path(path) / ".codex-plugin" / "plugin.json"
    try:
        version = json.loads(man.read_text())["version"]
    except (OSError, ValueError, KeyError) as exc:
        return _st("undetermined", why=f"манифест codex не прочитан ({man}): {exc}")
    top = git(path, "rev-parse", "--show-toplevel")
    head = git(path, "rev-parse", "HEAD")
    remote = git(path, "remote", "get-url", "origin")
    github_sources = {"https://github.com/shagiev/claude-gates",
                      "https://github.com/shagiev/claude-gates.git",
                      "git@github.com:shagiev/claude-gates.git",
                      "ssh://git@github.com/shagiev/claude-gates.git"}
    if top.returncode != 0 or head.returncode != 0:
        return _st("undetermined", why="каталог codex не git-клон — снапшот sha и откат "
                                       "невозможны")
    if remote.returncode != 0 or remote.stdout.strip() not in github_sources:
        return _st("undetermined", why="Codex marketplace обязан использовать GitHub "
                                       "shagiev/claude-gates; local/unknown source не переписывается")
    return _st("installed", version=version, sha=head.stdout.strip(), path=path,
               toplevel=top.stdout.strip())


_KIMI_SERVER = None
_RELEASE_URL = re.compile(
    r"https://github\.com/shagiev/claude-gates/releases/download/"
    r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)/"
    r"gates-\1\.\2\.\3\.zip\Z", re.ASCII)


def _kimi_home() -> pathlib.Path:
    return pathlib.Path(pwd.getpwuid(os.getuid()).pw_dir) / ".kimi-code"


def _canonical_release_url(version: str) -> str:
    match = re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)",
                         version, re.ASCII)
    if match is None:
        raise InstallError(f"недопустимая версия Kimi release: {version!r}")
    return (f"https://github.com/shagiev/claude-gates/releases/download/v{version}/"
            f"gates-{version}.zip")


def _release_version(source: str) -> str:
    if not isinstance(source, str):
        raise InstallError("Kimi source обязан быть canonical release URL")
    match = _RELEASE_URL.fullmatch(source)
    if match is None:
        raise InstallError("Kimi source отклонён: разрешён только canonical GitHub Release ZIP")
    return ".".join(match.groups())


def _kimi_server():
    global _KIMI_SERVER
    if _KIMI_SERVER is None:
        proc, base, token = ka._start_server(pathlib.Path("/"))
        ka.require_plugin_api(ka._api(base, token, "GET", "/openapi.json"))
        _KIMI_SERVER = proc, base, token
    proc, base, token = _KIMI_SERVER
    if proc.poll() is not None:
        raise InstallError("Kimi Server API завершился во время deploy")
    return base, token


def _stop_kimi_server():
    global _KIMI_SERVER
    if _KIMI_SERVER is not None:
        proc, _base, _token = _KIMI_SERVER
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        _KIMI_SERVER = None


def _kimi_call(method: str, path: str, body=None):
    base, token = _kimi_server()
    return ka._api(base, token, method, path, body)


def _kimi_records():
    payload = _kimi_call("GET", "/api/v1/plugins")
    records = payload.get("plugins") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise ka.ApiSchemaError("Kimi plugins response не содержит plugins list")
    return records


def _managed_tree(root: pathlib.Path) -> dict:
    entries = {}
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            path = pathlib.Path(base) / name
            if path.is_symlink():
                raise OSError(f"symlink в managed installation: {path}")
        for name in files:
            path = pathlib.Path(base) / name
            entries[path.relative_to(root).as_posix()] = " ".join(blob_entry(path))
    return entries


def _installed_kimi():
    home = _kimi_home()
    managed = home / "plugins/managed/gates"
    try:
        records = [record for record in _kimi_records()
                   if isinstance(record, dict) and record.get("id") == "gates"]
        if len(records) > 1:
            return _st("undetermined", kimi_home=str(home), why="несколько записей Kimi gates")
        if not records:
            return _st("absent", kimi_home=str(home), path=str(managed), why="Kimi gates не установлен")
        record = records[0]
        if record.get("state") != "ok" or not isinstance(record.get("enabled"), bool):
            return _st("undetermined", kimi_home=str(home),
                       why=f"Kimi gates state/enabled не определены: {record!r}")
        if managed.is_symlink():
            return _st("undetermined", kimi_home=str(home),
                       why="gate-owned managed/gates является symlink")
        if "root" in record:
            root = pathlib.Path(record["root"])
            if not root.is_absolute() or root.resolve() != managed.resolve():
                return _st("undetermined", kimi_home=str(home),
                           why="Kimi API root не совпадает с resolved gate-owned managed/gates")
        manifest = managed / ".kimi-plugin/plugin.json"
        version = json.loads(manifest.read_text())["version"]
        source = record.get("originalSource")
        return _st("installed", version=version, path=str(managed), kimi_home=str(home),
                   enabled=record["enabled"], source=source,
                   rollback_source=_canonical_release_url(version), tree=_managed_tree(managed),
                   commandCount=record.get("commandCount"), hookCount=record.get("hookCount"))
    except (OSError, ValueError, KeyError, AttributeError, TypeError, RuntimeError,
            ka.ApiSchemaError) as exc:
        return _st("undetermined", kimi_home=str(home),
                   why=f"Kimi API/managed state недоступен: {type(exc).__name__}: {exc}")


def installed(channel):
    """Три состояния, и «не определено» НЕ схлопывается в «не установлено».

    Снапшот — это ЦЕЛЬ ОТКАТА. Недостижимый хост или сломанный агент, объявившие себя первой
    установкой, пропустили бы деплой на машину, откатить которую будет некуда."""
    if channel not in CHANNELS:
        return _st("undetermined", why=f"канал {channel!r} неизвестен")
    if channel == "claude":
        return _installed_claude()
    if channel == "codex":
        return _installed_codex()
    return _installed_kimi()


# ── идентичность установленного дерева ─────────────────────────────────────────────────────
def blob_entry(path: pathlib.Path):
    """(mode, git-oid) файла — та же идентичность, которой оперирует сам git.

    Прежний дайджест был sha256 по `*.py` верхнего уровня: 4 файла из 20, слепой к exec-биту и
    к подмене файла симлинком (проверено), и мимо него проходили `hooks.json`, шим `gates-run` и
    тексты скиллов — объявленная поверхность гейта."""
    if path.is_symlink():
        data, mode = os.fsencode(os.readlink(path)), "120000"
    elif not path.is_file():
        # Аллоулист типов: FIFO на месте файла вешал `read_bytes` навсегда, без таймаута и без
        # диагностики — деплой просто переставал существовать.
        raise OSError(f"не обычный файл и не симлинк ({path})")
    else:
        data = path.read_bytes()
        # Именно owner-бит: `os.access` зависит от euid и от `noexec` на точке монтирования,
        # а git смотрит только на него.
        mode = "100755" if path.stat().st_mode & 0o100 else "100644"
    return mode, hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()


def tree_digest(entries: dict) -> str:
    """Дайджест карты {путь: "mode oid"} — одинаково считается из git-дерева и с диска хоста."""
    h = hashlib.sha256()
    for rel in sorted(entries):
        h.update(f"{entries[rel]}\t{rel}\n".encode())
    return h.hexdigest()[:16]


#: Единственное, что законно лежит в установке помимо закреплённого дерева. Замерено на живых
#: установках обоих каналов: `.in_use/<pid>` (Claude), `.orphaned_at`, метаданные маркетплейса
#: Codex, `.git` клона. `__pycache__` НЕ tolerated, а обнаруживается без удаления. Список экземплярный
#: сознательно: неизвестный новый файл красит деплой в КРАСНЫЙ, и это верная сторона отказа.
_EXTRA_DIRS = frozenset({".in_use", ".git"})
_EXTRA_FILES = frozenset({".orphaned_at", ".codex-marketplace-install.json", ".DS_Store"})


def _extra_ok(rel: str) -> bool:
    parts = rel.split(os.sep)
    return bool(_EXTRA_DIRS & set(parts[:-1])) or parts[0] in _EXTRA_DIRS \
        or parts[-1] in _EXTRA_FILES


def _raise_walk_error(error: OSError) -> None:
    raise error


def check_tree(root: pathlib.Path, want: dict):
    """Установка обязана быть РАВНА закреплённому дереву: каждый файл на месте с тем же режимом
    и содержимым, и НИЧЕГО лишнего сверх известных метаданных установщика.

    Терпимость к лишним файлам была ошибкой: каталог `scripts` первым лежит в `sys.path` того,
    что из него запускается, поэтому подброшенный `scripts/json.py` затеняет stdlib для
    `ladder_gate.py`. `__pycache__` любого типа — отдельный fail-closed результат: verifier
    только сообщает путь и НЕ удаляет/не открывает cache. Это сохраняет данные даже при bind
    mount и не оставляет подавленных ошибок удаления."""
    problems = []
    for dirpath, dirnames, filenames in os.walk(root, onerror=_raise_walk_error):
        for d in list(dirnames):
            rel_d = os.path.relpath(os.path.join(dirpath, d), root)
            if d == "__pycache__":
                problems.append(f"bytecode cache {rel_d} — проверь и удали его вручную")
                dirnames.remove(d)
                continue
            if os.path.islink(os.path.join(dirpath, d)) and rel_d not in want:
                # `os.walk` симлинк-каталоги НЕ обходит, а цикл ожиданий читает файлы СКВОЗЬ
                # них: подменённый `scripts/` → симлинк на чужой каталог с верными копиями плюс
                # лишним модулем проходил проверку зелёным (воспроизведено).
                problems.append(f"каталог {rel_d} — симлинк, которого нет в закреплённом дереве")
        dirnames[:] = [d for d in dirnames if d not in _EXTRA_DIRS
                       and not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if name == "__pycache__":
                problems.append(f"bytecode cache {rel} — проверь и удали его вручную")
            elif rel not in want and not _extra_ok(rel):
                problems.append(f"лишний файл {rel} — установка не равна закреплённому дереву")
    for rel in sorted(want):
        f = root / rel
        if os.path.relpath(f, root).startswith(".." + os.sep) or os.path.isabs(rel):
            problems.append(f"путь вне установки: {rel}")
            continue
        if not (f.exists() or f.is_symlink()):
            problems.append(f"отсутствует {rel}")
            continue
        try:
            mode, oid = blob_entry(f)
        except OSError as exc:
            problems.append(f"не прочитан {rel}: {exc}")
            continue
        if f"{mode} {oid}" != want[rel]:
            problems.append(f"{rel}: {mode} {oid[:8]} != ожидаемого {want[rel][:15]}…")
    return problems


# ── поведенческие smoke ────────────────────────────────────────────────────────────────────
def _gate_state_of(scripts_dir: pathlib.Path, repo: pathlib.Path) -> str:
    r = subprocess.run(
        ["python3", "-B", "-c", f"import sys; sys.path.insert(0, {str(scripts_dir)!r});"
                                "import codex_review_gate as g; print(g._gate_state_dir() or '')"],
        cwd=repo, capture_output=True, text=True, env=E)
    return r.stdout.strip()


def _drop_gate_state(scripts_dir: pathlib.Path, repo: pathlib.Path, existed_before: bool):
    """Запуск лесенки создаёт каталог состояния гейта, ключ которого — путь временного репо.
    Спрашиваем адрес у САМОГО кода (правило ключа живёт там), иначе на каждом хосте флота от
    каждого деплоя оставался бы мусор, а прогон тестов писал бы в боевой корень."""
    # Условие «в пути есть claude-gates» защиты не давало: подстрока есть у КАЖДОГО репозитория,
    # включая этот, а под его ключом лежат журналы гейта и сертификация флота. Удаляется только
    # то, чего до прогона не существовало.
    got = _gate_state_of(scripts_dir, repo)
    if got and not existed_before:
        shutil.rmtree(got, ignore_errors=True)


def _scratch_repo(files: dict):
    """Временный репозиторий для smoke. Каркас общий, чтобы изоляция (`init -b main`,
    `.gitignore`, первый коммит) не разъезжалась между проверками."""
    base = pathlib.Path(tempfile.mkdtemp(prefix="gates-smoke-"))
    repo = base / "repo"
    repo.mkdir()
    init = git(repo, "init", "-q", "-b", "main", ".")
    if init.returncode != 0:
        # Иначе всё дальнейшее сравнивало пустую строку с пустой строкой, и smoke отчитывался
        # успехом; на git < 2.28 (`init -b` не понимает) он вдобавок печатал ЛОЖНУЮ тревогу о
        # вернувшейся дыре безопасности вместо «среда smoke не собралась».
        raise RuntimeError(f"git init не удался (rc={init.returncode}): "
                           f"{(init.stderr or '').strip()[:160]}")
    for rel, body in files.items():
        f = repo / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(body)
    return base, repo


def _run_gate(install_root: pathlib.Path, repo: pathlib.Path, script, *args, env=None):
    """Запуск гейта ТАК ЖЕ, как его запускает хук: через шим `gates-run`.

    Свой лаунчер доказывал бы путь, которым не ходит ни один хук, — и промах резолва (тот
    самый, что четыре раза делал ревьюера слепым) остался бы невидим.

    Шим резолвит установку САМ через OS account home. В smoke-копии resolver точка home
    подменяется на синтетическую (НЕ env-override в поставке): иначе smoke доказывал бы
    работоспособность ДРУГОЙ установки — той, что случайно активна у пользователя. Все проверки
    путей, ownership, digest и запуска остаются настоящими."""
    shim = install_root / "templates" / "githooks" / "gates-run"
    e = {**E, **(env or {})}
    if not shim.is_file():
        # Фолбэк на свой лаунчер был fail-OPEN: поставка БЕЗ шима проходила smoke зелёной,
        # хотя шим — часть закреплённого дерева и единственное, чем хуки запускают гейт.
        raise FileNotFoundError(f"в поставке нет шима {shim} — хукам гейт запускать нечем")
    home = repo.parent / "home"
    installed = home / ".claude/plugins/cache/lenar-gates/gates/smoke"
    installed.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(install_root, installed, dirs_exist_ok=True)
    meta = home / ".claude" / "plugins" / "installed_plugins.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"plugins": {"gates@lenar-gates": [
        {"scope": "user", "installPath": str(installed), "version": "smoke"}]}}))
    dst = repo / ".githooks" / "gates-run"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(shim, dst)
    resolver_src = install_root / "templates" / "githooks" / "kimi_install.py"
    if resolver_src.is_file():
        body = dst.read_text()
        body, home_count = re.subn(r"GATE_HOME=.*", f'GATE_HOME="{home}"', body, count=1)
        if home_count == 1:
            resolver_body = resolver_src.read_text().replace(
                "return Path(pwd.getpwuid(os.getuid()).pw_dir)", f"return Path({str(home)!r})")
            digest = hashlib.sha256(resolver_body.encode()).hexdigest()
            body, count = re.subn(r"BOOT_SHA256=[0-9a-f]{64}", f"BOOT_SHA256={digest}", body)
            if count != 1:
                raise RuntimeError("smoke не нашёл digest-pin в gates-run")
            dst.write_text(body)
            resolver = home / ".local/lib/claude-gates/kimi_install.py"
            resolver.parent.mkdir(parents=True, exist_ok=True)
            resolver.write_text(resolver_body)
    return subprocess.run(["/bin/sh", str(dst), script, *args], cwd=repo,
                          capture_output=True, text=True, env={**e, "HOME": str(home)})


def _compute_tree(scripts_dir, repo):
    return subprocess.run(
        ["python3", "-B", "-c",
         f"import sys; sys.path.insert(0, {str(scripts_dir)!r});"
         "import ladder_gate as l, pathlib; print(l.compute_tree(pathlib.Path('.')))"],
        cwd=repo, capture_output=True, text=True, env=E).stdout.strip()


def smoke_compute_tree(install_root):
    """`compute_tree` установленного скрипта равен `git add -A` на ветках, где этот код уже
    ломался: удаление, exec-бит, симлинк.

    Clean-фильтр проверяется ОТДЕЛЬНО и ОБРАТНЫМ утверждением: `git add -A` фильтр применяет, а
    `compute_tree` намеренно хэширует сырые байты. Требовать здесь равенства значило бы давить
    реализацию в сторону возврата закрытой подмены содержимого."""
    install_root = pathlib.Path(install_root)
    scripts_dir = install_root / "scripts"
    base, r = _scratch_repo({".gitignore": ".claude/\n", "keep.py": "x = 1\n",
                             "gone.py": "bye\n", "exec.sh": "#!/bin/sh\n"})
    try:
        os.chmod(r / "exec.sh", 0o755)
        os.symlink("keep.py", r / "link.py")
        # Симлинк на ДИРЕКТОРИЮ, а не только на файл: 0.9.0 падал именно на нём
        # (BUG-0.9.0-symlink-tree-hash.md), а smoke проверяла лишь ссылку на файл.
        (r / "pkg").mkdir()
        (r / "pkg" / "mod.py").write_text("y = 1\n")
        os.symlink("pkg", r / "link_dir")
        git(r, "add", "-A")
        git(r, "commit", "-qm", "i")
        (r / "gone.py").unlink()                       # удаление
        (r / "keep.py").write_text("x = 2\n")          # правка
        idx = base / "idx"
        e2 = {**E, "GIT_INDEX_FILE": str(idx)}
        for a in (["read-tree", "HEAD"], ["add", "-A"]):
            subprocess.run(["git", *a], cwd=r, env=e2, capture_output=True)
        want = subprocess.run(["git", "write-tree"], cwd=r, env=e2,
                              capture_output=True, text=True).stdout.strip()
        if len(want) != 40:
            raise RuntimeError(f"оракул `git add -A` не построен (write-tree дал {want!r})")
        got = _compute_tree(scripts_dir, r)
        if got != want:
            return f"compute_tree={got or '(пусто)'} != git add -A={want}"

        (r / ".gitattributes").write_text("keep.py filter=mask\n")
        git(r, "config", "filter.mask.clean", "printf 'ПОДМЕНА\\n'")
        got2 = _compute_tree(scripts_dir, r)
        blob = git(r, "ls-tree", "-r", got2, "--", "keep.py").stdout.split()
        raw = git(r, "hash-object", "--no-filters", "--", "keep.py").stdout.strip()
        if not blob or blob[2] != raw:
            return "clean-фильтр подменил содержимое в дереве (закрытая дыра вернулась)"
        return None
    finally:
        shutil.rmtree(base, ignore_errors=True)


def smoke_gate_blocks(install_root):
    """Гейт РЕАЛЬНО блокирует и РЕАЛЬНО пропускает.

    «Точка входа не упала» ничего не доказывает: no-op с нулевым кодом такую проверку проходит,
    и деплой объявил бы зелёными нерабочие fail-closed гейты — а они и есть весь продукт."""
    install_root = pathlib.Path(install_root)
    base, r = _scratch_repo({".gitignore": ".claude/\nlogs/\n", "app/x.py": "x = 1\n",
                             ".codex-gate.yaml": "code_paths:\n  prefixes: [app/]\n"})
    state_before = os.path.isdir(_gate_state_of(install_root / "scripts", r) or os.devnull)
    try:
        git(r, "add", "-A")
        git(r, "commit", "-qm", "i")
        (r / "app" / "x.py").write_text("x = 2\n")     # правка КОД-пути без ревью
        git(r, "add", "-A")
        blocked = _run_gate(install_root, r, "ladder_gate.py", "check-precommit")
        if blocked.returncode != 2:
            return (f"код-правка БЕЗ ревью не заблокирована (rc={blocked.returncode}) — "
                    f"гейт не выполняет своё единственное обещание. "
                    f"{(blocked.stderr or blocked.stdout).strip()[:160]}")
        env = {"CLAUDE_SESSION_ID": "deploy-smoke"}
        for p in ("simplify", "code-review", "security"):
            for cmd in ("begin", "mark"):
                q = _run_gate(install_root, r, "ladder_gate.py", cmd, p, env=env)
                if q.returncode != 0:
                    return f"{cmd} {p} упал: {q.stderr.strip()[:160]}"
        git(r, "add", "-A")
        allowed = _run_gate(install_root, r, "ladder_gate.py", "check-precommit", env=env)
        if allowed.returncode != 0:
            return (f"честно пройденная цепочка НЕ пропускает коммит (rc={allowed.returncode}) — "
                    f"гейт неисполним, работа встанет. "
                    f"{(allowed.stderr or allowed.stdout).strip()[:160]}")
        return None
    finally:
        _drop_gate_state(install_root / "scripts", r, state_before)
        shutil.rmtree(base, ignore_errors=True)


SMOKES = (smoke_compute_tree, smoke_gate_blocks)


# ── подкоманды ─────────────────────────────────────────────────────────────────────────────
def cmd_installed(arg):
    return {"channels": {ch: installed(ch) for ch in arg.split(",") if ch}}


def _problem(code, channel, text):
    """Проблемы структурны. Вызывающий НИКОГДА не разбирает человеческий текст: фильтр по
    подстроке зачёл бы чужую проблему со словом «sha» как успешный откат."""
    return {"code": code, "channel": channel, "text": text}


def _extract_release(source: str, version: str, destination: pathlib.Path, zip_sha256=None,
                     observed=None):
    if _release_version(source) != version:
        raise InstallError("rollback source version не совпадает со snapshot version")
    try:
        with ka.urllib.request.urlopen(source, timeout=60) as response:
            data = response.read(100 * 1024 * 1024 + 1)
    except OSError as exc:
        raise InstallError(f"release не скачан: {type(exc).__name__}: {exc}") from exc
    if observed is not None:
        observed["zip_sha256"] = hashlib.sha256(data).hexdigest()
    if len(data) > 100 * 1024 * 1024:
        raise ReleaseArchiveError("release ZIP превышает 100 MiB")
    if zip_sha256 is not None and hashlib.sha256(data).hexdigest() != zip_sha256:
        raise ReleaseDigestError("release ZIP SHA-256 не совпадает с publisher digest")
    total = 0
    try:
        archive = destination.parent / "release.zip"
        archive.parent.mkdir(parents=True, exist_ok=True)
        archive.write_bytes(data)
        with zipfile.ZipFile(archive) as bundle:
            names = [info.filename for info in bundle.infolist() if not info.is_dir()]
            if len(names) != len(set(names)):
                raise ReleaseArchiveError("duplicate entry names в release ZIP")
            for info in bundle.infolist():
                name = info.filename
                parts = pathlib.PurePosixPath(name).parts
                mode = info.external_attr >> 16
                if (not name or name.startswith(("/", "plugins/gates/")) or "\\" in name
                        or ".." in parts or "__pycache__" in parts or name.endswith(".pyc")
                        or info.flag_bits & 1
                        or (not info.is_dir() and stat.S_IFMT(mode) not in (0, stat.S_IFREG))):
                    raise ReleaseArchiveError(f"небезопасный entry в release ZIP: {name!r}")
                if info.is_dir():
                    continue
                if info.file_size > _MAX_ENTRY_BYTES:
                    raise ReleaseArchiveError(f"ZIP entry превышает per-entry limit: {name!r}")
                if total + info.file_size > _MAX_EXTRACT_BYTES:
                    raise ReleaseArchiveError("ZIP total uncompressed size превышает limit")
                target = destination.joinpath(*parts)
                target.parent.mkdir(parents=True, exist_ok=True)
                written = 0
                with bundle.open(info) as src, open(target, "wb") as dst:
                    while chunk := src.read(1024 * 1024):
                        written += len(chunk)
                        total += len(chunk)
                        if written > _MAX_ENTRY_BYTES:
                            raise ReleaseArchiveError(f"ZIP entry превышает per-entry limit: {name!r}")
                        if total > _MAX_EXTRACT_BYTES:
                            raise ReleaseArchiveError("ZIP total uncompressed size превышает limit")
                        dst.write(chunk)
                if written != info.file_size:
                    raise ReleaseArchiveError(f"ZIP entry size drift: {name!r}")
                target.chmod(0o755 if mode & 0o100 else 0o644)
    except OSError as exc:
        raise InstallError(f"release local I/O failed: {type(exc).__name__}: {exc}") from exc
    except (ValueError, zipfile.BadZipFile) as exc:
        raise ReleaseArchiveError(
            f"release ZIP не распакован: {type(exc).__name__}: {exc}") from exc
    return destination


def _remove_kimi_if_present():
    if any(record.get("id") == "gates" for record in _kimi_records()
           if isinstance(record, dict)):
        _kimi_call("POST", "/api/v1/plugins/gates:remove")


def _install_kimi_release(source: str, enabled: bool):
    _remove_kimi_if_present()
    _kimi_call("POST", "/api/v1/plugins", {"source": source})
    _kimi_call("POST", "/api/v1/plugins/gates:" + ("enable" if enabled else "disable"))


def cmd_update(spec: str):
    want = json.loads(spec)
    source = want.get("source")
    version = want.get("version")
    if _release_version(source) != version or source != _canonical_release_url(version):
        raise InstallError("Kimi update source/version не образуют canonical release identity")
    try:
        _install_kimi_release(source, enabled=True)
        return {"ok": True, "problems": []}
    except (OSError, ValueError, RuntimeError, ka.ApiSchemaError) as exc:
        return {"ok": False, "problems": [_problem(
            "kimi-install", "kimi", f"Kimi API install failed: {type(exc).__name__}: {exc}")]}


def _verify_kimi_source(want, observed=None):
    with tempfile.TemporaryDirectory(prefix="gates-kimi-rollback-") as tmp:
        root = _extract_release(want["source"], want["version"], pathlib.Path(tmp) / "plugin",
                                want.get("zip_sha256"), observed)
        problems = check_tree(root, want["tree"])
    return problems


def cmd_verify(spec):
    """spec: {"channels": {ch: {"version":…, "sha":…|null, "tree": {rel: "mode oid"}|null,
                                "checks": ["version","sha","tree","smoke"]}}}"""
    spec = json.loads(spec)
    report = {"ok": True, "problems": [], "channels": {}}
    for ch, want in spec["channels"].items():
        info = installed(ch)
        report["channels"][ch] = info
        checks = want.get("checks") or []
        if want.get("state", "installed") == "absent":
            # Откат «в отсутствие»: канал, которого до деплоя не было, обязан снова отсутствовать.
            if info["state"] != "absent":
                report["problems"].append(_problem(
                    "not-absent", ch, f"канал должен был исчезнуть, но состояние "
                                      f"{info['state']} ({info.get('version')})"))
            continue
        if info["state"] != "installed":
            report["problems"].append(_problem(
                "not-installed", ch,
                f"канал ОБЪЯВЛЕН, но состояние {info['state']}: {info.get('why', '')}"))
            continue
        if "version" in checks and info["version"] != want["version"]:
            report["problems"].append(_problem(
                "version", ch, f"версия {info['version']} != ожидаемой {want['version']}"))
        if "sha" in checks and want.get("sha") and info.get("sha") != want["sha"]:
            report["problems"].append(_problem(
                "sha", ch, f"sha {info.get('sha')} != {want['sha']}"))
        if ch == "kimi":
            if "enabled" in checks and info.get("enabled") is not want.get("enabled"):
                report["problems"].append(_problem(
                    "enabled", ch, f"enabled {info.get('enabled')} != snapshot {want.get('enabled')}"))
            if info.get("enabled") is True \
                    and (info.get("commandCount"), info.get("hookCount")) != (5, 3):
                report["problems"].append(_problem(
                    "kimi-registration", ch, "Kimi commands/hooks не зарегистрированы полностью"))
            if "source" in checks:
                if info.get("source") != want.get("source") and not want.get("migration"):
                    report["problems"].append(_problem(
                        "source", ch, "активная Kimi установка получена не из ожидаемого release URL"))
                source_code = "source"
                observed = {}
                try:
                    source_problems = _verify_kimi_source(want, observed)
                    info["zip_sha256"] = observed["zip_sha256"]
                    source_code = "source-tree"
                except ReleaseDigestError as exc:
                    source_code = "zip-sha256"
                    source_problems = [str(exc)]
                except ReleaseArchiveError as exc:
                    source_code = "source-archive"
                    source_problems = [str(exc)]
                except (InstallError, OSError, ValueError, RuntimeError) as exc:
                    source_problems = [f"{type(exc).__name__}: {exc}"]
                for why in source_problems:
                    report["problems"].append(_problem(source_code, ch, why))
        root = pathlib.Path(info["path"])
        if "tree" in checks and want.get("tree"):
            for why in check_tree(root, want["tree"]):
                report["problems"].append(_problem("tree", ch, why))
        # Расхождение уже зафиксировано — smoke проверял бы заведомо не ту версию, которую
        # деплоят, и стоил бы ~3 с на канал ради информации, которой в отчёте уже нет места.
        if "smoke" in checks and not any(p["channel"] == ch for p in report["problems"]):
            for smoke in SMOKES:
                try:
                    why = smoke(root)
                except Exception as exc:                # noqa: BLE001 — отчёт вместо трассы
                    why = f"проверка не выполнена: {type(exc).__name__}: {exc}"
                if why:
                    report["problems"].append(_problem(smoke.__name__, ch, why))
            if "tree" in checks and want.get("tree"):
                for why in check_tree(root, want["tree"]):
                    report["problems"].append(_problem("post-smoke-tree", ch, why))
    report["ok"] = not report["problems"]
    return report


def _atomic_write(path: pathlib.Path, text: str):
    tmp = path.with_name(path.name + ".deploy-tmp")
    with open(tmp, "w") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    tmp.replace(path)


def cmd_restore(spec):
    """spec: {"channel": ch, "snapshot": {...}} — вернуть канал на снапшотное состояние.

    Временная мера: долговременный фикс — `git revert` и новый деплой."""
    spec = json.loads(spec)
    ch, snap = spec["channel"], spec["snapshot"]
    # Цель мутации обязана приехать В СНАПШОТЕ. Умолчание «текущий ~» стоило реального
    # инцидента: тест передал `{"channel": "claude", "state": "absent"}` без цели, ветка
    # «снять канал» выполнила `claude plugin uninstall` НА РАБОЧЕЙ МАШИНЕ и снесла живой
    # плагин. Пока адрес брался из окружения, никакая аккуратность в тестах этого не
    # исключала; теперь неполный спек не может ничего мутировать.
    if ch not in CHANNELS:
        return {"ok": False, "problems": [_problem("no-target", ch, "неизвестный канал")]}
    target = (snap.get("registry") if ch == "claude" else
              snap.get("kimi_home") if ch == "kimi" else snap.get("toplevel"))
    if snap.get("state") == "absent":
        # У ветки «снять канал» адрес не просто ОБЪЯВЛЕН, он УПРАВЛЯЕТ мутацией: раньше здесь
        # проверялась непустота строки, значение которой игнорировалось, а команда била туда,
        # куда смотрит `E["HOME"]`, зафиксированный на импорте. Формулировка «адрес обязан
        # приехать в спеке» для этой ветки была декорацией — именно ей поверил бы следующий.
        target = (snap.get("registry") if ch == "claude" else
                  snap.get("kimi_home") if ch == "kimi" else snap.get("codex_home"))
    if snap.get("state") in ("absent", "installed") and not target:
        return {"ok": False, "problems": [_problem(
            "no-target", ch, "в снапшоте нет адреса цели — мутация без явного адреса запрещена")]}
    if snap.get("state") == "absent":
        # Канал установлен ЭТИМ деплоем: вернуть состояние — значит снять его, иначе откат
        # отчитался бы успехом, оставив канал, которого до деплоя не было.
        if ch == "kimi":
            try:
                _remove_kimi_if_present()
                after = installed(ch)
                return {"ok": after["state"] == "absent", "problems": [] if after["state"] == "absent"
                        else [_problem("remove-failed", ch, "Kimi gates не удалён через API")]}
            except (OSError, ValueError, RuntimeError, ka.ApiSchemaError) as exc:
                return {"ok": False, "problems": [_problem("remove-failed", ch, str(exc))]}
        env = dict(E)
        if ch == "claude":
            env["HOME"] = str(pathlib.Path(target).resolve().parents[2])
        else:
            env["CODEX_HOME"] = target
        r = subprocess.run([CHANNELS[ch]["cli"], *CHANNELS[ch]["remove"]],
                           capture_output=True, text=True, env=env)
        after = installed(ch)
        if after["state"] == "absent":
            return {"ok": True, "problems": []}
        return {"ok": False, "problems": [_problem(
            "remove-failed", ch, f"канал не снят (rc={r.returncode}, состояние "
                                 f"{after['state']}): {(r.stderr or '').strip()[:160]}")]}
    if snap.get("state") != "installed":
        return {"ok": False, "problems": [_problem("no-target", ch, "снапшота нет")]}
    if ch == "kimi":
        try:
            source = snap.get("rollback_source") or snap.get("source")
            if _release_version(source) != snap.get("version"):
                raise InstallError("rollback source/version mismatch")
            digest = snap.get("zip_sha256")
            if not isinstance(digest, str) or re.fullmatch(r"[0-9a-f]{64}", digest, re.ASCII) is None:
                raise InstallError("rollback ZIP digest отсутствует в snapshot")
            with tempfile.TemporaryDirectory(prefix="gates-kimi-restore-") as tmp:
                _extract_release(source, snap["version"], pathlib.Path(tmp) / "plugin", digest)
            _install_kimi_release(source, enabled=snap.get("enabled") is True)
            return {"ok": True, "problems": []}
        except (OSError, ValueError, RuntimeError, ka.ApiSchemaError) as exc:
            return {"ok": False, "problems": [_problem("restore-failed", ch, str(exc))]}
    if ch == "claude":
        p = pathlib.Path(target)
        d = json.loads(p.read_text())
        for e in d["plugins"]["gates@lenar-gates"]:
            e["version"], e["installPath"] = snap["version"], snap["path"]
            if snap.get("sha"):
                e["gitCommitSha"] = snap["sha"]
        _atomic_write(p, json.dumps(d, ensure_ascii=False, indent=2))
        return {"ok": True, "problems": []}
    # codex: каталог плагина — рабочее дерево git-клона маркетплейса, версионного кэша нет,
    # поэтому возврат = checkout снапшотного sha в этом клоне.
    r = git(target, "checkout", "-q", "--detach", snap["sha"])
    if r.returncode != 0:
        return {"ok": False, "problems": [_problem(
            "restore-failed", ch, f"checkout {snap['sha'][:12]} в {target}: "
                                  f"{(r.stderr or '').strip()[:160]}")]}
    return {"ok": True, "problems": []}


def cmd_sessions(arg):
    sessions = []
    enumeration = "known"
    for channel in (value for value in arg.split(",") if value):
        if channel not in CHANNELS:
            enumeration = "unknown"
            continue
        if channel != "kimi":
            enumeration = "unknown"
            continue
        try:
            payload = _kimi_call("GET", "/api/v1/sessions")
            records = payload.get("items") if isinstance(payload, dict) else None
            if not isinstance(records, list) or payload.get("has_more") is not False:
                raise ka.ApiSchemaError("Kimi sessions response неполный или без items list")
            for record in records:
                if not isinstance(record, dict) or not isinstance(record.get("id"), str) \
                        or re.fullmatch(r"[A-Za-z0-9._-]+", record["id"], re.ASCII) is None:
                    raise ka.ApiSchemaError("Kimi session без валидного id")
                sessions.append({"channel": "kimi", "id": record["id"],
                                 "kind": record.get("kind", "unknown")})
        except (OSError, ValueError, RuntimeError, ka.ApiSchemaError) as exc:
            return {"enumeration": "unknown", "sessions": [], "error": str(exc)}
    if not sessions:
        enumeration = "unknown"
    return {"enumeration": enumeration, "sessions": sessions}


def cmd_activate(arg):
    inventory = json.loads(arg).get("sessions") or []
    proofs = []
    errors = []
    for session in inventory:
        if session.get("channel") != "kimi" or not session.get("id"):
            continue
        try:
            _kimi_call("POST", f"/api/v1/sessions/{session['id']}:reload")
            state = _kimi_call("GET", f"/api/v1/sessions/{session['id']}")
            if not isinstance(state, dict):
                raise ka.ApiSchemaError("Kimi session state не object")
            if state.get("gates_version") and state.get("gates_smoke") == "passed":
                proofs.append({"id": session["id"], "version": state["gates_version"],
                               "smoke": "passed"})
        except (OSError, ValueError, RuntimeError, ka.ApiSchemaError) as exc:
            errors.append({"id": session["id"], "error": f"{type(exc).__name__}: {exc}"})
    return {"sessions": proofs, "errors": errors}


def main(argv):
    cmds = {"installed": cmd_installed, "verify": cmd_verify, "restore": cmd_restore,
            "update": cmd_update, "sessions": cmd_sessions, "activate": cmd_activate}
    if not argv or argv[0] not in cmds:
        print(f"usage: deploy_verify.py {'|'.join(cmds)} <arg>", file=sys.stderr)
        return 2
    try:
        out = cmds[argv[0]](argv[1])
        print(json.dumps(out, ensure_ascii=False))
        return 0 if out.get("ok", True) else 1
    finally:
        _stop_kimi_server()


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
