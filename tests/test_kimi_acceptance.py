"""Artifact-bound Kimi acceptance helpers: API schema и crash-safe rollback (S16)."""
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import kimi_acceptance as ka  # noqa: E402


def _registry(home, gates=True):
    plugins = [{"id": "unrelated", "root": "/unrelated", "enabled": True}]
    if gates:
        plugins.append({"id": "gates", "root": "/old/gates", "enabled": False,
                        "source": "local-path", "originalSource": "/old"})
    path = home / "plugins/installed.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"version": 1, "plugins": plugins}, indent=2))
    return path


def _fake_api(home):
    registry = home / "plugins/installed.json"
    managed = home / "plugins/managed/gates"

    def api(_base, _token, method, path, body=None):
        data = json.loads(registry.read_text()) if registry.exists() else {"version": 1, "plugins": []}
        records = data["plugins"]
        if method == "GET" and path == "/api/v1/plugins":
            return records
        if method == "POST" and path == "/api/v1/plugins/gates:remove":
            data["plugins"] = [p for p in records if p.get("id") != "gates"]
            registry.write_text(json.dumps(data))
            return None
        if method == "POST" and path == "/api/v1/plugins":
            if managed.exists():
                ka.shutil.rmtree(managed)
            ka.shutil.copytree(body["source"], managed)
            record = {"id": "gates", "root": str(managed), "enabled": True, "state": "ok",
                      "source": "local-path", "originalSource": body["source"],
                      "commandCount": 5, "hookCount": 3}
            data["plugins"] = [p for p in records if p.get("id") != "gates"] + [record]
            registry.write_text(json.dumps(data))
            return record
        if method == "POST" and path in ("/api/v1/plugins/gates:enable",
                                         "/api/v1/plugins/gates:disable"):
            current = next(p for p in records if p.get("id") == "gates")
            current["enabled"] = path.endswith(":enable")
            registry.write_text(json.dumps(data))
            return current
        raise AssertionError((method, path, body))

    return api


def test_snapshot_restore_returns_exact_previous_state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    registry = _registry(home)
    managed = home / "plugins/managed/gates"
    managed.mkdir(parents=True)
    (managed / "old.txt").write_text("old")
    (managed / "tool.sh").write_text("#!/bin/sh\n")
    (managed / "tool.sh").chmod(0o755)
    (managed / "empty").mkdir()
    before_managed = ka.directory_digest(managed)

    journal = ka.snapshot(home)
    ka.shutil.rmtree(managed)
    managed.mkdir(parents=True)
    (managed / "old.txt").write_text("candidate")
    candidate = {"id": "gates", "root": "/candidate", "enabled": True}
    registry.write_text(json.dumps({"version": 1, "plugins": [
        {"id": "unrelated", "root": "/changed-independently", "enabled": True}, candidate]}))
    journal.update(phase="candidate", expected_current=candidate,
                   expected_managed_digest=ka.directory_digest(managed))
    journal_path = ka.save_journal(journal, tmp_path / "state/recovery.json")
    monkeypatch.setattr(ka, "_api", _fake_api(home))
    assert ka.recover(journal_path) is True

    current = json.loads(registry.read_text())
    unrelated = next(p for p in current["plugins"] if p["id"] == "unrelated")
    gates = next(p for p in current["plugins"] if p["id"] == "gates")
    assert unrelated["root"] == "/changed-independently"
    assert gates["enabled"] is False
    assert ka.directory_digest(managed) == before_managed
    assert (managed / "tool.sh").stat().st_mode & 0o111
    assert (managed / "empty").is_dir()


def test_same_digest_candidate_still_restores_registry_via_api(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _registry(home)
    managed = home / "plugins/managed/gates"
    managed.mkdir(parents=True)
    (managed / "old.txt").write_text("same")
    journal = ka.snapshot(home)
    candidate = {"id": "gates", "root": "/candidate-source", "enabled": True}
    (home / "plugins/installed.json").write_text(json.dumps({"version": 1, "plugins": [candidate]}))
    journal.update(phase="candidate", expected_current=candidate,
                   expected_managed_digest=ka.directory_digest(managed))
    journal_path = ka.save_journal(journal, tmp_path / "state/recovery.json")
    calls = []
    api = _fake_api(home)
    monkeypatch.setattr(ka, "_api", lambda *args: calls.append(args) or api(*args))
    assert ka.recover(journal_path) is True
    assert any(args[2] == "POST" and args[3].endswith(":remove") for args in calls)
    assert any(args[2] == "POST" and args[3] == "/api/v1/plugins" for args in calls)
    current = json.loads((home / "plugins/installed.json").read_text())["plugins"]
    assert current[0]["enabled"] is False


def test_absent_plugin_is_removed_from_active_registry_on_restore(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _registry(home, gates=False)
    journal = ka.snapshot(home)
    managed = home / "plugins/managed/gates"
    managed.mkdir(parents=True)
    (managed / "candidate").write_text("new")
    candidate = {"id": "gates", "root": str(managed), "enabled": True}
    (home / "plugins/installed.json").write_text(json.dumps({"version": 1, "plugins": [candidate]}))
    journal.update(phase="candidate", expected_current=candidate,
                   expected_managed_digest=ka.directory_digest(managed))
    journal_path = ka.save_journal(journal, tmp_path / "state/recovery.json")
    monkeypatch.setattr(ka, "_api", _fake_api(home))
    assert ka.recover(journal_path) is True
    assert all(p["id"] != "gates" for p in json.loads(
        (home / "plugins/installed.json").read_text())["plugins"])


def test_recovery_after_sigkill_uses_persistent_journal_in_new_process(tmp_path, monkeypatch):
    home = tmp_path / "home"
    registry = _registry(home)
    managed = home / "plugins/managed/gates"
    managed.mkdir(parents=True)
    (managed / "old.txt").write_text("old")
    journal_path = tmp_path / "state/recovery.json"
    child = r"""
import json, os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
import kimi_acceptance as ka
home = Path(sys.argv[1])
journal_path = Path(sys.argv[2])
journal = ka.snapshot(home)
journal["phase"] = "installing"
ka.save_journal(journal, journal_path)
candidate = {"id": "gates", "root": "/candidate", "enabled": True, "state": "ok"}
(home / "plugins/installed.json").write_text(json.dumps({"version": 1, "plugins": [
    {"id": "unrelated", "enabled": True}, candidate]}))
managed = home / "plugins/managed/gates"
(managed / "old.txt").write_text("candidate")
journal.update(phase="candidate", expected_current=candidate,
               expected_managed_digest=ka.directory_digest(managed))
ka.save_journal(journal, journal_path)
os.kill(os.getpid(), signal.SIGKILL)
"""
    crashed = subprocess.run([sys.executable, "-c", child, str(home), str(journal_path),
                              str(ROOT / "scripts")], capture_output=True, text=True)
    assert crashed.returncode == -9
    assert journal_path.is_file()
    monkeypatch.setattr(ka, "_api", _fake_api(home))
    assert ka.recover(journal_path) is True
    current = json.loads(registry.read_text())["plugins"]
    assert next(p for p in current if p["id"] == "gates")["enabled"] is False
    assert (managed / "old.txt").read_text() == "old"
    assert not journal_path.exists()


def test_recovery_resumes_installing_original_after_api_remove(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _registry(home)
    managed = home / "plugins/managed/gates"
    managed.mkdir(parents=True)
    (managed / "old.txt").write_text("old")
    journal = ka.snapshot(home)
    staged = managed.parent / ".gates-acceptance-original"
    ka._write_tree(staged, journal["managed"], journal["managed_modes"], journal["managed_dirs"])
    (home / "plugins/installed.json").write_text('{"version":1,"plugins":[]}')
    (managed / "old.txt").write_text("candidate")
    journal["phase"] = "installing-original"
    journal_path = ka.save_journal(journal, tmp_path / "state/recovery.json")
    monkeypatch.setattr(ka, "_api", _fake_api(home))
    assert ka.recover(journal_path) is True
    assert (managed / "old.txt").read_text() == "old"
    current = json.loads((home / "plugins/installed.json").read_text())["plugins"]
    assert current[0]["enabled"] is False


def test_recovery_after_sigkill_between_api_remove_and_next_phase(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _registry(home)
    managed = home / "plugins/managed/gates"
    managed.mkdir(parents=True)
    (managed / "old.txt").write_text("old")
    journal = ka.snapshot(home)
    candidate = {"id": "gates", "root": "/candidate", "enabled": True}
    (home / "plugins/installed.json").write_text(json.dumps({"version": 1, "plugins": [candidate]}))
    (managed / "old.txt").write_text("candidate")
    ka._write_tree(managed.parent / ".gates-acceptance-original", journal["managed"],
                   journal["managed_modes"], journal["managed_dirs"])
    journal.update(phase="removing-candidate", expected_current=candidate,
                   expected_managed_digest=ka.directory_digest(managed))
    journal_path = ka.save_journal(journal, tmp_path / "state/recovery.json")
    child = r"""
import json, os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[2])
import kimi_acceptance as ka
home = Path(sys.argv[1])
def api(*args):
    (home / "plugins/installed.json").write_text('{"version":1,"plugins":[]}')
    os.kill(os.getpid(), signal.SIGKILL)
ka._api = api
ka.recover(Path(sys.argv[3]))
"""
    crashed = subprocess.run([sys.executable, "-c", child, str(home), str(ROOT / "scripts"),
                              str(journal_path)], capture_output=True, text=True)
    assert crashed.returncode == -9
    monkeypatch.setattr(ka, "_api", _fake_api(home))
    assert ka.recover(journal_path) is True
    assert (managed / "old.txt").read_text() == "old"


def test_external_managed_change_after_api_restore_is_a_conflict(tmp_path):
    home = tmp_path / "home"
    _registry(home)
    managed = home / "plugins/managed/gates"
    managed.mkdir(parents=True)
    (managed / "old.txt").write_text("old")
    journal = ka.snapshot(home)
    journal["phase"] = "restoring-enabled"
    journal_path = ka.save_journal(journal, tmp_path / "state/recovery.json")
    (managed / "old.txt").write_text("external-after-api-restore")
    with pytest.raises(ka.RestoreConflict, match="после API restore"):
        ka.recover(journal_path)
    assert (managed / "old.txt").read_text() == "external-after-api-restore"
    assert journal_path.is_file()


def test_live_restore_conflict_preserves_external_change_and_journal(tmp_path, monkeypatch):
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    home = tmp_path / "os-home/.kimi-code"
    registry = _registry(home)
    (home / "config.toml").write_text("")
    managed = home / "plugins/managed/gates"
    managed.mkdir(parents=True)
    (managed / "old.txt").write_text("old")
    journal_path = tmp_path / "state/recovery.json"
    monkeypatch.setattr(ka, "_journal_path", lambda: journal_path)
    artifact = tmp_path / "artifact.tar"
    plugin_file = tmp_path / "payload/plugins/gates/.kimi-plugin/plugin.json"
    plugin_file.parent.mkdir(parents=True)
    plugin_file.write_text('{"version":"0.11.0","hooks":[]}')
    with ka.tarfile.open(artifact, "w") as bundle:
        bundle.add(plugin_file, arcname="plugins/gates/.kimi-plugin/plugin.json")
    monkeypatch.setattr(ka.pwd, "getpwuid",
                        lambda _uid: SimpleNamespace(pw_dir=str(home.parent)))
    monkeypatch.setattr(ka, "build_artifact", lambda _root: (
        artifact, "head", ka.hashlib.sha256(artifact.read_bytes()).hexdigest()))
    monkeypatch.setattr(ka, "_start_server", lambda _root: (None, "http://local", "token"))
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(ka, "_scratch_repo", lambda _base: repo)
    monkeypatch.setattr(ka, "_install_reviewed_runner", lambda _repo, _plugin: None)
    candidate = {"id": "gates", "root": str(managed), "enabled": True, "state": "ok",
                 "commandCount": 5, "hookCount": 3}

    def api(_base, _token, method, path, body=None):
        if path == "/openapi.json":
            return {"paths": {
                "/api/v1/plugins": {"get": {}, "post": {}},
                "/api/v1/plugins/{plugin_id}:{action}": {"post": {}},
                "/api/v1/sessions": {"post": {}},
                "/api/v1/sessions/{session_id}": {"get": {}},
                "/api/v1/sessions/{session_id}/prompts": {"get": {}, "post": {}},
                "/api/v1/sessions/{session_id}/messages": {"get": {}},
                "/api/v1/sessions/{session_id}/transcript/user-messages": {"get": {}},
                "/openapi.json": {"get": {}},
            }}
        if method == "POST" and path == "/api/v1/plugins":
            if managed.exists():
                ka.shutil.rmtree(managed)
            ka.shutil.copytree(body["source"], managed)
            registry.write_text(json.dumps({"version": 1, "plugins": [candidate]}))
            return candidate
        if method == "GET" and path == "/api/v1/plugins":
            return [candidate]
        raise AssertionError((method, path, body))

    def external_change(_base, _token, _repo, _digest):
        external = {**candidate, "source": "external-update"}
        registry.write_text(json.dumps({"version": 1, "plugins": [external]}))
        (managed / "external.txt").write_text("keep")
        raise RuntimeError("stop acceptance")

    monkeypatch.setattr(ka, "_api", api)
    monkeypatch.setattr(ka, "_drive_acceptance", external_change)
    with pytest.raises(ka.RestoreConflict, match="изменилась вне acceptance"):
        ka.live()
    assert (managed / "external.txt").read_text() == "keep"
    assert journal_path.is_file()


def test_unexpected_gates_change_is_a_restore_conflict(tmp_path):
    home = tmp_path / "home"
    registry = _registry(home)
    managed = home / "plugins/managed/gates"
    managed.mkdir(parents=True)
    (managed / "old.txt").write_text("old")
    journal = ka.snapshot(home)
    journal.update(phase="candidate", expected_current=journal["gates"],
                   expected_managed_digest=ka.directory_digest(managed))
    journal_path = ka.save_journal(journal, tmp_path / "state/recovery.json")
    registry.write_text(json.dumps({"version": 1, "plugins": [
        {"id": "gates", "root": "/external-change", "enabled": True},
    ]}))
    with pytest.raises(ka.RestoreConflict):
        ka.recover(journal_path)


def test_snapshot_refuses_managed_symlink(tmp_path):
    home = tmp_path / "home"
    _registry(home)
    managed = home / "plugins/managed/gates"
    managed.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.write_text("not readable as plugin state")
    (managed / "linked.py").symlink_to(outside)
    with pytest.raises(ka.RestoreConflict, match="symlink"):
        ka.snapshot(home)


def test_global_registration_is_required_and_checked_against_manifest(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    skill = (ROOT / "plugins/gates/skills/gates-init/SKILL.md").read_text()
    config = skill.split("```toml", 1)[1].split("```", 1)[0]
    (home / "config.toml").write_text(config)
    ka._require_global_hooks(home, ROOT / "plugins/gates")
    (home / "config.toml").write_text("[[hooks]]\nevent=\"PreToolUse\"\n")
    with pytest.raises(RuntimeError, match="global Kimi hook"):
        ka._require_global_hooks(home, ROOT / "plugins/gates")


def test_openapi_plugin_routes_are_required():
    good = {"paths": {
        "/api/v1/plugins": {"get": {}, "post": {}},
        "/api/v1/plugins/{plugin_id}:{action}": {"post": {}},
        "/api/v1/sessions": {"post": {}},
        "/api/v1/sessions/{session_id}": {"get": {}},
        "/api/v1/sessions/{session_id}/prompts": {"get": {}, "post": {}},
        "/api/v1/sessions/{session_id}/messages": {"get": {}},
        "/api/v1/sessions/{session_id}/transcript/user-messages": {"get": {}},
        "/openapi.json": {"get": {}},
    }}
    ka.require_plugin_api(good)
    with pytest.raises(ka.ApiSchemaError):
        ka.require_plugin_api({"paths": {"/api/v1/plugins": {"get": {}}}})


def test_sigkill_before_candidate_identity_is_a_conflict_not_unguarded_restore(tmp_path):
    home = tmp_path / "home"
    registry = _registry(home)
    managed = home / "plugins/managed/gates"
    managed.mkdir(parents=True)
    (managed / "old.txt").write_text("old")
    journal_path = tmp_path / "state/recovery.json"
    child = r"""
import json, os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
import kimi_acceptance as ka
home = Path(sys.argv[1])
journal = ka.snapshot(home)
journal["phase"] = "installing"
ka.save_journal(journal, Path(sys.argv[2]))
(home / "plugins/installed.json").write_text(json.dumps({"version": 1, "plugins": [
    {"id": "gates", "root": "/candidate", "enabled": True}]}))
(home / "plugins/managed/gates/old.txt").write_text("candidate")
os.kill(os.getpid(), signal.SIGKILL)
"""
    crashed = subprocess.run([sys.executable, "-c", child, str(home), str(journal_path),
                              str(ROOT / "scripts")], capture_output=True, text=True)
    assert crashed.returncode == -9
    with pytest.raises(ka.RestoreConflict, match="identity candidate"):
        ka.recover(journal_path)
    assert (managed / "old.txt").read_text() == "candidate"
    assert json.loads(registry.read_text())["plugins"][0]["root"] == "/candidate"
    assert journal_path.is_file()


def test_sigkill_managed_only_mutation_before_identity_is_a_conflict(tmp_path):
    home = tmp_path / "home"
    _registry(home)
    managed = home / "plugins/managed/gates"
    managed.mkdir(parents=True)
    (managed / "old.txt").write_text("old")
    journal_path = tmp_path / "state/recovery.json"
    child = r"""
import os, signal, sys
from pathlib import Path
sys.path.insert(0, sys.argv[3])
import kimi_acceptance as ka
home = Path(sys.argv[1])
journal = ka.snapshot(home)
journal["phase"] = "installing"
ka.save_journal(journal, Path(sys.argv[2]))
(home / "plugins/managed/gates/old.txt").write_text("partial")
os.kill(os.getpid(), signal.SIGKILL)
"""
    crashed = subprocess.run([sys.executable, "-c", child, str(home), str(journal_path),
                              str(ROOT / "scripts")], capture_output=True, text=True)
    assert crashed.returncode == -9
    with pytest.raises(ka.RestoreConflict, match="identity candidate"):
        ka.recover(journal_path)
    assert (managed / "old.txt").read_text() == "partial"


def test_managed_tree_change_without_registry_change_blocks_recovery(tmp_path):
    home = tmp_path / "home"
    _registry(home)
    managed = home / "plugins/managed/gates"
    managed.mkdir(parents=True)
    (managed / "old.txt").write_text("old")
    journal = ka.snapshot(home)
    candidate = {"id": "gates", "root": "/candidate", "enabled": True}
    journal.update(phase="candidate", expected_current=candidate,
                   expected_managed_digest=ka.directory_digest(managed))
    journal_path = ka.save_journal(journal, tmp_path / "state/recovery.json")
    (home / "plugins/installed.json").write_text(json.dumps({"version": 1, "plugins": [candidate]}))
    (managed / "old.txt").write_text("external")
    with pytest.raises(ka.RestoreConflict, match="managed/gates изменился"):
        ka.recover(journal_path)
    assert (managed / "old.txt").read_text() == "external"


def test_tool_evidence_requires_matching_call_result_and_status():
    messages = [
        {"role": "assistant", "content": [
            {"type": "tool_use", "id": "call1", "name": "Write",
             "input": {"file_path": "app/blocked.py"}}]},
        {"role": "tool", "content": [
            {"type": "tool_result", "tool_use_id": "call1", "is_error": True}]},
    ]
    ka._require_tool(messages, "Write", "app/blocked.py", error=True)
    with pytest.raises(RuntimeError, match="error=False"):
        ka._require_tool(messages, "Write", "app/blocked.py", error=False)
    with pytest.raises(RuntimeError, match="transcript"):
        ka._require_tool(messages, "Bash", "write-marker", error=False)
    messages[1]["content"] = []
    with pytest.raises(RuntimeError, match="tool_result"):
        ka._require_tool(messages, "Write", "app/blocked.py", error=True)


def test_reviewer_evidence_requires_canonical_command_and_current_turn():
    def messages(command, turn="turn1"):
        return [
            {"role": "assistant", "prompt_id": turn, "content": [
                {"type": "tool_use", "id": "review1", "name": "Bash",
                 "input": {"command": command}}]},
            {"role": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "review1", "is_error": False}]},
        ]

    canonical = ka._reviewer_invocation("runner-digest")
    ka._require_tool(messages(canonical), "Bash", "companion-review", error=False,
                     turn="turn1", reviewer=True, runner_digest="runner-digest")
    for invalid in ("printf companion-review", canonical + " || true", canonical + " & true",
                    canonical + " x||true", canonical + " x&true", canonical + " x;true",
                    canonical + " x>out", canonical + " ${IFS}true",
                    canonical + " --scope branch --base HEAD~1",
                    canonical.replace(ka.REVIEW_FOCUS, "--scope=branch"),
                    canonical.replace("--scope working-tree", "--design-file docs/design.md")):
        with pytest.raises(RuntimeError, match="transcript"):
            ka._require_tool(messages(invalid), "Bash", "companion-review", error=False,
                             turn="turn1", reviewer=True)
    with pytest.raises(RuntimeError, match="transcript"):
        ka._require_tool(messages(canonical, turn="other"), "Bash", "companion-review",
                         error=False, turn="turn1", reviewer=True)


def test_reviewer_invocation_executes_verified_runner_bytes(tmp_path):
    runner = tmp_path / ".githooks/gates-run"
    runner.parent.mkdir(parents=True)
    runner.write_text("printf '# Codex Adversarial Review\\nTarget: working tree diff\\n"
                      "Verdict: approve\\n\\nNo material findings.\\n'\n")
    digest = ka.hashlib.sha256(runner.read_bytes()).hexdigest()
    result = subprocess.run(ka._reviewer_invocation(digest), shell=True, cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert "Verdict: approve" in result.stdout


def test_second_canonical_review_in_same_turn_is_rejected():
    def messages(command, call_id):
        return [
            {"role": "assistant", "prompt_id": "turn1", "content": [
                {"type": "tool_use", "id": call_id, "name": "Bash",
                 "input": {"command": command}}]},
            {"role": "tool", "content": [
                {"type": "tool_result", "tool_use_id": call_id, "is_error": False}]},
        ]

    canonical = ka._reviewer_invocation("runner-digest")
    with pytest.raises(RuntimeError, match="2 canonical"):
        ka._require_tool(messages(canonical, "review1") + messages(canonical, "review2"),
                         "Bash", "companion-review",
                         error=False, turn="turn1", reviewer=True, runner_digest="runner-digest")


def test_design_review_requires_design_file_command_not_code_review():
    def messages(command):
        return [
            {"role": "assistant", "prompt_id": "turn1", "content": [
                {"type": "tool_use", "id": "design1", "name": "Bash",
                 "input": {"command": command}}]},
            {"role": "tool", "content": [
                {"type": "tool_result", "tool_use_id": "design1", "is_error": False}]},
        ]

    canonical = ka._reviewer_invocation("runner-digest", design=True)
    ka._require_tool(messages(canonical), "Bash", "companion-review", error=False,
                     turn="turn1", design=True, runner_digest="runner-digest")
    for invalid in (canonical.replace("--design-file docs/design.md", "--scope working-tree"),
                    canonical + " --no-receipt"):
        with pytest.raises(RuntimeError, match="transcript"):
            ka._require_tool(messages(invalid), "Bash", "companion-review", error=False,
                             turn="turn1", design=True)


def test_reviewer_result_must_be_actual_approve_with_unchanged_diff(tmp_path, monkeypatch):
    monkeypatch.setattr(ka, "_working_diff_digest", lambda _repo: "current")
    result = {"content": "# Codex Adversarial Review\nTarget: working tree diff\n"
                         "Verdict: approve\n\nNo material findings."}
    ka._require_reviewer_result(result, tmp_path, "current")
    for invalid in ({"content": "review-round artifact=range:forged"},
                    {"content": "# Codex Adversarial Review\nVerdict: approve\n- [high] do not ship"},
                    {"content": "# Codex Adversarial Review\nVerdict: needs-attention\n"
                                "Verdict: approve — это цитата из finding"}):
        with pytest.raises(RuntimeError, match="approve"):
            ka._require_reviewer_result(invalid, tmp_path, "current")
    with pytest.raises(RuntimeError, match="изменился"):
        ka._require_reviewer_result(result, tmp_path, "other")


def test_prompt_driver_uses_real_queue_and_message_transcript(monkeypatch):
    monkeypatch.setattr(ka.time, "sleep", lambda _seconds: None)

    def api(_base, _token, method, path, body=None):
        if method == "POST" and path.endswith("/prompts"):
            return {"prompt_id": "turn1", "user_message_id": "user1", "status": "running"}
        if path.endswith("/messages?page_size=100"):
            return {"items": [{"id": "assistant1", "role": "assistant", "content": []}]}
        if path.endswith("/prompts"):
            return {"active": None, "queued": []}
        if path.endswith("/session1"):
            return {"last_turn_reason": "completed"}
        raise AssertionError((method, path, body))

    monkeypatch.setattr(ka, "_api", api)
    seen = set()
    turn, messages = ka._run_prompt("base", "token", "session1", "prompt", seen)
    assert turn == "turn1"
    assert messages[0]["id"] == "assistant1"
    assert seen == {"assistant1"}


def test_plugin_command_requires_exact_origin_and_current_turn(monkeypatch):
    record = {"agents": [{"messages": [{"turn_id": "turn1", "origin": "plugin-command",
                                        "prompt": "/gates:code-review"}]}]}
    monkeypatch.setattr(ka, "_api", lambda *args: record)
    ka._require_command("base", "token", "session1", "/gates:code-review", "turn1")
    record["agents"][0]["messages"][0]["origin"] = "not-plugin-command"
    with pytest.raises(RuntimeError, match="turn turn1"):
        ka._require_command("base", "token", "session1", "/gates:code-review", "turn1")
    record["agents"][0]["messages"][0]["origin"] = "plugin-command"
    with pytest.raises(RuntimeError, match="turn turn2"):
        ka._require_command("base", "token", "session1", "/gates:code-review", "turn2")


def test_reviewed_runner_is_installed_and_probed_without_onboarding_skip(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    plugin = tmp_path / "plugin"
    names = ("gates-run", "pre-commit", "post-commit", "pre-push")
    for name in names:
        path = plugin / "templates/githooks" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("#!/bin/sh\n")
    calls = []
    monkeypatch.setattr(ka, "_run", lambda argv, cwd: calls.append((argv, cwd)) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    digest = ka._install_reviewed_runner(repo, plugin)
    assert digest == ka.hashlib.sha256((repo / ".githooks/gates-run").read_bytes()).hexdigest()
    assert all((repo / ".githooks" / name).is_file() for name in names)
    assert calls[-1][0] == ["/bin/sh", str(repo / ".githooks/gates-run"),
                            "ladder_gate.py", "check-precommit"]
    assert all("LADDER_SKIP" not in arg for argv, _cwd in calls for arg in argv)


def test_runner_mutation_during_prompt_is_detected(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    runner = repo / ".githooks/gates-run"
    runner.parent.mkdir(parents=True)
    runner.write_text("reviewed")
    digest = ka.hashlib.sha256(runner.read_bytes()).hexdigest()
    monkeypatch.setattr(ka, "_api", lambda *args: {"id": "session1"})

    def mutate(*_args):
        runner.write_text("substituted")
        return "turn1", []

    monkeypatch.setattr(ka, "_run_prompt", mutate)
    with pytest.raises(RuntimeError, match="во время prompt"):
        ka._drive_acceptance("base", "token", repo, digest)


def test_server_supervisor_closes_child_after_early_runner_sigkill(tmp_path, monkeypatch):
    import socket
    import time
    fake = tmp_path / "fake_kimi.py"
    fake.write_text(r'''#!/usr/bin/python3
import http.server, sys, threading, time
port = int(sys.argv[sys.argv.index("--port") + 1])
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")
    def log_message(self, *args):
        pass
server = http.server.HTTPServer(("127.0.0.1", port), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
print(f"Local: http://127.0.0.1:{port}/#token=fake", flush=True)
print("Token: fake", flush=True)
while True:
    time.sleep(1)
''')
    fake.chmod(0o755)
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    child = r'''
import sys, time
sys.path.insert(0, sys.argv[1])
import kimi_acceptance as ka
ka._kimi_bin = lambda: sys.argv[2]
proc, base, token = ka._start_server(ka.Path(sys.argv[3]), int(sys.argv[4]))
print("READY", base, token, flush=True)
time.sleep(60)
'''
    runner = subprocess.Popen([sys.executable, "-c", child, str(ROOT / "scripts"), str(fake),
                               str(tmp_path), str(port)], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True)
    runner.kill()
    runner.wait()
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            ka.urllib.request.urlopen(f"http://127.0.0.1:{port}/api/v1/healthz", timeout=0.2)
        except OSError:
            break
        time.sleep(0.1)
    else:
        raise AssertionError("fake kimi web остался сиротой после SIGKILL runner")
    monkeypatch.setattr(ka, "_kimi_bin", lambda: str(fake))
    proc, base, token = ka._start_server(tmp_path, port)
    assert base == f"http://127.0.0.1:{port}" and token == "fake"
    proc.terminate()
    proc.wait(timeout=10)
