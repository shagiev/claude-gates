"""Kimi host: manifest/commands, hook parity и одноразовый session handoff (S1–S7, R8)."""
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import codex_review_gate as g
import native_hook as nh

ROOT = Path(__file__).resolve().parent.parent
NONCE = "ab" * 16


def _command(nonce=NONCE):
    return (f"GATES_SESSION_NONCE={nonce} /bin/sh .githooks/gates-run "
            "codex_review_gate.py write-marker trivial 'kimi smoke'")


def _bash_hook(session="kimi-s1", nonce=NONCE):
    return json.dumps({
        "hook_event_name": "PreToolUse",
        "session_id": session,
        "cwd": str(g.REPO_ROOT),
        "tool_name": "Bash",
        "tool_input": {"command": _command(nonce)},
    })


def test_kimi_manifest_exposes_workflow_and_native_hooks():
    # EARS-8/9/11: пакет содержит реальные команды, а не ссылки на Claude builtins.
    manifest = json.loads((ROOT / "plugins/gates/.kimi-plugin/plugin.json").read_text())
    assert manifest["name"] == "gates"
    assert manifest["skills"] == "./skills/"
    assert manifest["commands"] == "./commands/"
    hooks = manifest["hooks"]
    assert {(h["event"], h["matcher"]) for h in hooks} == {
        ("PreToolUse", "Bash"),
        ("PreToolUse", next(h["matcher"] for h in hooks if h["event"] == "PreToolUse"
                            and h["matcher"] != "Bash")),
        ("SessionStart", ""),
    }
    for h in hooks:
        assert h["command"].startswith("/usr/bin/python3 -I -B -c ")
        assert "-I" in h["command"] and "-B" in h["command"]
        assert "/.local/lib/claude-gates/native_hook.py" in h["command"]
        assert "${HOME}" not in h["command"] and "KIMI_PLUGIN_ROOT" not in h["command"]
    for name in ("design-review", "simplify", "security-review", "code-review", "gates-init"):
        assert (ROOT / f"plugins/gates/commands/{name}.md").is_file()


def test_global_and_native_hook_commands_are_identical_for_kimi_dedup():
    manifest = json.loads((ROOT / "plugins/gates/.kimi-plugin/plugin.json").read_text())
    init = (ROOT / "plugins/gates/skills/gates-init/SKILL.md").read_text()
    init = init.replace('\\"', '"').replace("\\n", "\n")
    for hook in manifest["hooks"]:
        assert init.count(f'command = "{hook["command"]}"') == 1
    launcher = (ROOT / "plugins/gates/scripts/native_hook.py").read_text()
    assert "KIMI_PLUGIN_ROOT" not in launcher
    assert 'namespace["resolve_kimi"]' in launcher
    assert "ЗАМЕНИ" in init and "claude-gates/launch.sh" in init


def test_manifest_command_ignores_hostile_home_outside_onboarding(tmp_path):
    manifest = json.loads((ROOT / "plugins/gates/.kimi-plugin/plugin.json").read_text())
    command = manifest["hooks"][0]["command"]
    env = {**g.os.environ, "HOME": "/nonexistent/home", "KIMI_CODE_HOME": "/nonexistent/kimi"}
    payload = json.dumps({"cwd": str(tmp_path)})
    result = g.subprocess.run(command, shell=True, cwd=tmp_path, env=env,
                              input=payload, text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_native_dispatcher_is_opt_in_before_installation_resolution(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".local/lib/claude-gates").mkdir(parents=True)
    payload = json.dumps({"cwd": str(tmp_path)})
    monkeypatch.setattr(nh, "_home", lambda: home)
    monkeypatch.setattr(nh, "_onboarded", lambda _payload: False)
    monkeypatch.setattr(nh.sys, "stdin", g.io.StringIO(payload))
    assert nh.main(["codex_review_gate.py", "gate-edit"]) == 0
    monkeypatch.setattr(nh, "_onboarded", lambda _payload: True)
    (home / ".local/lib/claude-gates/kimi_install.py").write_text("# invalid\n")
    monkeypatch.setattr(nh.sys, "stdin", g.io.StringIO(payload))
    assert nh.main(["codex_review_gate.py", "gate-edit"]) == 2


def test_native_git_metadata_errors_are_not_an_opt_out(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / ".codex-gate.yaml").write_text("code_paths: {}\n")
    monkeypatch.setattr(nh, "_git_binary", lambda: "/usr/bin/false")
    with pytest.raises(RuntimeError, match="rev-parse"):
        nh._onboarded({"cwd": str(tmp_path)})

    def fake_run(argv, **kwargs):
        if "rev-parse" in argv and "--verify" not in argv:
            return g.subprocess.CompletedProcess(argv, 0, str(tmp_path), "")
        if "--verify" in argv:
            return g.subprocess.CompletedProcess(argv, 0, "a" * 40, "")
        return g.subprocess.CompletedProcess(argv, 1, "", "object store failed")

    (tmp_path / ".codex-gate.yaml").unlink()
    monkeypatch.setattr(nh, "_git_binary", lambda: "/usr/bin/git")
    monkeypatch.setattr(nh.subprocess, "run", fake_run)
    with pytest.raises(RuntimeError, match="HEAD"):
        nh._onboarded({"cwd": str(tmp_path)})


def test_native_unborn_head_is_not_onboarded_or_an_error(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    monkeypatch.setattr(nh, "_git_binary", lambda: "/usr/bin/git")

    def fake_run(argv, **kwargs):
        assert kwargs["env"]["GIT_NO_REPLACE_OBJECTS"] == "1"
        if "rev-parse" in argv and "--verify" not in argv:
            return g.subprocess.CompletedProcess(argv, 0, str(tmp_path), "")
        if "show-ref" in argv:
            return g.subprocess.CompletedProcess(argv, 1, "", "")
        return g.subprocess.CompletedProcess(argv, 128, "", "unborn HEAD")

    monkeypatch.setattr(nh.subprocess, "run", fake_run)
    assert nh._onboarded({"cwd": str(tmp_path)}) is False


def test_native_malformed_payload_is_fail_closed(tmp_path, monkeypatch):
    for raw in ("not json", "[]", "{}"):
        monkeypatch.setattr(nh.sys, "stdin", g.io.StringIO(raw))
        assert nh.main(["codex_review_gate.py", "gate-edit"]) == 2


def test_kimi_code_review_is_independent_not_self_review():
    text = (ROOT / "plugins/gates/commands/code-review.md").read_text()
    assert "companion-review" in text
    assert "--scope working-tree" in text
    assert "Kimi" in text and "саморевью" in text


def test_kimi_design_command_requires_literal_nonce_not_shell_substitution():
    text = (ROOT / "plugins/gates/commands/design-review.md").read_text()
    assert "32-hex-literal" in text
    command = text.split("```bash", 1)[1].split("```", 1)[0]
    assert "$NONCE" not in command
    assert "$(python3" not in command


def test_gates_run_launch_chain_has_no_ambient_shell_or_python():
    text = (ROOT / "plugins/gates/templates/githooks/gates-run").read_text()
    assert text.startswith("#!/bin/sh\n")
    assert "dirname" not in text
    assert "/usr/bin/env bash" not in text
    assert "-I -B" in text
    resolver = ROOT / "plugins/gates/templates/githooks/kimi_install.py"
    digest = hashlib.sha256(resolver.read_bytes()).hexdigest()
    assert f"BOOT_SHA256={digest}" in text


def test_tampered_or_stale_bootstrap_is_rejected_by_digest(tmp_path):
    home = tmp_path / "home"
    bootstrap = home / ".local/lib/claude-gates/kimi_install.py"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("# stale bootstrap\n")
    script = tmp_path / "gates-run"
    body = (ROOT / "plugins/gates/templates/githooks/gates-run").read_text()
    marker = 'GATE_HOME=$("$PY" -I -B -c \'import os,pwd; print(pwd.getpwuid(os.getuid()).pw_dir)\')'
    script.write_text(body.replace(marker, f'GATE_HOME="{home}"'))
    script.chmod(0o755)
    result = g.subprocess.run([str(script), "ladder_gate.py", "check-precommit"],
                              capture_output=True, text=True)
    assert result.returncode != 0 and "digest" in result.stderr


def test_bootstrap_executes_the_same_buffer_that_passed_digest(tmp_path):
    home = tmp_path / "home"
    bootstrap = home / ".local/lib/claude-gates/kimi_install.py"
    bootstrap.parent.mkdir(parents=True)
    benign = tmp_path / "benign.py"
    marker = tmp_path / "verified-buffer"
    benign.write_text(f"from pathlib import Path\nPath({str(marker)!r}).write_text('ok')\n")
    source = ("from pathlib import Path\n"
              "Path(__file__).write_text('raise RuntimeError(\"replacement executed\")')\n"
              f"print({str(benign)!r})\n")
    bootstrap.write_text(source)
    digest = hashlib.sha256(source.encode()).hexdigest()
    script = tmp_path / "gates-run"
    body = (ROOT / "plugins/gates/templates/githooks/gates-run").read_text()
    marker_home = 'GATE_HOME=$("$PY" -I -B -c \'import os,pwd; print(pwd.getpwuid(os.getuid()).pw_dir)\')'
    body = body.replace(marker_home, f'GATE_HOME="{home}"')
    import re
    body, count = re.subn(r"BOOT_SHA256=[0-9a-f]{64}", f"BOOT_SHA256={digest}", body)
    assert count == 1
    script.write_text(body)
    script.chmod(0o755)
    result = g.subprocess.run([str(script), "ladder_gate.py", "check-precommit"],
                              capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert marker.is_file()


@pytest.mark.parametrize(("name", "args"), (
    ("pre-commit", ()),
    ("pre-push", ()),
    ("gates-run", ("ladder_gate.py", "check-precommit")),
))
def test_git_hook_startup_cannot_be_bypassed_by_shell_environment(tmp_path, name, args):
    marker = tmp_path / "startup-bypassed"
    malicious = tmp_path / "shell-startup"
    malicious.write_text(f"touch {marker}\nexit 0\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "bash").write_text(f"#!/bin/sh\ntouch {marker}\nexit 0\n")
    (fake_bin / "bash").chmod(0o755)
    script = tmp_path / name
    script.write_text((ROOT / f"plugins/gates/templates/githooks/{name}").read_text())
    script.chmod(0o755)
    env = {**g.os.environ, "BASH_ENV": str(malicious), "ENV": str(malicious),
           "PATH": str(fake_bin)}
    result = g.subprocess.run([str(script), *args], capture_output=True, text=True, env=env)
    assert not marker.exists()
    if name != "gates-run":
        assert result.returncode != 0


def test_exact_kimi_review_command_cannot_be_bypassed_by_shell_environment(tmp_path):
    text = (ROOT / "plugins/gates/commands/code-review.md").read_text()
    command = text.split("```bash", 1)[1].split("```", 1)[0].replace("\\\n", " ").strip()
    malicious = tmp_path / "shell-startup"
    malicious.write_text("exit 0\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "bash").write_text("#!/bin/sh\nexit 0\n")
    (fake_bin / "bash").chmod(0o755)
    env = {**g.os.environ, "BASH_ENV": str(malicious), "ENV": str(malicious),
           "PATH": str(fake_bin), "BASH_FUNC_bash%%": "() { exit 0; }"}
    result = g.subprocess.run(["/bin/sh", "-c", command], cwd=tmp_path,
                              capture_output=True, text=True, env=env)
    assert result.returncode != 0


def test_worktree_bootstrap_resolver_is_never_executed(tmp_path):
    script = tmp_path / "gates-run"
    script.write_text((ROOT / "plugins/gates/templates/githooks/gates-run").read_text())
    script.chmod(0o755)
    marker = tmp_path / "resolver-executed"
    (tmp_path / "kimi_install.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).write_text('executed')\n")
    g.subprocess.run([str(script), "ladder_gate.py", "check-precommit"], cwd=tmp_path,
                     capture_output=True, text=True)
    assert not marker.exists()


def test_kimi_hook_matchers_and_commands_match_existing_gate():
    existing = json.loads((ROOT / "plugins/gates/hooks/hooks.json").read_text())["hooks"]
    kimi = json.loads((ROOT / "plugins/gates/.kimi-plugin/plugin.json").read_text())["hooks"]
    old_edit = next(e for e in existing["PreToolUse"] if e["matcher"] != "Bash")
    new_edit = next(e for e in kimi if e["event"] == "PreToolUse" and e["matcher"] != "Bash")
    assert new_edit["matcher"] == old_edit["matcher"]
    assert new_edit["command"].endswith("codex_review_gate.py gate-edit")
    bash = next(e for e in kimi if e["event"] == "PreToolUse" and e["matcher"] == "Bash")
    assert bash["command"].endswith("codex_review_gate.py gate-bash")
    start = next(e for e in kimi if e["event"] == "SessionStart")
    assert start["command"].endswith("codex_review_gate.py clear-marker")


@pytest.mark.parametrize("path", (
    "plugins/gates/.kimi-plugin/plugin.json",
    "plugins/gates/kimi.plugin.json",
    "plugins/gates/commands/code-review.md",
    ".kimi-plugin/plugin.json",
    "kimi.plugin.json",
))
def test_kimi_manifests_are_hard_code_paths(path):
    # EARS-9: ни один из двух приоритетных manifest-путей нельзя вывести из-под лесенки.
    assert g.is_code_path(path) is True


def test_handoff_binds_marker_to_hook_session_once(monkeypatch):
    # EARS-3: Bash не имеет KIMI_SESSION_ID; nonce переносит именно payload session.
    assert g.gate_bash_cli(_bash_hook()) == 0
    monkeypatch.setenv("GATES_SESSION_NONCE", NONCE)
    assert g._env_session() == "kimi-s1"
    assert g._env_session() == ""       # replay не даёт сессию повторно


def test_handoff_is_required_for_kimi_marker_route(monkeypatch):
    assert g.gate_bash_cli(_bash_hook()) == 0
    monkeypatch.setenv("GATES_SESSION_NONCE", NONCE)
    assert g.main(["write-marker", "trivial", "kimi smoke"]) == 0
    hook = json.dumps({"session_id": "kimi-s1", "tool_name": "Write",
                       "tool_input": {"file_path": "app/x.py"}})
    assert g.gate_edit_cli(hook) == 0
    other = json.dumps({"session_id": "kimi-s2", "tool_name": "Write",
                        "tool_input": {"file_path": "app/x.py"}})
    assert g.gate_edit_cli(other) == 2


def test_file_marker_consumes_handoff_once_and_binds_hook_session(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    monkeypatch.setattr(g, "REPO_ROOT", root)
    monkeypatch.setattr(g, "DESIGN_MARKER", root / ".claude" / ".design-approved")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    text = ("# Дизайн\n\n## Решения и альтернативы\n\n- A вместо B\n\n"
            "## Сценарии и приёмочные критерии (BSAC)\n\n- S1: маркер принадлежит Kimi-сессии\n")
    (root / "docs/design.md").write_text(text)
    sha = hashlib.sha256(text.encode()).hexdigest()
    g._record_design_receipt("docs/design.md", sha)
    command = (f"GATES_SESSION_NONCE={NONCE} /bin/sh .githooks/gates-run codex_review_gate.py "
               f"write-marker design ok {sha} --file docs/design.md")
    hook = json.dumps({"session_id": "kimi-s1", "cwd": str(root), "tool_name": "Bash",
                       "tool_input": {"command": command}})
    assert g.gate_bash_cli(hook) == 0
    monkeypatch.setenv("GATES_SESSION_NONCE", NONCE)
    assert g.main(["write-marker", "design", "ok", sha, "--file", "docs/design.md"]) == 0
    marker = g._load_marker("kimi-s1")
    assert marker["designs"] == [{"file": "docs/design.md", "hash": sha}]
    assert g._load_marker("nosession") is None
    edit = json.dumps({"session_id": "kimi-s1", "cwd": str(root), "tool_name": "Write",
                       "tool_input": {"file_path": "app/x.py"}})
    assert g.gate_edit_cli(edit) == 0


def test_handoff_accepts_canonical_absolute_runner():
    data = json.loads(_bash_hook())
    data["tool_input"]["command"] = data["tool_input"]["command"].replace(
        ".githooks/gates-run", str(g.REPO_ROOT / ".githooks/gates-run"))
    assert g.gate_bash_cli(json.dumps(data)) == 0
    assert g._consume_session_handoff(NONCE) == "kimi-s1"


@pytest.mark.parametrize(("old", "new"), (
    ("/bin/sh", "/tmp/sh"),
    ("/bin/sh", "/bin/bash"),
    (".githooks/gates-run", "/tmp/gates-run"),
    (".githooks/gates-run", "/tmp/evil/.githooks/gates-run"),
    (".githooks/gates-run", "docs/.githooks/gates-run"),
))
def test_handoff_rejects_noncanonical_launcher(old, new):
    data = json.loads(_bash_hook())
    data["tool_input"]["command"] = data["tool_input"]["command"].replace(old, new)
    assert g.gate_bash_cli(json.dumps(data)) == 2
    assert g._consume_session_handoff(NONCE) == ""


def test_duplicate_nonce_cannot_replace_session_binding():
    assert g.gate_bash_cli(_bash_hook("first")) == 0
    assert g.gate_bash_cli(_bash_hook("second")) == 2
    assert g._consume_session_handoff(NONCE) == "first"
    assert g._consume_session_handoff(NONCE) == ""


def test_same_nonce_publish_race_has_one_winner():
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda s: g.gate_bash_cli(_bash_hook(s)), ("first", "second")))
    assert sorted(results) == [0, 2]
    assert g._consume_session_handoff(NONCE) in {"first", "second"}
    assert g._consume_session_handoff(NONCE) == ""


def test_malformed_expired_and_wrong_repo_handoffs_do_not_authorize(monkeypatch):
    assert g.gate_bash_cli(_bash_hook(nonce="not-a-nonce")) == 2
    assert g.gate_bash_cli(_bash_hook()) == 0
    monkeypatch.setattr(g.time, "time", lambda: 2_000_000_000)
    assert g._consume_session_handoff(NONCE) == ""

    assert g.gate_bash_cli(_bash_hook(session="fresh")) == 0
    monkeypatch.setattr(g, "_gate_state_dir", lambda: g.REPO_ROOT / "other-state")
    assert g._consume_session_handoff(NONCE) == ""


def test_handoff_does_not_change_claude_env_session(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "claude-real")
    monkeypatch.delenv("GATES_SESSION_NONCE", raising=False)
    assert g._env_session() == "claude-real"


def test_session_start_clears_own_marker_and_writes_audit_evidence(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    root.mkdir()
    monkeypatch.setattr(g, "REPO_ROOT", root)
    monkeypatch.setattr(g, "DESIGN_MARKER", root / ".claude/.design-approved")
    monkeypatch.setattr(g, "AUDIT_LOG", root / "logs/codex_review_audit.log")
    g._write_marker_payload("kimi-s1", {"session": "kimi-s1", "kind": "trivial"})
    hook = json.dumps({"session_id": "kimi-s1", "cwd": str(root)})
    assert g.clear_marker_cli(hook) == 0
    assert not g._marker_path("kimi-s1").exists()
    assert "SESSION-START session=kimi-s1" in g.AUDIT_LOG.read_text()
