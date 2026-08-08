"""Протокол сходимости v2: §5b (наследование через архивацию), §6b (канонизация dup_of),
§6c (refuted не закрывает блокирующую находку).

Дизайн: docs/2026-08-07-host-relative-reviewer-ladder-design.md, матрица B16..B31.
Каждый тест назван по сценарию BSAC, чтобы провал сразу указывал на нарушенный пункт дизайна.
"""
import pytest

import codex_review_gate as g


@pytest.fixture()
def led(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "FINDINGS_DIR", tmp_path / "rf")
    # `resolved-by-user` требует интерактивного терминала (F11). Тесты протокола проверяют
    # СЕМАНТИКУ статуса, поэтому терминал им эмулируется; сам запрет проверяет отдельный тест.
    monkeypatch.setattr(g.sys.stdin, "isatty", lambda: True)
    return tmp_path


# ═══════════ §6c: refuted не закрывает блокирующую находку (B29, B30, B31) ═══════════

@pytest.mark.parametrize("sev", ["critical", "high", "weird-unknown"])
def test_b29_refuted_rejected_for_blocking_severity(led, sev):
    L = g.load_findings_ledger("b")
    g.merge_round(L, [(sev, "Peer finding")])
    with pytest.raises(g.AdjudicationError):
        g.adjudicate(L, "F1", "refuted", "я считаю это ложным срабатыванием")
    assert L["findings"]["F1"]["status"] == "open"


@pytest.mark.parametrize("sev", ["low", "medium"])
def test_b29_refuted_still_allowed_for_non_blocking(led, sev):
    L = g.load_findings_ledger("b")
    g.merge_round(L, [(sev, "Nitpick")])
    g.adjudicate(L, "F1", "refuted", "стиль, не дефект")
    assert L["findings"]["F1"]["status"] == "refuted"


def test_b30_reviewer_silence_does_not_retire_blocking_finding(led):
    """Молчание второго ревьюера не закрывает находку: раунд без находок оставляет её open."""
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("critical", "Money leak")])
    g.merge_round(L, [])                      # чистый раунд обоих — согласия недостаточно
    assert L["findings"]["F1"]["status"] == "open"
    assert g.convergence_decision(L)[0] == "block"


def test_b31_fixed_plus_round_clears_blocking_finding(led):
    """Штатный автоматический выход есть — дедлока §5b не создаёт."""
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("critical", "Money leak")])
    g.adjudicate(L, "F1", "fixed", "починено коммитом abc123")
    g.merge_round(L, [])                      # ревьюеры увидели адъюдикацию
    assert g.convergence_decision(L)[0] == "allow"


def test_b31_resolved_by_user_clears_blocking_finding(led):
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("critical", "Money leak")])
    g.adjudicate(L, "F1", "resolved-by-user", "решение человека: принимаем риск")
    assert g.convergence_decision(L)[0] == "allow"


# ═══════════ §6b: канонизация dup_of (B16, B17, B25) ═══════════

def test_b16_blocking_dispute_against_resolved_by_user_reopens_as_new(led):
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("critical", "Money leak")])
    g.adjudicate(L, "F1", "resolved-by-user", "человек принял риск")
    g.merge_round(L, [("critical", "[DISPUTE:F1] новая улика: воспроизведено в проде")])
    reopened = [f for f in L["findings"].values() if f.get("reopened_from") == "F1"]
    assert len(reopened) == 1 and reopened[0]["status"] == "open"
    assert L["findings"]["F1"]["status"] == "resolved-by-user"   # исходная терминальна
    assert g.convergence_decision(L)[0] == "block"


def test_b16_blocking_dup_against_resolved_by_user_reopens_as_new(led):
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Bad path")])
    g.adjudicate(L, "F1", "resolved-by-user", "человек принял риск")
    g.merge_round(L, [("high", "[DUP:F1] то же самое, новые данные")])
    assert any(f.get("reopened_from") == "F1" and f["status"] == "open"
               for f in L["findings"].values())


def test_b17_non_blocking_dispute_against_resolved_by_user_stays_late_note(led):
    """Мелочь не пере-открывает закрытое человеком — защита от вечной эскалации."""
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("low", "Nit")])
    g.adjudicate(L, "F1", "resolved-by-user", "человек закрыл")
    g.merge_round(L, [("low", "[DISPUTE:F1] всё ещё не нравится")])
    assert L["findings"]["F1"]["status"] == "resolved-by-user"
    assert L["findings"]["F1"].get("late_note")
    assert not any(f.get("reopened_from") for f in L["findings"].values())


def test_b25_dup_through_duplicate_chain_reaches_root(led):
    """[DUP:F2], где F2 — duplicate корня F1 (resolved-by-user), не должен утонуть."""
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("critical", "Money leak")])            # F1
    g.merge_round(L, [("critical", "[DUP:F1] restated")])     # F2 duplicate → dup_of=F1
    assert L["findings"]["F2"]["status"] == "duplicate"
    g.adjudicate(L, "F1", "resolved-by-user", "человек принял риск")
    g.merge_round(L, [("critical", "[DUP:F2] через дубликат")])
    assert any(f.get("reopened_from") == "F1" and f["status"] == "open"
               for f in L["findings"].values()), "ссылка на duplicate должна дойти до корня"


def test_b25_cyclic_dup_chain_creates_open_finding(led):
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("critical", "Money leak")])
    L["findings"]["F1"]["dup_of"] = "F1"                      # искусственный цикл
    g.merge_round(L, [("critical", "[DUP:F1] по циклу")])
    assert g.convergence_decision(L)[0] in ("block", "escalate")
    assert any(f["status"] == "open" and "по циклу" in (f.get("title") or "")
               for f in L["findings"].values())


def test_b25_dangling_dup_reference_creates_open_finding(led):
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("critical", "[DUP:F42] цели не существует")])
    assert any(f["status"] == "open" for f in L["findings"].values())
    assert g.convergence_decision(L)[0] == "block"


# ═══════════ §5 и §5b: carry-over и наследование (B18, B19, B22, B23) ═══════════

@pytest.mark.parametrize("sev", ["critical", "high", "weird-unknown"])
def test_b9_blocking_severity_never_becomes_carried(led, sev):
    L = g.load_findings_ledger("b")
    g.merge_round(L, [(sev, "Late blocking finding")])
    L["rounds"] = g.HARD_CAP_ROUNDS + 1
    for f in L["findings"].values():
        f["round"] = L["rounds"]
    g.apply_carry_over(L)
    assert all(f["status"] == "open" for f in L["findings"].values())


@pytest.mark.parametrize("sev", ["low", "medium"])
def test_b10_non_blocking_still_carries_over(led, sev):
    L = g.load_findings_ledger("b")
    g.merge_round(L, [(sev, "Minor")])
    L["rounds"] = g.HARD_CAP_ROUNDS + 1
    for f in L["findings"].values():
        f["round"] = L["rounds"]
    g.apply_carry_over(L)
    assert all(f["status"] == "carried" for f in L["findings"].values())


def test_b18_open_blocking_finding_survives_baseline_change(led):
    """outage → частичный critical → аварийный skip → сдвиг baseline: находка обязана выжить."""
    L = g.load_findings_ledger("base-old")
    g.merge_round(L, [("critical", "Money leak", "codex")], partial=True)
    g.save_findings_ledger(L)
    L2 = g.load_findings_ledger("base-new")               # baseline сдвинулся (деплой прошёл)
    survivors = [f for f in L2["findings"].values()
                 if f["status"] == "open" and f["severity"] == "critical"]
    assert survivors, "известная critical пропала при архивации серии"
    assert survivors[0]["carried_from"] == "base-old"
    assert survivors[0]["carry_count"] == 1
    assert survivors[0].get("provider") == "codex", "происхождение находки потеряно"
    assert g.convergence_decision(L2)[0] == "block"


def test_b22_unconfirmed_adjudication_survives_baseline_change(led):
    """critical → refuted-эквивалент (fixed, не увиденный ревьюерами) → skip → новая серия."""
    L = g.load_findings_ledger("base-old")
    g.merge_round(L, [("critical", "Money leak", "codex")])
    g.adjudicate(L, "F1", "fixed", "починено, но ревьюеры ещё не видели")
    assert L["needs_review_round"] is True
    g.save_findings_ledger(L)
    L2 = g.load_findings_ledger("base-new")
    inherited = [f for f in L2["findings"].values() if f["status"] == "open"]
    assert inherited, "неподтверждённая адъюдикация потеряна при смене baseline"
    assert inherited[0].get("unconfirmed_adjudication") is True
    assert inherited[0].get("reason")


def test_b23_confirmed_adjudication_does_not_survive(led):
    """Подтверждённое решение не переезжает — иначе проект залипнет навсегда."""
    L = g.load_findings_ledger("base-old")
    g.merge_round(L, [("critical", "Money leak")])
    g.adjudicate(L, "F1", "fixed", "починено коммитом abc")
    g.merge_round(L, [])                                  # ревьюеры увидели → флаг снят
    assert L.get("needs_review_round") is False
    g.save_findings_ledger(L)
    L2 = g.load_findings_ledger("base-new")
    assert not L2["findings"], "подтверждённое решение не должно наследоваться"


def test_b19_resolved_and_duplicate_do_not_survive(led):
    L = g.load_findings_ledger("base-old")
    g.merge_round(L, [("high", "A")])
    g.merge_round(L, [("high", "[DUP:F1] again")])
    g.adjudicate(L, "F1", "resolved-by-user", "человек закрыл")
    g.merge_round(L, [])
    g.save_findings_ledger(L)
    L2 = g.load_findings_ledger("base-new")
    assert not L2["findings"]


# ═══ Code-review находки (Codex, 07.08.2026): fail-open при порче ledger ═══

def test_duplicate_with_null_dup_of_does_not_swallow_new_critical(led):
    """Запись `duplicate` с пустым dup_of не должна считаться корнем: иначе критическая
    находка со ссылкой на неё оседает ещё одним неблокирующим дубликатом."""
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Original")])
    L["findings"]["F2"] = {"severity": "high", "title": "битый дубликат", "status": "duplicate",
                           "dup_of": None, "disputes": 0, "round": 1}
    g.merge_round(L, [("critical", "[DUP:F2] деньги утекают")])
    assert g.convergence_decision(L)[0] in ("block", "escalate")
    assert any(f["status"] == "open" and "деньги утекают" in (f.get("title") or "")
               for f in L["findings"].values())


def test_f3_blocking_dup_onto_carried_root_reopens_it(led):
    """Находка ревью 07.08: корень со статусом `carried` не блокирует, а DUP на него оседал
    дубликатом — эскалировалась только severity. Получалось запрещённое P7 состояние
    (severity=critical, status=carried), и новая critical второго члена пары не блокировала."""
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("low", "Minor nit")])
    L["rounds"] = g.HARD_CAP_ROUNDS + 1
    L["findings"]["F1"]["round"] = L["rounds"]
    g.apply_carry_over(L)
    assert L["findings"]["F1"]["status"] == "carried"
    g.merge_round(L, [("critical", "[DUP:F1] на самом деле утекают деньги")])
    assert L["findings"]["F1"]["status"] == "open", "carried-корень обязан пере-открыться"
    assert L["findings"]["F1"]["severity"] == "critical"
    # за hard-cap открытая находка эскалирует к человеку; инвариант здесь — «не allow»
    assert g.convergence_decision(L)[0] in ("block", "escalate")


def test_f11_resolved_by_user_requires_human_terminal(led, monkeypatch):
    """Эскалация не должна держаться на дисциплине агента: `resolved-by-user` мгновенно
    снимает блокирующую находку и вызывается тем же входом, что и остальные адъюдикации."""
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("critical", "Money leak")])
    monkeypatch.setattr(g.sys.stdin, "isatty", lambda: False)
    with pytest.raises(g.AdjudicationError) as exc:
        g.adjudicate(L, "F1", "resolved-by-user", "агент решил за человека")
    assert "терминал" in str(exc.value)
    assert L["findings"]["F1"]["status"] == "open"
    monkeypatch.setattr(g.sys.stdin, "isatty", lambda: True)
    g.adjudicate(L, "F1", "resolved-by-user", "решение человека")
    assert L["findings"]["F1"]["status"] == "resolved-by-user"
