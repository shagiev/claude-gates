"""Ф1/Ф3: переключение провайдеров и режим both (спека
docs/2026-07-25-reviewer-provider-switch-design.md). Матрица P1-P21, M1-M4, N1-N4."""
import json

import pytest

import codex_review_gate as g

# Протокольный токен собирается из частей: буквальная строка в фикстурах попадала
# в ревьюируемый дифф и создавала ту самую неоднозначность, которую строгий парсер
# обязан отвергать (находка F12).
_VT = "Verd" + "ict:"

_CLEAN = f"{_VT} approve\n\nNo material findings.\n"
_BLOCK = f"{_VT} needs-attention\n\n- [high] реальная проблема (app/x.py:1)\n"


@pytest.fixture()
def gate(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(g, "FINDINGS_DIR", tmp_path / "rf")
    monkeypatch.setattr(g, "VERDICT_DIR", tmp_path / "verdicts")
    monkeypatch.setattr(g, "LAST_REVIEWED", tmp_path / ".lr")
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".dm")
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "_resolve_baseline_gate", lambda head: ("HEAD~1", 0))
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 0)
    monkeypatch.setattr(g, "_empirical_gate", lambda baseline, head: 0)
    monkeypatch.setattr(g, "_empirical_config", lambda root, ref: ("absent", None, 600))
    monkeypatch.setattr(g, "_ladder_range_skips", lambda baseline: [])
    return tmp_path


def _providers(monkeypatch, codex=None, cursor=None):
    """Стабы провайдеров: значение = текст контракта или None (отказ)."""
    monkeypatch.setattr(g, "run_companion_review",
                        lambda base, scope: codex)
    monkeypatch.setattr(g, "run_cursor_review",
                        lambda base, head: ((cursor, "model=grok-4.7-high")
                                            if cursor else (None, "стаб-отказ")))


def _verdict():
    files = list(g.VERDICT_DIR.glob("*.json"))
    return json.loads(files[0].read_text()) if files else None


# ═══ P1/P7 + M1-M4: резолюция провайдера и allow-list модели ═══

def test_p1_unset_provider_is_not_legacy_codex(monkeypatch):
    monkeypatch.delenv("REVIEW_PROVIDER", raising=False)
    providers, err = g.resolve_providers()
    assert providers is None
    assert "universal default `portable`" in err


def test_p7_unknown_provider_blocks(gate, monkeypatch):
    monkeypatch.setenv("REVIEW_PROVIDER", "cursorr")
    assert g.check_reviewed_cli() == 2                      # без тихого фолбэка на codex
    monkeypatch.setenv("REVIEW_PROVIDER", "")
    assert g.check_reviewed_cli() == 2


def _fake_models(monkeypatch, output="", *, rc=0, raises=None):
    calls = []
    monkeypatch.setattr(g, "_resolve_cursor_bin", lambda: "/opt/cursor-agent")
    real = g.subprocess.run

    def fake(cmd, **kw):
        if isinstance(cmd, list) and cmd == ["/opt/cursor-agent", "models"]:
            calls.append((cmd, kw))
            if raises is not None:
                raise raises

            class R:
                returncode = rc
            if kw.get("stdout") is not None:
                kw["stdout"].write(output.encode("utf-8"))
                kw["stderr"].write(("models failed" if rc else "").encode("utf-8"))
            else:
                R.stdout = output
                R.stderr = "models failed" if rc else ""
            return R
        return real(cmd, **kw)

    monkeypatch.setattr(g.subprocess, "run", fake)
    return calls


@pytest.mark.parametrize("output,expected", [
    ("cursor-grok-4.5-high - Grok 4.5\ngrok-4.7-high - Grok 4.7\n"
     "cursor-grok-4.6-high - Grok 4.6\n", "grok-4.7-high"),
    ("cursor-grok-4.9-high - Grok 4.9\ngrok-4.10-high - Grok 4.10\n",
     "grok-4.10-high"),
    ("grok-4.7-high - Grok 4.7\ncursor-grok-4.7-high - Grok 4.7\n",
     "cursor-grok-4.7-high"),
    ("grok-4.10-high - Grok 4.10\ngrok-4.10.2-high - Grok 4.10.2\n",
     "grok-4.10.2-high"),
])
def test_cursor_model_selects_newest_numeric_version(monkeypatch, output, expected):
    _fake_models(monkeypatch, "Available models\n\n" + output)
    monkeypatch.delenv("CURSOR_REVIEW_MODEL", raising=False)
    assert g.resolve_cursor_model() == (expected, "")                 # EARS-1 / S1-S3


@pytest.mark.parametrize("marker", ["current", "default", "current, default", "default, current"])
def test_cursor_model_current_default_without_description(monkeypatch, marker):
    _fake_models(monkeypatch,
                 f"grok-4.8-high ({marker})\ngrok-4.7-high - Grok 4.7\n")
    monkeypatch.delenv("CURSOR_REVIEW_MODEL", raising=False)
    assert g.resolve_cursor_model() == ("grok-4.8-high", "")


def test_cursor_model_ignores_non_exact_variants(monkeypatch):
    _fake_models(monkeypatch,
                 "grok-4.8-high-fast - fast\ngrok-4.8-low - low\n"
                 "grok-4.8-high-rc1 - prerelease\ncursor-grok-4.6-high - stable\n")
    monkeypatch.delenv("CURSOR_REVIEW_MODEL", raising=False)
    assert g.resolve_cursor_model()[0] == "cursor-grok-4.6-high"       # S3


@pytest.mark.parametrize("malformed", [
    "grok-4.７-high", "grok-1234567890.1-high", "grok-" + "9" * 5000 + ".1-high",
])
def test_cursor_model_malformed_digits_never_crash_or_win(monkeypatch, malformed):
    _fake_models(monkeypatch, malformed + " - malformed\ngrok-4.6-high - stable\n")
    monkeypatch.delenv("CURSOR_REVIEW_MODEL", raising=False)
    assert g.resolve_cursor_model() == ("grok-4.6-high", "")           # code review L1


def test_cursor_model_override_must_be_listed_exact_grok_high(monkeypatch):
    _fake_models(monkeypatch, "grok-4.7-high - newest\ncursor-grok-4.6-high - older\n")
    monkeypatch.setenv("CURSOR_REVIEW_MODEL", "cursor-grok-4.6-high")
    assert g.resolve_cursor_model() == ("cursor-grok-4.6-high", "")    # EARS-2 / S4
    for bad in ("gpt-5.3-codex-high-fast", "claude-opus-5-thinking-high", "auto",
                "bogus-model", "grok-4.8-high", "grok-4.7-high-fast", "grok-4.7-low",
                "grok-4.7-high-rc1"):
        monkeypatch.setenv("CURSOR_REVIEW_MODEL", bad)
        model, err = g.resolve_cursor_model()
        assert model is None and err, bad


@pytest.mark.parametrize("output", [
    "", "gpt-5.6-sol-high - GPT\nauto - Auto\n",
    "grok-4.7-high-fast - fast\ngrok-4.7-low - low\ngrok-4.7-high-rc1 - rc\n",
])
def test_cursor_model_empty_or_without_exact_grok_blocks(monkeypatch, output):
    _fake_models(monkeypatch, output)
    monkeypatch.delenv("CURSOR_REVIEW_MODEL", raising=False)
    model, err = g.resolve_cursor_model()
    assert model is None and err                                       # EARS-3 / S5-S6


def test_cursor_model_cli_failure_timeout_and_oversize_block(monkeypatch):
    monkeypatch.delenv("CURSOR_REVIEW_MODEL", raising=False)
    _fake_models(monkeypatch, rc=1)
    assert g.resolve_cursor_model()[0] is None
    _fake_models(monkeypatch, raises=g.subprocess.TimeoutExpired("cursor-agent", 30))
    assert g.resolve_cursor_model()[0] is None
    for error in (OSError("models I/O"), UnicodeError("models encoding")):
        _fake_models(monkeypatch, raises=error)
        assert g.resolve_cursor_model()[0] is None
    monkeypatch.setattr(g, "_CURSOR_MODELS_OUTPUT_LIMIT", 16, raising=False)
    _fake_models(monkeypatch, "cursor-grok-4.7-high - Grok 4.7\n")
    assert g.resolve_cursor_model()[0] is None                         # обрезанный список ≠ пустой


def test_cursor_model_missing_cli_blocks(monkeypatch):
    monkeypatch.setattr(g, "_resolve_cursor_bin", lambda: None)
    monkeypatch.delenv("CURSOR_REVIEW_MODEL", raising=False)
    model, err = g.resolve_cursor_model()
    assert model is None and "не найден" in err


def test_cursor_discovery_auth_key_is_scoped_to_cursor(monkeypatch):
    monkeypatch.setenv("CURSOR_API_KEY", "unit-test-cursor-key")
    monkeypatch.setenv("CURSOR_API_ENDPOINT", "https://attacker.invalid")
    env = g._certified_subprocess_env(cursor_auth=True)
    assert env["CURSOR_API_KEY"] == "unit-test-cursor-key"
    assert "CURSOR_API_ENDPOINT" not in env
    assert "CURSOR_API_KEY" not in g._certified_subprocess_env()


def test_cursor_model_discovery_uses_sterile_bounded_call(monkeypatch):
    env_calls = []
    monkeypatch.setattr(g, "_certified_subprocess_env",
                        lambda **kwargs: env_calls.append(kwargs) or {"PATH": "/trusted"})
    calls = _fake_models(monkeypatch, "grok-4.7-high - Grok 4.7\n")
    monkeypatch.delenv("CURSOR_REVIEW_MODEL", raising=False)
    assert g.resolve_cursor_model()[0] == "grok-4.7-high"
    assert len(calls) == 1 and env_calls == [{"cursor_auth": True}]
    _cmd, kw = calls[0]
    assert kw["env"] == {"PATH": "/trusted"}
    assert kw["cwd"] != g.REPO_ROOT and kw["timeout"] <= 60            # S7
    assert "capture_output" not in kw and kw["stdout"] is not None      # code review L2: файл, не RAM


def test_m1_bad_model_blocks_deploy(gate, monkeypatch):
    monkeypatch.setenv("REVIEW_PROVIDER", "cursor")
    monkeypatch.setenv("CURSOR_REVIEW_MODEL", "claude-opus-5-thinking-high")
    assert g.check_reviewed_cli() == 2


# ═══ N1-N4: нормализация narration ═══

def test_n1_narration_glued_normalized():
    out = g.normalize_reviewer_text("I'll check the repo.Verdict: needs-attention\n\n- [high] x (a:1)")
    assert out is not None and out.startswith("Verdict: needs-attention")
    assert g.parse_review_output(out).valid


def test_n2_ambiguous_two_verdicts_rejected():
    assert g.normalize_reviewer_text(
        "Verdict: needs-attention\n\n- [high] real (a:1)\nпример: Verdict: approve\n"
        "No material findings.") is None


def test_n3_n4_no_verdict_or_invalid():
    assert g.normalize_reviewer_text("просто текст без вердикта") is None
    assert g.normalize_reviewer_text("") is None
    # строгая структурная валидация (F1 ревью): прочая проза в блоке → отказ УЖЕ на нормализации
    assert g.normalize_reviewer_text("Verdict: approve\n\nSummary: ok") is None


# ═══ P2/P3: cursor как единственный провайдер ═══

# ═══ §4: легаси-значения REVIEW_PROVIDER больше НЕ понижают панель ═══
# Раньше эти значения выбирали панель МЕНЬШЕ обязательной пары, то есть агент, запускающий
# деплой, отключал независимость той же переменной, которую сам и выставляет (ревью ред. 2 №1).
# Семантика union/частичного раунда/кэша теперь проверяется на обязательной паре —
# tests/test_portable_reviewers.py.

@pytest.mark.parametrize("legacy", ["codex", "cursor", "both"])
def test_b4_legacy_provider_values_block_deploy(gate, monkeypatch, legacy, capsys):
    monkeypatch.setenv("REVIEW_PROVIDER", legacy)
    assert g.check_reviewed_cli() == 2
    err = capsys.readouterr().err
    assert "больше не поддерживается" in err
    assert "CODEX_REVIEW_SKIP" in err          # аварийный выход назван явно, а не подразумевается


def test_b4_legacy_value_does_not_downgrade_panel(gate, monkeypatch):
    """Ключевое: легаси-значение НЕ приводит к одиночной панели — оно вообще не строит план."""
    monkeypatch.setenv("REVIEW_PROVIDER", "codex")
    called = []
    monkeypatch.setattr(g, "run_certified_reviewer",
                        lambda cert, base, head: called.append(cert.provider))
    assert g.check_reviewed_cli() == 2
    assert called == [], "ни один ревьюер не должен быть запущен по легаси-значению"


# ═══ Ревью cursor'ом собственной реализации: ветки адаптера входят по-настоящему ═══

def _fake_cursor(monkeypatch, *, rc=0, stdout=None, raises=None, models_stdout=None):
    """Подменяет subprocess.run ТОЛЬКО для cursor-agent — тело run_cursor_review исполняется."""
    calls = []
    resolves = []

    def resolve():
        resolves.append(1)
        return "/opt/cursor-agent"

    monkeypatch.setattr(g, "_resolve_cursor_bin", resolve)
    real = g.subprocess.run

    def fake(cmd, **kw):
        # сопоставляем по БАЗОВОМУ имени: адаптер вызывает абсолютный путь (F2), и проверка
        # по голому имени пропускала бы вызов к НАСТОЯЩЕМУ cursor-agent (тест повисал на 600с)
        if isinstance(cmd, list) and cmd and str(cmd[0]).endswith("cursor-agent"):
            calls.append((cmd, kw))

            class R:
                returncode = 0
            if cmd[1:] == ["models"]:
                discovered = ("grok-4.7-high - Grok 4.7\n"
                              if models_stdout is None else models_stdout)
                if kw.get("stdout") is not None:
                    kw["stdout"].write(discovered.encode("utf-8"))
                else:
                    R.stdout = discovered
                    R.stderr = ""
                return R
            if raises is not None:
                raise raises
            R.returncode = rc
            R.stdout = stdout or ""
            R.stderr = "" if rc == 0 else "auth failed: api_key=LEAKEDTOKEN123"
            return R
        return real(cmd, **kw)

    monkeypatch.setattr(g.subprocess, "run", fake)
    return calls, resolves


def _cursor_envelope(result_text, usage=None):
    return json.dumps({"result": result_text,
                       "usage": usage or {"inputTokens": 100, "outputTokens": 20}})


def test_adapter_happy_path_and_usage_audited(gate, monkeypatch):
    calls, resolves = _fake_cursor(monkeypatch, stdout=_cursor_envelope(
        f"I checked the repo.{_VT} approve\n\nNo material findings."))
    text, info = g.run_cursor_review("HEAD~1", "HEAD")
    assert text is not None and g.parse_review_output(text).valid
    assert info == "model=grok-4.7-high"
    assert len(resolves) == 1                                          # один binary на discovery+review
    review_cmd = next(cmd for cmd, _kw in calls if "--model" in cmd)
    assert review_cmd[review_cmd.index("--model") + 1] == "grok-4.7-high"
    audit = (gate / "audit.log").read_text()
    assert "cursor-review bin=" in audit and "cursor-agent" in audit     # фактический бинарь (F2)
    assert "model=grok-4.7-high in=100 out=20" in audit           # наблюдаемость затрат


def test_adapter_discovery_failure_never_launches_review(gate, monkeypatch):
    calls, _resolves = _fake_cursor(monkeypatch, models_stdout="auto - Auto\n")
    text, info = g.run_cursor_review("HEAD~1", "HEAD")
    assert text is None and info
    assert [cmd[1:] for cmd, _kw in calls] == [["models"]]              # EARS-3


def test_failed_cursor_resolve_has_unknown_model(monkeypatch):
    monkeypatch.setenv("CURSOR_REVIEW_MODEL", "gpt-5.6-sol-high")
    monkeypatch.setattr(g, "run_cursor_review", lambda base, head: (None, "discovery failed"))
    verdict, model, info = g.review_with_provider("cursor", "HEAD~1", "HEAD")
    assert verdict is None and model == "unknown" and info == "discovery failed"


def test_adapter_p4_auth_error_blocks_and_redacts(gate, monkeypatch):
    _fake_cursor(monkeypatch, rc=1)
    text, info = g.run_cursor_review("HEAD~1", "HEAD")
    assert text is None and "LEAKEDTOKEN123" not in info     # источник #9 редактируется


def test_adapter_p5_timeout(gate, monkeypatch):
    _fake_cursor(monkeypatch, raises=g.subprocess.TimeoutExpired(cmd="cursor-agent", timeout=600))
    text, info = g.run_cursor_review("HEAD~1", "HEAD")
    assert text is None and "таймаут" in info


def test_adapter_p8_diff_over_limit(gate, monkeypatch):
    monkeypatch.setattr(g, "_CURSOR_DIFF_LIMIT", 10)
    _fake_cursor(monkeypatch, stdout=_cursor_envelope("Verdict: approve\n\nNo material findings."))
    text, info = g.run_cursor_review("HEAD~1", "HEAD")
    assert text is None and "лимита" in info                 # не усечённое ревью, а блок


def test_adapter_non_json_and_ambiguous_output(gate, monkeypatch):
    _fake_cursor(monkeypatch, stdout="не json")
    assert g.run_cursor_review("HEAD~1", "HEAD")[0] is None
    _fake_cursor(monkeypatch, stdout=_cursor_envelope(
        "Verdict: needs-attention\n\n- [high] real (a:1)\nпример: Verdict: approve"))
    text, info = g.run_cursor_review("HEAD~1", "HEAD")
    assert text is None and "Verdict" in info                # ambiguous → не засчитано


def test_cache_gated_on_union_not_first_provider(gate, monkeypatch):
    # находка cursor не должна позволить закэшировать «чистый» вердикт codex
    monkeypatch.setenv("REVIEW_PROVIDER", "both")
    _providers(monkeypatch, codex=_CLEAN, cursor=_BLOCK)
    assert g.check_reviewed_cli() == 2
    assert not list((gate / "ledger").glob("*.json")) if (gate / "ledger").exists() else True


def test_f1_quoted_example_not_accepted_as_clean_review():
    # F1 (финальное ревью): narration с примером в блоке кода + отказ ревьюера давала approve
    refusal = ("I could not fetch the diff. Example of the expected format:\n"
               "```\nVerdict: approve\nNo material findings.\n```\n"
               "Sorry, I was unable to inspect the changes.")
    assert g.normalize_reviewer_text(refusal) is None        # отказ НЕ засчитывается одобрением


def test_f1_real_contract_still_accepted():
    ok = ("Checking the repo now.Verdict: needs-attention\n\nFindings:\n"
          "- [high] реальная проблема (app/x.py:1)\n- [low] мелочь (b.py:2)\n")
    out = g.normalize_reviewer_text(ok)
    assert out is not None and g.parse_review_output(out).blocking


def test_f3_codex_model_from_config_reaches_cache_record(gate, monkeypatch):
    """F3: в кэш/вердикт идёт РЕАЛЬНАЯ модель, а не строка «codex» — иначе её смена не
    инвалидировала бы кэш. Деплой-путь теперь строит пару, поэтому проверяем запись напрямую."""
    cert = g.reviewer_certification("codex", g.codex_model(), "blocking", allow_candidate=True)
    assert cert is not None, "в реестре должна быть запись codex под текущую модель"
    rec = g._cert_cache_record(cert, "blocking")
    assert rec["provider"] == "codex" and rec["model"] not in ("", "codex")
    assert rec["attestation"] == "declared"        # §6: аттестации у companion нет


def test_r2_prose_appended_to_clean_marker_rejected():
    # ревью R2: search() принимал любую строку, СОДЕРЖАЩУЮ маркер
    assert g.normalize_reviewer_text(
        "Verdict: approve\n\nNo material findings. I could not inspect the diff.") is None
    assert g.normalize_reviewer_text("Verdict: approve\n\nNo material findings.") is not None


def test_r3_markdown_heading_escape_removed():
    # ревью R3: `#+\s` принимал ЛЮБОЙ текст под видом заголовка
    assert g.normalize_reviewer_text(
        "Verdict: approve\n\nNo material findings.\n# I could not inspect the diff.") is None
    assert g.normalize_reviewer_text(
        "Verdict: needs-attention\n\nFindings:\n- [high] x (a:1)\n") is not None   # контракт цел


def test_r4_pre_verdict_refusal_rejected():
    # ревью R4: narration отбрасывалась, поэтому «не смог посмотреть» + approve = одобрение
    for refusal in ("I could not inspect the diff.Verdict: approve\n\nNo material findings.",
                    "Unable to access the changes.Verdict: approve\n\nNo material findings.",
                    "I failed to read the patch.Verdict: approve\n\nNo material findings.",
                    "I wasn't able to review the code.Verdict: approve\n\nNo material findings."):
        assert g.normalize_reviewer_text(refusal) is None, refusal


def test_r4_legitimate_narration_still_accepted():
    # ключевое: обычная narration и «не нашёл проблем» НЕ должны ловиться детектором отказа
    for ok in ("I'll check the repo invariants first.Verdict: approve\n\nNo material findings.",
               "I could not find any issues after reading the diff.Verdict: approve\n\n"
               "No material findings.",
               "Reviewed all files.Verdict: needs-attention\n\n- [high] x (a:1)\n"):
        assert g.normalize_reviewer_text(ok) is not None, ok


def test_r5_codex_model_honors_codex_home(tmp_path, monkeypatch):
    # ревью R5: модель читалась из ~/.codex/config.toml, а companion уважает CODEX_HOME —
    # ревью шло бы под одной моделью, а кэш/вердикт фиксировали другую
    (tmp_path / "config.toml").write_text('model = "gpt-5.4-high"\nmodel_reasoning_effort = "high"\n')
    monkeypatch.setenv("CODEX_HOME", str(tmp_path))
    assert g.codex_model() == "gpt-5.4-high"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "нет-такого"))
    assert g.codex_model() == "unknown"                  # нечитаемо → явный маркер
