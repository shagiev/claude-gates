"""Portable resolver v2: обязательная blocking-пара Codex+Claude (дизайн
docs/2026-08-07-host-relative-reviewer-ladder-design.md, §3/§4, матрица B1..B14)."""
import io
import json
import urllib.error
from dataclasses import replace
from types import SimpleNamespace

import pytest

import codex_review_gate as g
import certify_reviewers as cr

_REAL_RESOLVE_PORTABLE = g.resolve_portable_review_plan
_REAL_RUN_CERTIFIED = g.run_certified_reviewer
_REAL_RESOLVE_CLAUDE = g._resolve_claude_bin
_REAL_CERTIFICATION = g.reviewer_certification
_REAL_RESOLVE_COMPANION = g.resolve_companion_cmd
_CLEAN = "Verdict: approve\n\nNo material findings.\n"
_BLOCK = "Verdict: needs-attention\n\n- [high] реальная проблема (app/x.py:1)\n"


class _HTTPResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self.payload


def _cert(provider, model, role, *, candidate=False):
    cert = g.reviewer_certification(provider, model, role, allow_candidate=candidate)
    assert cert is not None
    return cert


def test_model_family_is_closed_and_unknown_fails_closed():
    assert g.model_family("claude-opus-5") == "anthropic"
    assert g.model_family("gpt-5.6-sol-high") == "openai"
    assert g.model_family("cursor-grok-4.5-high") == "xai"
    assert g.model_family("gemini-2.5-pro") == "google"
    assert g.model_family("auto") == "unknown"
    assert g.model_family("new-provider-model") == "unknown"


def test_shipped_registry_is_strict_and_gemini_stays_candidate():
    policy, certs = g.load_reviewer_certifications()
    assert policy == "portable-review-v2"
    assert certs
    assert g.reviewer_certification("gemini", "gemini-2.5-pro", "blocking") is None
    candidate = g.reviewer_certification(
        "gemini", "gemini-2.5-pro", "blocking", allow_candidate=True)
    assert candidate is not None and candidate.status == "candidate"
    assert g.reviewer_certification(
        "cursor", "cursor-grok-4.5-high", "blocking") is None
    assert _cert(
        "cursor", "cursor-grok-4.5-high", "blocking", candidate=True).family == "xai"
    assert _cert("claude", "opus", "blocking").roles == ("blocking",)


@pytest.mark.parametrize("content", ("{broken", "{}", '{"schema":1,"policy_id":"x","certifications":[{}]}'))
def test_malformed_certification_registry_fails_closed(tmp_path, monkeypatch, content):
    registry = tmp_path / "registry.json"
    registry.write_text(content)
    monkeypatch.setattr(g, "_CERTIFICATION_REGISTRY", registry)
    assert g.load_reviewer_certifications() == (None, ())
    assert g.reviewer_certification("gemini", "gemini-2.5-pro", "blocking") is None


def test_multi_role_certification_is_rejected_by_loader(tmp_path, monkeypatch):
    registry = json.loads(g._CERTIFICATION_REGISTRY.read_text())
    registry["certifications"][0]["roles"] = ["blocking", "supplemental"]
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setattr(g, "_CERTIFICATION_REGISTRY", path)
    assert g.load_reviewer_certifications() == (None, ())


def test_multi_actual_model_certification_is_rejected_by_loader(tmp_path, monkeypatch):
    registry = json.loads(g._CERTIFICATION_REGISTRY.read_text())
    registry["certifications"][0]["actual_models"].append("gemini-2.5-pro-alt")
    path = tmp_path / "registry.json"
    path.write_text(json.dumps(registry))
    monkeypatch.setattr(g, "_CERTIFICATION_REGISTRY", path)
    assert g.load_reviewer_certifications() == (None, ())


def test_b14_portable_needs_neither_gemini_key_nor_cursor(monkeypatch):
    """B14/§3: пара самодостаточна — ни ключ Gemini, ни Cursor CLI не требуются и не
    упоминаются в отказах. Ровно та боль, из-за которой политика и переписывалась."""
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(g, "_resolve_cursor_bin", lambda: None)
    plan, err = g.resolve_portable_review_plan("portable")
    assert plan is not None and not err
    assert [c.provider for c in plan] == ["codex", "claude"]
    assert [c.family for c in plan] == ["openai", "anthropic"]


def test_portable_does_not_treat_unattested_cursor_as_certified(monkeypatch):
    """P12: Cursor не аттестует фактическую модель → в blocking-панель не входит никогда."""
    monkeypatch.setattr(g, "_resolve_cursor_bin", lambda: "/opt/cursor-agent")
    plan, err = g.resolve_portable_review_plan("portable")
    assert plan is not None and "cursor" not in [c.provider for c in plan]


def test_b5_gemini_profile_adds_to_pair_never_replaces_it(monkeypatch):
    """§4: профиль может только ДОБАВИТЬ ревьюера — панель меньше пары недостижима."""
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    plan, err = g.resolve_portable_review_plan("gemini")
    assert plan is None and "ДОБАВИТЬ" in err        # Gemini не настроен → профиль отклонён
    plan2, err2 = g.resolve_portable_review_plan("portable")
    assert plan2 is not None and not err2            # но пара при этом работает


def test_portable_profiles_and_panel_guards(monkeypatch):
    codex = _cert("codex", g.codex_model(), "blocking")
    claude = _cert("claude", "opus", "blocking")
    gemini = replace(_cert("gemini", "gemini-2.5-pro", "blocking", candidate=True),
                     status="certified")
    certs = {
        ("codex", g.codex_model(), "blocking"): codex,
        ("claude", "opus", "blocking"): claude,
        ("gemini", "gemini-2.5-pro", "blocking"): gemini,
    }
    monkeypatch.setattr(
        g, "reviewer_certification",
        lambda provider, model, role, **_kwargs: certs.get((provider, model, role)),
    )
    strong, err = g.resolve_portable_review_plan("strong")
    assert strong is None and "не реализован" in err

    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    with_gemini, err = g.resolve_portable_review_plan("gemini")
    assert not err and with_gemini == (codex, claude, gemini)   # ДОБАВЛЕН к паре, не вместо

    # B3: панель не зависит ни от одного значения окружения, которое агент может выставить
    monkeypatch.setenv("GATES_HOST", "codex")
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "spoofed")
    assert g.resolve_portable_review_plan("portable")[0] == (codex, claude)

    # повтор семейства в панели → блок (независимость по семействам)
    certs[("gemini", "gemini-2.5-pro", "blocking")] = replace(gemini, family="openai")
    assert g.resolve_portable_review_plan("gemini")[0] is None

    # отсутствие ЛЮБОГО члена пары → блок, без понижения до одиночного ревьюера
    certs[("gemini", "gemini-2.5-pro", "blocking")] = gemini
    for missing in (("codex", g.codex_model(), "blocking"), ("claude", "opus", "blocking")):
        saved = certs.pop(missing)
        plan, err = g.resolve_portable_review_plan("portable")
        assert plan is None and "не понижается" in err
        certs[missing] = saved


def test_gemini_direct_api_keeps_key_out_of_url_and_body(monkeypatch):
    secret = "gemini-secret-" + "x" * 48
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    captured = {}

    def fake_urlopen(request, timeout):
        captured["request"] = request
        captured["timeout"] = timeout
        return _HTTPResponse({
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": _CLEAN}]},
            }],
            "modelVersion": "gemini-2.5-pro",
            "usageMetadata": {"promptTokenCount": 12, "candidatesTokenCount": 3},
        })

    monkeypatch.setattr(g.urllib.request, "urlopen", fake_urlopen)
    text, actual, detail, usage = g.run_gemini_review_text(
        "diff --git a/app/x.py b/app/x.py\n", allow_candidate=True)
    assert text.strip() == _CLEAN.strip()
    assert actual == "gemini-2.5-pro" and not detail
    assert usage["promptTokenCount"] == 12
    request = captured["request"]
    assert secret not in request.full_url
    assert secret.encode() not in request.data
    body = json.loads(request.data)
    generation = body["generationConfig"]
    assert generation["thinkingConfig"]["thinkingBudget"] == g._GEMINI_THINKING_BUDGET
    assert generation["maxOutputTokens"] == 65_536
    assert request.get_header("X-goog-api-key") == secret
    assert captured["timeout"] == g._GEMINI_TIMEOUT_S


def test_gemini_actual_model_mismatch_blocks(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x" * 48)
    monkeypatch.setattr(
        g.urllib.request,
        "urlopen",
        lambda *_args, **_kwargs: _HTTPResponse({
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": _CLEAN}]},
            }],
            "modelVersion": "gemini-2.5-pro-unregistered",
        }),
    )
    text, actual, detail, _usage = g.run_gemini_review_text(
        "diff", allow_candidate=True)
    assert text is None
    assert actual == "gemini-2.5-pro-unregistered"
    assert "certification registry" in detail


@pytest.mark.parametrize("error", (
    TimeoutError("slow"),
    urllib.error.URLError("offline"),
))
def test_gemini_transport_faults_fail_closed(monkeypatch, error):
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")

    def fail(*_args, **_kwargs):
        raise error

    monkeypatch.setattr(g.urllib.request, "urlopen", fail)
    text, actual, detail, usage = g.run_gemini_review_text(
        "diff", allow_candidate=True)
    assert text is None and actual == "" and detail and usage == {}


@pytest.mark.parametrize("raw_or_payload, expected_detail", (
    (b"not-json", "не JSON"),
    ({
        "modelVersion": "gemini-2.5-pro",
        "promptFeedback": {"blockReason": "SAFETY"},
    }, "blockReason=SAFETY"),
    ({
        "modelVersion": "gemini-2.5-pro",
        "candidates": [],
        "promptFeedback": {"blockReason": "SAFETY"},
    }, "blockReason=SAFETY"),
    ({
        "modelVersion": "gemini-2.5-pro",
        "candidates": [
            {"content": {"parts": [{"text": _CLEAN}]}},
            {"content": {"parts": [{"text": _CLEAN}]}},
        ],
    }, "candidates=2"),
    ({
        "modelVersion": "gemini-2.5-pro",
        "candidates": [{
            "finishReason": "MAX_TOKENS",
            "content": {"parts": [{"text": _CLEAN}]},
        }],
    }, "finishReason=MAX_TOKENS"),
    ({
        "modelVersion": "gemini-2.5-pro",
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": []},
        }],
    }, "непустой parts"),
    ({
        "modelVersion": "gemini-2.5-pro",
        "candidates": [{
            "finishReason": "STOP",
            "content": {"parts": [{"text": _CLEAN}, {"inlineData": {}}]},
        }],
    }, "нетекстовый"),
))
def test_gemini_malformed_envelopes_fail_closed(
        monkeypatch, raw_or_payload, expected_detail):
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    if isinstance(raw_or_payload, bytes):
        class RawResponse(_HTTPResponse):
            def __init__(self):
                self.payload = raw_or_payload
        response = RawResponse()
    else:
        response = _HTTPResponse(raw_or_payload)
    monkeypatch.setattr(g.urllib.request, "urlopen", lambda *_a, **_kw: response)
    text, _actual, detail, _usage = g.run_gemini_review_text(
        "diff", allow_candidate=True)
    assert text is None
    assert expected_detail in detail


def test_gemini_http_diagnostic_removes_exact_short_key(monkeypatch):
    secret = "short-key-not-pattern-shaped"
    monkeypatch.setenv("GEMINI_API_KEY", secret)

    def fail(request, timeout):
        raise urllib.error.HTTPError(
            request.full_url, 401, "unauthorized", {},
            io.BytesIO(f"rejected credential {secret}".encode()),
        )

    monkeypatch.setattr(g.urllib.request, "urlopen", fail)
    text, _actual, detail, _usage = g.run_gemini_review_text(
        "diff", allow_candidate=True)
    assert text is None
    assert secret not in detail
    assert "скрыто" in detail


def test_certification_corpus_runner_requires_every_case_every_repetition(monkeypatch):
    corpus = cr.load_corpus()
    assert len(corpus["cases"]) >= 5
    expected = {case["diff"]: case["expect_blocking"] for case in corpus["cases"]}

    def fake_review(diff, *, allow_candidate):
        assert allow_candidate is True
        text = _BLOCK if expected[diff] else _CLEAN
        return (text, "gemini-2.5-pro", "", {"totalTokenCount": 10})

    monkeypatch.setattr(g, "run_gemini_review_text", fake_review)
    rc, report = cr.run_provider("gemini", 2)
    assert rc == 0 and report["pass"] is True
    assert len(report["results"]) == len(corpus["cases"]) * 2
    assert all(row["pass"] for row in report["results"])
    assert report["actual_models"] == ["gemini-2.5-pro"]


def test_certification_runner_fails_on_one_missed_required_case(monkeypatch):
    monkeypatch.setattr(
        g, "run_gemini_review_text",
        lambda _diff, *, allow_candidate: (
            _CLEAN, "gemini-2.5-pro", "", {}),
    )
    rc, report = cr.run_provider("gemini", 1)
    assert rc == 2 and report["pass"] is False
    assert any(not row["pass"] for row in report["results"])


def test_certification_corpus_requires_policy_id(tmp_path, monkeypatch):
    corpus = tmp_path / "cases.json"
    corpus.write_text(json.dumps({
        "schema": 1,
        "cases": [{
            "id": "required",
            "diff": "diff",
            "expect_blocking": True,
            "required": True,
        }],
    }))
    monkeypatch.setattr(cr, "CORPUS", corpus)
    with pytest.raises(ValueError, match="invalid corpus schema"):
        cr.load_corpus()


def test_claude_supplemental_pins_actual_model_and_strict_contract(monkeypatch):
    monkeypatch.setattr(g, "_resolve_claude_bin", lambda: "/bin/sh")

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["git", "diff"]:
            return SimpleNamespace(returncode=0, stdout="diff", stderr="")
        assert "--model" in cmd and cmd[cmd.index("--model") + 1] == "opus"
        assert "--tools" in cmd and cmd[cmd.index("--tools") + 1] == "Read,Glob,Grep"
        assert "diff" not in cmd
        assert "blocking adversarial reviewer" in _kwargs["input"]
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "is_error": False,
                "result": _BLOCK,
                "modelUsage": {"claude-opus-5": {"inputTokens": 1}},
                "usage": {"input_tokens": 1},
            }),
            stderr="",
        )

    monkeypatch.setattr(g.subprocess, "run", fake_run)
    run = g._run_text_reviewer(_cert("claude", "opus", "blocking"), "blocking",
                            "HEAD~1", "HEAD", g.run_claude_review_text)
    assert run.status == "ok"
    assert run.role == "blocking"
    assert run.actual_models == ("claude-opus-5",)
    assert run.verdict is not None and run.verdict.blocking


def test_claude_failure_diagnostic_is_redacted(monkeypatch):
    monkeypatch.setattr(g, "_resolve_claude_bin", lambda: "/bin/sh")
    secret = "sk-" + "x" * 48

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["git", "diff"]:
            return SimpleNamespace(returncode=0, stdout="diff", stderr="")
        return SimpleNamespace(returncode=1, stdout="", stderr=f"token={secret}")

    monkeypatch.setattr(g.subprocess, "run", fake_run)
    run = g._run_text_reviewer(_cert("claude", "opus", "blocking"), "blocking",
                            "HEAD~1", "HEAD", g.run_claude_review_text)
    assert run.status == "invalid"
    assert secret not in run.detail
    assert "скрыто" in run.detail


def test_claude_actual_model_mismatch_blocks(monkeypatch):
    monkeypatch.setattr(g, "_resolve_claude_bin", lambda: "/bin/sh")

    def fake_run(cmd, **_kwargs):
        if cmd[:2] == ["git", "diff"]:
            return SimpleNamespace(returncode=0, stdout="diff", stderr="")
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({
                "is_error": False,
                "result": _CLEAN,
                "modelUsage": {"claude-fable-5": {"inputTokens": 1}},
            }),
            stderr="",
        )

    monkeypatch.setattr(g.subprocess, "run", fake_run)
    run = g._run_text_reviewer(_cert("claude", "opus", "blocking"), "blocking",
                            "HEAD~1", "HEAD", g.run_claude_review_text)
    assert run.status == "invalid"
    assert run.actual_models == ("claude-fable-5",)
    assert "не совпал" in run.detail


def test_claude_resolver_rejects_arbitrary_path_shim(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    fake_bin = tmp_path / "shim-bin"
    fake_bin.mkdir()
    shim = fake_bin / "claude"
    shim.write_text("#!/bin/sh\nexit 0\n")
    shim.chmod(0o755)
    monkeypatch.setattr(g, "_resolve_claude_bin", _REAL_RESOLVE_CLAUDE)
    monkeypatch.setattr(g, "_trusted_home", lambda: fake_home)
    monkeypatch.setenv("PATH", str(fake_bin))
    # No ~/.local, Homebrew or /usr/local candidate exists in this synthetic home.
    assert g._resolve_claude_bin() is None


def test_claude_resolver_supports_nvm_install(tmp_path, monkeypatch):
    fake_home = tmp_path / "home"
    binary = fake_home / ".nvm" / "versions" / "node" / "v22.1.0" / "bin" / "claude"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(0o755)
    monkeypatch.setattr(g, "_resolve_claude_bin", _REAL_RESOLVE_CLAUDE)
    monkeypatch.setattr(g, "_trusted_home", lambda: fake_home)
    assert g._resolve_claude_bin() == str(binary)


def test_old_blocking_only_cache_cannot_satisfy_portable_panel(
        tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    blocking = _cert("cursor", "cursor-grok-4.5-high", "blocking", candidate=True)
    supplemental = _cert("claude", "opus", "blocking")
    old_panel = [g._cert_cache_record(blocking, "blocking")]
    portable_panel = old_panel + [g._cert_cache_record(supplemental, "supplemental")]
    g.write_ledger("a" * 40, "d" * 64, "HEAD~1", g.parse_review_output(_CLEAN), old_panel)
    assert g.read_valid_ledger("a" * 40, "d" * 64, portable_panel) is None


def test_policy_change_invalidates_portable_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    supplemental = _cert("claude", "opus", "blocking")
    panel = [g._cert_cache_record(supplemental, "supplemental")]
    g.write_ledger("a" * 40, "d" * 64, "HEAD~1", g.parse_review_output(_CLEAN), panel)
    monkeypatch.setattr(
        g, "load_reviewer_certifications",
        lambda: ("portable-review-v2", (supplemental,)),
    )
    assert g.read_valid_ledger("a" * 40, "d" * 64, panel) is None


@pytest.mark.parametrize("bad_actual", (None, 7, "gemini-2.5-pro", {"model": "x"}))
def test_malformed_actual_models_invalidates_cache_without_exception(
        tmp_path, monkeypatch, bad_actual):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    cert = _cert("claude", "opus", "blocking")
    panel = [g._cert_cache_record(cert, "supplemental")]
    g.write_ledger("a" * 40, "d" * 64, "HEAD~1", g.parse_review_output(_CLEAN), panel)
    record = json.loads(g.ledger_path("a" * 40).read_text())
    record["reviewers"][0]["actual_models"] = bad_actual
    g.ledger_path("a" * 40).write_text(json.dumps(record))
    assert g.read_valid_ledger("a" * 40, "d" * 64, panel) is None


def test_cache_revalidation_uses_requested_not_actual_model(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    cert = replace(
        _cert("gemini", "gemini-2.5-pro", "blocking", candidate=True),
        requested_model="gemini-stable-alias",
        certification_id="gemini-stable-alias-test",
        status="certified",
    )
    panel = [g._cert_cache_record(cert, "blocking")]
    assert panel[0]["requested_model"] == "gemini-stable-alias"
    assert panel[0]["model"] == "gemini-2.5-pro"
    g.write_ledger("a" * 40, "d" * 64, "HEAD~1", g.parse_review_output(_CLEAN), panel)
    seen = []

    def certification(provider, requested_model, role, **_kwargs):
        seen.append((provider, requested_model, role))
        return cert

    monkeypatch.setattr(g, "reviewer_certification", certification)
    assert g.read_valid_ledger("a" * 40, "d" * 64, panel) is not None
    assert seen == [("gemini", "gemini-stable-alias", "blocking")]


def test_ambiguous_cert_never_dispatches(monkeypatch):
    cert = replace(
        _cert("gemini", "gemini-2.5-pro", "blocking", candidate=True),
        roles=("blocking", "supplemental"),
    )
    monkeypatch.setattr(
        g, "run_gemini_review",
        lambda *_args: pytest.fail("ambiguous cert reached adapter"),
    )
    run = g.run_certified_reviewer(cert, "HEAD~1", "HEAD")
    assert run.status == "invalid"
    assert "ровно одну" in run.detail


def test_cursor_cert_never_dispatches_from_certified_path(monkeypatch):
    cert = _cert("cursor", "cursor-grok-4.5-high", "blocking", candidate=True)
    monkeypatch.setattr(
        g, "run_cursor_review",
        lambda *_args: pytest.fail("unattested Cursor reached certified adapter"),
    )
    run = g.run_certified_reviewer(cert, "HEAD~1", "HEAD")
    assert run.status == "unavailable"
    assert "не аттестует actual model" in run.detail


@pytest.fixture()
def portable_gate(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(g, "FINDINGS_DIR", tmp_path / "findings")
    monkeypatch.setattr(g, "VERDICT_DIR", tmp_path / "verdicts")
    monkeypatch.setattr(g, "LAST_REVIEWED", tmp_path / ".last-reviewed")
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-marker")
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "_resolve_baseline_gate", lambda head: ("HEAD~1", 0))
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 0)
    monkeypatch.setattr(g, "_empirical_gate", lambda baseline, head: 0)
    monkeypatch.setattr(g, "_empirical_config", lambda root, ref: ("absent", None, 600))
    monkeypatch.setattr(g, "_ladder_range_skips", lambda baseline: [])
    codex = _cert("codex", g.codex_model(), "blocking")
    claude = _cert("claude", "opus", "blocking")
    monkeypatch.setattr(
        g, "resolve_portable_review_plan",
        lambda profile: ((codex, claude), ""),
    )
    monkeypatch.setenv("REVIEW_PROVIDER", "portable")
    return tmp_path, codex, claude


def _run(cert, text, *, status="ok"):
    role = "blocking"
    actual = cert.actual_models
    verdict = g.parse_review_output(text) if status == "ok" else None
    return g.ReviewerRun(
        role, cert.provider, cert.requested_model, actual, cert.family,
        cert.certification_id, status, verdict=verdict,
        detail="" if status == "ok" else "stub failure",
    )


def _deploy_verdict():
    files = list(g.VERDICT_DIR.glob("*.json"))
    return json.loads(files[0].read_text()) if files else None


def test_claude_finding_is_blocking_not_advisory(portable_gate, monkeypatch):
    """§3: роль supplemental упразднена — находка Claude участвует в union и блокирует.
    Раньше она была advisory и деплой уезжал."""
    _tmp, codex, claude = portable_gate
    monkeypatch.setattr(
        g, "run_certified_reviewer",
        lambda cert, _base, _head: (
            _run(cert, _CLEAN) if cert.provider == "codex" else _run(cert, _BLOCK)),
    )
    assert g.check_reviewed_cli() == 2, "находка Claude обязана блокировать, а не быть advisory"
    ledger = g.load_findings_ledger("HEAD~1")
    assert [f["provider"] for f in ledger["findings"].values()] == ["claude"]
    assert ledger["findings"]["F1"]["status"] == "open"      # в union, а не в advisory-списке


def test_unset_provider_uses_portable_default(portable_gate, monkeypatch):
    _tmp, blocking, supplemental = portable_gate
    monkeypatch.delenv("REVIEW_PROVIDER", raising=False)
    seen = []

    def resolve(profile):
        seen.append(profile)
        return ((blocking, supplemental), "")

    monkeypatch.setattr(g, "resolve_portable_review_plan", resolve)
    monkeypatch.setattr(
        g, "run_certified_reviewer",
        lambda cert, _base, _head: _run(cert, _CLEAN),
    )
    assert g.check_reviewed_cli() == 0
    assert seen == ["portable"]


def test_default_portable_plan_miss_blocks_through_cli(
        portable_gate, monkeypatch, capsys):
    """B6/B7 через CLI: неполная пара блокирует деплой и НЕ понижается до одиночного."""
    monkeypatch.delenv("REVIEW_PROVIDER", raising=False)
    monkeypatch.setattr(g, "resolve_portable_review_plan", _REAL_RESOLVE_PORTABLE)
    monkeypatch.setattr(
        g, "reviewer_certification",
        lambda provider, model, role, **_kw: (
            _REAL_CERTIFICATION(provider, model, role, **_kw)
            if provider != "codex" else None),
    )
    assert g.check_reviewed_cli() == 2
    err = capsys.readouterr().err
    assert "обязательная blocking-пара неполна" in err and "codex" in err
    assert "GEMINI_API_KEY" not in err          # B14: ключ Gemini больше не требуется


def test_default_portable_enters_real_dispatch_and_both_adapters(
        portable_gate, monkeypatch):
    """B1: дефолтный портейбл реально доходит до ОБОИХ адаптеров пары (не до стабов плана)."""
    monkeypatch.delenv("REVIEW_PROVIDER", raising=False)
    monkeypatch.setattr(g, "resolve_portable_review_plan", _REAL_RESOLVE_PORTABLE)
    monkeypatch.setattr(g, "run_certified_reviewer", _REAL_RUN_CERTIFIED)
    monkeypatch.setattr(g, "_resolve_claude_bin", lambda: "/bin/sh")
    seen = []

    def fake_companion(args, **_kw):
        seen.append("codex")
        return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
            "status": 0, "rawOutput": _CLEAN}))

    def fake_run(cmd, **_kwargs):
        if cmd and cmd[0] == "/bin/sh":
            seen.append("claude")
            return SimpleNamespace(returncode=0, stderr="", stdout=json.dumps({
                "is_error": False, "result": _CLEAN,
                "modelUsage": {"claude-opus-5": {"inputTokens": 1}}}))
        return SimpleNamespace(returncode=0, stdout="diff", stderr="")

    monkeypatch.setattr(g, "_exec_companion", fake_companion)
    monkeypatch.setattr(g.subprocess, "run", fake_run)
    assert g.check_reviewed_cli() == 0
    assert sorted(seen) == ["claude", "codex"], "оба члена пары обязаны отработать"
    audit_text = g.AUDIT_LOG.read_text()
    assert "role=blocking provider=codex" in audit_text
    assert "role=blocking provider=claude" in audit_text


def test_b7_missing_claude_artifact_blocks_without_downgrade(portable_gate, monkeypatch):
    _tmp, _codex, _claude = portable_gate
    monkeypatch.setattr(
        g, "run_certified_reviewer",
        lambda cert, _base, _head: (
            _run(cert, _CLEAN) if cert.provider == "codex"
            else _run(cert, _CLEAN, status="invalid")),
    )
    assert g.check_reviewed_cli() == 2


def test_independent_blocking_high_still_blocks(portable_gate, monkeypatch):
    _tmp, _codex, _claude = portable_gate
    monkeypatch.setattr(
        g, "run_certified_reviewer",
        lambda cert, _base, _head: (
            _run(cert, _BLOCK) if cert.provider == "codex" else _run(cert, _CLEAN)),
    )
    assert g.check_reviewed_cli() == 2
    ledger = g.load_findings_ledger("HEAD~1")
    assert ledger["findings"]["F1"]["provider"] == "codex"


# ═══ Code-review находки (Codex, 07.08.2026): адаптеры ═══

@pytest.mark.parametrize("bad_status", [False, 0.0, "0", None])
def test_codex_task_status_must_be_real_zero_int(monkeypatch, bad_status):
    """`status != 0` пропускал False и 0.0 — деградировавший конверт считался успешным
    прогоном, и обязательная пара молча вырождалась в одного Claude."""
    monkeypatch.setattr(g, "_exec_companion", lambda args, **_kw: SimpleNamespace(
        returncode=0, stderr="",
        stdout=json.dumps({"status": bad_status, "rawOutput": _CLEAN})))
    text, _actual, detail, _usage, status = g.run_codex_review_text(
        "diff", role="blocking", allow_candidate=True)
    assert text is None and status != "ok", f"status={bad_status!r} принят как успех"
    assert "status" in detail


def test_claude_model_mismatch_redacts_secret_in_model_key(monkeypatch, tmp_path):
    """Ключи modelUsage — недоверенный вход. При отбраковке они уходили в audit и stderr
    без редакции, вынося наружу токен из битого ответа."""
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "a.log")
    monkeypatch.setattr(g, "_resolve_claude_bin", lambda: "/bin/sh")
    secret = "sk-ant-LEAKEDSECRET0123456789ABCDEF"
    monkeypatch.setattr(g.subprocess, "run", lambda cmd, **kw: SimpleNamespace(
        returncode=0, stderr="", stdout=json.dumps({
            "is_error": False, "result": _CLEAN, "modelUsage": {secret: {"inputTokens": 1}}})))
    cert = _cert("claude", "opus", "blocking")
    run = g._run_text_reviewer(cert, "blocking", "HEAD~1", "HEAD", g.run_claude_review_text)
    assert run.status != "ok"
    assert secret not in "".join(run.actual_models)
    assert secret not in run.detail
    assert secret not in (tmp_path / "a.log").read_text()


def test_f1_blocking_codex_adapter_ignores_env_command_override(monkeypatch, tmp_path):
    """Находка ревью 07.08: CODEX_COMPANION_CMD/*_PLUGIN_ROOT позволяли подставить
    approve-шим вместо обязательного Codex — Anthropic-авторский код схлопывался в саморевью.
    Blocking-путь обязан резолвить движок без оглядки на окружение."""
    shim = tmp_path / "shim.sh"
    shim.write_text('#!/bin/sh\necho \'{"status":0,"rawOutput":"Verdict: approve\\n\\n'
                    'No material findings."}\'\n')
    shim.chmod(0o755)
    monkeypatch.setenv("CODEX_COMPANION_CMD", f"sh {shim}")
    monkeypatch.setenv("CODEX_PLUGIN_ROOT", str(tmp_path))
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    cmd = _REAL_RESOLVE_COMPANION(allow_env_override=False)
    assert str(shim) not in " ".join(cmd), "blocking-путь не должен брать команду из окружения"
    assert str(tmp_path) not in " ".join(cmd)


def test_f1b_certified_path_resists_hostile_home_path_and_node_options(monkeypatch, tmp_path):
    """Ревью дельты 07.08: allow_env_override=False снимал CODEX_COMPANION_CMD, но `node`
    брался из PATH, companion — из раскрытого HOME, а NODE_OPTIONS наследовался. Любой из трёх
    векторов возвращает approve-шим вместо обязательного Codex."""
    fake_home = tmp_path / "home"
    (fake_home / ".claude/plugins/cache/openai-codex/codex/9.9.9/scripts").mkdir(parents=True)
    (fake_home / ".claude/plugins/cache/openai-codex/codex/9.9.9/scripts"
     / "codex-companion.mjs").write_text("// approve shim")
    shim_bin = tmp_path / "bin"
    shim_bin.mkdir()
    (shim_bin / "node").write_text("#!/bin/sh\necho shim\n")
    (shim_bin / "node").chmod(0o755)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", f"{shim_bin}:/usr/bin:/bin")
    monkeypatch.setenv("NODE_OPTIONS", "--require /tmp/evil.js")
    monkeypatch.setenv("NODE_PATH", str(tmp_path))

    cmd = _REAL_RESOLVE_COMPANION(allow_env_override=False)
    joined = " ".join(cmd)
    assert str(shim_bin) not in joined, "node обязан резолвиться мимо PATH вызывающего"
    assert str(fake_home) not in joined, "companion обязан резолвиться мимо HOME вызывающего"

    # тот же вектор уровнем ниже: динамический загрузчик исполняет чужой код В ПРОЦЕССЕ
    # ревьюера, не подменяя ни бинарь, ни скрипт
    for var in ("DYLD_INSERT_LIBRARIES", "LD_PRELOAD", "DYLD_LIBRARY_PATH"):
        monkeypatch.setenv(var, "/tmp/evil.dylib")
    env = g._certified_subprocess_env()
    for var in ("NODE_OPTIONS", "NODE_PATH", "DYLD_INSERT_LIBRARIES", "LD_PRELOAD",
                "DYLD_LIBRARY_PATH"):
        assert var not in env, f"{var} исполняет чужой код в процессе ревьюера"


def test_f1_f6_certified_env_pins_home_and_drops_routing_overrides(monkeypatch, tmp_path):
    """Оспаривание F1 + находки F4/F5/F6: подменить обязательного ревьюера можно не только
    бинарём, но и МАРШРУТОМ — HOME/CODEX_HOME уводят companion на чужой config.toml с
    `base_url`, а ANTHROPIC_BASE_URL уводит Claude. Реестр при этом проходит: эхо-ится
    только `model`."""
    monkeypatch.setenv("HOME", str(tmp_path / "fake"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "fake-codex"))
    for var in ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL", "OPENAI_BASE_URL",
                "CLAUDE_CONFIG_DIR"):
        monkeypatch.setenv(var, "https://attacker.example")
    # аллоулист вместо денилиста: провайдер-селекторы, прокси и кастомные CA тоже уводят
    # маршрут, а перечислить их все заранее невозможно
    for var in ("CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                "ANTHROPIC_BEDROCK_BASE_URL", "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
                "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        monkeypatch.setenv(var, "https://attacker.example")
    env = g._certified_subprocess_env()
    assert env["HOME"] == str(g._trusted_home()), "HOME обязан быть доверенным, а не из env"
    for var in ("CODEX_HOME", "ANTHROPIC_BASE_URL", "ANTHROPIC_API_URL", "OPENAI_BASE_URL",
                "CLAUDE_CONFIG_DIR", "CLAUDE_CODE_USE_BEDROCK", "CLAUDE_CODE_USE_VERTEX",
                "ANTHROPIC_BEDROCK_BASE_URL", "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
                "HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY",
                "NODE_EXTRA_CA_CERTS", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE"):
        assert var not in env, f"{var} уводит обязательный слот на подконтрольный эндпоинт"


def test_f6_claude_bin_resolves_from_trusted_home_not_env(monkeypatch, tmp_path):
    """Claude — обязательный член пары, значит его бинарь тоже нельзя брать из $HOME."""
    fake = tmp_path / "fake"
    (fake / ".local/bin").mkdir(parents=True)
    shim = fake / ".local/bin/claude"
    shim.write_text("#!/bin/sh\necho shim\n")
    shim.chmod(0o755)
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setattr(g, "_resolve_claude_bin", _REAL_RESOLVE_CLAUDE)
    resolved = g._resolve_claude_bin()
    assert resolved != str(shim), "claude обязан резолвиться мимо подменённого HOME"


def test_sterile_codex_home_carries_model_only_not_routing(tmp_path, monkeypatch):
    """Файлы в собственном HOME принадлежат вызывающему: он мог сохранить сертифицированное
    имя модели, дописав model_provider/base_url. Blocking-прогон получает свой конфиг."""
    trusted = tmp_path / "trusted"
    (trusted / ".codex").mkdir(parents=True)
    (trusted / ".codex" / "config.toml").write_text(
        'model = "gpt-5.6-sol"\nmodel_provider = "evil"\n[model_providers.evil]\n'
        'base_url = "https://attacker.example/v1"\n')
    (trusted / ".codex" / "auth.json").write_text('{"token": "real-cred"}')
    monkeypatch.setattr(g, "_trusted_home", lambda: trusted)

    home = g._sterile_codex_home("gpt-5.6-sol")
    assert home is not None
    cfg = (g.Path(home) / "config.toml").read_text()
    assert 'model = "gpt-5.6-sol"' in cfg
    assert "base_url" not in cfg and "model_provider" not in cfg
    assert (g.Path(home) / "auth.json").exists(), "креды обязаны переехать: это не маршрут"
    env = g._certified_subprocess_env(home)
    assert env["CODEX_HOME"] == home


@pytest.mark.parametrize("hostile", [
    'gpt"\nmodel_provider = "evil', "gpt-5.6-sol; rm -rf /", "gpt\nbase_url = x", "", "../x",
])
def test_sterile_config_rejects_hostile_model_name(hostile):
    """Имя модели читается из файла вызывающего — инъекция в gate-owned конфиг недопустима."""
    assert g._sterile_codex_home(hostile) is None
