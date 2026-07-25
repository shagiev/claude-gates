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


# ═══ Редакция секретов в operator-facing выводе (ревью 25.07: security-класс конституции) ═══

def test_redact_secret_classes():
    cases = {
        "Authorization: Bearer sk-abc123def456ghi789jkl": "sk-abc123",
        "api_key=AKIAIOSFODNN7EXAMPLE tail": "AKIAIOSFODNN7EXAMPLE",
        "postgres://user:supersecret@db:5432/x": "supersecret",
        "https://s3/x?X-Amz-Signature=deadbeefcafe1234&y=1": "deadbeefcafe1234",
        "t eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTYifQ.abcdefghijk": "eyJhbGci",
        "ghp_1234567890abcdefghij": "ghp_1234567890",
        "password: hunter2hunter2": "hunter2hunter2",
        "raw " + "A" * 45: "A" * 45,                       # длинный неразрывный токен
    }
    for text, secret in cases.items():
        out = g.redact_secrets(text)
        assert secret not in out, f"секрет утёк: {text[:40]}"
        assert "скрыто" in out, text[:40]


def test_redact_preserves_failure_reason():
    # диагностическая ценность не должна страдать: причина квоты сохраняется дословно
    live = ("You've hit your usage limit. Upgrade to Pro (https://chatgpt.com/explore/pro), "
            "visit https://chatgpt.com/codex/settings/usage to purchase more credits or try "
            "again at Jul 28th, 2026 8:06 PM.")
    assert g.redact_secrets(live) == live
    assert g.redact_secrets("stream error: rate limited, retry in 30s") \
        == "stream error: rate limited, retry in 30s"


def test_outage_details_redacts_all_sources():
    envelope = json.dumps({"codex": {"status": 1, "stderr": "auth failed: api_key=SECRETVALUE123"},
                           "parseError": "token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig12345",
                           "rawOutput": "Bearer ghp_abcdefghij1234567890"})
    out = g.outage_details(envelope)
    for secret in ("SECRETVALUE123", "eyJhbGciOiJIUzI1NiJ9", "ghp_abcdefghij"):
        assert secret not in out, secret
    assert "auth failed" in out                              # смысл причины сохранён


def test_outage_details_redacts_raw_text():
    assert "sk-live-abcdef123456" not in g.outage_details("fatal: key sk-live-abcdef123456 bad")


def test_empirical_tail_redacted(tmp_path):
    # тот же класс у ДРУГОГО продюсера: хвост тест-команды (инвариант у всех продюсеров)
    result, tail = g._run_empirical(f"printenv", 10, tmp_path)   # реальный дамп окружения
    assert "скрыто" in tail or all(
        marker not in tail for marker in ("api_key=", "TOKEN=", "SECRET=")), tail[:200]
    out2 = g.redact_secrets("DATABASE_URL=postgres://u:pass1234@h/db")
    assert "pass1234" not in out2


def test_finding_titles_redacted_at_parse_chokepoint():
    # ревью R2: валидный finding title мог унести секрет в ledger, сообщение блока и аудит
    env = json.dumps({"codex": {"status": 0},
                      "result": {"verdict": "needs-attention", "summary": "s", "next_steps": [],
                                 "findings": [{"severity": "high",
                                               "title": "Hardcoded api_key=sk-live-abcdef123456",
                                               "body": "b", "file": "a.py", "line_start": 1,
                                               "line_end": 1, "confidence": 0.9,
                                               "recommendation": "r"}]}})
    v = g.parse_review_output(env)
    assert v.valid and v.blocking
    title = v.findings[0][1]
    assert "sk-live-abcdef123456" not in title and "скрыто" in title
    assert "Hardcoded" in title                              # смысл находки сохранён


def test_text_contract_titles_redacted():
    v = g.parse_review_output("Verdict: needs-attention\nFindings:\n"
                              "- [high] token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig9999 leak (a.py:1)\n")
    assert "eyJhbGciOiJIUzI1NiJ9" not in v.findings[0][1]


def test_redacted_title_propagates_to_ledger_and_message(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "FINDINGS_DIR", tmp_path / "rf")
    # ниже по потоку: то, что попало в ledger и в сообщение блока, уже отредактировано
    L = g.load_findings_ledger("b")
    v = g.parse_review_output("Verdict: needs-attention\nFindings:\n"
                              "- [high] leaked api_key=SUPERSECRETVALUE here (a.py:1)\n")
    g.merge_round(L, v.findings)
    stored = L["findings"]["F1"]["title"]
    assert "SUPERSECRETVALUE" not in stored
    decision, msg = g.convergence_decision(L)
    assert decision == "block" and "SUPERSECRETVALUE" not in msg


# ═══ Ревью R3: все источники недоверенного текста (реестр #1-#7 в коде) ═══

def test_r3_quoted_and_json_style_values_redacted():
    for text, secret in [('Hardcoded api_key="supersecret123" in cfg', "supersecret123"),
                         ('{"access_token": "abc/def+ghi123456"}', "abc/def+ghi123456"),
                         ("password: 'hunter2hunter2'", "hunter2hunter2"),
                         ('secret = "shortish12"', "shortish12")]:
        assert secret not in g.redact_secrets(text), text


def test_r3_companion_stderr_redacted(gate_env, monkeypatch, capsys):
    # источник #2: non-zero exit печатал stderr дословно
    monkeypatch.setenv("CODEX_COMPANION_CMD",
                       "bash -c 'echo \"fatal: api_key=LEAKEDSECRET123\" >&2; exit 1'")
    assert g.check_reviewed_cli() == 2
    err = capsys.readouterr().err
    assert "LEAKEDSECRET123" not in err and "fatal" in err     # секрета нет, смысл есть


def test_r3_adjudication_reason_redacted(tmp_path, monkeypatch):
    # источник #4: причина уходила в ledger, audit, `findings` и промпт следующего раунда
    monkeypatch.setattr(g, "FINDINGS_DIR", tmp_path / "rf")
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "a.log")
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "issue")])
    g.adjudicate(L, "F1", "refuted", "проверено: token=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig1")
    assert "eyJhbGciOiJIUzI1NiJ9" not in L["findings"]["F1"]["reason"]
    assert "eyJhbGciOiJIUzI1NiJ9" not in (tmp_path / "a.log").read_text()
    g.save_findings_ledger(L)
    assert "eyJhbGciOiJIUzI1NiJ9" not in (g.FINDINGS_DIR / "current.json").read_text()
    assert "eyJhbGciOiJIUzI1NiJ9" not in g._adjudication_prompt_block()   # не циркулирует


def test_r3_skip_reasons_and_trivial_detail_redacted(tmp_path, monkeypatch):
    # источник #5
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "a2.log")
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".dm")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    g.write_marker("trivial", "быстрый фикс, password=hunter2hunter2")
    assert "hunter2hunter2" not in (tmp_path / "a2.log").read_text()


def test_r3_test_command_echo_redacted(gate_env, monkeypatch, capsys):
    # источник #6: эхо test_command в сообщении прогона
    monkeypatch.setattr(g, "_empirical_config",
                        lambda root, ref: ("enabled", "pytest --token=SUPERSECRETTOKEN1", 600))
    monkeypatch.setattr(g, "_run_empirical", lambda cmd, t, root: ("fail", ""))
    g._empirical_gate("HEAD~1", g.git_head())
    assert "SUPERSECRETTOKEN1" not in capsys.readouterr().err


# ═══ Ревью R4: структурная переделка редакции ═══

def test_r4_escaped_quotes_in_envelope_redacted():
    # R4-F1: редактировался сериализованный JSON (кавычки экранированы) → секрет проходил
    env = json.dumps({"codex": {"status": 1, "stderr": 'fatal api_key="ABCD1234EFGH"'},
                      "parseError": None})
    out = g.outage_details(env)
    assert "ABCD1234EFGH" not in out and "fatal" in out
    env2 = json.dumps({"codex": {"status": 1, "stderr": '{"access_token": "abc/def+ghi123"}'}})
    assert "abc/def+ghi123" not in g.outage_details(env2)


def test_r4_signed_url_shape_preserved():
    # R4-F4: эвристика по числу групп уродовала signed URL в DSN-форму
    out = g.redact_secrets("https://x/?sig=deadbeefcafe1234&x=1")
    assert "deadbeefcafe1234" not in out
    assert out.startswith("https://x/?sig=") and out.endswith("&x=1"), out
    dsn = g.redact_secrets("postgres://user:supersecret@db/x")
    assert dsn == "postgres://user:«скрыто»@db/x"


def test_r4_marker_detail_redacted_in_persisted_file(tmp_path, monkeypatch):
    # R4-F3: detail сохранялся в .claude/.design-approved-<session> ДО редакции
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "a3.log")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    g.write_marker("trivial", "быстрый фикс, password=hunter2hunter2")
    persisted = (tmp_path / ".design-approved-s1").read_text()
    assert "hunter2hunter2" not in persisted and "скрыто" in persisted
    # design-маркер и file-binding идут через ту же точку записи
    (tmp_path / "d.md").write_text("## Сценарии\nv1")
    monkeypatch.setattr(g, "REPO_ROOT", tmp_path)
    import hashlib
    h = hashlib.sha256((tmp_path / "d.md").read_bytes()).hexdigest()
    g.add_design_file_binding("api_key=SECRETINDETAIL1", "d.md", h)
    rec = json.loads((tmp_path / ".design-approved-s1").read_text())
    assert "SECRETINDETAIL1" not in rec["detail"]
    assert rec["designs"][0]["hash"] == h            # хэш НЕ отредактирован (64-hex цел)


def test_r4_test_command_change_message_redacted(tmp_path, monkeypatch, capsys):
    # R4-F2: ветка смены команды печатала обе версии дословно.
    # NB: без фикстуры gate_env — она мокает _empirical_gate целиком, а нам нужен настоящий.
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "a4.log")
    def cfg(root, ref):
        return ("enabled", "pytest --api-key=NEWSECRETTOKEN1", 600) if ref != "HEAD~1" \
            else ("enabled", "pytest --api-key=OLDSECRETTOKEN1", 600)
    monkeypatch.setattr(g, "_empirical_config", cfg)
    assert g._empirical_gate("HEAD~1", g.git_head()) == 2
    err = capsys.readouterr().err
    assert "NEWSECRETTOKEN1" not in err and "OLDSECRETTOKEN1" not in err
    assert "изменилась" in err                        # смысл блока сохранён


def test_r5_dsn_without_username_redacted():
    # R5-F1: правило требовало ≥1 символ имени пользователя
    out = g.redact_secrets("redis://:supersecret@cache.internal/0")
    assert "supersecret" not in out and out == "redis://:«скрыто»@cache.internal/0"


def test_r5_exception_text_with_argv_redacted(gate_env, monkeypatch, capsys):
    # R5-F2: TimeoutExpired.__str__ включает весь argv — секрет в аргументе утекал
    monkeypatch.setenv("CODEX_COMPANION_CMD", "sleep 5 --api-key=SUPERSECRETVALUE1")
    monkeypatch.setattr(g, "_REVIEW_TIMEOUT_S", 1)
    assert g.run_companion_review(base="HEAD~1", scope="branch") is None
    assert "SUPERSECRETVALUE1" not in capsys.readouterr().err


def test_r5_empirical_exception_text_redacted(tmp_path):
    result, tail = g._run_empirical("no-such-cmd-xyz --token=SUPERSECRETVALUE2", 5, tmp_path)
    assert result == "error" and "SUPERSECRETVALUE2" not in tail
