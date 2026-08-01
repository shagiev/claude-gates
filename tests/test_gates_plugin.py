"""Тесты НОВОЙ поверхности плагина gates (спека docs/2026-07-22-gates-plugin-port-design.md):
конфиг-экстернализация (.codex-gate.yaml, строгие дефолты), жёсткие код-пути (ML-P1),
opt-in автосрабатывающих хуков (BS-P1), hard_cap-валидация, эпоха лесенки из конфига."""
import json
import os
import subprocess
from pathlib import Path

import pytest

import codex_review_gate as g
import ladder_gate as lg


# --- строгий режим is_code_path (BS-P4: нет/битый конфиг → всё код) ---

def test_strict_mode_everything_is_code(monkeypatch):
    monkeypatch.setattr(g, "CODE_PATH_PREFIXES", None)
    monkeypatch.setattr(g, "CODE_PATH_EXACT", set())
    assert g.is_code_path("docs/notes.md") is True        # экземпции исчезают
    assert g.is_code_path("README.md") is True
    assert g.is_code_path(".claude/settings.json") is True
    assert g.is_code_path("random/file.txt") is True


def test_strict_mode_absolute_outside_repo_not_code(monkeypatch):
    monkeypatch.setattr(g, "CODE_PATH_PREFIXES", None)
    assert g.is_code_path("/etc/passwd") is False         # вне репо — не наш код-путь


# --- жёсткие код-пути (ML-P1: конфиг не может вывести их из-под гейта) ---

def test_hard_paths_survive_empty_config(monkeypatch):
    monkeypatch.setattr(g, "CODE_PATH_PREFIXES", ())      # конфиг «всё не-код»
    monkeypatch.setattr(g, "CODE_PATH_EXACT", set())
    assert g.is_code_path(".codex-gate.yaml") is True
    assert g.is_code_path("Makefile") is True
    assert g.is_code_path(".githooks/pre-commit") is True
    assert g.is_code_path(".githooks/gates-run") is True
    assert g.is_code_path(".codex/hooks.json") is True
    assert g.is_code_path(".codex/config.toml") is True
    assert g.is_code_path(".claude/settings.json") is True
    assert g.is_code_path(".claude/settings.local.json") is True
    assert g.is_code_path(".claude/.design-approved-s1") is True
    assert g.is_code_path(".claude/.review-disabled-s1") is True
    assert g.is_code_path(".claude/.last-reviewed-sha") is True
    assert g.is_code_path(".claude/.last-deployed-sha") is True
    assert g.is_code_path(".claude/.deploy-section-pin") is True
    assert g.is_code_path(".codex-plugin/plugin.json") is True
    assert g.is_code_path(".claude-plugin/plugin.json") is True
    assert g.is_code_path(".agents/plugins/marketplace.json") is True
    assert g.is_code_path("hooks/hooks.json") is True
    assert g.is_code_path("plugins/gates/.codex-plugin/plugin.json") is True
    assert g.is_code_path("plugins/gates/.claude-plugin/plugin.json") is True
    assert g.is_code_path("plugins/gates/hooks/hooks.json") is True
    assert g.is_code_path("vendor/tool/.agents/plugins/marketplace.json") is True
    assert g.is_code_path("plugins/gates/reviewer_certifications.json") is True
    assert g.is_code_path("plugins/gates/reviewer_corpus/cases.json") is True
    assert g.is_code_path("makefile") is True
    assert g.is_code_path(".CODEX-GATE.YAML") is True
    assert g.is_code_path(".GitHooks/pre-commit") is True
    assert g.is_code_path("app/x.py") is False            # а обычный код конфиг убрал


def test_config_gate_yaml_always_code_with_normal_config():
    # с пинованным конфигом (conftest) — тоже код
    assert g.is_code_path(".codex-gate.yaml") is True
    assert g.is_code_path("docs/../.codex-gate.yaml") is True   # normpath не обходится


# --- парс конфига ---

def test_code_paths_from_config_valid():
    cfg = {"code_paths": {"prefixes": ["src/"], "exact": ["justfile"]}}
    assert g._code_paths_from_config(cfg) == (("src/",), {"justfile"})


def test_code_paths_from_config_invalid_shapes_strict():
    for bad in (None, {}, {"code_paths": "src/"}, {"code_paths": {"prefixes": "src/"}},
                {"code_paths": {"prefixes": [1]}}, {"code_paths": {"exact": {"a": 1}}}):
        prefixes, exact = g._code_paths_from_config(bad)
        assert prefixes is None and exact == set(), bad   # строгий режим


def test_hard_cap_from_config():
    assert g._hard_cap_from_config(None) == 8
    assert g._hard_cap_from_config({"convergence": {"hard_cap": 5}}) == 5
    for bad in ({"convergence": {"hard_cap": 0}}, {"convergence": {"hard_cap": -3}},
                {"convergence": {"hard_cap": True}}, {"convergence": {"hard_cap": "9"}},
                {"convergence": "x"}, {}):
        assert g._hard_cap_from_config(bad) == 8, bad


def test_read_gate_config_states(tmp_path):
    assert g._read_gate_config(tmp_path) is None                      # нет файла
    (tmp_path / ".codex-gate.yaml").write_text("code_paths: [unclosed\n")
    assert g._read_gate_config(tmp_path) is None                      # битый YAML
    (tmp_path / ".codex-gate.yaml").write_text("- just\n- a list\n")
    assert g._read_gate_config(tmp_path) is None                      # не dict
    (tmp_path / ".codex-gate.yaml").write_text("code_paths:\n  prefixes: [\"src/\"]\n")
    cfg = g._read_gate_config(tmp_path)
    assert cfg == {"code_paths": {"prefixes": ["src/"]}}              # валидный


# --- opt-in признак онбординга (worktree OR HEAD, Codex R1-фикс спеки) ---

def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": os.environ["PATH"], "HOME": str(repo.parent)})


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-b", "main")
    (r / "f.txt").write_text("x\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    return r


def test_onboarded_neither_worktree_nor_head(repo):
    assert g._onboarded(repo) is False


def test_onboarded_worktree_only(repo):
    (repo / ".codex-gate.yaml").write_text("code_paths:\n  prefixes: []\n")
    assert g._onboarded(repo) is True


def test_onboarded_head_only_after_worktree_delete(repo):
    # «удалить → править → вернуть» не отключает хуки: конфиг в HEAD достаточен
    (repo / ".codex-gate.yaml").write_text("code_paths:\n  prefixes: []\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "onboard")
    (repo / ".codex-gate.yaml").unlink()
    assert g._onboarded(repo) is True


# --- opt-in хуков (BS-P1: не-онбордженный проект → плагин молчит) ---

def test_gate_edit_noop_when_not_onboarded(monkeypatch):
    monkeypatch.setattr(g, "ONBOARDED", False)
    hook = json.dumps({"session_id": "s1", "tool_input": {"file_path": "app/x.py"}})
    assert g.gate_edit_cli(hook) == 0                     # без маркера, но и без гейта


def test_gate_bash_noop_when_not_onboarded(monkeypatch):
    monkeypatch.setattr(g, "ONBOARDED", False)
    hook = json.dumps({"session_id": "s1", "tool_input": {"command": "sed -i s/a/b/ app/x.py"}})
    assert g.gate_bash_cli(hook) == 0


def test_gate_hooks_noop_outside_git_repo(monkeypatch):
    monkeypatch.setattr(g, "REPO_ROOT", None)
    monkeypatch.setattr(g, "ONBOARDED", False)
    hook = json.dumps({"session_id": "s1", "tool_input": {"file_path": "app/x.py"}})
    assert g.gate_edit_cli(hook) == 0
    assert g.main(["clear-marker"]) == 0                  # SessionStart вне репо — тихий no-op


def test_gate_edit_active_when_onboarded_broken_config(monkeypatch, tmp_path):
    # битый конфиг в онбордженном репо НЕ снимает G1 (строгий режим: всё код)
    monkeypatch.setattr(g, "ONBOARDED", True)
    monkeypatch.setattr(g, "CODE_PATH_PREFIXES", None)
    monkeypatch.setattr(g, "CODE_PATH_EXACT", set())
    hook = json.dumps({"session_id": "s1", "tool_input": {"file_path": "docs/x.md"}})
    assert g.gate_edit_cli(hook) == 2                     # даже docs гейтится в строгом режиме


@pytest.mark.parametrize("payload", ("{not-json", "[]"))
def test_gate_edit_malformed_top_level_payload_blocks_when_active(payload, capsys):
    assert g.gate_edit_cli(payload) == 2
    assert "fail-closed" in capsys.readouterr().err


def test_gate_edit_non_dict_tool_input_blocks_when_active(capsys):
    hook = json.dumps({"tool_name": "apply_patch", "tool_input": ["schema", "drift"]})
    assert g.gate_edit_cli(hook) == 2
    assert "tool_input изменил схему" in capsys.readouterr().err


def test_gate_edit_cannot_write_design_unlock_marker_directly(capsys):
    hook = json.dumps({
        "session_id": "s1",
        "tool_name": "Write",
        "tool_input": {"file_path": ".claude/.design-approved-s1"},
    })
    assert g.gate_edit_cli(hook) == 2
    assert "Дизайн-ревью не пройдено" in capsys.readouterr().err


def test_gate_edit_payload_cwd_non_onboarded_repo_is_noop(repo):
    hook = json.dumps({
        "cwd": str(repo),
        "session_id": "codex-s1",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Add File: app/x.py\n"
                "+x = 1\n"
                "*** End Patch\n"
            ),
        },
    })
    assert g.gate_edit_cli(hook) == 0


def test_gate_edit_nested_non_onboarded_repo_cannot_escape_active_parent(
        repo, monkeypatch, capsys):
    (repo / ".codex-gate.yaml").write_text("code_paths:\n  prefixes: [app/]\n")
    (repo / "app").mkdir()
    nested = repo / "vendor" / "lib"
    nested.mkdir(parents=True)
    _git(nested, "init", "-b", "main")
    monkeypatch.setattr(g, "REPO_ROOT", repo)
    monkeypatch.setattr(g, "ONBOARDED", True)
    monkeypatch.setattr(g, "CODE_PATH_PREFIXES", ("app/",))
    hook = json.dumps({
        "cwd": str(nested),
        "session_id": "codex-nested-s1",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Add File: ../../app/x.py\n"
                "+x = 1\n"
                "*** End Patch\n"
            ),
        },
    })
    assert g.gate_edit_cli(hook) == 2
    assert "Дизайн-ревью не пройдено" in capsys.readouterr().err


def test_gate_edit_nested_non_onboarded_repo_stays_opted_out(repo, monkeypatch):
    (repo / ".codex-gate.yaml").write_text("code_paths:\n  prefixes: [app/]\n")
    nested = repo / "vendor" / "lib"
    nested.mkdir(parents=True)
    _git(nested, "init", "-b", "main")
    monkeypatch.setattr(g, "REPO_ROOT", repo)
    monkeypatch.setattr(g, "ONBOARDED", True)
    hook = json.dumps({
        "cwd": str(nested),
        "session_id": "codex-nested-s2",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Add File: local.py\n"
                "+x = 1\n"
                "*** End Patch\n"
            ),
        },
    })
    assert g.gate_edit_cli(hook) == 0


@pytest.mark.parametrize("header", ("Add File", "Update File", "Delete File"))
def test_gate_edit_codex_apply_patch_code_path_blocked(header):
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "codex-s1",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                f"*** {header}: app/x.py\n"
                "+x = 1\n"
                "*** End Patch\n"
            ),
        },
    })
    assert g.gate_edit_cli(hook) == 2


def test_gate_edit_codex_apply_patch_move_and_mixed_paths_blocked():
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "codex-s1",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Update File: docs/notes.md\n"
                "*** Move to: app/notes.py\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** Add File: README-extra.md\n"
                "+text\n"
                "*** End Patch\n"
            ),
        },
    })
    assert g.gate_edit_cli(hook) == 2


def test_gate_edit_codex_apply_patch_noncode_only_passes():
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "codex-s1",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Update File: docs/notes.md\n"
                "@@\n"
                "-old\n"
                "+new\n"
                "*** End Patch\n"
            ),
        },
    })
    assert g.gate_edit_cli(hook) == 0


def test_gate_edit_codex_apply_patch_malformed_blocks(capsys):
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "codex-s1",
        "tool_name": "apply_patch",
        "tool_input": {"command": "*** Begin Patch\n*** End Patch\n"},
    })
    assert g.gate_edit_cli(hook) == 2
    assert "apply_patch" in capsys.readouterr().err


def test_gate_edit_codex_apply_patch_unknown_control_header_blocks(capsys):
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "codex-s1",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Update File: docs/known.md\n"
                "*** Rename File: app/hidden.py\n"
                "*** End Patch\n"
            ),
        },
    })
    assert g.gate_edit_cli(hook) == 2
    assert "apply_patch" in capsys.readouterr().err


def test_gate_edit_codex_apply_patch_paths_are_relative_to_payload_cwd(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    (repo / "subdir").mkdir(parents=True)
    monkeypatch.setattr(g, "REPO_ROOT", repo)
    hook = json.dumps({
        "cwd": str(repo / "subdir"),
        "session_id": "codex-s1",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Add File: ../app/x.py\n"
                "+x = 1\n"
                "*** End Patch\n"
            ),
        },
    })
    assert g.gate_edit_cli(hook) == 2


def test_gate_edit_codex_apply_patch_cwd_outside_repo_blocks(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    monkeypatch.setattr(g, "REPO_ROOT", repo)
    hook = json.dumps({
        "cwd": str(outside),
        "session_id": "codex-s1",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Update File: docs/looks-safe.md\n"
                "*** End Patch\n"
            ),
        },
    })
    assert g.gate_edit_cli(hook) == 2
    assert "нельзя доказуемо привязать" in capsys.readouterr().err


def test_gate_edit_codex_apply_patch_absolute_outside_repo_blocks(
        tmp_path, monkeypatch, capsys):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    monkeypatch.setattr(g, "REPO_ROOT", repo)
    hook = json.dumps({
        "cwd": str(repo),
        "session_id": "codex-s1",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                f"*** Update File: {outside / 'looks-safe.md'}\n"
                "*** End Patch\n"
            ),
        },
    })
    assert g.gate_edit_cli(hook) == 2
    assert "patch path вне repo root" in capsys.readouterr().err


def test_gate_edit_codex_apply_patch_resolves_symlinked_checkout(
        tmp_path, monkeypatch, capsys):
    physical = tmp_path / "physical"
    logical = tmp_path / "logical"
    (physical / "app").mkdir(parents=True)
    logical.symlink_to(physical, target_is_directory=True)
    monkeypatch.setattr(g, "REPO_ROOT", physical)
    docs_hook = json.dumps({
        "cwd": str(logical),
        "session_id": "codex-s1",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                f"*** Update File: {logical / 'docs' / 'notes.md'}\n"
                "*** End Patch\n"
            ),
        },
    })
    assert g.gate_edit_cli(docs_hook) == 0
    code_hook = json.loads(docs_hook)
    code_hook["tool_input"]["command"] = (
        "*** Begin Patch\n"
        f"*** Update File: {logical / 'app' / 'x.py'}\n"
        "*** End Patch\n"
    )
    assert g.gate_edit_cli(json.dumps(code_hook)) == 2
    assert "Дизайн-ревью не пройдено" in capsys.readouterr().err


def test_gate_edit_codex_apply_patch_missing_cwd_blocks(capsys):
    hook = json.dumps({
        "session_id": "codex-s1",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Update File: docs/notes.md\n"
                "*** End Patch\n"
            ),
        },
    })
    assert g.gate_edit_cli(hook) == 2
    assert "cwd отсутствует" in capsys.readouterr().err


@pytest.mark.parametrize("tool_name", ("apply_patch_v2", "mcp__x__apply_patch", None))
def test_gate_edit_codex_apply_patch_tool_name_drift_blocks(tool_name):
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "codex-s1",
        "tool_name": tool_name,
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Add File: app/x.py\n"
                "+x = 1\n"
                "*** End Patch\n"
            ),
        },
    })
    assert g.gate_edit_cli(hook) == 2


def test_gate_edit_unrecognized_payload_blocks(capsys):
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "codex-s1",
        "tool_name": "WriteV2",
        "tool_input": {"new_schema_path": "app/x.py"},
    })
    assert g.gate_edit_cli(hook) == 2
    assert "заблокирована" in capsys.readouterr().err


def test_gate_edit_non_file_tool_accidentally_sent_to_hook_is_noop():
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "codex-s1",
        "tool_name": "TodoWrite",
        "tool_input": {"todos": []},
    })
    assert g.gate_edit_cli(hook) == 0


@pytest.mark.parametrize("tool_name", (
    "mcp__calendar__create_event",
    "mcp__database__update_record",
    "mcp__memory__save_checkpoint",
))
def test_non_file_mcp_mutator_names_are_noop_if_sent_accidentally(tool_name):
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "codex-s1",
        "tool_name": tool_name,
        "tool_input": {"title": "not a file mutation"},
    })
    assert g.gate_edit_cli(hook) == 0


def test_gate_bash_schema_drift_is_explicit_best_effort_noop():
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "codex-s1",
        "tool_name": "Bash",
        "tool_input": ["schema", "drift"],
    })
    assert g.gate_bash_cli(hook) == 0


def test_codex_apply_patch_hook_matcher_registered_and_anchored():
    hooks_path = Path(__file__).resolve().parent.parent / "plugins" / "gates" / "hooks" / "hooks.json"
    config = json.loads(hooks_path.read_text())
    matchers = [entry.get("matcher", "") for entry in config["hooks"]["PreToolUse"]]
    matcher = next(matcher for matcher in matchers if "apply" in matcher)
    assert "(?i" not in matcher   # hook consumers are not guaranteed modern V8 regex modifiers
    import re
    compiled = re.compile(matcher)
    for tool_name in ("Edit", "MultiEdit", "EditV2", "Write", "WriteV2", "NotebookEdit",
                      "apply_patch", "apply_patch_v2", "mcp__x__apply_patch",
                      "mcp__filesystem__EditFile", "mcp__filesystem__WriteFile",
                      "mcp__filesystem__edit_file", "mcp__filesystem__write_file",
                      "mcp__filesystem__create_file", "mcp__filesystem__update_file",
                      "mcp__filesystem__patch_file", "mcp__editor__str_replace_editor",
                      "mcp__editor__str_replace_based_edit_tool",
                      "mcp__filesystem__replace_in_file", "mcp__filesystem__save_file",
                      "mcp__filesystem__delete_file", "mcp__filesystem__remove_file",
                      "mcp__filesystem__move_file", "mcp__filesystem__rename_file",
                      "mcp__filesystem__append_file", "mcp__filesystem__copy_file",
                      "mcp__filesystem__truncate_file"):
        assert compiled.fullmatch(tool_name), tool_name
    assert compiled.fullmatch("TodoWrite") is None
    assert compiled.fullmatch("mcp__calendar__create_event") is None
    assert compiled.fullmatch("mcp__database__update_record") is None
    assert compiled.fullmatch("mcp__memory__save_checkpoint") is None


@pytest.mark.parametrize("tool_name", (
    "mcp__filesystem__write_file",
    "mcp__filesystem__edit_file",
    "mcp__filesystem__WriteFile",
    "mcp__filesystem__create_file",
    "mcp__filesystem__update_file",
    "mcp__editor__str_replace_editor",
    "mcp__editor__str_replace_based_edit_tool",
    "mcp__filesystem__replace_in_file",
    "mcp__filesystem__save_file",
))
def test_mcp_file_path_payload_enters_real_edit_gate(tool_name):
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "mcp-s1",
        "tool_name": tool_name,
        "tool_input": {"path": "app/x.py", "content": "x = 1"},
    })
    assert g.gate_edit_cli(hook) == 2


def test_mcp_file_tool_unknown_payload_fails_closed(capsys):
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "mcp-s1",
        "tool_name": "mcp__filesystem__write_file",
        "tool_input": {"filepath_v2": "app/x.py"},
    })
    assert g.gate_edit_cli(hook) == 2
    assert "заблокирована" in capsys.readouterr().err


def test_routed_file_symlink_outside_repo_blocks(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    repo.mkdir()
    outside.mkdir()
    (repo / "escape").symlink_to(outside, target_is_directory=True)
    monkeypatch.setattr(g, "REPO_ROOT", repo)
    hook = json.dumps({
        "cwd": str(repo),
        "session_id": "mcp-s1",
        "tool_name": "mcp__filesystem__write_file",
        "tool_input": {"path": "escape/x.py", "content": "x = 1"},
    })
    assert g.gate_edit_cli(hook) == 2


def test_explicit_external_file_target_remains_opt_in_noop(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    outside = tmp_path / "scratch.py"
    repo.mkdir()
    monkeypatch.setattr(g, "REPO_ROOT", repo)
    hook = json.dumps({
        "cwd": str(repo),
        "session_id": "mcp-s1",
        "tool_name": "mcp__filesystem__write_file",
        "tool_input": {"path": str(outside), "content": "x = 1"},
    })
    assert g.gate_edit_cli(hook) == 0


def test_mcp_move_checks_source_and_destination():
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "mcp-s1",
        "tool_name": "mcp__filesystem__move_file",
        "tool_input": {"source": "docs/safe.md", "destination": "app/hidden.py"},
    })
    assert g.gate_edit_cli(hook) == 2


def test_mcp_unknown_camelcase_destination_key_fails_closed(capsys):
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "mcp-s1",
        "tool_name": "mcp__filesystem__move_file",
        "tool_input": {"path": "docs/safe.md", "futureDestinationPath": "app/hidden.py"},
    })
    assert g.gate_edit_cli(hook) == 2
    assert "path-like key" in capsys.readouterr().err


@pytest.mark.parametrize("unknown_key", (
    "destpath", "filepath", "newpath", "paths", "outputDir", "output_dir",
    "directory", "folder", "URIPath", "newDestination", "move_destination",
    "new_target", "copy_dst",
))
def test_mcp_unrecognized_pathlike_aliases_fail_closed(unknown_key):
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "mcp-s1",
        "tool_name": "mcp__filesystem__move_file",
        "tool_input": {"path": "docs/safe.md", unknown_key: "app/hidden.py"},
    })
    assert g.gate_edit_cli(hook) == 2


def test_mcp_delete_code_path_enters_gate():
    hook = json.dumps({
        "cwd": str(g.REPO_ROOT),
        "session_id": "mcp-s1",
        "tool_name": "mcp__filesystem__delete_file",
        "tool_input": {"path": "app/x.py"},
    })
    assert g.gate_edit_cli(hook) == 2


def test_codex_plugin_manifest_and_marketplace_point_to_same_plugin():
    root = Path(__file__).resolve().parent.parent
    manifest = json.loads(
        (root / "plugins" / "gates" / ".codex-plugin" / "plugin.json").read_text())
    marketplace = json.loads(
        (root / ".agents" / "plugins" / "marketplace.json").read_text())
    assert marketplace["plugins"]
    assert all(entry["policy"]["installation"] == "AVAILABLE"
               for entry in marketplace["plugins"])
    entry = marketplace["plugins"][0]
    assert manifest["name"] == "gates"
    assert entry["name"] == manifest["name"]
    assert entry["source"] == {"source": "local", "path": "./plugins/gates"}
    assert entry["policy"] == {
        "installation": "AVAILABLE",
        "authentication": "ON_INSTALL",
    }
    plugin_root = root / entry["source"]["path"]
    assert "hooks" not in manifest  # validator текущего Codex отвергает это поле
    assert (plugin_root / "hooks" / "hooks.json").is_file()


def test_gate_edit_codex_apply_patch_subprocess_enters_real_cli(repo):
    (repo / ".codex-gate.yaml").write_text("code_paths:\n  prefixes: [app/]\n")
    (repo / "app").mkdir()
    script = (Path(__file__).resolve().parent.parent / "plugins" / "gates" / "scripts"
              / "codex_review_gate.py")
    hook = json.dumps({
        "cwd": str(repo),
        "session_id": "codex-live-s1",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Add File: app/live.py\n"
                "+x = 1\n"
                "*** End Patch\n"
            ),
        },
    })
    result = subprocess.run(
        ["python3", str(script), "gate-edit"],
        cwd=repo,
        input=hook,
        text=True,
        capture_output=True,
        env={"PATH": os.environ["PATH"]},
    )
    assert result.returncode == 2
    assert "Дизайн-ревью не пройдено" in result.stderr


def test_gate_edit_codex_payload_cwd_selects_repo_when_process_cwd_is_outside(repo, tmp_path):
    (repo / ".codex-gate.yaml").write_text("code_paths:\n  prefixes: [app/]\n")
    (repo / "app").mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    script = (Path(__file__).resolve().parent.parent / "plugins" / "gates" / "scripts"
              / "codex_review_gate.py")
    hook = json.dumps({
        "cwd": str(repo),
        "session_id": "codex-live-s2",
        "tool_name": "apply_patch",
        "tool_input": {
            "command": (
                "*** Begin Patch\n"
                "*** Add File: app/live.py\n"
                "+x = 1\n"
                "*** End Patch\n"
            ),
        },
    })
    result = subprocess.run(
        ["python3", str(script), "gate-edit"],
        cwd=outside,
        input=hook,
        text=True,
        capture_output=True,
        env={"PATH": os.environ["PATH"]},
    )
    assert result.returncode == 2
    assert "Дизайн-ревью не пройдено" in result.stderr


def test_set_hook_repo_context_updates_all_repo_derived_paths(repo):
    (repo / ".codex-gate.yaml").write_text("code_paths:\n  prefixes: [app/]\n")
    g._set_hook_repo_context(repo)
    assert g.REPO_ROOT == repo.resolve()
    assert g.AUDIT_LOG == repo / "logs" / "codex_review_audit.log"
    assert g.DESIGN_MARKER == repo / ".claude" / ".design-approved"
    assert g.LEDGER_DIR == repo / "logs" / "review_ledger"
    assert g.LAST_DEPLOYED == repo / ".claude" / ".last-deployed-sha"
    assert g.LAST_REVIEWED == repo / ".claude" / ".last-reviewed-sha"
    assert g.DEPLOY_PIN == repo / ".claude" / ".deploy-section-pin"
    assert g.FINDINGS_DIR == repo / "logs" / "review_findings"
    assert g.VERDICT_DIR == repo / "logs" / "review_verdicts"


def test_explicit_gate_requires_repo(monkeypatch):
    monkeypatch.setattr(g, "REPO_ROOT", None)
    assert g.check_reviewed_cli() == 2                    # fail-closed, не traceback
    assert g.main(["check-decision"]) == 2
    assert g.main(["findings"]) == 2


# --- эпоха лесенки из конфига root'а (решение 2) ---

def test_effective_epoch_reads_config(tmp_path):
    assert lg._effective_epoch(tmp_path) is None                       # нет конфига → выключена
    (tmp_path / ".codex-gate.yaml").write_text("ladder:\n  epoch_sha: abc123\n")
    assert lg._effective_epoch(tmp_path) == "abc123"
    (tmp_path / ".codex-gate.yaml").write_text("ladder:\n  epoch_sha: ''\n")
    assert lg._effective_epoch(tmp_path) is None                       # пустая строка = нет


def test_effective_epoch_override_wins(tmp_path, monkeypatch):
    (tmp_path / ".codex-gate.yaml").write_text("ladder:\n  epoch_sha: from-config\n")
    monkeypatch.setattr(lg, "LADDER_EPOCH_SHA", "override")
    assert lg._effective_epoch(tmp_path) == "override"


def test_check_range_epoch_from_config_file(repo, capsys):
    # эпоха, записанная gates-init в конфиг, реально grandfather-ит до-эпоховую историю
    baseline = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                              text=True, check=True).stdout.strip()
    (repo / "app").mkdir()
    (repo / "app" / "x.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "pre-epoch code")
    epoch = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                           text=True, check=True).stdout.strip()
    (repo / ".codex-gate.yaml").write_text(f"ladder:\n  epoch_sha: {epoch}\n")
    assert lg.check_range(repo, baseline) == 0            # всё до эпохи покрыто
    (repo / "app" / "x.py").write_text("x = 2\n")
    _git(repo, "add", "app/x.py")
    _git(repo, "commit", "-m", "post-epoch code")         # без лесенки и записи
    assert lg.check_range(repo, baseline) == 2            # пост-эпоховый — блок


# --- Codex code-R1: лаундеринг незастейдженным конфигом + битые формы конфига ---

def test_precommit_dirty_config_not_exempt(repo, monkeypatch):
    # незастейдженное ослабление .codex-gate.yaml НЕ превращает код-коммит в exempt:
    # классификация «не-код» (сымитирована пустыми prefixes) не даёт 0 при dirty-конфиге
    (repo / ".codex-gate.yaml").write_text('code_paths:\n  prefixes: ["app/"]\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "onboard")
    (repo / "app").mkdir()
    (repo / "app" / "x.py").write_text("x = 1\n")
    _git(repo, "add", "app/x.py")
    (repo / ".codex-gate.yaml").write_text("code_paths:\n  prefixes: []\n")   # worktree, НЕ staged
    monkeypatch.setattr(g, "CODE_PATH_PREFIXES", ())    # import-time чтение ослабленного конфига
    monkeypatch.setattr(g, "CODE_PATH_EXACT", set())
    assert lg.check_precommit(repo) == 2                # НЕ exempt — цепочка требуется


def test_precommit_committed_config_weakening_is_visible_channel(repo, monkeypatch):
    # застейдженная правка конфига — легитимный канал (видна деплой-ревью): exempt работает
    (repo / ".codex-gate.yaml").write_text("code_paths:\n  prefixes: []\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "onboard weak")
    (repo / "notes.txt").write_text("n\n")
    _git(repo, "add", "notes.txt")
    monkeypatch.setattr(g, "CODE_PATH_PREFIXES", ())
    monkeypatch.setattr(g, "CODE_PATH_EXACT", set())
    assert lg.check_precommit(repo) == 0                # конфиг чист (worktree == index)


def test_record_commit_dirty_config_no_exempt_record(repo, monkeypatch, capsys):
    (repo / ".codex-gate.yaml").write_text('code_paths:\n  prefixes: ["app/"]\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "onboard")
    (repo / "app").mkdir()
    (repo / "app" / "x.py").write_text("x = 1\n")
    _git(repo, "add", "app/x.py")
    _git(repo, "commit", "-m", "code commit")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()
    (repo / ".codex-gate.yaml").write_text("code_paths:\n  prefixes: []\n")   # worktree-ослабление
    monkeypatch.setattr(g, "CODE_PATH_PREFIXES", ())
    monkeypatch.setattr(g, "CODE_PATH_EXACT", set())
    lg.record_commit(repo)
    assert lg.read_ledger(repo, head) is None           # exempt-запись НЕ отчеканена (fail-closed)


def test_record_commit_clean_config_exempt_record_still_works(repo):
    # регресс: недирти-конфиг не ломает штатный exempt-noncode путь
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "docs")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()
    lg.record_commit(repo)
    assert lg.read_ledger(repo, head)["passes"] == ["exempt-noncode"]


def test_symlink_config_is_strict(tmp_path):
    target = tmp_path / "elsewhere.yaml"
    target.write_text("code_paths:\n  prefixes: []\n")
    (tmp_path / ".codex-gate.yaml").symlink_to(target)
    assert g._read_gate_config(tmp_path) is None        # симлинк = битый → строгий режим
    assert lg._gate_config(tmp_path) is None


def test_non_utf8_config_is_strict_not_crash(tmp_path):
    (tmp_path / ".codex-gate.yaml").write_bytes(b"\xff\xfe\x00broken")
    assert g._read_gate_config(tmp_path) is None        # UnicodeError → строгий, не traceback
    assert lg._gate_config(tmp_path) is None


def test_precommit_dirty_enabled_false_not_honored(repo, monkeypatch):
    # Codex code-R2: незастейдженный enabled:false НЕ гасит pre-commit (без skip-аудита)
    (repo / ".codex-gate.yaml").write_text('code_paths:\n  prefixes: ["app/"]\nladder:\n  enabled: true\n')
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "onboard")
    (repo / "app").mkdir()
    (repo / "app" / "x.py").write_text("x = 1\n")
    _git(repo, "add", "app/x.py")
    (repo / ".codex-gate.yaml").write_text('code_paths:\n  prefixes: ["app/"]\nladder:\n  enabled: false\n')
    assert lg.check_precommit(repo) == 2                # dirty enabled=false игнорируется
