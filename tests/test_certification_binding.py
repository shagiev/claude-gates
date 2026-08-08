"""§6/§7: attestation-пометка и механическая связка отчёта сертификации с реестром.

Дизайн: docs/2026-08-07-host-relative-reviewer-ladder-design.md, матрица B12, B20, B21,
B26..B28. Возражение ревью ред. 2 №4 и ред. 5 №2: правка одного JSON не должна изготавливать
blocking-сертификацию, а агрегатный отчёт не должен пропускать модель, не прошедшую корпус.
"""
import hashlib
import json

import pytest

import codex_review_gate as g

REQUIRED_CATEGORIES = [
    "fail-open", "config-weakening", "command-security", "reviewer-independence",
    "benign", "secret-handling", "outage", "schema-drift", "large-multifile",
    "prompt-injection",
]
CASE_IDS = [f"case-{c}" for c in REQUIRED_CATEGORIES]


def _corpus(tmp_path):
    cases = [{"id": cid, "category": cat, "diff": "d", "expect_blocking": True,
              "required": True, "forbidden_output": []}
             for cid, cat in zip(CASE_IDS, REQUIRED_CATEGORIES)]
    body = {"schema": 1, "policy_id": "portable-review-v2", "cases": cases}
    p = tmp_path / "cases.json"
    p.write_text(json.dumps(body))
    return p


def _report(tmp_path, *, model="gpt-5.6-sol", reps=2, all_pass=True, rows=None,
            cert_id="codex-blocking-1", corpus_sha=""):
    if rows is None:
        rows = [{"case": cid, "repetition": r, "pass": all_pass, "actual_model": model}
                for cid in CASE_IDS for r in range(1, reps + 1)]
    body = {
        "schema": 1, "policy_id": "portable-review-v2", "provider": "codex",
        "adapter": "codex-companion", "requested_model": model, "role": "blocking",
        "certification_id": cert_id, "actual_models": [model], "family": "openai",
        "attestation": "declared", "repetitions": reps,
        "corpus_sha256": corpus_sha, "pass": all(r["pass"] for r in rows),
        "results": rows,
    }
    d = tmp_path / "reports"
    d.mkdir(exist_ok=True)
    p = d / f"{cert_id}.json"
    p.write_text(json.dumps(body))
    return p


def _registry(tmp_path, report_path, *, status="certified", attestation="declared",
              model="gpt-5.6-sol", cert_id="codex-blocking-1", corpus_sha="", reps=2):
    entry = {
        "provider": "codex", "adapter": "codex-companion", "requested_model": model,
        "actual_models": [model], "family": "openai", "roles": ["blocking"],
        "certification_id": cert_id, "status": status, "attestation": attestation,
        "report": {
            "path": f"reports/{report_path.name}",
            "sha256": hashlib.sha256(report_path.read_bytes()).hexdigest(),
            "corpus_sha256": corpus_sha, "repetitions": reps,
        },
    }
    body = {"schema": 2, "policy_id": "portable-review-v2", "certifications": [entry]}
    p = tmp_path / "reviewer_certifications.json"
    p.write_text(json.dumps(body))
    return p


@pytest.fixture()
def wired(tmp_path, monkeypatch):
    """Реестр + корпус + отчёт, согласованные между собой (эталон «всё сходится»)."""
    corpus = _corpus(tmp_path)
    corpus_sha = hashlib.sha256(corpus.read_bytes()).hexdigest()
    monkeypatch.setattr(g, "_CORPUS_PATH", corpus)
    monkeypatch.setattr(g, "_REPORTS_DIR", tmp_path / "reports")

    def build(**kw):
        rep = _report(tmp_path, corpus_sha=kw.pop("report_corpus_sha", corpus_sha),
                      **{k: v for k, v in kw.items()
                         if k in ("model", "reps", "all_pass", "rows", "cert_id")})
        reg = _registry(tmp_path, rep, corpus_sha=kw.get("registry_corpus_sha", corpus_sha),
                        **{k: v for k, v in kw.items()
                           if k in ("status", "attestation", "cert_id")})
        monkeypatch.setattr(g, "_CERTIFICATION_REGISTRY", reg)
        return reg, rep
    return build


def _blocking(g_mod=g):
    return g_mod.reviewer_certification("codex", "gpt-5.6-sol", "blocking")


def test_wired_registry_is_valid(wired):
    wired()
    policy, certs = g.load_reviewer_certifications()
    assert policy == "portable-review-v2" and len(certs) == 1
    assert _blocking() is not None


def test_b12_attestation_is_exposed(wired):
    wired()
    cert = _blocking()
    assert cert.attestation == "declared"
    rec = g._cert_cache_record(cert, "blocking")
    assert rec["attestation"] == "declared", "пометка обязана попасть в verdict и cache key"
    other = dict(rec, attestation="verified")
    assert g._reviewers_key([rec]) != g._reviewers_key([other]), \
        "смена attestation обязана инвалидировать кэш чистого ревью"


def test_b21_status_flip_without_report_is_rejected(tmp_path, monkeypatch, wired):
    """Ровно тот обход, который не закрывала ред. 2: правка status руками."""
    reg, _rep = wired()
    body = json.loads(reg.read_text())
    del body["certifications"][0]["report"]
    reg.write_text(json.dumps(body))
    assert _blocking() is None, 'прод не должен получить blocking-слот'


def test_b20_missing_report_file_is_rejected(wired):
    _reg, rep = wired()
    rep.unlink()
    assert _blocking() is None      # понижено до candidate: прод без слота


def test_b20_tampered_report_is_rejected(wired):
    _reg, rep = wired()
    body = json.loads(rep.read_text())
    body["results"][0]["pass"] = True
    body["note"] = "подправлено после подсчёта sha"
    rep.write_text(json.dumps(body))
    assert _blocking() is None      # понижено до candidate: прод без слота


def test_b20_failed_report_is_rejected(wired):
    wired(all_pass=False)
    assert _blocking() is None      # понижено до candidate: прод без слота


def test_b20_too_few_repetitions_is_rejected(wired):
    wired(reps=1)
    assert _blocking() is None      # понижено до candidate: прод без слота


def test_b20_report_from_other_corpus_is_rejected(wired):
    wired(report_corpus_sha="0" * 64)
    assert _blocking() is None      # понижено до candidate: прод без слота


def test_b27_incomplete_matrix_is_rejected(wired):
    """Все категории на месте, но один повтор не прогнан."""
    rows = [{"case": cid, "repetition": r, "pass": True, "actual_model": "gpt-5.6-sol"}
            for cid in CASE_IDS for r in (1, 2)]
    rows.pop()
    wired(rows=rows)
    assert _blocking() is None      # понижено до candidate: прод без слота


def test_b27_single_failed_row_is_rejected(wired):
    rows = [{"case": cid, "repetition": r, "pass": True, "actual_model": "gpt-5.6-sol"}
            for cid in CASE_IDS for r in (1, 2)]
    rows[3]["pass"] = False
    wired(rows=rows)
    assert _blocking() is None      # понижено до candidate: прод без слота


def test_b28_split_actual_models_is_rejected(wired):
    """Модель A проходит почти весь корпус, модель B — одну строку; агрегат сходится."""
    rows = [{"case": cid, "repetition": r, "pass": True, "actual_model": "gpt-5.6-sol"}
            for cid in CASE_IDS for r in (1, 2)]
    rows[0]["actual_model"] = "gpt-mini-untested"
    wired(rows=rows)
    # понижено до candidate: модель, не прошедшая полную матрицу, не получает blocking-слот
    assert _blocking() is None


@pytest.mark.parametrize("bad", ["../outside.json", "/etc/passwd", "reports/../../escape.json"])
def test_b26_report_path_outside_shipped_dir_is_rejected(wired, bad):
    reg, _rep = wired()
    body = json.loads(reg.read_text())
    body["certifications"][0]["report"]["path"] = bad
    reg.write_text(json.dumps(body))
    assert _blocking() is None      # понижено до candidate: прод без слота


def test_candidate_entry_needs_no_report(wired, tmp_path, monkeypatch):
    """`candidate` не даёт blocking-слота, поэтому и отчёта не требует."""
    reg, _rep = wired()
    body = json.loads(reg.read_text())
    body["certifications"][0]["status"] = "candidate"
    del body["certifications"][0]["report"]
    reg.write_text(json.dumps(body))
    policy, certs = g.load_reviewer_certifications()
    assert policy == "portable-review-v2" and len(certs) == 1
    assert _blocking() is None                       # но blocking по нему не выдаётся


@pytest.mark.parametrize("bad_id", ["../../evil", "a/b", "..", "with space", ""])
def test_certification_id_is_path_safe(wired, bad_id):
    """certification_id попадает в путь отчёта у раннера — формат обязан быть узким."""
    reg, _rep = wired()
    body = json.loads(reg.read_text())
    body["certifications"][0]["certification_id"] = bad_id
    reg.write_text(json.dumps(body))
    assert g.load_reviewer_certifications() == (None, ())


def test_runner_lookup_survives_stale_report_but_production_does_not(wired):
    """Ловушка, из-за которой падала пересъёмка: раннер перезаписывает отчёт, sha расходится,
    и весь реестр отваливается — включая запись, которую раннер как раз и сертифицирует.
    Боевой путь обязан остаться fail-closed, инструмент — работать."""
    _reg, rep = wired()
    rep.write_text(rep.read_text() + "\n")          # отчёт изменился → sha разошёлся
    # Запись ПОНИЖАЕТСЯ до candidate, реестр остаётся читаемым: иначе пересъёмка отчёта
    # обрушала бы реестр и делала следующую пересъёмку невозможной.
    policy, certs = g.load_reviewer_certifications()
    assert policy == "portable-review-v2" and certs
    assert g.reviewer_certification("codex", "gpt-5.6-sol", "blocking") is None   # прод: нет
    assert g.reviewer_certification("codex", "gpt-5.6-sol", "blocking",
                                    allow_candidate=True) is not None             # инструмент
    # флага, отключающего проверку связки, быть не должно: он выдавал бы certified без улик
    import inspect
    assert "require_report" not in inspect.signature(g.reviewer_certification).parameters


def test_demoted_entry_explains_itself_to_operator(wired, monkeypatch):
    """Понижение не должно быть немым: оператор обязан узнать, что запись есть, но связка
    отчёта не сошлась — иначе он вслепую перезапускает сертификацию (реальный сценарий)."""
    _reg, rep = wired()
    rep.write_text(rep.read_text() + "\n")
    monkeypatch.setattr(g, "codex_model", lambda **_kw: "gpt-5.6-sol")
    _plan, err = g.resolve_portable_review_plan("portable")
    assert "НЕ certified" in err and "связка отчёта" in err
    assert "audit" in err
