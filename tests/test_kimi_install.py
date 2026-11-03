"""Trusted Claude/Kimi installation resolver (S8–S11, R3/R8)."""
import json
import os
import shutil
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins/gates/templates/githooks"))
import kimi_install as ki  # noqa: E402


@pytest.fixture()
def home(tmp_path, monkeypatch):
    h = tmp_path / "os-home"
    h.mkdir()
    monkeypatch.setattr(ki.pwd, "getpwuid", lambda _uid: SimpleNamespace(pw_dir=str(h)))
    monkeypatch.setenv("HOME", str(tmp_path / "attacker-home"))
    monkeypatch.setenv("KIMI_CODE_HOME", str(tmp_path / "attacker-kimi"))
    return h


def _plugin(root):
    root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(ROOT / "plugins/gates", root,
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"))
    return root


def _kimi(home, enabled=True, state=None):
    root = _plugin(home / ".kimi-code/plugins/managed/gates")
    rec = {"id": "gates", "root": str(root), "enabled": enabled}
    if state is not None:
        rec["state"] = state
    registry = home / ".kimi-code/plugins/installed.json"
    registry.write_text(json.dumps({"version": 1, "plugins": [rec]}))
    return root


def _claude(home, version="9.9.9"):
    root = _plugin(home / f".claude/plugins/cache/lenar-gates/gates/{version}")
    registry = home / ".claude/plugins/installed_plugins.json"
    registry.parent.mkdir(parents=True, exist_ok=True)
    registry.write_text(json.dumps({"plugins": {"gates@lenar-gates": [{
        "scope": "user", "installPath": str(root), "version": version,
    }]}}))
    return root


def test_kimi_only_resolves_real_script(home):
    root = _kimi(home)
    assert ki.resolve_kimi("ladder_gate.py") == root / "scripts/ladder_gate.py"


def test_disabled_kimi_never_executes(home):
    _kimi(home, enabled=False)
    assert ki.resolve_kimi("ladder_gate.py") is None


@pytest.mark.parametrize("state", ("error", "installing", "quarantined", "unknown", 1))
def test_not_ok_kimi_state_never_executes(home, state):
    _kimi(home, state=state)
    with pytest.raises(ki.InstallError):
        ki.resolve_kimi("ladder_gate.py")


def test_absent_kimi_is_not_an_installation(home):
    (home / ".kimi-code/plugins").mkdir(parents=True)
    (home / ".kimi-code/plugins/installed.json").write_text('{"version":1,"plugins":[]}')
    assert ki.resolve_kimi("ladder_gate.py") is None


def test_root_manifest_precedence_and_bytecode_fail_closed(home):
    root = _kimi(home)
    (root / "kimi.plugin.json").write_text('{"name":"gates","hooks":[]}')
    with pytest.raises(ki.InstallError, match="manifest"):
        ki.resolve_kimi("ladder_gate.py")
    (root / "kimi.plugin.json").unlink()
    cache = root / "scripts/__pycache__"
    cache.mkdir()
    (cache / "codex_review_gate.cpython-314.pyc").write_bytes(b"not-validated-code")
    with pytest.raises(ki.InstallError, match="bytecode|pyc"):
        ki.resolve_kimi("ladder_gate.py")


def test_manifest_parent_symlink_is_rejected(home, tmp_path):
    root = _kimi(home)
    manifest = root / ".kimi-plugin"
    moved = tmp_path / "repo-local-manifest"
    shutil.move(manifest, moved)
    manifest.symlink_to(moved, target_is_directory=True)
    with pytest.raises(ki.InstallError, match="symlink"):
        ki.resolve_kimi("ladder_gate.py")


def test_repo_local_hook_command_is_rejected(home):
    root = _kimi(home)
    path = root / ".kimi-plugin/plugin.json"
    manifest = json.loads(path.read_text())
    manifest["hooks"][0]["command"] = "/bin/sh ${KIMI_PLUGIN_ROOT}/repo-local-hook"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ki.InstallError, match="hook schema drift"):
        ki.resolve_kimi("ladder_gate.py")


@pytest.mark.parametrize("key", ("commands", "skills"))
def test_alternate_manifest_targets_are_rejected(home, key):
    root = _kimi(home)
    shutil.copytree(root / key, root / "alternate")
    path = root / ".kimi-plugin/plugin.json"
    manifest = json.loads(path.read_text())
    manifest[key] = "./alternate/"
    path.write_text(json.dumps(manifest))
    with pytest.raises(ki.InstallError, match=f"manifest.{key}"):
        ki.resolve_kimi("ladder_gate.py")


def test_missing_claude_registry_never_uses_stale_cache(home):
    _claude(home)
    (home / ".claude/plugins/installed_plugins.json").unlink()
    _kimi(home, enabled=False)
    assert ki.resolve_script("ladder_gate.py") is None


def test_symlink_and_traversal_never_escape_managed_root(home):
    root = _kimi(home)
    script = root / "scripts/ladder_gate.py"
    script.unlink()
    script.symlink_to(ROOT / "plugins/gates/scripts/ladder_gate.py")
    with pytest.raises(ki.InstallError):
        ki.resolve_kimi("ladder_gate.py")
    with pytest.raises(ki.InstallError):
        ki.resolve_kimi("../ladder_gate.py")


@pytest.mark.parametrize("channel", ("kimi", "claude"))
def test_sibling_import_symlink_is_rejected_in_single_host_mode(home, channel):
    root = _kimi(home) if channel == "kimi" else _claude(home)
    sibling = root / "scripts/codex_review_gate.py"
    sibling.unlink()
    sibling.symlink_to(ROOT / "plugins/gates/scripts/codex_review_gate.py")
    resolver = ki.resolve_kimi if channel == "kimi" else ki.resolve_claude
    with pytest.raises(ki.InstallError, match="symlink"):
        resolver("ladder_gate.py")


@pytest.mark.parametrize(("channel", "relative"), (
    ("kimi", ".kimi-code"),
    ("kimi", ".kimi-code/plugins"),
    ("kimi", ".kimi-code/plugins/managed"),
    ("claude", ".claude"),
    ("claude", ".claude/plugins"),
    ("claude", ".claude/plugins/cache"),
    ("claude", ".claude/plugins/cache/lenar-gates"),
))
def test_parent_symlink_never_authorizes_canonical_installation(home, tmp_path, channel, relative):
    if channel == "kimi":
        _kimi(home)
    else:
        _claude(home)
    path = home / relative
    moved = tmp_path / "moved-installation"
    shutil.move(path, moved)
    path.symlink_to(moved, target_is_directory=True)
    resolver = ki.resolve_kimi if channel == "kimi" else ki.resolve_claude
    with pytest.raises(ki.InstallError, match="symlink"):
        resolver("ladder_gate.py")


def test_delivery_parity_and_both_skew_directions(home):
    claude = _claude(home)
    kimi = _kimi(home)
    assert ki.delivery_digest(claude) == ki.delivery_digest(kimi)
    (claude / "scripts/ladder_gate.py").write_text("# stale claude\n")
    assert ki.delivery_digest(claude) != ki.delivery_digest(kimi)
    _claude(home, version="10.0.0")
    (kimi / "scripts/ladder_gate.py").write_text("# stale kimi\n")
    assert ki.delivery_digest(claude) != ki.delivery_digest(kimi)


def test_resolve_script_equal_digest_selects_claude(home):
    claude = _claude(home)
    _kimi(home)
    assert ki.resolve_script("ladder_gate.py") == claude / "scripts/ladder_gate.py"


@pytest.mark.parametrize("stale", ("claude", "kimi"))
def test_resolve_script_rejects_delivery_digest_skew(home, stale):
    claude = _claude(home)
    kimi = _kimi(home)
    root = claude if stale == "claude" else kimi
    (root / "scripts/ladder_gate.py").write_text(f"# stale {stale}\n")
    with pytest.raises(ki.InstallError, match="delivery digest"):
        ki.resolve_script("ladder_gate.py")


def test_claude_uses_canonical_home_not_fake_home(home):
    root = _claude(home)
    assert ki.resolve_claude("ladder_gate.py") == root / "scripts/ladder_gate.py"
    fake = Path(os.environ["HOME"]) / ".claude/plugins/installed_plugins.json"
    assert not fake.exists()


def test_broken_claude_registry_does_not_fall_back_to_cache(home):
    _claude(home)
    (home / ".claude/plugins/installed_plugins.json").write_text("not json")
    with pytest.raises(ki.InstallError):
        ki.resolve_claude("ladder_gate.py")


def test_active_kimi_fake_home_cannot_inject_claude(home):
    _kimi(home)
    fake_root = _plugin(Path(os.environ["HOME"]) / ".claude/cache/gates")
    registry = fake_root.parent / "installed_plugins.json"
    registry.write_text(json.dumps({"plugins": {"gates@lenar-gates": [{
        "installPath": str(fake_root), "version": "666",
    }]}}))
    assert ki.resolve_claude("ladder_gate.py") is None
    assert ki.resolve_kimi("ladder_gate.py").is_file()


def test_active_coexistence_fake_home_does_not_change_parity(home):
    claude = _claude(home)
    kimi = _kimi(home)
    assert ki.delivery_digest(claude) == ki.delivery_digest(kimi)
    assert ki.resolve_claude("ladder_gate.py") == claude / "scripts/ladder_gate.py"
    assert ki.resolve_kimi("ladder_gate.py") == kimi / "scripts/ladder_gate.py"


def test_python_environment_is_irrelevant_to_resolver(home, monkeypatch):
    _kimi(home)
    monkeypatch.setenv("PATH", "/nonexistent")
    monkeypatch.setenv("PYTHONPATH", "/attacker")
    monkeypatch.setenv("PYTHONHOME", "/attacker")
    assert ki.resolve_kimi("ladder_gate.py").is_file()


@pytest.mark.parametrize("tree, expected", (
    ({".kimi-plugin/plugin.json": "x"}, True),
    ({"kimi.plugin.json": "x"}, True),
    ({".claude-plugin/plugin.json": "x"}, False),
))
def test_manifest_detector_covers_both_paths(tree, expected):
    assert ki.has_kimi_manifest(tree) is expected
