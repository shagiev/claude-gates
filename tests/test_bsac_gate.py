"""Структурная валидация BSAC в design-маркере (тикет #2, спека
docs/2026-07-23-bsac-structural-gate-design.md). Матрица S1-S10 + _has_bsac юниты."""
import hashlib
import json

import pytest

import codex_review_gate as g


def _h(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    return tmp_path


def _put(root, name, text):
    (root / name).write_text(text)
    return _h(text)


# ═══ _has_bsac юниты ═══

def test_has_bsac_recognized():
    for t in ("## BSAC\n...", "## Бизнес-сценарии", "раздел Сценарии", "## EARS — критерии",
              "## Приёмочные критерии", "Acceptance criteria:", "Scenario matrix"):
        assert g._has_bsac(t) is True, t


def test_has_bsac_case_insensitive():
    assert g._has_bsac("## bsac") is True
    assert g._has_bsac("SCENARIO") is True
    assert g._has_bsac("сЦеНаРиИ") is True


def test_has_bsac_ears_token_no_false_positive():
    assert g._has_bsac("EARS") is True
    assert g._has_bsac("## EARS-критерии") is True                  # с дефисом — граница токена
    assert g._has_bsac("over the years appears clearly") is False   # строчное
    assert g._has_bsac("FIVE YEARS PLAN, IT APPEARS") is False      # ВЕРХНИЙ регистр (Codex code-R1)
    assert g._has_bsac("YEARSEARS EARSY") is False                  # EARS внутри слова — не токен
    assert g._has_bsac("just a three-line stub design") is False


def test_stub_with_uppercase_years_blocks(env):
    # code-R1: hash-валидный стаб с 'YEARS' НЕ должен пройти как BSAC
    h = _put(env, "stub.md", "# FIVE YEARS PLAN\nделаем X")
    assert g.add_design_file_binding("d", "stub.md", h) == 2
    assert g._marker_state("s1") == "drifted"


# ═══ S1-S4/S6: распознавание секции ═══

def test_s1_s4_recognized_sections_valid(env):
    for i, body in enumerate(["## BSAC\nтело", "## Бизнес-сценарии\nтело",
                              "## EARS\n- WHEN...", "## Приёмочные критерии\nтело"]):
        f = f"d{i}.md"
        h = _put(env, f, body)
        assert g.add_design_file_binding("d", f, h) == 0, body
    assert g._marker_state("s1") == "valid"


def test_s6_case_insensitive_section(env):
    h = _put(env, "d.md", "## scenario matrix\n...")
    assert g.add_design_file_binding("d", "d.md", h) == 0
    assert g._marker_state("s1") == "valid"


# ═══ S5: стаб без BSAC → биндинг записан + exit2 + drifted ═══

def test_s5_stub_records_binding_and_drifts(env):
    h = _put(env, "stub.md", "трёхстрочный дизайн без матрицы\nделаем X\nготово")
    assert g.add_design_file_binding("d", "stub.md", h) == 2      # exit 2 (совет)
    rec = json.loads((env / ".design-approved-s1").read_text())
    assert any(b["file"] == "stub.md" for b in rec["designs"])    # биндинг ЗАПИСАН
    assert g._marker_state("s1") == "drifted"                     # маркер невалиден
    assert "NO-BSAC" in (env / "audit.log").read_text()


# ═══ S9: стаб B при валидном A рушит coarse-маркер ═══

def test_s9_stub_b_breaks_marker_not_left_valid_on_a(env):
    ha = _put(env, "A.md", "## Сценарии\nполный дизайн A")
    assert g.add_design_file_binding("A", "A.md", ha) == 0
    assert g._marker_state("s1") == "valid"                       # A валиден
    hb = _put(env, "B.md", "стаб B без секций")
    assert g.add_design_file_binding("B", "B.md", hb) == 2        # стаб B
    assert g._marker_state("s1") == "drifted"                     # маркер (A+B) невалиден — НЕ valid на A


# ═══ S10: анти-разъезд версий (reviewed V1 без BSAC, файл был V2 с BSAC, откат к V1) ═══

def test_s10_version_skew_reverts_to_stub_drifts(env):
    v1 = "стаб V1 без секций"
    hv1 = _h(v1)
    (env / "d.md").write_text("## BSAC\nV2 с секцией")            # при пометке файл = V2
    rc = g.add_design_file_binding("d", "d.md", hv1)             # reviewed_hash от V1
    assert rc == 2                                                # mismatch (файл V2 ≠ hV1)
    (env / "d.md").write_text(v1)                                 # откат к V1 (hash совпадёт с hV1)
    assert g._marker_state("s1") == "drifted"                    # _has_bsac(V1)=false → не valid


# ═══ S7: trivial не затронут ═══

def test_s7_trivial_not_validated(env):
    g.write_marker("trivial", "typo")
    assert g._marker_state("s1") == "valid"


# ═══ реальный маршрут: стаб-дизайн → gate_edit блокирует ═══

def test_stub_design_blocks_gate_edit(env, monkeypatch):
    monkeypatch.setattr(g, "ONBOARDED", True)
    h = _put(env, "stub.md", "стаб без матрицы")
    assert g.add_design_file_binding("d", "stub.md", h) == 2
    hook = json.dumps({"session_id": "s1", "tool_input": {"file_path": "app/x.py"}})
    assert g.gate_edit_cli(hook) == 2                             # правка кода заблокирована
    # добавили секцию + перепометили → разблокировано
    body = "## Сценарии\nстаб без матрицы\n+ матрица"
    (env / "stub.md").write_text(body)
    assert g.add_design_file_binding("d", "stub.md", _h(body)) == 0
    assert g.gate_edit_cli(hook) == 0
