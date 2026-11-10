"""Kimi host: manifest/commands, hook parity и одноразовый session handoff (S1–S7, R8)."""
import getpass
import hashlib
import json
import re
import sys
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


_CANDIDATES = ("homebrew", "local", "system")


def _regular(path):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n")
    path.chmod(0o755)


def _resolver_case(tmp_path, *, uname=None, readlink=None):
    """Исполняет resolver из поставляемого shim; root ownership моделируется только probe."""
    fs = tmp_path / "fs"
    paths = {
        "homebrew": fs / "opt/homebrew/bin/python3",
        "local": fs / "usr/local/bin/python3",
        "system": fs / "usr/bin/python3",
    }
    for path in paths.values():
        path.parent.mkdir(parents=True, exist_ok=True)
    probe = tmp_path / "find_probe.py"
    probe.write_text(f"""#!{sys.executable}
import os
import subprocess
import sys

path = sys.argv[1]
args = set(sys.argv[2:])
if '-perm' in args:
    kind = 'mode'
elif '-user' in args:
    kind = 'owner'
elif '-acl' in args:
    kind = 'acl'
else:
    sys.exit(74)
failure = os.environ.get('PROBE_FAIL')
if failure in (path, f'{{kind}}:{path}'):
    sys.exit(73)
st = os.lstat(path)
if kind == 'mode':
    if os.environ.get('PROBE_MODE_BAD') == path:
        bad = True
    else:
        result = subprocess.run(['/usr/bin/find', path, *sys.argv[2:]],
                                capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.exit(result.returncode)
elif kind == 'owner':
    bad = st.st_uid not in (0, os.getuid())
    bad = bad or os.environ.get('PROBE_OWNER_BAD') == path
else:
    if os.environ.get('PROBE_SIMULATE_ACL'):
        bad = os.environ.get('PROBE_ACL_BAD') == path
    else:
        result = subprocess.run(['/usr/bin/find', path, *sys.argv[2:]],
                                capture_output=True, text=True)
        sys.stdout.write(result.stdout)
        sys.exit(result.returncode)
if bad:
    print(path)
""")
    probe.chmod(0o755)
    body = (ROOT / "plugins/gates/templates/githooks/gates-run").read_text()
    resolver = body[body.index('PY=""'):body.index("GATE_HOME=")]
    resolver = resolver.replace("/usr/bin/find", "__FIND_PROBE__")
    resolver = resolver.replace("/usr/bin/readlink", "__READLINK__")
    resolver = resolver.replace("/usr/bin/uname", "__UNAME__")
    if "TRUST_ROOT=/" in resolver:
        resolver = resolver.replace("TRUST_ROOT=/", f"TRUST_ROOT={fs}")
    else:
        resolver = resolver.replace("/opt/homebrew", "__HOMEBREW__")
        resolver = resolver.replace("/usr/local", "__LOCAL__")
        resolver = resolver.replace("/usr", "__USR__")
        resolver = resolver.replace("__HOMEBREW__", str(fs / "opt/homebrew"))
        resolver = resolver.replace("__LOCAL__", str(fs / "usr/local"))
        resolver = resolver.replace("__USR__", str(fs / "usr"))
    resolver = resolver.replace("__FIND_PROBE__", str(probe))
    resolver = resolver.replace("__READLINK__", readlink or "/usr/bin/readlink")
    resolver = resolver.replace("__UNAME__", uname or "/usr/bin/uname")
    script = tmp_path / "resolver.sh"
    script.write_text("#!/bin/sh\nset -u\n" + resolver + "\nprintf '%s\\n' \"$PY\"\n")
    script.chmod(0o755)
    return script, paths


def _select(script, env=None):
    return g.subprocess.run([str(script)], capture_output=True, text=True,
                            env={**g.os.environ, **(env or {})})


def _selected(result, path):
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == str(path)


def _ancestors(path, tmp_path):
    root = tmp_path / "fs"
    result = []
    current = path.parent
    while True:
        result.append(current)
        if current == root:
            return result
        current = current.parent


@pytest.mark.parametrize("kind", _CANDIDATES)
def test_resolver_accepts_regular_candidate_with_trusted_chain(tmp_path, kind):
    # EARS-1: прежние regular-пути работают на каждой ветке выбора.
    script, paths = _resolver_case(tmp_path)
    _regular(paths[kind])
    _selected(_select(script), paths[kind])


def test_resolver_preserves_regular_candidate_priority(tmp_path):
    script, paths = _resolver_case(tmp_path)
    for path in paths.values():
        _regular(path)
    _selected(_select(script), paths["homebrew"])


@pytest.mark.parametrize("version", ("3.9", "3.14", "3.100", "3.14.1"))
def test_resolver_follows_only_versioned_system_default(tmp_path, version):
    # EARS-2: Linux /usr/bin/python3 -> slashless numeric target исполняет regular target.
    script, paths = _resolver_case(tmp_path)
    target = paths["system"].with_name(f"python{version}")
    _regular(target)
    paths["system"].symlink_to(target.name)
    _selected(_select(script), target)


def test_resolver_uses_default_not_newest_or_creation_order(tmp_path):
    script, paths = _resolver_case(tmp_path)
    for version in ("3.14", "3.9", "3.10"):
        _regular(paths["system"].with_name(f"python{version}"))
    target = paths["system"].with_name("python3.14")
    paths["system"].symlink_to(target.name)
    _selected(_select(script, {"LC_ALL": "C"}), target)


@pytest.mark.parametrize("target", (
    "/tmp/python3.14", "../python3.14", "sub/python3.14", "python3.14-config",
    "python3.", "python3..14", "python3.14.", "python3.a",
))
def test_resolver_rejects_non_exact_default_target(tmp_path, target):
    # EARS-4: никаких absolute/alternatives/patch/suffix targets.
    script, paths = _resolver_case(tmp_path)
    paths["system"].symlink_to(target)
    result = _select(script)
    assert result.returncode != 0 and not result.stdout


def test_resolver_rejects_readlink_failure(tmp_path):
    readlink = tmp_path / "readlink-fails"
    readlink.write_text("#!/bin/sh\nexit 1\n")
    readlink.chmod(0o755)
    script, paths = _resolver_case(tmp_path, readlink=str(readlink))
    target = paths["system"].with_name("python3.14")
    _regular(target)
    paths["system"].symlink_to(target.name)
    assert _select(script).returncode != 0


@pytest.mark.parametrize(("kind", "fallback"), (("homebrew", "local"), ("local", "system")))
def test_resolver_rejects_non_system_symlink_before_readlink(tmp_path, kind, fallback):
    marker = tmp_path / "non-system-readlink-called"
    readlink = tmp_path / "readlink-non-system-sentinel"
    readlink.write_text(f"#!/bin/sh\ntouch {marker}\nprintf 'python3.14\\n'\n")
    readlink.chmod(0o755)
    script, paths = _resolver_case(tmp_path, readlink=str(readlink))
    target = paths[kind].with_name("python3.14")
    _regular(target)
    paths[kind].symlink_to(target.name)
    _regular(paths[fallback])
    _selected(_select(script), paths[fallback])
    assert not marker.exists()


def test_resolver_rejects_versioned_symlink_target(tmp_path):
    script, paths = _resolver_case(tmp_path)
    real = paths["system"].with_name("python3.9")
    target = paths["system"].with_name("python3.14")
    _regular(real)
    target.symlink_to(real.name)
    paths["system"].symlink_to(target.name)
    assert _select(script).returncode != 0


@pytest.mark.parametrize("mode", (0o644, 0o775, 0o757, 0o777))
def test_resolver_rejects_non_executable_or_writable_default_target(tmp_path, mode):
    script, paths = _resolver_case(tmp_path)
    target = paths["system"].with_name("python3.14")
    _regular(target)
    target.chmod(mode)
    paths["system"].symlink_to(target.name)
    assert _select(script).returncode != 0


@pytest.mark.parametrize("kind", _CANDIDATES)
@pytest.mark.parametrize("mode", (0o775, 0o757))
def test_resolver_rejects_group_or_world_writable_ancestor(tmp_path, kind, mode):
    # Регрессия code-review: find -perm -022 требует ОБА бита, нужен OR -020/-002.
    script, paths = _resolver_case(tmp_path)
    _regular(paths[kind])
    for ancestor in _ancestors(paths[kind], tmp_path):
        ancestor.chmod(mode)
        result = _select(script)
        ancestor.chmod(0o755)
        assert result.returncode != 0, f"mode {mode:o} принят на {ancestor}"


@pytest.mark.parametrize("kind", _CANDIDATES)
def test_resolver_rejects_symlink_ancestor_for_every_candidate(tmp_path, kind):
    script, paths = _resolver_case(tmp_path)
    path = paths[kind]
    _regular(path)
    parent = path.parent
    real = parent.with_name(parent.name + "-real")
    parent.rename(real)
    parent.symlink_to(real.name, target_is_directory=True)
    assert _select(script).returncode != 0


@pytest.mark.parametrize("which", ("link", "target"))
def test_resolver_rejects_owner_mismatch_in_default_chain(tmp_path, which):
    script, paths = _resolver_case(tmp_path)
    target = paths["system"].with_name("python3.14")
    _regular(target)
    paths["system"].symlink_to(target.name)
    bad = paths["system"] if which == "link" else target
    assert _select(script, {"PROBE_OWNER_BAD": str(bad)}).returncode != 0


@pytest.mark.parametrize("kind", _CANDIDATES)
@pytest.mark.parametrize("probe", ("PROBE_OWNER_BAD", "PROBE_MODE_BAD", "PROBE_FAIL"))
def test_resolver_rejects_bad_ancestor_for_every_regular_candidate(tmp_path, kind, probe):
    # EARS-3: full-chain, а не только parent; каждый probe failure закрыт.
    script, paths = _resolver_case(tmp_path)
    _regular(paths[kind])
    for ancestor in _ancestors(paths[kind], tmp_path):
        result = _select(script, {probe: str(ancestor)})
        assert result.returncode != 0, f"{probe} проигнорирован на {ancestor}"


@pytest.mark.parametrize("kind", _CANDIDATES)
@pytest.mark.parametrize("probe", ("PROBE_OWNER_BAD", "PROBE_FAIL"))
def test_resolver_rejects_bad_file_probe_for_every_candidate(tmp_path, kind, probe):
    script, paths = _resolver_case(tmp_path)
    _regular(paths[kind])
    assert _select(script, {probe: str(paths[kind])}).returncode != 0


@pytest.mark.parametrize("probe_kind", ("owner", "acl"))
def test_resolver_rejects_failed_owner_and_acl_probes(tmp_path, probe_kind):
    uname = tmp_path / "uname-darwin-probe-failure"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n")
    uname.chmod(0o755)
    script, paths = _resolver_case(tmp_path, uname=str(uname))
    path = paths["system"]
    _regular(path)
    env = {"PROBE_FAIL": f"{probe_kind}:{path}", "PROBE_SIMULATE_ACL": "1"}
    assert _select(script, env).returncode != 0


def test_resolver_rejects_darwin_acl_on_default_symlink(tmp_path):
    uname = tmp_path / "uname-darwin-default-link"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n")
    uname.chmod(0o755)
    script, paths = _resolver_case(tmp_path, uname=str(uname))
    target = paths["system"].with_name("python3.14")
    _regular(target)
    paths["system"].symlink_to(target.name)
    env = {"PROBE_SIMULATE_ACL": "1", "PROBE_ACL_BAD": str(paths["system"])}
    assert _select(script, env).returncode != 0


def test_shipped_permission_probes_match_any_write_bit():
    for path in (ROOT / ".githooks/gates-run",
                 ROOT / "plugins/gates/templates/githooks/gates-run"):
        text = path.read_text()
        assert "-perm -022" not in text
        assert "-perm -020 -o -perm -002" in text


@pytest.mark.parametrize("kind", _CANDIDATES)
def test_resolver_rejects_darwin_acl_on_file_and_every_ancestor(tmp_path, kind):
    uname = tmp_path / "uname-darwin"
    uname.write_text("#!/bin/sh\nprintf 'Darwin\\n'\n")
    uname.chmod(0o755)
    script, paths = _resolver_case(tmp_path, uname=str(uname))
    path = paths[kind]
    _regular(path)
    for bad in (path, *_ancestors(path, tmp_path)):
        result = _select(script, {"PROBE_SIMULATE_ACL": "1", "PROBE_ACL_BAD": str(bad)})
        assert result.returncode != 0, f"Darwin ACL принят на {bad}"


@pytest.mark.skipif(g.os.uname().sysname != "Darwin", reason="реальный macOS ACL")
@pytest.mark.parametrize("which", ("file", "parent"))
def test_resolver_rejects_real_darwin_acl_with_mode_0755(tmp_path, which):
    # EARS-3: ACL write/add_file/delete_child невидим для mode 0755, но не для find -acl.
    script, paths = _resolver_case(tmp_path)
    path = paths["system"]
    _regular(path)
    target = path if which == "file" else path.parent
    permissions = "write" if which == "file" else "write,add_file,delete_child"
    g.subprocess.run(["/bin/chmod", "+a", f"{getpass.getuser()} allow {permissions}",
                      str(target)], check=True, capture_output=True)
    assert target.stat().st_mode & 0o777 == 0o755
    assert _select(script).returncode != 0


def test_resolver_rejects_unsupported_os(tmp_path):
    uname = tmp_path / "uname-freebsd"
    uname.write_text("#!/bin/sh\nprintf 'FreeBSD\\n'\n")
    uname.chmod(0o755)
    script, paths = _resolver_case(tmp_path, uname=str(uname))
    _regular(paths["system"])
    assert _select(script).returncode != 0


def test_resolver_rejects_failed_uname_probe(tmp_path):
    uname = tmp_path / "uname-fails"
    uname.write_text("#!/bin/sh\nexit 1\n")
    uname.chmod(0o755)
    script, paths = _resolver_case(tmp_path, uname=str(uname))
    _regular(paths["system"])
    assert _select(script).returncode != 0


def test_resolver_checks_ancestors_before_readlink(tmp_path):
    marker = tmp_path / "readlink-called"
    readlink = tmp_path / "readlink-sentinel"
    readlink.write_text(f"#!/bin/sh\ntouch {marker}\nprintf 'python3.14\\n'\n")
    readlink.chmod(0o755)
    script, paths = _resolver_case(tmp_path, readlink=str(readlink))
    target = paths["system"].with_name("python3.14")
    _regular(target)
    paths["system"].symlink_to(target.name)
    result = _select(script, {"PROBE_FAIL": str(paths["system"].parent)})
    assert result.returncode != 0 and not marker.exists()


def test_trusted_parent_prevents_directory_entry_substitution(tmp_path):
    script, paths = _resolver_case(tmp_path)
    path = paths["system"]
    _regular(path)
    replacement = path.with_name("replacement")
    _regular(replacement)
    path.parent.chmod(0o555)
    try:
        with pytest.raises(PermissionError):
            replacement.replace(path)
        _selected(_select(script), path)
    finally:
        path.parent.chmod(0o755)


def test_resolver_without_candidates_fails_closed(tmp_path):
    script, _paths = _resolver_case(tmp_path)
    result = _select(script)
    assert result.returncode == 1
    assert "trusted system Python" in result.stderr and not result.stdout


def test_repo_gates_run_matches_shipped_template():
    assert ((ROOT / ".githooks/gates-run").read_bytes()
            == (ROOT / "plugins/gates/templates/githooks/gates-run").read_bytes())


def test_full_shipped_gates_run_linux_symlink_in_offline_docker():
    # S10: полный home→bootstrap→registry→exec маршрут, без усечения production shim.
    image = "python:3.13-slim"
    try:
        available = g.subprocess.run(["docker", "image", "inspect", image],
                                     capture_output=True, text=True, timeout=15)
    except (OSError, g.subprocess.TimeoutExpired):
        available = None
    if available is None or available.returncode != 0:
        pytest.skip("локальные Docker и python:3.13 недоступны; image не скачивается")
    old_shim = g.subprocess.run([
        "git", "show", "ad2795fd2bd764dc4b6085b27549df6c54aeef8f:plugins/gates/templates/githooks/gates-run"
    ], cwd=ROOT, capture_output=True, text=True, check=True).stdout
    script = r'''
set -eu
cp /usr/local/bin/python3.13 /usr/bin/python3.13
ln -sf python3.13 /usr/bin/python3
mkdir -p /root/.local/lib/claude-gates /root/.claude/plugins/cache/lenar-gates/gates
cp /src/plugins/gates/templates/githooks/kimi_install.py /root/.local/lib/claude-gates/kimi_install.py
cp -r /src/plugins/gates /root/.claude/plugins/cache/lenar-gates/gates/integration
rm -rf /root/.claude/plugins/cache/lenar-gates/gates/integration/scripts/__pycache__ \
  /root/.claude/plugins/cache/lenar-gates/gates/integration/templates/githooks/__pycache__
python3.13 -c "import json,pathlib; p=pathlib.Path('/root/.claude/plugins/installed_plugins.json'); p.write_text(json.dumps({'plugins':{'gates@lenar-gates':[{'scope':'user','installPath':'/root/.claude/plugins/cache/lenar-gates/gates/integration','version':'integration'}]}}))"
printf "import sys\npayload = sys.stdin.read()\nassert payload in (\"\", \"pre-push-ref\"), repr(payload)\nprint(\"FINAL_GATE_EXECUTABLE=\" + sys.executable + \";STDIN=\" + payload)\n" \
  > /root/.claude/plugins/cache/lenar-gates/gates/integration/scripts/ladder_gate.py
printf 'pre-push-ref' | /src/.githooks/gates-run ladder_gate.py
printf 'pre-push-ref' | /src/.githooks/gates-run ladder_gate.py
/bin/sh -s ladder_gate.py < /src/.githooks/gates-run
cat > /root/old-gates-run <<'OLD_SHIM'
__OLD_0112_SHIM__
OLD_SHIM
chmod 755 /root/old-gates-run
if /root/old-gates-run ladder_gate.py; then
  echo "broken 0.11.2 shim unexpectedly accepted versioned symlink" >&2
  exit 1
fi
if /src/.githooks/gates-run --gates-second-stage; then
  echo "invalid script name accepted" >&2
  exit 1
fi
'''
    script = script.replace("__OLD_0112_SHIM__", old_shim)
    result = g.subprocess.run([
        "docker", "run", "--rm", "--network", "none", "-v", f"{ROOT}:/src:ro",
        image, "/bin/sh", "-c", script,
    ], capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "FINAL_GATE_EXECUTABLE=/usr/bin/python3.13;STDIN=pre-push-ref",
        "FINAL_GATE_EXECUTABLE=/usr/bin/python3.13;STDIN=pre-push-ref",
        "FINAL_GATE_EXECUTABLE=/usr/bin/python3.13;STDIN=",
    ]


def test_tampered_or_stale_bootstrap_is_rejected_by_digest(tmp_path):
    home = tmp_path / "home"
    bootstrap = home / ".local/lib/claude-gates/kimi_install.py"
    bootstrap.parent.mkdir(parents=True)
    bootstrap.write_text("# stale bootstrap\n")
    script = tmp_path / "gates-run"
    body = (ROOT / "plugins/gates/templates/githooks/gates-run").read_text()
    body, count = re.subn(r"GATE_HOME=.*", f'GATE_HOME="{home}"', body, count=1)
    assert count == 1
    script.write_text(body)
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
    body, count = re.subn(r"GATE_HOME=.*", f'GATE_HOME="{home}"', body, count=1)
    assert count == 1
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
