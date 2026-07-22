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
