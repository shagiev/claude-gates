"""Ф2 сессионное выключение ревьюера (спека docs/2026-07-25-reviewer-provider-switch-design.md).
Матрица S1-S12: громкий баннер, обязательная причина, G1 пропускает / деплой блокирует,
проверка ДО кэш-ветки, авто-истечение по сессии, аварийный контур поверх выключателя."""
import json

import pytest

import codex_review_gate as g


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(g, "ONBOARDED", True)
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    return tmp_path


def _disable(reason="квота Codex исчерпана"):
    return g.main(["review-disable", reason])


# ═══ S1/S2/S7: CLI, обязательная причина, включение обратно ═══

def test_s1_disable_writes_marker_and_audits(env, capsys):
    assert _disable() == 0
    assert g.review_disabled_reason("s1") == "квота Codex исчерпана"
    audit = (env / "audit.log").read_text()
    assert "review-disable" in audit and "квота Codex" in audit
    assert "ВЫКЛЮЧЕН" in capsys.readouterr().err            # громкий баннер


def test_s2_disable_without_reason_rejected(env):
    assert g.main(["review-disable"]) == 1
    assert g.main(["review-disable", "   "]) == 1
    assert g.review_disabled_reason("s1") is None            # маркер НЕ записан


def test_s7_enable_removes_and_audits(env):
    _disable()
    assert g.main(["review-enable"]) == 0
    assert g.review_disabled_reason("s1") is None
    assert "review-enable" in (env / "audit.log").read_text()


def test_review_status(env, capsys):
    assert g.main(["review-status"]) == 0
    assert "включён" in capsys.readouterr().out
    _disable("причина X")
    capsys.readouterr()
    assert g.main(["review-status"]) == 0
    assert "ВЫКЛЮЧЕН" in capsys.readouterr().out


# ═══ S3: G1 пропускает с ГРОМКИМ баннером (дизайн-гейт fail-open by design) ═══

def test_s3_g1_passes_with_loud_banner(env, capsys):
    _disable()
    capsys.readouterr()
    hook = json.dumps({"session_id": "s1", "tool_input": {"file_path": "app/x.py"}})
    assert g.gate_edit_cli(hook) == 0                        # правка кода пропущена
    err = capsys.readouterr().err
    assert "РЕВЬЮЕР ВЫКЛЮЧЕН" in err and "review-enable" in err
    # и bash-гейт тоже
    bash_hook = json.dumps({"session_id": "s1",
                            "tool_input": {"command": "sed -i s/a/b/ app/x.py"}})
    assert g.gate_bash_cli(bash_hook) == 0


# ═══ S4/S12: деплой блокируется, в т.ч. при ВАЛИДНОМ кэше (проверка ДО кэш-ветки) ═══

def _deploy_env(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(g, "LAST_REVIEWED", tmp_path / ".lr")
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "_resolve_baseline_gate", lambda head: ("HEAD~1", 0))
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 0)
    monkeypatch.setattr(g, "_empirical_gate", lambda baseline, head: 0)


def test_s4_deploy_blocked_with_two_exits(env, tmp_path, monkeypatch, capsys):
    _deploy_env(monkeypatch, tmp_path)
    _disable()
    capsys.readouterr()
    assert g.check_reviewed_cli() == 2
    err = capsys.readouterr().err
    assert "review-enable" in err and "CODEX_REVIEW_SKIP=1" in err
    assert "Смена провайдера выключение НЕ обходит" in err   # без ложного третьего выхода


def test_s12_valid_cache_does_not_bypass_disable(env, tmp_path, monkeypatch):
    # ЦЕНТРАЛЬНЫЙ сценарий R2-F3: ранний allow по кэшу шёл раньше проверки маркера
    _deploy_env(monkeypatch, tmp_path)
    head, diff = g.git_head(), g.diff_sha256("HEAD~1", g.git_head())
    g.write_ledger(head, diff, "HEAD~1",
                   g.parse_review_output("Verdict: approve\nNo material findings.\n"))
    assert g.check_reviewed_cli() == 0                       # без выключателя кэш пускает
    _disable()
    assert g.check_reviewed_cli() == 2                       # с выключателем — блок


def test_s5_check_decision_blocked(env, tmp_path, monkeypatch):
    monkeypatch.setattr(g, "FINDINGS_DIR", tmp_path / "rf")
    _disable()
    assert g.main(["check-decision"]) == 2


# ═══ S8/S9/S10: авто-истечение, аварийный контур, неизвестная сессия ═══

def test_s8_other_session_not_affected(env):
    _disable()
    assert g.review_disabled_reason("s1") is not None
    assert g.review_disabled_reason("другая-сессия") is None   # пер-сессионность


def test_s9_emergency_skip_works_over_disable(env, tmp_path, monkeypatch):
    _deploy_env(monkeypatch, tmp_path)
    monkeypatch.setattr(g, "VERDICT_DIR", tmp_path / "verdicts")
    monkeypatch.setattr(g, "_ladder_range_skips", lambda baseline: [])
    monkeypatch.setattr(g, "_empirical_config", lambda root, ref: ("absent", None, 600))
    _disable()
    monkeypatch.setenv("CODEX_REVIEW_SKIP", "1")
    assert g.check_reviewed_cli() == 0                       # аварийный контур жив
    assert "CODEX_REVIEW_SKIP" in (env / "audit.log").read_text()


def test_s10_unknown_session_rejected(env, monkeypatch):
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert g.main(["review-disable", "причина"]) == 2
    assert g.main(["review-enable"]) == 2


def test_disable_reason_redacted(env):
    _disable("временно, api_key=SUPERSECRETVALUE9")
    assert "SUPERSECRETVALUE9" not in g.review_disabled_reason("s1")
    assert "SUPERSECRETVALUE9" not in (env / "audit.log").read_text()
