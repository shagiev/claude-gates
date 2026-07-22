"""Эмпирический гейт деплоя (тикет #1, спека docs/2026-07-22-empirical-gate-design.md).
Тест-матрица S1–S15 + юниты _empirical_config (трёхстатусно) / _run_empirical.
Изоляция: tmp git-репо (как test_ladder_gate); git_head/working_tree_clean монипатчатся —
они бьют по cwd процесса, не по tmp-репо."""
import os
import subprocess
from pathlib import Path

import pytest

import codex_review_gate as g


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": os.environ["PATH"], "HOME": str(repo.parent)})


def _sha(repo, ref="HEAD"):
    return subprocess.run(["git", "rev-parse", ref], cwd=repo, capture_output=True,
                          text=True, check=True).stdout.strip()


def _commit_config(repo, content, msg):
    """Записать .codex-gate.yaml (content=None → удалить файл) и закоммитить. Вернуть SHA."""
    p = repo / ".codex-gate.yaml"
    if content is None:
        if p.exists():
            p.unlink()
        (repo / f"tick-{msg}.txt").write_text(msg)   # непустой коммит
    else:
        p.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", msg)
    return _sha(repo)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-b", "main")
    (r / "seed.txt").write_text("seed\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "seed")
    return r


_ENABLED = "empirical:\n  test_command: \"{cmd}\"\n"
_ENABLED_TO = "empirical:\n  test_command: \"{cmd}\"\n  timeout_s: {to}\n"


# ═══ юниты _empirical_config: три состояния (EARS-8/9, R4-F1) ═══

def test_config_absent_no_file(repo):
    assert g._empirical_config(repo, "HEAD")[0] == "absent"          # файла нет — доказано


def test_config_absent_no_section(repo):
    _commit_config(repo, "code_paths:\n  prefixes: []\n", "cfg-no-emp")
    assert g._empirical_config(repo, "HEAD")[0] == "absent"          # файл есть, секции нет


def test_config_absent_section_without_command(repo):
    _commit_config(repo, "empirical:\n  timeout_s: 10\n", "cfg-emp-nocmd")
    assert g._empirical_config(repo, "HEAD")[0] == "absent"          # секция без валидной команды


def test_config_enabled(repo):
    _commit_config(repo, _ENABLED.format(cmd="true"), "cfg-emp")
    state, cmd, timeout = g._empirical_config(repo, "HEAD")
    assert state == "enabled" and cmd == "true" and timeout == 600


def test_config_timeout_valid_and_invalid(repo):
    _commit_config(repo, _ENABLED_TO.format(cmd="true", to=30), "cfg-to")
    assert g._empirical_config(repo, "HEAD")[2] == 30
    _commit_config(repo, "empirical:\n  test_command: \"true\"\n  timeout_s: -5\n", "cfg-to-bad")
    assert g._empirical_config(repo, "HEAD")[2] == 600               # невалидный → дефолт (S9)


def test_config_unreadable_broken_yaml(repo):
    _commit_config(repo, "empirical:\n  test_command: [unclosed\n", "cfg-broken")
    assert g._empirical_config(repo, "HEAD")[0] == "unreadable"      # S7b


def test_config_unreadable_no_pyyaml(repo, monkeypatch):
    _commit_config(repo, _ENABLED.format(cmd="true"), "cfg-emp")
    monkeypatch.setattr(g, "yaml", None)                             # нет PyYAML при наличии файла
    assert g._empirical_config(repo, "HEAD")[0] == "unreadable"      # S7b


def test_config_unreadable_bad_ref(repo):
    # ls-tree по несуществующему ref падает → unreadable, НЕ absent (R4-F1, git-сбой ≠ «чисто»)
    assert g._empirical_config(repo, "deadbeef" * 5)[0] == "unreadable"   # S14


# ═══ юниты _run_empirical ═══

def test_run_pass_fail(tmp_path):
    # argv-исполнение (shell=False): реальные бинарники, не shell-builtins
    assert g._run_empirical("true", 10, tmp_path)[0] == "pass"
    assert g._run_empirical("false", 10, tmp_path)[0] == "fail"


def test_run_missing_command_blocks(tmp_path):
    # команда не найдена → FileNotFoundError → 'error' (не 'pass'); гейт блокирует (S4)
    assert g._run_empirical("definitely-not-a-real-cmd-xyz", 10, tmp_path)[0] == "error"


def test_run_no_shell_metachars(tmp_path):
    # defense-in-depth: метасимволы НЕ интерпретируются (shlex.split + shell=False).
    # 'echo hi > /tmp/x' с shell распарсился бы в редирект; как argv — echo печатает литералы,
    # файл не создаётся. Проверяем, что редирект-побочки нет.
    marker = tmp_path / "pwned"
    g._run_empirical(f"echo hi > {marker}", 10, tmp_path)
    assert not marker.exists()   # редирект не сработал — shell не задействован


def test_run_timeout(tmp_path):
    assert g._run_empirical("sleep 5", 1, tmp_path)[0] == "timeout"  # S5


# ═══ _empirical_gate: решение (S1–S8b, S12/S13). REPO_ROOT + git_head/clean монипатчатся ═══

def _gate_env(monkeypatch, repo, head_sha, clean=True, live_head=None):
    monkeypatch.setattr(g, "REPO_ROOT", repo)
    monkeypatch.setattr(g, "git_head", lambda: live_head or head_sha)   # без race == head_sha
    monkeypatch.setattr(g, "working_tree_clean", lambda: clean)


def test_gate_s1_absent_both_skips(repo, monkeypatch):
    base = _sha(repo)                                    # seed: файла нет
    head = _commit_config(repo, None, "still-no-cfg")    # тоже нет
    _gate_env(monkeypatch, repo, head)
    assert g._empirical_gate(base, head) == 0            # absent/absent → скип


def test_gate_s2_enabled_pass(repo, monkeypatch):
    base = _sha(repo)
    head = _commit_config(repo, _ENABLED.format(cmd="true"), "emp-on")
    _gate_env(monkeypatch, repo, head)
    assert g._empirical_gate(base, head) == 0            # тесты зелёные → дальше


def test_gate_s3_enabled_fail_blocks(repo, monkeypatch):
    base = _sha(repo)
    head = _commit_config(repo, _ENABLED.format(cmd="false"), "emp-fail")
    _gate_env(monkeypatch, repo, head)
    assert g._empirical_gate(base, head) == 2            # падение → блок (денежный fail-closed)


def test_gate_s4_unrunnable_blocks(repo, monkeypatch):
    base = _sha(repo)
    head = _commit_config(repo, _ENABLED.format(cmd="no-such-cmd-xyz"), "emp-badcmd")
    _gate_env(monkeypatch, repo, head)
    assert g._empirical_gate(base, head) == 2            # «не запустилось» ≠ «прошло»


def test_gate_s5_timeout_blocks(repo, monkeypatch):
    base = _sha(repo)
    head = _commit_config(repo, _ENABLED_TO.format(cmd="sleep 5", to=1), "emp-hang")
    _gate_env(monkeypatch, repo, head)
    assert g._empirical_gate(base, head) == 2            # таймаут → блок


def test_gate_s7_removed_after_enabled_blocks(repo, monkeypatch):
    base = _commit_config(repo, _ENABLED.format(cmd="true"), "emp-on")   # base: включён
    head = _commit_config(repo, "code_paths:\n  prefixes: []\n", "emp-removed")  # head: снят
    _gate_env(monkeypatch, repo, head)
    assert g._empirical_gate(base, head) == 2            # снятие гейта без EMPIRICAL_SKIP → блок


def test_gate_s16_command_change_blocks(repo, monkeypatch):   # Codex code-R1 (ML-E2 замена)
    base = _commit_config(repo, _ENABLED.format(cmd="false"), "emp-pytest")   # base: строгая
    head = _commit_config(repo, _ENABLED.format(cmd="true"), "emp-weakened")  # head: no-op
    _gate_env(monkeypatch, repo, head)
    assert g._empirical_gate(base, head) == 2   # смена команды без EMPIRICAL_SKIP → блок


def test_gate_same_command_runs(repo, monkeypatch):   # смена нет → команда бежит (регресс)
    base = _commit_config(repo, _ENABLED.format(cmd="true"), "emp-on")
    head = _commit_config(repo, _ENABLED_TO.format(cmd="true", to=30), "emp-on-touch")  # cmd тот же
    _gate_env(monkeypatch, repo, head)
    assert g._empirical_gate(base, head) == 0   # команда та же (изменился лишь timeout) → бежит, pass


def test_gate_enable_from_absent_runs(repo, monkeypatch):   # base=absent → включение, не смена
    base = _sha(repo)                                        # файла нет
    head = _commit_config(repo, _ENABLED.format(cmd="true"), "emp-enable")
    _gate_env(monkeypatch, repo, head)
    assert g._empirical_gate(base, head) == 0   # ВКЛючение гейта (не ослабление) → бежит


def test_gate_s7b_head_unreadable_blocks(repo, monkeypatch):
    base = _sha(repo)
    head = _commit_config(repo, "empirical:\n  test_command: [unclosed\n", "emp-broken")
    _gate_env(monkeypatch, repo, head)
    assert g._empirical_gate(base, head) == 2            # HEAD unreadable → блок


def test_gate_s8b_base_unreadable_blocks(repo, monkeypatch):
    base = _commit_config(repo, "empirical: [unclosed\n", "base-broken")   # base: unreadable
    head = _commit_config(repo, None, "head-absent")                       # head: absent
    _gate_env(monkeypatch, repo, head)
    assert g._empirical_gate(base, head) == 2            # base мог быть enabled — не доказать


def test_gate_s12_head_race_blocks(repo, monkeypatch):
    base = _sha(repo)
    head = _commit_config(repo, _ENABLED.format(cmd="true"), "emp-on")
    _gate_env(monkeypatch, repo, head, live_head="0" * 40)   # HEAD «уехал» за прогон
    assert g._empirical_gate(base, head) == 2            # тест не для задеплоенного SHA


def test_gate_s13_tree_race_blocks(repo, monkeypatch):
    base = _sha(repo)
    head = _commit_config(repo, _ENABLED.format(cmd="true"), "emp-on")
    _gate_env(monkeypatch, repo, head, clean=False)     # дерево загрязнилось за прогон
    assert g._empirical_gate(base, head) == 2


# ═══ интеграция через check_reviewed_cli (S6, S10 ordering, S11 независимость) ═══
# Как существующие check_reviewed-тесты: реальный cwd-репо для git-операций (ancestry по cwd,
# не по REPO_ROOT), resolve_baseline→"HEAD~1", _empirical_config застабан (tmp-репо не нужен).

def test_s6_empirical_skip_audited(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(g, "LAST_REVIEWED", tmp_path / ".lr")
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "resolve_baseline", lambda: "HEAD~1")
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 0)
    monkeypatch.setenv("EMPIRICAL_SKIP", "1")
    monkeypatch.setenv("CODEX_REVIEW_SKIP", "1")            # чтобы не гнать реальный Codex
    assert g.check_reviewed_cli() == 0                      # эмпирика скипнута, деплой едет
    audit = (tmp_path / "audit.log").read_text()
    assert "EMPIRICAL_SKIP" in audit and "CODEX_REVIEW_SKIP" in audit


def test_s10_ordering_empirical_fail_before_codex(tmp_path, monkeypatch, capsys):
    # эмпирика падает → Codex НЕ вызывается (companion-стаб, кричащий при вызове, не тронут)
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "resolve_baseline", lambda: "HEAD~1")
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 0)
    monkeypatch.setattr(g, "_empirical_config",
                        lambda root, ref: ("enabled", "false", 600))   # тесты падают
    monkeypatch.setenv("CODEX_COMPANION_CMD", "bash -c 'echo SHOULD-NOT-RUN >&2; exit 1'")
    assert g.check_reviewed_cli() == 2                      # блок на эмпирике
    assert "SHOULD-NOT-RUN" not in capsys.readouterr().err  # companion не вызван (ordering)


def test_s11_codex_skip_does_not_bypass_empirical(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "resolve_baseline", lambda: "HEAD~1")
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 0)
    monkeypatch.setattr(g, "_empirical_config",
                        lambda root, ref: ("enabled", "false", 600))   # тесты падают
    monkeypatch.setenv("CODEX_REVIEW_SKIP", "1")            # Codex скипнут, но эмпирика — нет
    assert g.check_reviewed_cli() == 2                      # эмпирика независима от Codex-скипа
