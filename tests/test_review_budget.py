"""Стоп-политика v3: бюджет раундов по радиусу поражения
(docs/methodology/2026-08-11-review-budget-design.md, матрица B1..B8 + BM1..BM3).

Мотив эмпирический: правило «хард-кап ≈5 раундов» существовало и раньше, но НИЧЕГО его не
проверяло — и один дизайн собрал 15 раундов, из которых полезны были первые пять. Поэтому
тесты здесь про МЕХАНИКУ (счётчик виден, превышение блокирует старт), а не про прозу.
"""
import json

import pytest

import codex_review_gate as g


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    r = tmp_path / "proj"
    (r / ".claude").mkdir(parents=True)
    monkeypatch.setattr(g, "REPO_ROOT", r)
    monkeypatch.setattr(g, "_gate_state_dir", lambda: tmp_path / "state")
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(g, "_require_repo", lambda: True)
    return r


def _tier(repo, tier):
    (repo / g.GATE_CONFIG_NAME).write_text(f"review:\n  tier: {tier}\n")


def test_unknown_or_missing_tier_is_the_strictest(repo):
    """Неизвестно → строже всего: бюджет не должен становиться способом случайно ослабить
    ревью на денежном пути из-за опечатки в конфиге."""
    assert g.review_tier() == "money"
    _tier(repo, "нет-такого-яруса")
    assert g.review_tier() == "money"
    _tier(repo, "convenience")
    assert g.review_tier() == "convenience"


def test_b1_convenience_budget_is_one_round(repo):
    _tier(repo, "convenience")
    rnd, budget, refusal = g.review_round_check("range:x:branch")
    assert (rnd, budget, refusal) == (1, 1, "")
    g.review_round_record("range:x:branch")

    rnd2, budget2, refusal2 = g.review_round_check("range:x:branch")
    assert budget2 == 1 and refusal2, "второй раунд не отказан при бюджете 1"
    assert "residuals-accept" in refusal2, "отказ не показывает, как двигаться дальше"


def test_b3_money_budget_is_five_rounds(repo):
    _tier(repo, "money")
    for i in range(5):
        rnd, budget, refusal = g.review_round_check("range:a-b")
        assert (rnd, budget, refusal) == (i + 1, 5, ""), i
        g.review_round_record("range:a-b")
    assert g.review_round_check("range:a-b")[2], "шестой раунд не отказан"


def test_b2_residuals_accept_unlocks_the_next_round(repo, capsys):
    """Цикл не должен запираться навсегда: принятие остатков — один вызов."""
    _tier(repo, "convenience")
    g.review_round_record("range:x:branch")
    assert g.review_round_check("range:x:branch")[2]

    assert g.main(["residuals-accept", "range:x:branch", "--operator-confirmed", "цена принята"]) == 0
    assert g.review_round_check("range:x:branch")[2] == "", "после принятия остатков цикл заперт"


def test_residuals_accept_requires_a_human_and_a_reason(repo, capsys):
    """Принятие остатков — решение ЧЕЛОВЕКА: он принимает цену, а не агент за него."""
    assert g.main(["residuals-accept", "range:x:branch", "причина без подтверждения"]) == 2
    err = capsys.readouterr().err
    assert "--operator-confirmed" in err
    assert "critical/high" in err, "не напомнили про изъятие блокирующих из-под бюджета"
    assert g.main(["residuals-accept", "range:x:branch", "--operator-confirmed"]) == 1  # нет причины


def test_bm2_residuals_accept_does_not_touch_the_findings_ledger(repo, tmp_path, monkeypatch):
    """Центральная гарантия: бюджет управляет ТОЛЬКО тем, сколько раундов запускается, и
    никогда — тем, что считается пройденным. Открытая блокирующая находка остаётся открытой."""
    monkeypatch.setattr(g, "FINDINGS_DIR", tmp_path / "findings")
    led = g.load_findings_ledger("b" * 40)
    g.merge_round(led, [("high", "Живой fail-open")])
    g.save_findings_ledger(led)
    before = g.convergence_decision(g.load_findings_ledger(None))

    assert g.main(["residuals-accept", "range:x", "--operator-confirmed", "приняли"]) == 0

    after_led = g.load_findings_ledger(None)
    assert after_led["findings"]["F1"]["status"] == "open", "остатки закрыли находку ledger'а"
    assert g.convergence_decision(after_led)[0] == before[0] == "block"


def test_bm1_tier_and_acceptance_are_audited(repo, tmp_path):
    """Оператор может поставить `convenience` на денежный путь. Механически это не
    запрещено — но видно в аудите, и деплой-гейт от бюджета не зависит вовсе."""
    _tier(repo, "convenience")
    g.main(["residuals-accept", "range:money-path", "--operator-confirmed", "осознанно"])
    audit = (tmp_path / "audit.log").read_text()
    assert "residuals-accept" in audit and "range:money-path" in audit
    assert "tier=convenience" in audit


def test_b6_counter_is_per_artifact(repo):
    """Разные артефакты — разные циклы: ревью дизайна не должно съедать бюджет кода."""
    _tier(repo, "convenience")
    g.review_round_record("range:a:branch")
    assert g.review_round_check("range:a:branch")[2], "свой артефакт не исчерпан"
    assert g.review_round_check("range:b:branch")[2] == "", "чужой артефакт задет"


def test_b7_prompt_demands_the_cheapest_fix():
    """Без этого требования ревьюер предлагает ПОЛНОЕ решение, и оно затем строится —
    механика, из-за которой дизайн категории оброс HMAC-обязательствами и CAS."""
    prompt = g._build_reviewer_prompt("дифф", role="blocking")
    assert "CHEAPEST fix" in prompt
    assert "ALREADY control" in prompt
    assert "expected loss" in prompt


def test_rounds_state_survives_corruption(repo):
    """Битый файл счётчика не должен ронять ревью: худшее, что бывает — лишний раунд."""
    state = g._rounds_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text("{битый")
    assert g.review_round_check("range:x:branch")[0] == 1


# ── Находки код-ревью 11.08.2026 (взяты ДЕШЁВЫЕ правки) ─────────────────────────────────

def test_design_artifacts_are_outside_the_budget(repo):
    """Обещание «critical/high дизайн-ревью вне бюджета» было неисполнимым: находки дизайна
    нигде не сохраняются, значит `residuals-accept` их не проверит. Дешёвая правка —
    вывести дизайн из-под механизма ЦЕЛИКОМ, пока нет структурного ledger'а."""
    _tier(repo, "convenience")
    key = "design:docs/plan.md"
    for _ in range(5):
        assert g.review_round_check(key)[2] == "", "дизайн попал под бюджет"
        g.review_round_record(key)
    assert g.is_design_artifact(key) and not g.is_design_artifact("range:a:b")


def test_artifact_key_is_canonical_across_flag_order(repo):
    """Ключ из сырого порядка аргументов давал новый счётчик при перестановке флагов —
    то есть бюджет обходился переписыванием команды."""
    a = g._review_artifact_key(["--base", "abc", "--scope", "branch", "фокус"])
    b = g._review_artifact_key(["--scope", "branch", "--base", "abc", "фокус"])
    assert a == b == "range:abc:branch"


def test_outage_does_not_consume_a_round(repo, monkeypatch, capsys):
    """Три транзиентных сбоя на ярусе decision отказывали бы в ПЕРВОМ состоявшемся ревью.
    Тот же принцип, что у счётчика раундов сходимости при partial-прогоне."""
    _tier(repo, "decision")
    monkeypatch.setattr(g, "_exec_companion", lambda *a, **k: None)   # аутэйдж
    for _ in range(4):
        assert g.main(["companion-review", "--base", "x", "--scope", "branch", "фокус"]) == 2
    assert g.review_round_check("range:x:branch") == (1, 3, ""), "аутэйдж сжёг бюджет"


def test_successful_review_consumes_exactly_one_round(repo, monkeypatch):
    from types import SimpleNamespace
    _tier(repo, "decision")
    monkeypatch.setattr(g, "_exec_companion",
                        lambda *a, **k: SimpleNamespace(
                            returncode=0,
                            stdout="Verdict: approve\n\nNo material findings.\n", stderr=""))
    monkeypatch.setattr(g, "outage_details", lambda _s: None)
    assert g.main(["companion-review", "--base", "x", "--scope", "branch", "фокус"]) == 0
    assert g.review_round_check("range:x:branch")[0] == 2


def test_design_is_declared_by_flag_not_guessed_from_text(repo):
    """Типовая команда скилла передаёт `--base/--scope` и фокус, где путь к дизайну лежит
    ВНУТРИ фразы: угадывание по тексту отправляло дизайн под кодовый бюджет, а код-ревью со
    словом «...md» делало безлимитным (код-ревью 11.08.2026)."""
    skill_like = ["--base", "abc", "--scope", "branch",
                  "Review this design document (docs/plan.md) AND its BSAC."]
    assert g._review_artifact_key(skill_like) == "range:abc:branch"
    assert not g.is_design_artifact(g._review_artifact_key(skill_like))

    declared = ["--design-file", "docs/plan.md", "--base", "abc", "--scope", "branch", "фокус"]
    assert g._review_artifact_key(declared) == "design:docs/plan.md"
    assert g.is_design_artifact(g._review_artifact_key(declared))


def test_exit_zero_garbage_does_not_consume_a_round(repo, monkeypatch, capsys):
    """`You have hit your usage limit` с кодом 0 — не ревью. Без проверки контракта первая же
    деградация съедала весь бюджет и предлагала принять несуществующие остатки."""
    from types import SimpleNamespace
    _tier(repo, "convenience")
    monkeypatch.setattr(g, "_exec_companion", lambda *a, **k: SimpleNamespace(
        returncode=0, stdout="You have hit your usage limit.\n", stderr=""))
    monkeypatch.setattr(g, "outage_details", lambda _s: None)
    # Ответ не отвергается (дизайн-ревью возвращает прозу — она обязана проходить), но
    # раунд за него не засчитывается.
    assert g.main(["companion-review", "--base", "x", "--scope", "branch", "фокус"]) == 0
    assert "НЕ засчитан" in capsys.readouterr().err
    assert g.review_round_check("range:x:branch") == (1, 1, ""), "мусор сжёг бюджет"


@pytest.mark.parametrize("payload", ['{"range:x:branch": {}}', '{"range:x:branch": "два"}',
                                     '{"range:x:branch": -1}', '[1,2,3]'])
def test_corrupt_counter_resets_instead_of_crashing(repo, payload):
    """«Порча восстанавливает бюджет» было реализовано только для битого JSON: валидно
    сериализованная порча роняла ревью TypeError/ValueError уже после чтения."""
    state = g._rounds_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_text(payload)
    assert g.review_round_check("range:x:branch")[0] == 1


def test_corrupt_counter_survives_non_utf8(repo):
    state = g._rounds_path()
    state.parent.mkdir(parents=True, exist_ok=True)
    state.write_bytes(b"\xff\xfe not utf-8")
    assert g.review_round_check("range:x:branch")[0] == 1


def test_refusal_message_quotes_the_artifact(repo):
    """Git принимает `$()`/бэктики в именах ссылок, а сообщение об отказе КОПИРУЕТСЯ в шелл:
    неэкранированная подстановка исполнилась бы под аккаунтом оператора."""
    _tier(repo, "convenience")
    evil = "range:$(touch /tmp/pwned):branch"
    g.review_round_record(evil)
    refusal = g.review_round_check(evil)[2]
    assert refusal
    # Проверяем ПО СУЩЕСТВУ: строка команды, разобранная шеллом, обязана дать артефакт ОДНИМ
    # аргументом. Наличие `$(` внутри кавычек безопасно — опасна подстановка без них.
    import shlex as _shlex
    line = next(l for l in refusal.splitlines() if "residuals-accept" in l)
    assert evil in _shlex.split(line), f"артефакт не пережил разбор шеллом: {line}"
