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
import shutil
import subprocess
import sys
import tempfile

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
}


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


def _installed_codex():
    r = subprocess.run(["codex", "plugin", "list"], capture_output=True, text=True, env=E)
    if r.returncode != 0:
        return _st("undetermined", why=f"codex plugin list rc={r.returncode}: "
                                       f"{(r.stderr or '').strip()[:160]}")
    if "lenar-gates" not in r.stdout:
        # Разбор здесь заведомо хрупкий, и непрочитанный вывод обязан отличаться от «плагина
        # нет»: иначе смена формата CLI даёт «первая установка», деплой мутирует канал без
        # доказанной цели отката — ровно тот пустой вывод, неотличимый от «уже свежее».
        return _st("undetermined", why="вывод `codex plugin list` не опознан — маркетплейс "
                                       "lenar-gates в нём не упомянут вовсе")
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
            return _st("absent", why=f"статус {rows[0][0]!r}")
        return _st("undetermined", why="строка плагина в выводе не разобрана по колонкам")
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
    if top.returncode != 0 or head.returncode != 0:
        return _st("undetermined", why="каталог codex не git-клон — снапшот sha и откат "
                                       "невозможны")
    return _st("installed", version=version, sha=head.stdout.strip(), path=path,
               toplevel=top.stdout.strip())





def installed(channel):
    """Три состояния, и «не определено» НЕ схлопывается в «не установлено».

    Снапшот — это ЦЕЛЬ ОТКАТА. Недостижимый хост или сломанный агент, объявившие себя первой
    установкой, пропустили бы деплой на машину, откатить которую будет некуда."""
    if channel not in CHANNELS:
        return _st("undetermined", why=f"канал {channel!r} неизвестен")
    return _installed_claude() if channel == "claude" else _installed_codex()


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
#: установках обоих каналов: `.in_use/<pid>` (Claude), `__pycache__` (побочный продукт запуска
#: гейта), `.orphaned_at`, метаданные маркетплейса Codex, `.git` клона. Список экземплярный
#: сознательно: неизвестный новый файл красит деплой в КРАСНЫЙ, и это верная сторона отказа.
_EXTRA_DIRS = frozenset({".in_use", ".git"})
_EXTRA_FILES = frozenset({".orphaned_at", ".codex-marketplace-install.json", ".DS_Store"})


def _extra_ok(rel: str) -> bool:
    parts = rel.split(os.sep)
    return bool(_EXTRA_DIRS & set(parts[:-1])) or parts[0] in _EXTRA_DIRS \
        or parts[-1] in _EXTRA_FILES


def _drop_stale_bytecode(root: pathlib.Path) -> int:
    """Снять `__pycache__` из установки ПЕРЕД доказательством.

    Байткод был терпимым слепым пятном под неверной посылкой «source-less `.pyc` не
    импортируется». Правило другое: CPython при штатной инвалидации сверяет только 8 байт
    заголовка (mtime и размер исходника), содержимое не проверяется вовсе — подделанный `.pyc`
    с этими байтами, снятыми с нетронутого `.py`, грузится ВМЕСТО исходника. Воспроизведено:
    произвольный код исполнялся внутри гейта при зелёном дереве и обеих пройденных smoke.

    Снимаем, а не жалуемся: байткод — кэш без полномочий, удаление ничего не теряет и делает
    «что исполнится» равным «что захэшировано». Генерация закрыта отдельно — гейт и шим ходят
    через `python3 -B`. Граница честная: проверка утверждает состояние на МОМЕНТ проверки, и
    локальный процесс с правами на запись может подложить заново — ровно как может и
    отредактировать сам `.py`."""
    dropped = 0
    for dirpath, dirnames, _f in os.walk(root):
        for d in [d for d in dirnames if d == "__pycache__"]:
            shutil.rmtree(os.path.join(dirpath, d), ignore_errors=True)
            dirnames.remove(d)
            dropped += 1
    return dropped


def check_tree(root: pathlib.Path, want: dict):
    """Установка обязана быть РАВНА закреплённому дереву: каждый файл на месте с тем же режимом
    и содержимым, и НИЧЕГО лишнего сверх известных метаданных установщика.

    Терпимость к лишним файлам была ошибкой: каталог `scripts` первым лежит в `sys.path` того,
    что из него запускается, поэтому подброшенный `scripts/json.py` затеняет stdlib для
    `ladder_gate.py` — при полностью сошедшихся mode+oid всех 20 файлов и зелёном флоте.
    `__pycache__` терпится отдельно: source-less `.pyc` в нём не импортируется, а без него
    каждый запуск гейта красил бы деплой."""
    problems = []
    _drop_stale_bytecode(root)
    for dirpath, dirnames, filenames in os.walk(root):
        for d in list(dirnames):
            rel_d = os.path.relpath(os.path.join(dirpath, d), root)
            if os.path.islink(os.path.join(dirpath, d)) and rel_d not in want:
                # `os.walk` симлинк-каталоги НЕ обходит, а цикл ожиданий читает файлы СКВОЗЬ
                # них: подменённый `scripts/` → симлинк на чужой каталог с верными копиями плюс
                # лишним модулем проходил проверку зелёным (воспроизведено).
                problems.append(f"каталог {rel_d} — симлинк, которого нет в закреплённом дереве")
        dirnames[:] = [d for d in dirnames if d not in _EXTRA_DIRS
                       and not os.path.islink(os.path.join(dirpath, d))]
        for name in filenames:
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            if rel not in want and not _extra_ok(rel):
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
def _drop_gate_state(scripts_dir: pathlib.Path, repo: pathlib.Path):
    """Запуск лесенки создаёт каталог состояния гейта, ключ которого — путь временного репо.
    Спрашиваем адрес у САМОГО кода (правило ключа живёт там), иначе на каждом хосте флота от
    каждого деплоя оставался бы мусор, а прогон тестов писал бы в боевой корень."""
    r = subprocess.run(
        ["python3", "-c", f"import sys; sys.path.insert(0, {str(scripts_dir)!r});"
                          "import codex_review_gate as g; print(g._gate_state_dir() or '')"],
        cwd=repo, capture_output=True, text=True, env=E)
    got = r.stdout.strip()
    if got and os.sep in got and "claude-gates" in got:
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

    Шим резолвит установку САМ, через `$HOME`, поэтому ему подставляется синтетический дом,
    указывающий ровно на проверяемый каталог: иначе smoke молча доказывал бы работоспособность
    ДРУГОЙ установки — той, что случайно активна у пользователя. Логика резолва при этом
    исполняется настоящая, его собственная."""
    shim = install_root / "templates" / "githooks" / "gates-run"
    e = {**E, **(env or {})}
    if not shim.is_file():
        # Фолбэк на свой лаунчер был fail-OPEN: поставка БЕЗ шима проходила smoke зелёной,
        # хотя шим — часть закреплённого дерева и единственное, чем хуки запускают гейт.
        raise FileNotFoundError(f"в поставке нет шима {shim} — хукам гейт запускать нечем")
    home = repo.parent / "home"
    meta = home / ".claude" / "plugins" / "installed_plugins.json"
    meta.parent.mkdir(parents=True, exist_ok=True)
    meta.write_text(json.dumps({"plugins": {"gates@lenar-gates": [
        {"scope": "user", "installPath": str(install_root), "version": "smoke"}]}}))
    dst = repo / ".githooks" / "gates-run"
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(shim, dst)
    return subprocess.run(["bash", str(dst), script, *args], cwd=repo,
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
        _drop_gate_state(install_root / "scripts", r)
        shutil.rmtree(base, ignore_errors=True)


SMOKES = (smoke_compute_tree, smoke_gate_blocks)


# ── подкоманды ─────────────────────────────────────────────────────────────────────────────
def cmd_installed(arg):
    return {"channels": {ch: installed(ch) for ch in arg.split(",") if ch}}


def _problem(code, channel, text):
    """Проблемы структурны. Вызывающий НИКОГДА не разбирает человеческий текст: фильтр по
    подстроке зачёл бы чужую проблему со словом «sha» как успешный откат."""
    return {"code": code, "channel": channel, "text": text}


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
    target = snap.get("registry") if ch == "claude" else snap.get("toplevel")
    if snap.get("state") in ("absent", "installed") and not target:
        return {"ok": False, "problems": [_problem(
            "no-target", ch, "в снапшоте нет адреса цели — мутация без явного адреса запрещена")]}
    if snap.get("state") == "absent":
        # Канал установлен ЭТИМ деплоем: вернуть состояние — значит снять его, иначе откат
        # отчитался бы успехом, оставив канал, которого до деплоя не было.
        r = subprocess.run([CHANNELS[ch]["cli"], *CHANNELS[ch]["remove"]],
                           capture_output=True, text=True, env=E)
        after = installed(ch)
        if after["state"] == "absent":
            return {"ok": True, "problems": []}
        return {"ok": False, "problems": [_problem(
            "remove-failed", ch, f"канал не снят (rc={r.returncode}, состояние "
                                 f"{after['state']}): {(r.stderr or '').strip()[:160]}")]}
    if snap.get("state") != "installed":
        return {"ok": False, "problems": [_problem("no-target", ch, "снапшота нет")]}
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


def main(argv):
    cmds = {"installed": cmd_installed, "verify": cmd_verify, "restore": cmd_restore}
    if not argv or argv[0] not in cmds:
        print(f"usage: deploy_verify.py {'|'.join(cmds)} <arg>", file=sys.stderr)
        return 2
    out = cmds[argv[0]](argv[1])
    print(json.dumps(out, ensure_ascii=False))
    return 0 if out.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
