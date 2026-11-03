#!/usr/bin/env python3
"""Helpers for artifact-bound Kimi acceptance and crash-safe plugin-state rollback."""
import hashlib
import io
import json
import os
import pwd
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
from pathlib import Path


class RestoreConflict(Exception):
    pass


class ApiSchemaError(Exception):
    pass


def directory_digest(root: Path) -> str:
    root = Path(root)
    h = hashlib.sha256()
    if root.is_symlink():
        raise RestoreConflict(f"symlink в managed installation: {root}")
    if not root.exists():
        return h.hexdigest()
    for path in sorted([root, *root.rglob("*")]):
        if path.is_symlink():
            raise RestoreConflict(f"symlink в managed installation: {path}")
        rel = "." if path == root else path.relative_to(root).as_posix()
        mode = path.stat().st_mode & 0o7777
        if path.is_dir():
            h.update(f"dir\0{mode:o}\0{rel}\0".encode())
        elif path.is_file():
            data = path.read_bytes()
            h.update(f"file\0{mode:o}\0{rel}\0{len(data)}\0".encode())
            h.update(data)
        else:
            raise RestoreConflict(f"необычный файл в managed installation: {path}")
    return h.hexdigest()


def current_gates(journal: dict):
    return journal.get("gates")


def snapshot(home: Path) -> dict:
    home = Path(home)
    registry = home / "plugins/installed.json"
    managed = home / "plugins/managed/gates"
    data = json.loads(registry.read_text()) if registry.exists() else {"version": 1, "plugins": []}
    records = [p for p in data.get("plugins", []) if p.get("id") == "gates"]
    if len(records) > 1:
        raise RestoreConflict("несколько записей gates — snapshot неоднозначен")
    files = {}
    modes = {}
    directories = {}
    if managed.exists():
        for path in [managed, *managed.rglob("*")]:
            if path.is_symlink():
                raise RestoreConflict(f"symlink в managed installation: {path}")
            rel = "." if path == managed else path.relative_to(managed).as_posix()
            mode = path.stat().st_mode & 0o7777
            if path.is_dir():
                directories[rel] = mode
            elif path.is_file():
                files[rel] = path.read_bytes()
                modes[rel] = mode
            else:
                raise RestoreConflict(f"необычный файл в managed installation: {path}")
    return {
        "home": home,
        "gates": records[0] if records else None,
        "index": next((i for i, p in enumerate(data.get("plugins", []))
                       if p.get("id") == "gates"), 0),
        "managed": files if managed.exists() else None,
        "managed_modes": modes,
        "managed_dirs": directories,
        "managed_digest": directory_digest(managed),
    }


def _write_tree(root: Path, files: "dict | None", modes=None, directories=None) -> None:
    if root.exists():
        shutil.rmtree(root)
    if files is None:
        return
    modes = modes or {}
    directories = directories or {".": 0o755}
    root.mkdir(parents=True, exist_ok=True)
    for rel in sorted(directories, key=lambda value: value.count("/")):
        path = root if rel == "." else root / rel
        path.mkdir(parents=True, exist_ok=True)
    for rel, data in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(modes.get(rel, 0o644))
    for rel in sorted(directories, key=lambda value: value.count("/"), reverse=True):
        path = root if rel == "." else root / rel
        path.chmod(directories[rel])


def _atomic_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def _journal_path() -> Path:
    home = Path(pwd.getpwuid(os.getuid()).pw_dir)
    return home / ".local/state/claude-gates/kimi-acceptance-recovery.json"


def _encode_journal(journal: dict) -> dict:
    managed = journal.get("managed")
    return {**journal, "home": str(journal["home"]),
            "managed": None if managed is None else {rel: data.hex() for rel, data in managed.items()}}


def _decode_journal(data: dict) -> dict:
    required = ("home", "gates", "index", "managed", "managed_modes", "managed_dirs",
                "managed_digest")
    if not isinstance(data, dict) or any(key not in data for key in required) \
            or not isinstance(data["home"], str) or not Path(data["home"]).is_absolute() \
            or not isinstance(data["index"], int) or isinstance(data["index"], bool) \
            or not isinstance(data["managed_digest"], str) \
            or not isinstance(data["gates"], (dict, type(None))) \
            or not isinstance(data.get("expected_current"), (dict, type(None))) \
            or data.get("phase") not in (None, "snapshot", "installing", "candidate",
                                          "removing-candidate", "installing-original",
                                          "restoring-enabled") \
            or not isinstance(data.get("expected_managed_digest"), (str, type(None))):
        raise RestoreConflict("recovery journal имеет невалидную схему")

    def valid_modes(value, directory=False):
        return isinstance(value, dict) and all(
            isinstance(rel, str) and rel and (directory or rel != ".")
            and not Path(rel).is_absolute() and ".." not in Path(rel).parts
            and isinstance(mode, int) and not isinstance(mode, bool) and 0 <= mode <= 0o7777
            for rel, mode in value.items())

    raw = data["managed"]
    modes = data["managed_modes"]
    directories = data["managed_dirs"]
    if raw is not None and (not isinstance(raw, dict)
                            or not all(isinstance(k, str) and isinstance(v, str)
                                       for k, v in raw.items())):
        raise RestoreConflict("recovery journal managed имеет невалидную схему")
    if not valid_modes(modes) or not valid_modes(directories, directory=True) \
            or (raw is None and (modes or directories)) \
            or (raw is not None and (set(raw) != set(modes) or "." not in directories)):
        raise RestoreConflict("recovery journal modes/dirs имеет невалидную схему")
    try:
        managed = None if raw is None else {rel: bytes.fromhex(blob) for rel, blob in raw.items()}
    except ValueError as exc:
        raise RestoreConflict("recovery journal managed повреждён") from exc
    return {**data, "home": Path(data["home"]), "managed": managed}


def save_journal(journal: dict, path=None) -> Path:
    path = Path(path) if path is not None else _journal_path()
    _atomic_json(path, _encode_journal(journal))
    return path


def load_journal(path=None) -> "dict | None":
    path = Path(path) if path is not None else _journal_path()
    if not path.exists():
        return None
    return _decode_journal(json.loads(path.read_text()))


def _current_record(home: Path):
    registry = home / "plugins/installed.json"
    data = json.loads(registry.read_text()) if registry.exists() else {"plugins": []}
    records = [p for p in data.get("plugins", []) if p.get("id") == "gates"]
    if len(records) > 1:
        raise RestoreConflict("несколько записей gates — recovery неоднозначен")
    return records[0] if records else None


def _restore_via_api(journal: dict, journal_path: Path, base: str, token: str) -> None:
    home = Path(journal["home"])
    managed = home / "plugins/managed/gates"
    staged = managed.parent / ".gates-acceptance-original"
    original_digest = journal["managed_digest"]
    expected = journal.get("expected_current")
    expected_digest = journal.get("expected_managed_digest")
    phase = journal.get("phase")

    if phase == "candidate":
        if expected is None or not expected_digest:
            raise RestoreConflict("candidate identity не зафиксирована — recovery остановлен")
        if _current_record(home) != expected:
            raise RestoreConflict("запись gates изменилась вне acceptance — restore остановлен")
        if directory_digest(managed) != expected_digest:
            raise RestoreConflict("managed/gates изменился вне acceptance — restore остановлен")
        _write_tree(staged, journal.get("managed"), journal.get("managed_modes"),
                    journal.get("managed_dirs"))
        if directory_digest(staged) != original_digest:
            raise RestoreConflict("staged original digest не совпал")
        journal["phase"] = phase = "removing-candidate"
        save_journal(journal, journal_path)

    if phase == "removing-candidate":
        current = _current_record(home)
        if current is not None:
            if current != expected or directory_digest(managed) != expected_digest:
                raise RestoreConflict("candidate изменился до API remove — recovery остановлен")
            _api(base, token, "POST", "/api/v1/plugins/gates:remove")
        if journal["gates"] is None:
            if _current_record(home) is not None:
                raise RestoreConflict("API remove не удалил запись candidate")
            return
        journal["phase"] = phase = "installing-original"
        save_journal(journal, journal_path)

    if phase == "installing-original":
        if directory_digest(staged) != original_digest:
            raise RestoreConflict("staged original отсутствует или изменён")
        current = _current_record(home)
        if current is None:
            _api(base, token, "POST", "/api/v1/plugins", {"source": str(staged)})
        if directory_digest(managed) != original_digest:
            raise RestoreConflict("API install не восстановил исходный managed digest")
        journal["phase"] = phase = "restoring-enabled"
        save_journal(journal, journal_path)

    if phase != "restoring-enabled":
        raise RestoreConflict(f"неизвестная restore-фаза: {phase}")
    if directory_digest(managed) != original_digest:
        raise RestoreConflict("managed/gates изменился после API restore")
    current = _current_record(home)
    if current is None:
        raise RestoreConflict("исходная запись gates отсутствует после API install")
    desired = journal["gates"].get("enabled") is True
    if (current.get("enabled") is True) != desired:
        action = "enable" if desired else "disable"
        _api(base, token, "POST", f"/api/v1/plugins/gates:{action}")
        current = _current_record(home)
    if current is None or (current.get("enabled") is True) != desired \
            or current.get("state") not in (None, "ok") \
            or directory_digest(managed) != original_digest:
        raise RestoreConflict("API restore не подтвердил исходное состояние gates")


def recover(path=None, base="", token="") -> bool:
    path = Path(path) if path is not None else _journal_path()
    journal = load_journal(path)
    if journal is None:
        return False
    home = Path(journal["home"])
    managed = home / "plugins/managed/gates"
    if journal.get("phase") in (None, "snapshot", "installing"):
        if _current_record(home) != journal["gates"] \
                or directory_digest(managed) != journal["managed_digest"]:
            raise RestoreConflict("identity candidate не зафиксирована после install — "
                                  "recovery остановлен, нужен ручной restore по journal")
    else:
        _restore_via_api(journal, path, base, token)
    for staging in (managed.parent / ".gates-acceptance-original",
                    managed.parent / ".gates-acceptance-candidate"):
        if staging.exists():
            shutil.rmtree(staging)
    path.unlink()
    return True


def require_plugin_api(spec: dict) -> None:
    paths = spec.get("paths") if isinstance(spec, dict) else None
    required = {
        "/api/v1/plugins": {"get", "post"},
        "/api/v1/plugins/{plugin_id}:{action}": {"post"},
        "/api/v1/sessions": {"post"},
        "/api/v1/sessions/{session_id}": {"get"},
        "/api/v1/sessions/{session_id}/prompts": {"get", "post"},
        "/api/v1/sessions/{session_id}/messages": {"get"},
        "/api/v1/sessions/{session_id}/transcript/user-messages": {"get"},
        "/openapi.json": {"get"},
    }
    if not isinstance(paths, dict):
        raise ApiSchemaError("OpenAPI paths отсутствуют")
    for path, methods in required.items():
        operations = paths.get(path)
        if not isinstance(operations, dict) or not methods.issubset(operations):
            raise ApiSchemaError(f"OpenAPI не содержит {methods} {path}")


def _run(argv, cwd, **kwargs):
    return subprocess.run(argv, cwd=cwd, text=True, capture_output=True, **kwargs)


def build_artifact(root: Path) -> "tuple[Path, str, str]":
    result = _run([".githooks/gates-run", "codex_review_gate.py", "verify-deployable"], root)
    if result.returncode:
        raise RuntimeError(result.stderr[-1000:])
    values = dict(re.findall(r"^(GATES_[A-Z_]+)=(.+)$", result.stdout, re.MULTILINE))
    artifact = Path(values["GATES_ARTIFACT"])
    head = values["GATES_ARTIFACT_HEAD"]
    digest = values["GATES_ARTIFACT_SHA256"]
    decision = _run([".githooks/gates-run", "codex_review_gate.py", "check-decision",
                     "--head", head], root)
    checked = _run([".githooks/gates-run", "codex_review_gate.py", "check-artifact",
                    "--artifact", str(artifact), "--head", head, "--sha256", digest], root)
    if decision.returncode or checked.returncode:
        raise RuntimeError("reviewed HEAD/artifact drift: " + decision.stderr + checked.stderr)
    return artifact, head, digest


def _api(base: str, token: str, method: str, path: str, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(base + path, data=data, method=method,
                                 headers={"Authorization": f"Bearer {token}",
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        payload = json.load(response)
    if payload.get("code") != 0:
        raise RuntimeError(f"Server API {method} {path}: {payload}")
    return payload.get("data")


def _messages(base: str, token: str, session: str):
    data = _api(base, token, "GET", f"/api/v1/sessions/{session}/messages?page_size=100")
    if not isinstance(data, dict) or not isinstance(data.get("items"), list):
        raise ApiSchemaError("messages не содержит items")
    return data["items"]


def _run_prompt(base: str, token: str, session: str, text: str, seen: set,
                timeout_s=900):
    accepted = _api(base, token, "POST", f"/api/v1/sessions/{session}/prompts", {
        "content": [{"type": "text", "text": text}], "permission_mode": "auto"})
    if not isinstance(accepted, dict) or not accepted.get("user_message_id") \
            or not accepted.get("prompt_id"):
        raise ApiSchemaError("prompt acceptance не содержит prompt/user message id")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        time.sleep(0.5)
        history = _messages(base, token, session)
        new = [message for message in history if message.get("id") not in seen]
        queue = _api(base, token, "GET", f"/api/v1/sessions/{session}/prompts")
        state = _api(base, token, "GET", f"/api/v1/sessions/{session}")
        reason = state.get("last_turn_reason") if isinstance(state, dict) else None
        has_response = any(message.get("role") == "assistant" for message in new)
        if not queue.get("active") and not queue.get("queued") and has_response and reason:
            if reason != "completed":
                raise RuntimeError(f"Kimi turn завершился со статусом {reason}")
            seen.update(message["id"] for message in history)
            return accepted["prompt_id"], new
    raise RuntimeError(f"Kimi prompt не завершился за {timeout_s} секунд")


REVIEW_FOCUS = "Проверь корректность, fail-open, безопасность, регрессии и тесты новых веток по AGENTS.md"
DESIGN_FOCUS = "Проверь дизайн, альтернативы и BSAC по AGENTS.md"


def _reviewer_invocation(runner_digest: str, design=False) -> str:
    scope = ["--design-file", "docs/design.md"] if design else ["--scope", "working-tree"]
    focus = DESIGN_FOCUS if design else REVIEW_FOCUS
    args = ["codex_review_gate.py", "companion-review", *scope, focus]
    code = ("import hashlib,os,sys\n"
            "data=open(\".githooks/gates-run\",\"rb\").read()\n"
            f"if hashlib.sha256(data).hexdigest()!=\"{runner_digest}\": sys.exit(2)\n"
            "r,w=os.pipe()\n"
            "os.set_inheritable(r,True)\n"
            "os.dup2(r,0)\n"
            "with os.fdopen(w,\"wb\") as stream:\n"
            "    stream.write(data)\n"
            f"os.execv(\"/bin/sh\",[\"/bin/sh\",\"-s\",{args!r}[0],*{args!r}[1:]])")
    return "/usr/bin/python3 -I -B -c " + shlex.quote(code)


def _canonical_reviewer_command(command: str, runner_digest: str) -> bool:
    return command == _reviewer_invocation(runner_digest)


def _canonical_design_command(command: str, runner_digest: str) -> bool:
    return command == _reviewer_invocation(runner_digest, design=True)


def _require_tool(messages, name: str, needle: str, error: bool, turn=None, reviewer=False,
                  design=False, runner_digest=""):
    parts = [part for message in messages for part in message.get("content", [])
             if isinstance(part, dict)]
    call_parts = [part for message in messages
                  if turn is None or message.get("prompt_id") == turn
                  for part in message.get("content", []) if isinstance(part, dict)]
    matches = []
    for call in (p for p in call_parts if p.get("type") == "tool_use"):
        if call.get("name") != name or needle not in json.dumps(call.get("input"), ensure_ascii=False):
            continue
        command = (call.get("input") or {}).get("command", "")
        if (reviewer and not _canonical_reviewer_command(command, runner_digest)) \
                or (design and not _canonical_design_command(command, runner_digest)):
            continue
        call_id = call.get("id")
        results = [p for p in parts if p.get("type") == "tool_result"
                   and p.get("tool_use_id") == call_id]
        if len(results) != 1 or results[0].get("is_error") is not error:
            raise RuntimeError(f"tool_result для {name} {needle!r} не доказал error={error}")
        matches.append((call, results[0]))
    if (reviewer or design) and len(matches) != 1:
        raise RuntimeError(f"transcript содержит {len(matches)} canonical review вызовов вместо одного")
    if matches:
        return matches[0]
    raise RuntimeError(f"transcript не содержит {name} с {needle!r}")


def _require_command(base: str, token: str, session: str, command: str, turn: str):
    data = _api(base, token, "GET", f"/api/v1/sessions/{session}/transcript/user-messages")
    agents = data.get("agents") if isinstance(data, dict) else None
    if not isinstance(agents, list):
        raise ApiSchemaError("transcript/user-messages не содержит agents")
    for agent in agents:
        for message in agent.get("messages", []):
            if message.get("turn_id") == turn and message.get("origin") == "plugin-command" \
                    and command in message.get("prompt", ""):
                return
    raise RuntimeError(f"plugin command {command} не подтверждена для turn {turn}")


def _working_diff_digest(repo: Path) -> str:
    diff = _run(["git", "diff", "HEAD"], repo)
    diff.check_returncode()
    untracked = _run(["git", "ls-files", "--others", "--exclude-standard"], repo)
    untracked.check_returncode()
    h = hashlib.sha256(diff.stdout.encode())
    for name in sorted(untracked.stdout.splitlines()):
        h.update(name.encode() + b"\0")
        h.update((repo / name).read_bytes())
    return h.hexdigest()


def _require_reviewer_result(result, repo: Path, expected_diff: str) -> None:
    content = result.get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = "\n".join(part.get("text", "") for part in content
                         if isinstance(part, dict) and part.get("type") == "text")
    else:
        text = ""
    clean = re.fullmatch(r"# Codex Adversarial Review\nTarget:[^\n]*\n"
                         r"Verdict: approve\n\nNo material findings\.\n?", text.strip())
    if clean is None:
        raise RuntimeError("tool_result не содержит approve фактического Codex review")
    if _working_diff_digest(repo) != expected_diff:
        raise RuntimeError("working diff изменился во время независимого review")


def _drive_acceptance(base: str, token: str, repo: Path, runner_digest: str) -> dict:
    session = _api(base, token, "POST", "/api/v1/sessions",
                   {"metadata": {"cwd": str(repo)}})
    session_id = session.get("id") if isinstance(session, dict) else None
    if not session_id:
        raise ApiSchemaError("session response не содержит id")
    seen = set()
    steps = {}
    turns = {}
    runner = repo / ".githooks/gates-run"

    def run(name, prompt, timeout=900):
        if hashlib.sha256(runner.read_bytes()).hexdigest() != runner_digest:
            raise RuntimeError("gates-run изменился до prompt " + name)
        turn, messages = _run_prompt(base, token, session_id, prompt, seen, timeout)
        if hashlib.sha256(runner.read_bytes()).hexdigest() != runner_digest:
            raise RuntimeError("gates-run изменился во время prompt " + name)
        turns[name] = turn
        steps[name] = messages
        return messages

    blocked = run("blocked-edit", "Используй инструмент Write ровно для одной операции: создай "
                  "app/blocked.py с содержимым `blocked = True`. Не используй Bash и не создавай "
                  "другие файлы. Если hook отклонит операцию, заверши работу без повторных попыток.")
    _require_tool(blocked, "Write", "app/blocked.py", error=True)
    if (repo / "app/blocked.py").exists():
        raise RuntimeError("blocked Edit создал файл вопреки hook")

    docs = run("docs-edit", "Используй Write, чтобы создать docs/ok.md с содержимым `ok`. "
               "Не используй Bash и не меняй другие файлы.")
    _require_tool(docs, "Write", "docs/ok.md", error=False)

    design_diff = _working_diff_digest(repo)
    design_prompt = ("/gates:design-review Пройди ревью docs/design.md по инструкции команды, "
                     "поставь file-marker через nonce-handoff и ничего не меняй в коде. "
                     "Независимый review запусти РОВНО этой командой: "
                     + _reviewer_invocation(runner_digest, design=True))
    design = run("design-review", design_prompt, 1200)
    _require_command(base, token, session_id, "/gates:design-review", turns["design-review"])
    _, design_result = _require_tool(design, "Bash", "companion-review", error=False,
                                    turn=turns["design-review"], design=True,
                                    runner_digest=runner_digest)
    _require_reviewer_result(design_result, repo, design_diff)
    if "--no-receipt" in json.dumps(design, ensure_ascii=False):
        raise RuntimeError("design acceptance не разрешает --no-receipt")
    _require_tool(design, "Bash", "GATES_SESSION_NONCE=", error=False)
    _require_tool(design, "Bash", "write-marker", error=False)
    markers = list((repo / ".claude").glob(".design-approved-*"))
    valid_markers = []
    for path in markers:
        record = json.loads(path.read_text())
        if record.get("session") == session_id:
            valid_markers.append(path)
    if not valid_markers:
        raise RuntimeError("design marker не привязан к реальной Kimi-сессии")

    approved = run("approved-edit", "Используй Write, чтобы создать app/approved.py с "
                   "содержимым `approved = True`. Не используй Bash и не меняй другие файлы.")
    _require_tool(approved, "Write", "app/approved.py", error=False)

    for pass_name, command in (("simplify", "simplify"), ("code-review", "code-review"),
                               ("security", "security-review")):
        prompt = (f"/gates:{command} Сначала запиши ladder begin {pass_name} через nonce-handoff. "
                  f"Затем выполни проход по инструкции команды для текущего diff, обязательно "
                  f"проверь его через git diff --check и запиши ladder mark {pass_name} через "
                  "nonce-handoff. Не используй LADDER_SKIP.")
        if pass_name == "code-review":
            prompt += (" Независимый review запусти РОВНО этой командой: "
                       + _reviewer_invocation(runner_digest))
        review_diff = _working_diff_digest(repo) if pass_name == "code-review" else None
        passed = run(pass_name, prompt, 1800)
        _require_command(base, token, session_id, f"/gates:{command}", turns[pass_name])
        _require_tool(passed, "Bash", f"begin {pass_name}", error=False)
        _require_tool(passed, "Bash", f"mark {pass_name}", error=False)
        if pass_name == "code-review":
            _, review_result = _require_tool(passed, "Bash", "companion-review", error=False,
                                             turn=turns[pass_name], reviewer=True,
                                             runner_digest=runner_digest)
            _require_reviewer_result(review_result, repo, review_diff)
        else:
            _require_tool(passed, "Bash", "git diff --check", error=False)

    committed = run("commit", "Сделай git commit текущего изменения после уже пройденной "
                    "лесенки. Не используй LADDER_SKIP и ничего не меняй в файлах.")
    _require_tool(committed, "Bash", "git commit", error=False)
    if "LADDER_SKIP" in json.dumps(steps):
        raise RuntimeError("acceptance использовал запрещённый LADDER_SKIP")
    if not (repo / "docs/ok.md").is_file() or not (repo / "app/approved.py").is_file():
        raise RuntimeError("Edit acceptance artifacts не совпали")
    head = _run(["git", "rev-parse", "HEAD"], repo).stdout.strip()
    if not list((repo / "logs/ladder_ledger").glob(f"{head}*")):
        raise RuntimeError("ladder ledger для acceptance commit не найден")
    audit = (repo / "logs/codex_review_audit.log").read_text()
    if f"SESSION-START session={session_id}" not in audit:
        raise RuntimeError("SessionStart для acceptance session не подтверждён audit")
    return {"session_id": session_id, "steps": steps}


def _kimi_bin() -> str:
    for candidate in ("/opt/homebrew/bin/kimi", "/usr/local/bin/kimi", "/usr/bin/kimi"):
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    raise RuntimeError("kimi CLI не найден в trusted system paths")


_SUPERVISOR = r'''import os, select, signal, subprocess, sys
lifeline = int(sys.argv[1])
ready, _, _ = select.select([lifeline], [], [], 0)
if ready and os.read(lifeline, 1) == b"":
    sys.exit(0)
proc = subprocess.Popen(sys.argv[2:], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                        text=True, bufsize=1)
signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
try:
    while proc.poll() is None:
        ready, _, _ = select.select([proc.stdout, lifeline], [], [], 0.2)
        if lifeline in ready and os.read(lifeline, 1) == b"":
            break
        if proc.stdout not in ready:
            continue
        line = proc.stdout.readline()
        if not line:
            break
        sys.stdout.write(line)
        sys.stdout.flush()
finally:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
'''


def _start_server(root: Path, port=58690):
    lifeline, writer = os.pipe()
    proc = subprocess.Popen(["/usr/bin/python3", "-I", "-B", "-c", _SUPERVISOR, str(lifeline),
                             _kimi_bin(), "web", "--no-open", "--port", str(port)], cwd=root,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                            pass_fds=(lifeline,))
    os.close(lifeline)
    proc._gates_lifeline = writer
    token = None
    base = None
    deadline = time.time() + 30
    while (token is None or base is None) and time.time() < deadline:
        line = proc.stdout.readline()
        if not line and proc.poll() is not None:
            raise RuntimeError("kimi web завершился до выдачи bearer token")
        match = re.search(r"token[=:\s]+([A-Za-z0-9._-]+)", line, re.IGNORECASE)
        if match:
            token = match.group(1)
        address = re.search(r"http://127\.0\.0\.1:(\d+)", line)
        if address:
            base = f"http://127.0.0.1:{address.group(1)}"
    if not token or base is None:
        proc.terminate()
        raise RuntimeError("kimi web не выдал адрес и bearer token за 30 секунд")
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(base + "/api/v1/healthz", timeout=1):
                return proc, base, token
        except OSError:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError("kimi web healthz не стал доступен за 30 секунд")


def _scratch_repo(base: Path) -> Path:
    repo = base / "repo"
    (repo / "app").mkdir(parents=True)
    (repo / "docs").mkdir()
    (repo / ".codex-gate.yaml").write_text('{"code_paths":{"prefixes":["app/"]}}\n')
    (repo / ".gitignore").write_text(".claude/\nlogs/\n")
    (repo / "docs/design.md").write_text(
        "# Kimi acceptance\n\n## Решения и альтернативы\n\n"
        "Оставить как есть не проверяет hook. Выбран реальный Edit через плагин.\n\n"
        "## Сценарии и приёмочные критерии (BSAC)\n\n"
        "- S1: без маркера код заблокирован, docs разрешён, после маркера код разрешён.\n")
    _run(["git", "init", "-b", "main"], repo).check_returncode()
    _run(["git", "config", "user.name", "Kimi Acceptance"], repo).check_returncode()
    _run(["git", "config", "user.email", "kimi-acceptance@example.invalid"], repo).check_returncode()
    _run(["git", "add", "-A"], repo).check_returncode()
    _run(["git", "commit", "-m", "chore: acceptance fixture"], repo).check_returncode()
    return repo


def _install_reviewed_runner(repo: Path, plugin: Path) -> str:
    hooks = repo / ".githooks"
    hooks.mkdir()
    for name in ("gates-run", "pre-commit", "post-commit", "pre-push"):
        target = hooks / name
        shutil.copy2(plugin / "templates/githooks" / name, target)
        target.chmod(0o755)
    _run(["git", "add", ".githooks"], repo).check_returncode()
    _run(["git", "commit", "-m", "chore: install reviewed gates runner"], repo).check_returncode()
    _run(["git", "config", "core.hooksPath", ".githooks"], repo).check_returncode()
    result = _run(["/bin/sh", str(hooks / "gates-run"), "ladder_gate.py", "check-precommit"], repo)
    if result.returncode:
        raise RuntimeError("reviewed runner/bootstrap не запустился: " + result.stderr[-1000:])
    return hashlib.sha256((hooks / "gates-run").read_bytes()).hexdigest()


def _require_global_hooks(home: Path, plugin: Path) -> None:
    manifest = json.loads((plugin / ".kimi-plugin/plugin.json").read_text())
    try:
        config = tomllib.loads((home / "config.toml").read_text())
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError("global Kimi config.toml невалиден") from exc
    actual = {(hook.get("event"), hook.get("matcher", ""), hook.get("command"), hook.get("timeout"))
              for hook in config.get("hooks", []) if isinstance(hook, dict)}
    for hook in manifest["hooks"]:
        expected = (hook["event"], hook.get("matcher", ""), hook["command"], hook.get("timeout"))
        if expected not in actual:
            raise RuntimeError("global Kimi hook registration отсутствует или отличается")


def live() -> None:
    if os.environ.get("PYTEST_CURRENT_TEST"):
        raise RuntimeError("live acceptance запрещён под pytest")
    root = Path(__file__).resolve().parent.parent
    proc = None
    journal = None
    journal_path = None
    with tempfile.TemporaryDirectory(prefix="kimi-acceptance-") as tmp:
        try:
            proc, base, token = _start_server(root)
            require_plugin_api(_api(base, token, "GET", "/openapi.json"))
            recover(base=base, token=token)
            artifact, head, digest = build_artifact(root)
            print(f"Reviewed HEAD={head} artifact sha256={digest}")
            payload = Path(tmp) / "payload"
            payload.mkdir()
            artifact_bytes = artifact.read_bytes()
            if hashlib.sha256(artifact_bytes).hexdigest() != digest:
                raise RuntimeError("artifact digest изменился после check-artifact")
            with tarfile.open(fileobj=io.BytesIO(artifact_bytes)) as tf:
                tf.extractall(payload, filter="data")
            plugin = payload / "plugins/gates"
            artifact_delivery_digest = directory_digest(plugin)
            expected_version = json.loads(
                (plugin / ".kimi-plugin/plugin.json").read_text())["version"]
            repo = _scratch_repo(Path(tmp))
            runner_digest = _install_reviewed_runner(repo, plugin)
            home = Path(pwd.getpwuid(os.getuid()).pw_dir) / ".kimi-code"
            journal = snapshot(home)
            journal["phase"] = "snapshot"
            journal_path = save_journal(journal)
            journal["phase"] = "installing"
            save_journal(journal, journal_path)
            _api(base, token, "POST", "/api/v1/plugins", {"source": str(plugin)})
            plugins = _api(base, token, "GET", "/api/v1/plugins")
            gates = next((p for p in plugins if p.get("id") == "gates"), None)
            if not gates or not gates.get("enabled") or gates.get("state") != "ok":
                raise RuntimeError(f"Kimi plugin не активен: {gates}")
            if gates.get("commandCount") != 5 or gates.get("hookCount") != 3:
                raise RuntimeError(f"Kimi plugin commands/hooks не зарегистрированы: {gates}")
            _require_global_hooks(home, plugin)
            candidate = snapshot(home)
            if candidate["gates"] is None:
                raise RuntimeError("Kimi plugin активен в API, но отсутствует в installed.json")
            journal.update(phase="candidate", expected_current=candidate["gates"],
                           expected_managed_digest=candidate["managed_digest"])
            save_journal(journal, journal_path)
            if candidate["managed_digest"] != artifact_delivery_digest:
                raise RuntimeError("managed/gates не совпал с reviewed artifact digest")
            report = _drive_acceptance(base, token, repo, runner_digest)
            final = snapshot(home)
            final_version = json.loads(
                (home / "plugins/managed/gates/.kimi-plugin/plugin.json").read_text())["version"]
            if final["gates"] != candidate["gates"] \
                    or final["managed_digest"] != artifact_delivery_digest \
                    or final_version != expected_version:
                raise RuntimeError("candidate изменился после smoke — отчёт и journal не завершаются")
            version = _run([_kimi_bin(), "--version"], root).stdout.strip()
            report_path = root / "logs/kimi_acceptance" / f"{head}.json"
            _atomic_json(report_path, {"reviewed_head": head, "artifact_sha256": digest,
                                       "delivery_digest": artifact_delivery_digest,
                                       "kimi_version": version, **report})
            journal_path.unlink()
            journal = None
            print(f"Kimi acceptance passed; candidate 0.11.0 оставлен активным; report={report_path}")
        finally:
            try:
                if journal is not None:
                    recover(journal_path, base=base, token=token)
            finally:
                if proc is not None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        proc.kill()


def main() -> int:
    try:
        live()
        return 0
    except (OSError, ValueError, RuntimeError, RestoreConflict, ApiSchemaError,
            subprocess.CalledProcessError) as exc:
        print(f"[kimi-acceptance] ✗ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
