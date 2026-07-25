"""Деградация при исчерпании Codex-квоты (живая проверка 2026-07-25 → постоянные регрессы).
Четыре реальные формы отказа companion; инварианты: (1) деплой fail-closed; (2) раунды
протокола сходимости НЕ сгорают на outage; (3) причина (usage limit) ВИДНА оператору,
не маскируется «дрейфом схемы»; (4) кэш/вердикт не пишутся."""
import json

import pytest

import codex_review_gate as g

_ENV_B = json.dumps({"review": "Adversarial Review",
                     "codex": {"status": 1,
                               "stderr": "stream error: You have hit your usage limit. Try again at 3pm.",
                               "stdout": ""},
                     "result": None, "parseError": None, "rawOutput": ""})
_ENV_C = json.dumps({"review": "Adversarial Review",
                     "codex": {"status": 0, "stderr": "",
                               "stdout": "You have hit your usage limit for GPT. Resets at 15:00."},
                     "result": None, "parseError": "model did not return valid JSON",
                     "rawOutput": "You have hit your usage limit for GPT."})
_TEXT_D = "You have hit your usage limit. Your limit resets at 15:00."


@pytest.fixture()
def gate_env(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(g, "LAST_REVIEWED", tmp_path / ".lr")
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "_resolve_baseline_gate", lambda head: ("HEAD~1", 0))
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 0)
    monkeypatch.setattr(g, "_empirical_gate", lambda baseline, head: 0)
    return tmp_path


def _stub_output(monkeypatch, payload):
    monkeypatch.setattr(g, "run_companion_review", lambda base, scope: payload)


def _rounds():
    led = g.load_findings_ledger("HEAD~1")
    return led.get("rounds") if led else None


# ═══ юниты outage_details: причина извлекается из всех форм ═══

def test_outage_details_extracts_limit_reason():
    assert "usage limit" in g.outage_details(_ENV_B)          # из codex.stderr
    assert "usage limit" in g.outage_details(_ENV_C)          # из codex.stdout+parseError
    assert "parseError" in g.outage_details(_ENV_C)
    assert "usage limit" in g.outage_details(_TEXT_D)         # сырой текст как есть
    assert g.outage_details(None) == "" and g.outage_details("") == ""


def test_outage_details_bounded():
    assert len(g.outage_details("x" * 5000)) <= 300           # хвост ограничен
    huge = json.dumps({"codex": {"status": 1, "stderr": "e" * 5000}})
    assert len(g.outage_details(huge)) <= 400


# ═══ формы A–D через check_reviewed_cli: fail-closed + rounds целы + причина видна ═══

def test_form_a_companion_nonzero_fail_closed(gate_env, monkeypatch, capsys):
    monkeypatch.setenv("CODEX_COMPANION_CMD", "bash -c 'echo Usage limit reached >&2; exit 1'")
    assert g.check_reviewed_cli() == 2
    err = capsys.readouterr().err
    assert "Usage limit reached" in err                       # stderr companion виден
    assert "kill-switch" in err                               # аварийный контур ML6
    assert _rounds() == 0                                     # раунды НЕ сгорели


def test_form_b_envelope_cli_failed(gate_env, monkeypatch, capsys):
    _stub_output(monkeypatch, _ENV_B)
    assert g.check_reviewed_cli() == 2
    err = capsys.readouterr().err
    assert "usage limit" in err                               # причина видна (было: «дрейф схемы»)
    assert _rounds() == 0


def test_form_c_parse_error(gate_env, monkeypatch, capsys):
    _stub_output(monkeypatch, _ENV_C)
    assert g.check_reviewed_cli() == 2
    err = capsys.readouterr().err
    assert "usage limit" in err
    assert _rounds() == 0


def test_form_d_raw_text(gate_env, monkeypatch, capsys):
    _stub_output(monkeypatch, _TEXT_D)
    assert g.check_reviewed_cli() == 2
    assert "usage limit" in capsys.readouterr().err
    assert _rounds() == 0


def test_outage_writes_no_cache_no_verdict(gate_env, monkeypatch):
    _stub_output(monkeypatch, _ENV_C)
    assert g.check_reviewed_cli() == 2
    assert not list((gate_env / "ledger").glob("*.json")) if (gate_env / "ledger").exists() else True
    assert not (g.VERDICT_DIR.exists() and list(g.VERDICT_DIR.glob("*.json")))
    assert not g.LAST_REVIEWED.exists()                       # SHA не одобрен


def test_valid_review_message_unaffected(gate_env, monkeypatch, capsys):
    # регресс: валидный блокирующий вердикт НЕ получает outage-хвост (другая ветка)
    import pathlib
    fix = pathlib.Path(__file__).parent / "fixtures" / "stub_companion_block.sh"
    monkeypatch.setenv("CODEX_COMPANION_CMD", f"bash {fix}")
    assert g.check_reviewed_cli() == 2                        # блок по находке
    assert "невалидный вывод" not in capsys.readouterr().err


def test_live_quota_fixture_2026_07_25(gate_env, monkeypatch, capsys):
    # ЖИВОЙ артефакт: во время самой проверки quota-деградации у Codex реально кончилась
    # квота (2026-07-25). Реальная форма = B+C сразу: codex.status=1 И parseError с текстом
    # лимита. Фикстура усечена из живого envelope (tests/fixtures/codex_quota_live.json).
    import pathlib
    live = (pathlib.Path(__file__).parent / "fixtures" / "codex_quota_live.json").read_text()
    v = g.parse_review_output(live)
    assert v.valid is False                                   # fail-closed
    details = g.outage_details(live)
    assert "usage limit" in details.lower() and "exit=1" in details
    _stub_output(monkeypatch, live)
    assert g.check_reviewed_cli() == 2
    assert "usage limit" in capsys.readouterr().err.lower()   # оператор видит причину
    assert _rounds() == 0                                     # раунды не сгорели
