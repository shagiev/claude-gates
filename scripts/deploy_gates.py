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

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "gates" / "scripts"))
sys.path.insert(0, str(ROOT / "scripts"))
import codex_review_gate as g                                                    # noqa: E402
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
    if r is None or r.returncode != 0 or not r.stdout.strip():
        fail(f"агент не прочитан из коммита {sha[:12]} — на хост нечего везти")
    _AGENT_SRC = r.stdout


def agent(host: str, subcmd: str, arg: str) -> dict:
    """Хост-агент доставляется по stdin и вызывается подкомандой — ОДИН модуль на все нужды.

    Возврат — либо разобранный JSON, либо `{"error": …}`. Ошибка транспорта НИКОГДА не
    превращается в пустой результат: «хост не ответил» и «плагин не установлен» — разные
    факты, и их склейка объявила бы недостижимую машину первой установкой."""
    if _AGENT_SRC is None:
        fail("агент не загружен из коммита — вызов до `load_agent`")
    r = on_host(host, ["python3", "-", subcmd, arg], stdin=_AGENT_SRC)
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
    """Гейт принадлежит МУТИРУЮЩЕМУ скрипту, а не Makefile: `python3 scripts/deploy_gates.py` —
    документированный прямой вызов, и через него флот мутировался бы без проверки дерева и
    тестов вовсе."""
    if os.environ.get("PYTEST_CURRENT_TEST"):
        fail("pre-deploy гейт вызван из-под pytest — тест обязан его подменить, иначе прогон "
             "рекурсивно запустит сам себя")
    if not g.working_tree_clean():
        fail("дерево грязное — выкатывать нечего воспроизводимого. `git status` тут не годится "
             "как предикат (clean-фильтры, skip-worktree), поэтому сверяются сырые байты")
    if subprocess.run([sys.executable, "-m", "pytest", "tests/", "-q"], cwd=ROOT).returncode != 0:
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
    codex_ver = json.loads(
        (ROOT / "plugins/gates/.codex-plugin/plugin.json").read_text())["version"]
    mkt = json.loads((ROOT / ".claude-plugin/marketplace.json").read_text())
    mkt_vers = {mkt["metadata"]["version"], *(p["version"] for p in mkt["plugins"])}
    if codex_ver.split("+", 1)[0] != ver or mkt_vers != {ver}:
        fail(f"версии рассогласованы: plugin.json={ver}, codex={codex_ver}, marketplace={mkt_vers}")
    tree = expected_tree(sha)
    return {"version": {"claude": ver, "codex": codex_ver}, "sha": sha, "tree": tree,
            "digest": tree_digest(tree)}


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


def _spec(channels, versions, sha, tree, checks) -> str:
    return json.dumps({"channels": {
        ch: {"version": versions[ch], "sha": sha, "tree": tree, "checks": checks}
        for ch in channels}}, ensure_ascii=False)


# ── шаги ───────────────────────────────────────────────────────────────────────────────────
def snapshot(host: str, channels: "list[str]") -> dict:
    """Снапшот ВСЕХ объявленных каналов одним вызовом: деплой мутирует все, и восстановление
    одного оставило бы смешанное состояние."""
    got = agent(host, "installed", ",".join(channels))
    if "error" in got:
        raise DeployAbort(f"{host}: снапшот не снят — {got['error']}. Недостижимый хост НЕ "
                          "считается первой установкой: деплой без цели отката запрещён")
    snap = {"host": host, "ts": _now(), "channels": got["channels"]}
    STATE.mkdir(parents=True, exist_ok=True)
    (STATE / f"{host}-{snap['ts'].replace(':', '')}.json").write_text(
        json.dumps(snap, ensure_ascii=False, indent=2))
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
    by_sha: "dict[str, list[str]]" = {}
    for ch, info in live.items():
        if not info.get("sha"):
            return f"{host}/{ch}: у установки нет sha — цель отката недоказуема"
        by_sha.setdefault(info["sha"], []).append(ch)
    for sha, chans in by_sha.items():
        if (g._trusted_git("cat-file", "-e", f"{sha}^{{commit}}", cwd=ROOT) or
                subprocess.CompletedProcess([], 1)).returncode != 0:
            return (f"{host}/{','.join(chans)}: коммит {sha[:12]} не найден локально — "
                    "ожидаемое дерево цели отката не построить")
        rep = agent(host, "verify", _spec(
            chans, {ch: live[ch]["version"] for ch in chans}, sha, expected_tree(sha),
            ["version", "sha", "tree", "smoke"]))
        if "error" in rep:
            return f"{host}: цель отката не проверена — {rep['error']}"
        if not rep["ok"]:
            return (f"{host}: цель отката НЕ работает ("
                    + "; ".join(f"{p['channel']}/{p['code']}: {p['text']}"
                                for p in rep["problems"]) + ") — деплой не начинается")
    return None


def update(host: str, channels: "list[str]") -> "str | None":
    for ch in channels:
        spec = CHANNELS[ch]
        for args in spec["update"]:
            r = on_host(host, [spec["cli"], *args])
            if r.returncode != 0:
                return (f"{host}/{ch}: {' '.join(args)} → rc={r.returncode} " +
                        g.redact_secrets(g.strip_ansi(
                            (r.stderr or r.stdout or '').strip()))[:200])
    return None


def verify(host: str, channels, versions, sha, tree) -> dict:
    rep = agent(host, "verify", _spec(channels, versions, sha, tree,
                                      ["version", "sha", "tree", "smoke"]))
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
    live = {ch: i for ch, i in chans.items() if (i or {}).get("state") == "installed"}
    for ch, i in live.items():
        if not i.get("sha"):
            # Иначе канал молча выпадал из множества постпроверки, и «откат проверен»
            # печаталось про канал, который не проверяли вовсе.
            problems.append(f"{host}/{ch}: у снапшота нет sha — откат не подтвердить")
    if problems:
        return problems
    gone = [ch for ch, i in chans.items() if (i or {}).get("state") == "absent"]
    specs = [({ch: live[ch]["version"] for ch in group}, sha, expected_tree(sha),
              ["version", "sha", "tree"], "installed")
             for sha in {i["sha"] for i in live.values() if i.get("sha")}
             for group in ([ch for ch, i in live.items() if i.get("sha") == sha],)]
    if gone:
        specs.append(({ch: "" for ch in gone}, "", {}, [], "absent"))
    for versions, sha, tree, checks, state in specs:
        spec = json.loads(_spec(list(versions), versions, sha, tree, checks))
        for ch in spec["channels"]:
            spec["channels"][ch]["state"] = state
        rep = agent(host, "verify", json.dumps(spec, ensure_ascii=False))
        if "error" in rep:
            problems.append(f"{host}: откат не подтверждён — {rep['error']}")
        elif not rep["ok"]:
            problems.extend(f"{host}/{p['channel']}/{p['code']}: {p['text']}"
                            for p in rep["problems"])
    return problems


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
    ver, sha, tree, digest = pin["version"], pin["sha"], pin["tree"], pin["digest"]
    load_agent(sha)
    _take_lock(sha)
    try:
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
            for i, h in enumerate(hosts):
                tag = "канарейка" if i == 0 else f"хост {i + 1}/{len(hosts)}"
                print(f"[deploy] {tag}: {h['id']} ({', '.join(h['channels'])})")
                snap = snapshot(h["id"], h["channels"])
                why = check_rollback_target(h["id"], snap)
                if why:
                    raise DeployAbort(why)
                # Сертификация снимается ДО первой мутации: с этого момента запись `stable`
                # больше не соответствует флоту, чем бы прогон ни кончился.
                (STATE / "stable").unlink(missing_ok=True)
                cohort.append((h, snap))
                why = update(h["id"], h["channels"])
                if why:
                    raise DeployAbort(why)
                rep = verify(h["id"], h["channels"], ver, sha, tree)
                if not rep["ok"]:
                    raise DeployAbort("; ".join(f"{h['id']}/{p['channel']}/{p['code']}: "
                                                f"{p['text']}" for p in rep["problems"]))
                print(f"[deploy] ✓ {h['id']}: версия {ver['claude']}, дерево сошлось, гейт "
                      "блокирует и пропускает как должен")
        except DeployAbort as abort:
            print(f"[deploy] ✗ {abort}", file=sys.stderr)
            if not cohort:
                print("[deploy] мутаций не было — флот не тронут", file=sys.stderr)
                return 2
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
            print(f"[deploy] ⚠️ ЧАСТИЧНО: пропущены {', '.join(omitted)} — `stable` НЕ пишется, "
                  "флот не сертифицирован", file=sys.stderr)
            return 3
        (STATE / "stable").write_text(json.dumps(
            {"version": ver, "sha": sha, "digest": digest,
             "hosts": [h["id"] for h in hosts], "ts": _now()}, ensure_ascii=False, indent=2))
        print(f"[deploy] ✓ флот на {ver['claude']} ({sha[:12]}); stable записан")
        return 0
    finally:
        LOCK.unlink(missing_ok=True)
        _drop_mux([h["id"] for h in hosts])


if __name__ == "__main__":
    sys.exit(main())
