"""G4 portable reviewer resolver: direct Gemini + mandatory Claude supplemental."""
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
    assert policy == "portable-review-v1"
    assert certs
    assert g.reviewer_certification("gemini", "gemini-2.5-pro", "blocking") is None
    candidate = g.reviewer_certification(
        "gemini", "gemini-2.5-pro", "blocking", allow_candidate=True)
    assert candidate is not None and candidate.status == "candidate"
    assert g.reviewer_certification(
        "cursor", "cursor-grok-4.5-high", "blocking") is None
    assert _cert(
        "cursor", "cursor-grok-4.5-high", "blocking", candidate=True).family == "xai"
    assert _cert("claude", "opus", "supplemental").roles == ("supplemental",)


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


def test_portable_does_not_treat_unattested_cursor_as_certified(monkeypatch):
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(g, "_resolve_cursor_bin", lambda: "/opt/cursor-agent")
    plan, err = g.resolve_portable_review_plan("portable")
    assert plan is None
    assert "нет доступного certified" in err


def test_portable_without_certified_backend_blocks_with_candidate_hint(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "x" * 48)
    monkeypatch.setattr(g, "_resolve_cursor_bin", lambda: None)
    plan, err = g.resolve_portable_review_plan("portable")
    assert plan is None
    assert "candidate" in err and "certification suite" in err


def test_portable_profiles_and_panel_guards(monkeypatch):
    gemini = replace(
        _cert("gemini", "gemini-2.5-pro", "blocking", candidate=True),
        status="certified",
    )
    cursor = replace(
        _cert("cursor", "cursor-grok-4.5-high", "blocking", candidate=True),
        status="certified",
    )
    supplemental = _cert("claude", "opus", "supplemental")
    certs = {
        ("gemini", "gemini-2.5-pro", "blocking"): gemini,
        ("cursor", "cursor-grok-4.5-high", "blocking"): cursor,
        ("claude", "opus", "supplemental"): supplemental,
    }
    monkeypatch.setattr(
        g, "reviewer_certification",
        lambda provider, model, role, **_kwargs: certs.get((provider, model, role)),
    )
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    monkeypatch.setattr(g, "_resolve_cursor_bin", lambda: "/opt/cursor-agent")
    strong, err = g.resolve_portable_review_plan("strong")
    assert strong is None and "не реализован" in err
    gemini_only, err = g.resolve_portable_review_plan("gemini")
    assert not err and gemini_only == (gemini, supplemental)

    certs[("gemini", "gemini-2.5-pro", "blocking")] = replace(
        gemini, family="openai")
    assert g.resolve_portable_review_plan("portable")[0] is None
    certs[("gemini", "gemini-2.5-pro", "blocking")] = gemini
    del certs[("claude", "opus", "supplemental")]
    assert g.resolve_portable_review_plan("portable")[0] is None


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
    rc, report = cr.run_gemini(2)
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
    rc, report = cr.run_gemini(1)
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
        assert "supplemental advisory reviewer" in _kwargs["input"]
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
    run = g.run_claude_supplemental("HEAD~1", "HEAD")
    assert run.status == "ok"
    assert run.role == "supplemental"
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
    run = g.run_claude_supplemental("HEAD~1", "HEAD")
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
    run = g.run_claude_supplemental("HEAD~1", "HEAD")
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
    monkeypatch.setattr(g.Path, "home", lambda: fake_home)
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
    monkeypatch.setattr(g.Path, "home", lambda: fake_home)
    assert g._resolve_claude_bin() == str(binary)


def test_old_blocking_only_cache_cannot_satisfy_portable_panel(
        tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    blocking = _cert("cursor", "cursor-grok-4.5-high", "blocking", candidate=True)
    supplemental = _cert("claude", "opus", "supplemental")
    old_panel = [g._cert_cache_record(blocking, "blocking")]
    portable_panel = old_panel + [g._cert_cache_record(supplemental, "supplemental")]
    g.write_ledger("a" * 40, "d" * 64, "HEAD~1", g.parse_review_output(_CLEAN), old_panel)
    assert g.read_valid_ledger("a" * 40, "d" * 64, portable_panel) is None


def test_policy_change_invalidates_portable_cache(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    supplemental = _cert("claude", "opus", "supplemental")
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
    cert = _cert("claude", "opus", "supplemental")
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
    blocking = _cert("cursor", "cursor-grok-4.5-high", "blocking", candidate=True)
    supplemental = _cert("claude", "opus", "supplemental")
    monkeypatch.setattr(
        g, "resolve_portable_review_plan",
        lambda profile: ((blocking, supplemental), ""),
    )
    monkeypatch.setenv("REVIEW_PROVIDER", "portable")
    return tmp_path, blocking, supplemental


def _run(cert, text, *, status="ok"):
    role = "supplemental" if "supplemental" in cert.roles else "blocking"
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


def test_claude_high_is_visible_but_not_blocking(portable_gate, monkeypatch, capsys):
    _tmp, blocking, supplemental = portable_gate
    monkeypatch.setattr(
        g, "run_certified_reviewer",
        lambda cert, _base, _head: (
            _run(cert, _CLEAN) if cert == blocking else _run(cert, _BLOCK)),
    )
    assert g.check_reviewed_cli() == 0
    assert "[high] (claude)" in capsys.readouterr().err
    verdict = _deploy_verdict()
    assert {r["role"] for r in verdict["providers"]} == {"blocking", "supplemental"}
    assert verdict["supplemental_findings"] == [{
        "severity": "high",
        "title": "реальная проблема (app/x.py:1)",
        "provider": "claude",
    }]
    assert "supplemental-finding provider=claude severity=high" in g.AUDIT_LOG.read_text()
    assert not list(g.LEDGER_DIR.glob("*.json"))   # advisory не исчезнет через clean cache
    ledger = g.load_findings_ledger("HEAD~1")
    assert ledger["findings"] == {}


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
    monkeypatch.delenv("REVIEW_PROVIDER", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.setattr(g, "resolve_portable_review_plan", _REAL_RESOLVE_PORTABLE)
    monkeypatch.setattr(g, "_resolve_cursor_bin", lambda: None)
    assert g.check_reviewed_cli() == 2
    assert "нет доступного certified" in capsys.readouterr().err


def test_default_portable_enters_real_dispatch_and_both_adapters(
        portable_gate, monkeypatch):
    _tmp, _candidate_blocking, supplemental = portable_gate
    gemini = replace(
        _cert("gemini", "gemini-2.5-pro", "blocking", candidate=True),
        status="certified",
    )

    def certification(provider, model, role, **_kwargs):
        if (provider, model, role) == ("gemini", "gemini-2.5-pro", "blocking"):
            return gemini
        if (provider, model, role) == ("claude", "opus", "supplemental"):
            return supplemental
        return None

    monkeypatch.setattr(g, "reviewer_certification", certification)
    monkeypatch.setenv("GEMINI_API_KEY", "synthetic")
    monkeypatch.delenv("REVIEW_PROVIDER", raising=False)
    monkeypatch.setattr(g, "resolve_portable_review_plan", _REAL_RESOLVE_PORTABLE)
    monkeypatch.setattr(g, "run_certified_reviewer", _REAL_RUN_CERTIFIED)
    monkeypatch.setattr(g, "_resolve_cursor_bin", lambda: None)
    monkeypatch.setattr(g, "_resolve_claude_bin", lambda: "/bin/sh")
    monkeypatch.setattr(
        g.urllib.request, "urlopen",
        lambda *_args, **_kwargs: _HTTPResponse({
            "candidates": [{
                "finishReason": "STOP",
                "content": {"parts": [{"text": _CLEAN}]},
            }],
            "modelVersion": "gemini-2.5-pro",
        }),
    )
    calls = []

    def fake_run(cmd, **_kwargs):
        calls.append(tuple(cmd))
        if cmd[0] == "/bin/sh":
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "is_error": False,
                    "result": _BLOCK,
                    "modelUsage": {"claude-opus-5": {"inputTokens": 1}},
                }),
                stderr="",
            )
        return SimpleNamespace(returncode=0, stdout="diff", stderr="")

    monkeypatch.setattr(g.subprocess, "run", fake_run)
    assert g.check_reviewed_cli() == 0
    assert any(cmd and cmd[0] == "/bin/sh" for cmd in calls)
    audit_text = g.AUDIT_LOG.read_text()
    assert "role=blocking provider=gemini" in audit_text
    assert "role=supplemental provider=claude" in audit_text


def test_missing_claude_supplemental_artifact_blocks(portable_gate, monkeypatch):
    _tmp, blocking, supplemental = portable_gate
    monkeypatch.setattr(
        g, "run_certified_reviewer",
        lambda cert, _base, _head: (
            _run(cert, _CLEAN) if cert == blocking
            else _run(cert, _CLEAN, status="invalid")),
    )
    assert g.check_reviewed_cli() == 2


def test_independent_blocking_high_still_blocks(portable_gate, monkeypatch):
    _tmp, blocking, supplemental = portable_gate
    monkeypatch.setattr(
        g, "run_certified_reviewer",
        lambda cert, _base, _head: (
            _run(cert, _BLOCK) if cert == blocking else _run(cert, _CLEAN)),
    )
    assert g.check_reviewed_cli() == 2
    ledger = g.load_findings_ledger("HEAD~1")
    assert ledger["findings"]["F1"]["provider"] == "cursor"
