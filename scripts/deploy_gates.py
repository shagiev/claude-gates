#!/usr/bin/env python3
"""Деплой плагина gates на флот. Принцип: каждый шаг утверждает ровно то, что проверил.

Мотив: за две недели флот трижды разъехался молча — дев-сервер две недели работал на старой
версии, Codex-копия отставала независимо и четырежды делала ревьюера слепым, а `/plugin update`
при устаревшем каталоге печатает пустой вывод, неотличимый от «уже свежее».

Гейты fail-closed, поэтому сломанная версия на всех машинах разом останавливает работу везде:
отсюда канарейка, ДОКАЗАННАЯ до мутации цель отката и откат всей когорты.

Дизайн: docs/2026-08-23-plugin-deploy-design.md
"""
import argparse
import datetime
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import tempfile

sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "gates" / "scripts"))
sys.path.insert(0, str(ROOT / "scripts"))
import codex_review_gate as g                                                    # noqa: E402
import github_release as gr                                                      # noqa: E402
import kimi_acceptance as ka                                                     # noqa: E402
import prepush_gate                                                              # noqa: E402
from deploy_verify import CHANNELS, tree_digest                                  # noqa: E402

AGENT = ROOT / "scripts" / "deploy_verify.py"
#: Состояние деплоя живёт ВНЕ проверяемого репозитория — то же решение и та же причина, что у
#: состояния гейта (коммит 29b30ed): вход решения не должен лежать под управлением стороны,
#: чей код проверяют, и не должен попадать в дерево, чей хэш этот же деплой сверяет.
_STATE_ROOT = g._gate_state_dir()
if _STATE_ROOT is None:                                # не git-репозиторий: деплоить нечего
    print("[deploy] ✗ не git-репозиторий — идентичность выкатки не определить", file=sys.stderr)
    sys.exit(1)
STATE = _STATE_ROOT / "deploy"
LOCK = STATE / "lock"
_FETCH_TIMEOUT_S = 120
_SSH_MUX = ["-o", "ControlMaster=auto", "-o", "ControlPath=/tmp/gates-cm-%C",
            "-o", "ControlPersist=60s", "-o", "ConnectTimeout=15"]
#: Форма id — аллоулист: он идёт и в `ssh <host>`, и в ИМЯ ФАЙЛА снапшота, где `../` уже
#: воспроизводимо уводил запись за пределы каталога состояния.
_ID_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class DeployAbort(Exception):
    """Отказ ПОСЛЕ того, как флот начали мутировать.

    Отдельный тип, потому что путь выхода принципиально другой: `fail()` уходит из процесса, и
    когда так падал второй хост, уже обновлённая канарейка оставалась на новой версии, второй
    хост на старой, а `stable` от прошлого деплоя продолжал сертифицировать флот зелёным —
    ровно тот version skew, ради устранения которого всё делается."""

    def __init__(self, message, *, artifact=False):
        super().__init__(message)
        self.artifact = artifact


_ARTIFACT_PROBLEM_CODES = frozenset({
    "version", "sha", "tree", "post-smoke-tree", "kimi-registration", "zip-sha256",
    "source-tree", "source-archive", "smoke_compute_tree", "smoke_gate_blocks",
})


def fail(msg: str) -> "NoReturn":                      # noqa: F821
    print(f"[deploy] ✗ {msg}", file=sys.stderr)
    sys.exit(1)


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def on_host(host: str, argv: "list[str]", stdin: "str | None" = None):
    """Команда локально или по ssh. Пароля нет и быть не может — только ключ из ~/.ssh/config.

    Мультиплексирование не украшение: холодный коннект замерен в 1,00 с против 0,17 с через
    мастер-сокет, а на хост приходится под десяток вызовов."""
    cmd = argv if host == "local" else ["ssh", "-o", "BatchMode=yes", *_SSH_MUX, host,
                                        " ".join(map(shlex.quote, argv))]
    try:
        return subprocess.run(cmd, capture_output=True, text=True, input=stdin)
    except OSError as exc:
        # На ssh-хосте отсутствующий бинарь даёт rc=127 и обрабатывается штатно; локально он
        # бросал OSError мимо всей обработки — когорта оставалась мутированной без отката.
        return subprocess.CompletedProcess(cmd, 127, "", f"{type(exc).__name__}: {exc}")


def _drop_mux(hosts):
    for h in hosts:
        if h != "local":
            subprocess.run(["ssh", "-O", "exit", *_SSH_MUX, h], capture_output=True)


_AGENT_SRC = None


def load_agent(sha: str):
    """Исходник агента берётся ИЗ КОММИТА, а не с диска.

    С диска он был безопасен только потому, что на пути деплоя РАНЬШЕ отработал pre-deploy
    гейт. `--restore` его не зовёт, и `make gates-restore` — рефлекс оператора при зависшей
    выкатке — увозил на хост незакоммиченный рабочий код и исполнял его там (воспроизведено).
    «Верно, потому что раньше сработала другая проверка» — это и есть класс, который чинит
    весь этот файл."""
    global _AGENT_SRC
    r = g._trusted_git("show", f"{sha}:scripts/deploy_verify.py", cwd=ROOT)
    helper = g._trusted_git("show", f"{sha}:scripts/kimi_acceptance.py", cwd=ROOT)
    if (r is None or r.returncode != 0 or not r.stdout.strip()
            or helper is None or helper.returncode != 0 or not helper.stdout.strip()):
        fail(f"агент и Kimi API helper не прочитаны из коммита {sha[:12]} — на хост нечего везти")
    prelude = ("import sys as _deploy_sys\nimport types as _deploy_types\n"
               "_deploy_ka = _deploy_types.ModuleType('kimi_acceptance')\n"
               "_deploy_ka.__file__ = 'kimi_acceptance.py'\n"
               f"exec(compile({helper.stdout!r}, 'kimi_acceptance.py', 'exec'), _deploy_ka.__dict__)\n"
               "_deploy_sys.modules['kimi_acceptance'] = _deploy_ka\n")
    _AGENT_SRC = prelude + r.stdout


def agent(host: str, subcmd: str, arg: str) -> dict:
    """Хост-агент доставляется по stdin и вызывается подкомандой — ОДИН модуль на все нужды.

    Возврат — либо разобранный JSON, либо `{"error": …}`. Ошибка транспорта НИКОГДА не
    превращается в пустой результат: «хост не ответил» и «плагин не установлен» — разные
    факты, и их склейка объявила бы недостижимую машину первой установкой."""
    if _AGENT_SRC is None:
        fail("агент не загружен из коммита — вызов до `load_agent`")
    r = on_host(host, ["python3", "-B", "-", subcmd, arg], stdin=_AGENT_SRC)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        # ХВОСТ, а не голова: агент едет через `python3 -`, и первые 200 символов — это шапка
        # трейсбека с кадрами `File "<stdin>"`, тогда как тип и текст исключения (единственное
        # полезное) стоят в конце.
        tail = g.redact_secrets(g.strip_ansi((r.stderr or r.stdout or "").strip()))[-400:]
        return {"error": f"агент не ответил (rc={r.returncode}): …{tail}"}


def read_inventory() -> "list[dict]":
    """Инвентарь ИЗ РЕПОЗИТОРИЯ: он проходит те же гейты, что код, и разъезд виден в диффе.

    Разбор — настоящим загрузчиком, а не построчной регуляркой: та понимала только inline-форму
    `channels: [a, b]`, а на блочной молча отдавала пустой список — то есть забытый канал
    выглядел бы как «его тут и не было», ровно тот отказ, ради предотвращения которого каналы
    и объявляются."""
    import yaml
    doc = yaml.safe_load((ROOT / "deploy-hosts.yaml").read_text()) or {}
    hosts = doc.get("hosts") or []
    if not isinstance(hosts, list) or not hosts:
        fail("deploy-hosts.yaml пуст или не разобран — флот неизвестен, деплоить некуда")
    for h in hosts:
        if not isinstance(h, dict) or not h.get("id"):
            fail(f"запись инвентаря без id: {h!r}")
        chans = h.get("channels")
        if not isinstance(chans, list) or not chans:
            fail(f"{h['id']}: каналы не объявлены — молчаливо пропущенный канал и есть тот "
                 "разъезд, который ловит деплой")
        if not _ID_RE.match(str(h["id"])):
            fail(f"недопустимая форма id {h['id']!r} — id идёт в ssh и в имя файла снапшота")
        unknown = [c for c in chans if c not in CHANNELS]
        if unknown:
            # Аллоулист формы, а не default-fallthrough: с неявным `else` опечатка в канале
            # трактовалась бы как codex и была бы МУТИРОВАНА до первой же проверки.
            fail(f"{h['id']}: неизвестные каналы {unknown} (известны: {sorted(CHANNELS)})")
    ids = [h["id"] for h in hosts]
    if len(set(ids)) != len(ids):
        fail(f"дубли id в инвентаре: {sorted({i for i in ids if ids.count(i) > 1})}")
    return hosts


def pre_deploy_gate():
    """Гейт принадлежит МУТИРУЮЩЕМУ скрипту, а не Makefile: `python3 -B scripts/deploy_gates.py` —
    документированный прямой вызов, и через него флот мутировался бы без проверки дерева и
    тестов вовсе."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        fail("pre-deploy гейт вызван из-под pytest — тест обязан его подменить, иначе прогон "
             "рекурсивно запустит сам себя")
    if not g.working_tree_clean():
        fail("дерево грязное — выкатывать нечего воспроизводимого. `git status` тут не годится "
             "как предикат (clean-фильтры, skip-worktree), поэтому сверяются сырые байты")
    if subprocess.run([sys.executable, "-B", "-m", "pytest", "tests/", "-q"], cwd=ROOT).returncode != 0:
        fail("тесты красные — выкатка остановлена")


def pinned_identity() -> dict:
    """Одна неизменяемая идентичность выкатки. Без свежего fetch `origin/main` — локальная
    копия, которая вместе с устаревшим маркетплейсом даёт согласованную СТАРУЮ пару
    версия+sha и зелёный результат."""
    # `_trusted_git` для fetch НЕ годится: слой намеренно глушит сеть (`protocol.allow=never`),
    # и вызов падал с `transport 'ssh' not allowed`. Сетевая операция в проекте уже есть —
    # закалённый адаптер pre-push гейта: закреплённые бинарь git и ssh, аллоулист окружения,
    # валидация URL против `ext::`, разрешение РОВНО схемы провалидированного URL, отклонение
    # `remote.<name>.vcs`/`uploadpack`, нейтральный cwd. Третий способ ходить в сеть означал бы
    # третью копию правил доверия, которая разъедется первой (R-TRUSTED-LAYER-DUPLICATION).
    try:
        fetched = prepush_gate._prepush_fetch(ROOT, "origin", "main", _FETCH_TIMEOUT_S)
    except Exception as exc:                          # noqa: BLE001 — причина в диагностику
        fail(f"fetch отклонён политикой доверенного слоя: {type(exc).__name__}: {exc}")
    if fetched.returncode != 0:
        # Причина ОБЯЗАНА быть в сообщении: прежний текст «git fetch не удался» не давал ничего,
        # и настоящая причина (`transport 'ssh' not allowed`) искалась вручную.
        fail(f"git fetch не удался (rc={fetched.returncode}) — идентичность выкатки не "
             f"зафиксировать: {g.redact_secrets(g.strip_ansi((fetched.stderr or '').strip()))[:300]}")
    sha = _git_out("rev-parse", "origin/main")
    head = _git_out("rev-parse", "HEAD")
    if sha != head:
        fail(f"HEAD ({head[:12]}) != origin/main ({sha[:12]}) — хосты ставят плагин ИЗ GITHUB, "
             "незамерженное выкатить нельзя")
    ver = json.loads((ROOT / "plugins/gates/.claude-plugin/plugin.json").read_text())["version"]
    kimi_ver = json.loads(
        (ROOT / "plugins/gates/.kimi-plugin/plugin.json").read_text())["version"]
    codex_ver = json.loads(
        (ROOT / "plugins/gates/.codex-plugin/plugin.json").read_text())["version"]
    mkt = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    mkt_vers = {mkt["metadata"]["version"], *(p["version"] for p in mkt["plugins"])}
    if kimi_ver != ver or codex_ver.split("+", 1)[0] != ver or mkt_vers != {ver}:
        fail(f"версии рассогласованы: plugin.json={ver}, kimi={kimi_ver}, "
             f"codex={codex_ver}, marketplace={mkt_vers}")
    gr.release_identity(ver)
    tree = expected_tree(sha)
    return {"version": {"claude": ver, "codex": codex_ver, "kimi": kimi_ver},
            "sha": sha, "tree": tree, "digest": tree_digest(tree)}


def check_review_evidence(sha: str):
    panel_ok, panel_why = g._panel_evidence_ok(sha)
    if not panel_ok:
        fail(f"independent panel evidence для {sha[:12]} недействительно: {panel_why}")
    try:
        _artifact, reviewed_head, _digest = ka.build_artifact(ROOT)
    except (OSError, ValueError, RuntimeError, subprocess.CalledProcessError) as exc:
        fail(f"independent-review evidence для {sha[:12]} недействительно: {exc}")
    if reviewed_head != sha:
        fail(f"review evidence относится к {reviewed_head[:12]}, а выкатывается {sha[:12]}")


def build_and_publish(pin: dict) -> dict:
    with tempfile.TemporaryDirectory(prefix="gates-github-release-") as tmp:
        artifact = gr.build_release_artifact(pin["sha"], pin["version"]["kimi"],
                                             pin["tree"], pathlib.Path(tmp))
        gr.publish_release(artifact)
        print(f"[deploy] ✓ release {artifact['tag']} опубликован как prerelease; "
              f"ZIP SHA-256 {artifact['zip_sha256']}")
        return artifact


def _git_out(*args) -> str:
    r = g._trusted_git(*args, cwd=ROOT)
    if r is None or r.returncode != 0:
        fail(f"git {' '.join(args)} не удался — доверенный слой недоступен")
    return r.stdout.strip()


def expected_tree(sha: str) -> dict:
    """Ожидаемое дерево берётся ИЗ ЗАКРЕПЛЁННОГО КОММИТА, а не с диска.

    Прежняя версия считала дайджест по рабочему дереву и была верна лишь потому, что ДРУГОЙ
    файл (Makefile) проверял чистоту. Смысл единственной идентичности выкатки в том, чтобы
    грязное дерево не могло структурно определить, что должно стоять на флоте."""
    pref = "plugins/gates/"
    r = g._trusted_git("ls-tree", "-r", "-z", sha, "--", pref, cwd=ROOT)
    if r is None or r.returncode != 0 or not r.stdout.strip("\0"):
        fail(f"дерево плагина на {sha[:12]} не прочитано — идентичность выкатки не определена")
    tree = {}
    for rec in r.stdout.split("\0"):
        if not rec:
            continue
        meta, path = rec.split("\t", 1)
        mode, _typ, oid = meta.split()
        tree[path[len(pref):]] = f"{mode} {oid}"
    return tree


def _spec(channels, versions, sha, tree, checks, zip_sha256=None) -> str:
    result = {}
    for ch in channels:
        entry = {"version": versions[ch], "sha": sha, "tree": tree, "checks": list(checks)}
        if ch == "kimi":
            entry["source"] = gr.release_identity(versions[ch])["url"]
            entry["enabled"] = True
            if zip_sha256 is not None:
                entry["zip_sha256"] = zip_sha256
            for check in ("source", "enabled"):
                if check not in entry["checks"]:
                    entry["checks"].append(check)
        result[ch] = entry
    return json.dumps({"channels": result}, ensure_ascii=False)


# ── шаги ───────────────────────────────────────────────────────────────────────────────────
def _write_snapshot(snap: dict):
    STATE.mkdir(parents=True, exist_ok=True)
    path = STATE / f"{snap['host']}-{snap['ts'].replace(':', '')}.json"
    path.write_text(json.dumps(snap, ensure_ascii=False, indent=2))


def snapshot(host: str, channels: "list[str]") -> dict:
    """Снапшот ВСЕХ объявленных каналов одним вызовом: деплой мутирует все, и восстановление
    одного оставило бы смешанное состояние."""
    got = agent(host, "installed", ",".join(channels))
    if "error" in got:
        raise DeployAbort(f"{host}: снапшот не снят — {got['error']}. Недостижимый хост НЕ "
                          "считается первой установкой: деплой без цели отката запрещён")
    snap = {"host": host, "ts": _now(), "channels": got["channels"]}
    _write_snapshot(snap)
    return snap


def check_rollback_target(host: str, snap: dict) -> "str | None":
    """Цель отката ДОКАЗЫВАЕТСЯ тем же маршрутом, что новая установка: дерево снапшотного sha
    сходится и оба поведенческих smoke проходят.

    До мутации установленное И есть цель отката, поэтому «проверить текущую установку» и
    «проверить цель отката» — одна процедура, а не две разной силы. Прежний `test -f
    ladder_gate.py` доказывал лишь существование одного файла, тогда как обещано было
    совпадение дерева и прохождение smoke."""
    live = {ch: i for ch, i in snap["channels"].items() if i.get("state") == "installed"}
    undet = [f"{ch}: {i.get('why', '')}" for ch, i in snap["channels"].items()
             if i.get("state") == "undetermined"]
    if undet:
        return f"{host}: состояние не определено ({'; '.join(undet)}) — откатываться будет некуда"
    if not live:
        return None                                    # первая установка: откатывать не на что

    def failed(rep):
        if "error" in rep:
            return f"{host}: цель отката не проверена — {rep['error']}"
        if not rep.get("ok"):
            return (f"{host}: цель отката НЕ работает ("
                    + "; ".join(f"{p['channel']}/{p['code']}: {p['text']}"
                                for p in rep.get("problems", [])) + ") — деплой не начинается")
        return None

    for ch, info in live.items():
        if ch != "kimi":
            continue
        try:
            canonical = gr.release_identity(info.get("version"))["url"]
        except gr.ReleaseError:
            canonical = None
        if info.get("rollback_source", canonical) != canonical or not info.get("tree"):
            return f"{host}/{ch}: rollback source не canonical — откат запрещён"
        spec = {"channels": {"kimi": {
            "version": info["version"], "source": canonical, "tree": info["tree"],
            "enabled": info.get("enabled"), "migration": info.get("source") != canonical,
            "checks": ["version", "tree", "source", "enabled", "smoke"]}}}
        report = agent(host, "verify", json.dumps(spec, ensure_ascii=False))
        why = failed(report)
        if why:
            return why
        observed = (report.get("channels", {}).get("kimi") or {}).get("zip_sha256")
        if not isinstance(observed, str) or re.fullmatch(r"[0-9a-f]{64}", observed, re.ASCII) is None:
            return f"{host}/{ch}: rollback ZIP digest не зафиксирован — откат запрещён"
        info["zip_sha256"] = observed

    by_sha: "dict[str, list[str]]" = {}
    for ch, info in live.items():
        if ch == "kimi":
            continue
        if not info.get("sha"):
            return f"{host}/{ch}: у установки нет sha — цель отката недоказуема"
        by_sha.setdefault(info["sha"], []).append(ch)
    for sha, chans in by_sha.items():
        if (g._trusted_git("cat-file", "-e", f"{sha}^{{commit}}", cwd=ROOT) or
                subprocess.CompletedProcess([], 1)).returncode != 0:
            return (f"{host}/{','.join(chans)}: коммит {sha[:12]} не найден локально — "
                    "ожидаемое дерево цели отката не построить")
        why = failed(agent(host, "verify", _spec(
            chans, {ch: live[ch]["version"] for ch in chans}, sha, expected_tree(sha),
            ["version", "sha", "tree", "smoke"])))
        if why:
            return why
    return None


def update(host: str, channels: "list[str]", versions=None) -> "str | None":
    for ch in channels:
        if ch == "kimi":
            if not versions or not versions.get("kimi"):
                return f"{host}/{ch}: версия release не передана — локальный source запрещён"
            out = agent(host, "update", json.dumps({
                "version": versions["kimi"],
                "source": gr.release_identity(versions["kimi"])["url"]}))
            if "error" in out:
                return f"{host}/{ch}: {out['error']}"
            if not out.get("ok"):
                return f"{host}/{ch}: " + "; ".join(
                    problem["text"] for problem in out.get("problems", []))
            continue
        spec = CHANNELS[ch]
        for args in spec["update"]:
            r = on_host(host, [spec["cli"], *args])
            if r.returncode != 0:
                return (f"{host}/{ch}: {' '.join(args)} → rc={r.returncode} " +
                        g.redact_secrets(g.strip_ansi(
                            (r.stderr or r.stdout or '').strip()))[:200])
    return None


def verify(host: str, channels, versions, sha, tree, zip_sha256=None) -> dict:
    """Идентичность выкатки — ДЕРЕВО, а не коммит. Коммит остаётся диагностикой.

    Первая же настоящая выкатка это и показала: два коммита подряд трогали только `tests/` и
    `scripts/`, которые на хост не едут, поэтому поставка была побайтово одинакова, а
    `gitCommitSha` у установки отличался — и деплой блокировал различие без поведенческого
    смысла. Хуже: CLI обновляет по ВЕРСИИ, поэтому при неизменной версии он законно ничего не
    делает, и разойтись с закреплённым sha — норма, а не отказ.

    Строгость при этом не потеряна. Дерево сверяется целиком (20 файлов, режим и содержимое
    каждого), и «CLI отчитался успехом, ничего не сделав» ловится ровно тогда, когда это
    меняет исполняемый код."""
    rep = agent(host, "verify", _spec(channels, versions, sha, tree,
                                      ["version", "tree", "smoke"], zip_sha256))
    if "error" in rep:
        return {"ok": False, "problems": [{"code": "agent", "channel": "-",
                                           "text": rep["error"]}]}
    return rep


def rollback(host: str, snap: dict) -> "list[str]":
    """Возврат ВСЕХ каналов, снятых снапшотом: снапшот берётся по всем именно затем, чтобы
    восстановление одного не оставило смешанное состояние.

    Временная мера — долговременный фикс это `git revert` и новый деплой."""
    problems = []
    for ch, info in (snap.get("channels") or {}).items():
        if (info or {}).get("state") == "undetermined":
            problems.append(f"{host}/{ch}: состояние до деплоя не определено — возвращать не на "
                            "что, требуется ручное восстановление")
            continue
        # `absent` — ТОЖЕ цель отката: канал, установленный этим деплоем (у Codex это `plugin
        # add`), обязан быть снят. Пропуск таких каналов возвращал пустой список проблем, и
        # вызывающий печатал «откат проверен», оставив канал, которого до деплоя не было.
        out = agent(host, "restore", json.dumps({"channel": ch, "snapshot": info},
                                                ensure_ascii=False))
        if "error" in out:
            problems.append(f"{host}/{ch}: {out['error']}")
        elif not out.get("ok"):
            problems.extend(f"{host}/{ch}: {p['text']}" for p in out.get("problems", []))
    if problems:
        return problems
    # Откат тоже ДОКАЗЫВАЕТСЯ, и теми же структурными кодами: прежняя версия фильтровала
    # человеческий текст проблем по подстроке «sha», то есть чужая проблема с этим словом
    # молча зачлась бы как успешный откат.
    chans = (snap.get("channels") or {})
    live = {ch: i for ch, i in chans.items()
            if (i or {}).get("state") == "installed" and ch != "kimi"}
    for ch, i in live.items():
        if not i.get("sha"):
            problems.append(f"{host}/{ch}: у снапшота нет sha — откат не подтвердить")
    if problems:
        return problems

    def confirm(spec):
        rep = agent(host, "verify", json.dumps(spec, ensure_ascii=False))
        if "error" in rep:
            problems.append(f"{host}: откат не подтверждён — {rep['error']}")
        elif not rep.get("ok"):
            problems.extend(f"{host}/{p['channel']}/{p['code']}: {p['text']}"
                            for p in rep.get("problems", []))

    for ch, info in chans.items():
        if ch != "kimi" or (info or {}).get("state") != "installed":
            continue
        confirm({"channels": {"kimi": {
            "version": info.get("version"), "source": info.get("rollback_source") or info.get("source"),
            "tree": info.get("tree"), "enabled": info.get("enabled"),
            "zip_sha256": info.get("zip_sha256"),
            "checks": ["version", "tree", "source", "enabled", "smoke"]}}})

    gone = [ch for ch, i in chans.items() if (i or {}).get("state") == "absent" and ch != "kimi"]
    specs = [({ch: live[ch]["version"] for ch in group}, sha, expected_tree(sha),
              ["version", "sha", "tree", "smoke"], "installed")
             for sha in {i["sha"] for i in live.values() if i.get("sha")}
             for group in ([ch for ch, i in live.items() if i.get("sha") == sha],)]
    if gone:
        specs.append(({ch: "" for ch in gone}, "", {}, [], "absent"))
    for versions, sha, tree, checks, state in specs:
        spec = json.loads(_spec(list(versions), versions, sha, tree, checks))
        for ch in spec["channels"]:
            spec["channels"][ch]["state"] = state
        confirm(spec)
    if any(ch == "kimi" and (info or {}).get("state") == "absent"
           for ch, info in chans.items()):
        confirm({"channels": {"kimi": {"state": "absent", "checks": []}}})
    return problems


def session_inventory(hosts) -> dict:
    result = {"enumeration": "known", "hosts": {}, "sessions": []}
    for host in hosts:
        got = agent(host["id"], "sessions", ",".join(host["channels"]))
        if "error" in got:
            got = {"enumeration": "unknown", "sessions": [], "error": got["error"]}
        result["hosts"][host["id"]] = got
        sessions = got.get("sessions")
        if got.get("enumeration") != "known" or not isinstance(sessions, list) or not sessions:
            result["enumeration"] = "unknown"
            continue
        for session in sessions:
            if not isinstance(session, dict) or not session.get("id"):
                result["enumeration"] = "unknown"
                continue
            result["sessions"].append({"host": host["id"], **session})
    return result


def activation_status(inventory: dict, observations: dict, expected_version=None) -> dict:
    if inventory.get("enumeration") != "known" or not inventory.get("sessions"):
        return {"complete": False, "status": "sessions-unknown", "pending": ["enumeration"]}
    proofs = {}
    for host, observed in observations.items():
        for proof in observed.get("sessions", []):
            if isinstance(proof, dict) and proof.get("id"):
                proofs[(host, proof["id"])] = proof
    pending = []
    for session in inventory["sessions"]:
        proof = proofs.get((session["host"], session["id"]), {})
        version_ok = bool(proof.get("version")) and (
            expected_version is None or proof.get("version") == expected_version)
        proven = version_ok and proof.get("smoke") == "passed"
        if not proven:
            pending.append(session["id"])
    return {"complete": not pending, "status": "complete" if not pending else "reload-required",
            "pending": pending}


def activation(inventory: dict, hosts, expected_version=None) -> dict:
    observations = {}
    for host in hosts:
        sessions = [session for session in inventory.get("sessions", [])
                    if session["host"] == host["id"]]
        got = agent(host["id"], "activate", json.dumps({"sessions": sessions}, ensure_ascii=False))
        observations[host["id"]] = {} if "error" in got else got
    return activation_status(inventory, observations, expected_version)


def write_pending(status: str, pin: dict, hosts, activation_report=None):
    STATE.mkdir(parents=True, exist_ok=True)
    record = {"status": status, "sha": pin["sha"], "version": pin["version"],
              "hosts": [host["id"] for host in hosts], "ts": _now()}
    if activation_report is not None:
        record["activation"] = "complete" if activation_report.get("complete") else "pending"
        record["activation_detail"] = activation_report
    (STATE / "pending").write_text(json.dumps(record, ensure_ascii=False, indent=2))


def _take_lock(sha: str):
    """Атомарно, а не `exists()`+`write_text()`: между проверкой и записью помещается второй
    прогон, и два деплоя с разными sha дали бы зелёный результат на расколотом флоте."""
    STATE.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(LOCK, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            held = LOCK.read_text().strip()
        except OSError:
            held = "?"
        fail(f"уже идёт деплой ({held}) — два прогона с разными sha могли бы чередоваться и "
             f"дать зелёный результат на расколотом флоте. Если это остаток от упавшего "
             f"прогона: rm {LOCK}")
    with os.fdopen(fd, "w") as fh:
        fh.write(f"pid={os.getpid()} sha={sha[:12]} ts={_now()}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Деплой плагина gates на флот")
    ap.add_argument("--restore", metavar="HOST", help="вернуть хост на прошлую версию")
    ap.add_argument("--status", action="store_true", help="последняя сертификация флота")
    ap.add_argument("--check", action="store_true",
                    help="только pre-deploy гейт: дерево, тесты, HEAD, версии")
    args = ap.parse_args(argv)

    if args.status:
        f = STATE / "stable"
        # Это ЗАПИСЬ последней сертификации, а не опрос хостов: утверждать состояние флота, не
        # спросив его, — тот самый класс, который этот деплой и ловит. Опрос — `make deploy`.
        print("последняя сертификация (запись, не опрос флота):" if f.is_file() else "",
              f.read_text() if f.is_file() else f"(не сертифицирован; состояние: {STATE})")
        return 0

    if args.check:
        pre_deploy_gate()
        pin = pinned_identity()
        check_review_evidence(pin["sha"])
        print(f"[deploy] готово к выкатке: {pin['version']['claude']} ({pin['sha'][:12]}), "
              f"дерево {len(pin['tree'])} файлов")
        return 0

    inventory = read_inventory()
    known = {h["id"] for h in inventory}
    subset = [h.strip() for h in os.environ.get("GATES_HOSTS", "").split() if h.strip()]
    if set(subset) - known:
        fail(f"GATES_HOSTS называет хосты вне инвентаря: {sorted(set(subset) - known)}")
    hosts = [h for h in inventory if not subset or h["id"] in subset]
    omitted = [h["id"] for h in inventory if h["id"] not in {x["id"] for x in hosts}]

    if args.restore:
        if args.restore not in known:
            fail(f"{args.restore} не в инвентаре")
        # Тот же замок, что у деплоя: `make gates-restore` — естественный рефлекс оператора при
        # зависшей выкатке, и без замка он мутирует хост параллельно идущему деплою, порождая
        # ровно то смешанное состояние, ради которого замок и введён.
        load_agent(_git_out("rev-parse", "origin/main"))
        _take_lock(f"restore:{args.restore}")
        try:
            # Имя хоста сверяется по СОДЕРЖИМОМУ: глоб `vps-*` подхватывал снапшот `vps-a`.
            snaps = [s for s in sorted(STATE.glob(f"{args.restore}-*.json"))
                     if json.loads(s.read_text()).get("host") == args.restore]
            if not snaps:
                fail(f"снапшотов для {args.restore} нет (каталог {STATE})")
            # Сертификация флота больше не действительна: один хост уезжает на прошлую версию.
            (STATE / "stable").unlink(missing_ok=True)
            problems = rollback(args.restore, json.loads(snaps[-1].read_text()))
        finally:
            LOCK.unlink(missing_ok=True)
            _drop_mux([args.restore])
        print("\n".join(problems) if problems
              else f"[deploy] ✓ {args.restore} возвращён и проверен; сертификация флота снята",
              file=sys.stderr)
        return 1 if problems else 0

    pre_deploy_gate()
    pin = pinned_identity()
    check_review_evidence(pin["sha"])
    ver, sha, tree, digest = pin["version"], pin["sha"], pin["tree"], pin["digest"]
    load_agent(sha)
    _take_lock(sha)
    try:
        try:
            artifact = build_and_publish(pin)
        except gr.ReleaseError as exc:
            fail(f"release не опубликован/не проверен: {exc}")
        sessions = session_inventory(hosts)
        print(f"[deploy] версия {ver['claude']}, sha {sha[:12]}, дерево {len(tree)} файлов "
              f"(дайджест {digest})")
        print(f"[deploy] флот: {', '.join(h['id'] for h in hosts)}"
              + (f"   ПРОПУЩЕНЫ: {', '.join(omitted)}" if omitted else ""))
        # Когорта — хосты, которых мутация УЖЕ коснулась. Раньше отказ снапшота или цели
        # отката на втором хосте уходил через `fail()` мимо отката: канарейка оставалась на
        # новой версии, второй хост на старой, а `stable` прошлого деплоя продолжал
        # сертифицировать флот — ровно тот version skew, ради устранения которого всё делается.
        cohort: "list[tuple[dict, dict]]" = []
        try:
            prepared = []
            for h in hosts:
                print(f"[deploy] preflight: {h['id']} ({', '.join(h['channels'])})")
                snap = snapshot(h["id"], h["channels"])
                why = check_rollback_target(h["id"], snap)
                if why:
                    raise DeployAbort(why)
                _write_snapshot(snap)
                prepared.append((h, snap))
            # Ни один хост не мутируется, пока snapshots и rollback targets не доказаны для
            # всей выбранной когорты. Сертификация снимается ДО первой мутации.
            (STATE / "stable").unlink(missing_ok=True)
            for i, (h, snap) in enumerate(prepared):
                tag = "канарейка" if i == 0 else f"хост {i + 1}/{len(hosts)}"
                print(f"[deploy] {tag}: {h['id']} ({', '.join(h['channels'])})")
                cohort.append((h, snap))
                why = update(h["id"], h["channels"], ver)
                if why:
                    raise DeployAbort(why)
                rep = verify(h["id"], h["channels"], ver, sha, tree, artifact["zip_sha256"])
                if not rep["ok"]:
                    raise DeployAbort("; ".join(f"{h['id']}/{p['channel']}/{p['code']}: "
                                                f"{p['text']}" for p in rep["problems"]),
                                      artifact=any(p.get("code") in _ARTIFACT_PROBLEM_CODES
                                                   for p in rep["problems"]))
                drift = [f"{ch}: {(rep['channels'].get(ch) or {}).get('sha') or '?'}"[:20]
                         for ch in h["channels"]
                         if (rep["channels"].get(ch) or {}).get("sha") not in (None, sha)]
                print(f"[deploy] ✓ {h['id']}: версия {ver['claude']}, дерево сошлось, гейт "
                      "блокирует и пропускает как должен"
                      + (f"   (коммит установки иной: {', '.join(drift)} — поставка та же)"
                         if drift else ""))
        except DeployAbort as abort:
            print(f"[deploy] ✗ {abort}", file=sys.stderr)
            if not cohort:
                print("[deploy] мутаций не было — флот не тронут; release остаётся prerelease",
                      file=sys.stderr)
                return 2
            if abort.artifact:
                try:
                    gr.mark_release_draft(artifact)
                except gr.ReleaseError as exc:
                    print(f"[deploy] ⛔ release не переведён в draft: {exc}", file=sys.stderr)
            for prev_h, prev_snap in cohort:
                errs = rollback(prev_h["id"], prev_snap)
                if errs:
                    print(f"[deploy] ⛔ ОТКАТ НЕ УДАЛСЯ: {'; '.join(errs)}\n"
                          f"  снапшоты: {STATE}\n"
                          f"  вручную: make gates-restore HOST={prev_h['id']}", file=sys.stderr)
                else:
                    print(f"[deploy] откат {prev_h['id']}: проверен", file=sys.stderr)
            return 2
        if omitted:
            write_pending("partial-installed", pin, hosts)
            print(f"[deploy] ⚠️ ЧАСТИЧНО: пропущены {', '.join(omitted)} — `stable` НЕ пишется, "
                  "флот не сертифицирован", file=sys.stderr)
            return 3
        gr.mark_release_latest(artifact)
        activation_report = activation(sessions, hosts, ver["kimi"])
        if not activation_report.get("complete"):
            write_pending("installed", pin, hosts, activation_report)
            print("[deploy] ⚠️ установлено, но активные сессии не доказаны. Kimi: /reload или /new; "
                  "Claude Code/Codex: restart. stable НЕ записан", file=sys.stderr)
            return 4
        (STATE / "stable").write_text(json.dumps(
            {"version": ver, "sha": sha, "digest": digest, "release": artifact["url"],
             "hosts": [h["id"] for h in hosts], "activation": "complete",
             "ts": _now()}, ensure_ascii=False, indent=2))
        (STATE / "pending").unlink(missing_ok=True)
        print(f"[deploy] ✓ флот на {ver['claude']} ({sha[:12]}); release latest; stable записан")
        return 0
    finally:
        LOCK.unlink(missing_ok=True)
        _drop_mux([h["id"] for h in hosts])


if __name__ == "__main__":
    sys.exit(main())
