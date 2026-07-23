"""Детектор дрейфа design-review (тикет #3, спека docs/2026-07-23-design-drift-gate-design.md).
Матрица S1-S11 + back-compat inline. Изоляция: tmp REPO_ROOT/DESIGN_MARKER, CLAUDE_SESSION_ID."""
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


def _body(text: str) -> str:
    # дизайн-контент с распознаваемым заголовком секции (тикет #2: иначе bsac=false → стаб)
    return f"## Сценарии\n{text}\n"


def _write_design(root, name, text):
    body = _body(text)
    (root / name).write_text(body)
    return _h(body)


# ═══ S8/S8b: write-marker --file (reviewed-hash биндинг) ═══

def test_s8_file_binding_match_exit0(env):
    h = _write_design(env, "design.md", "DESIGN v1")
    assert g.add_design_file_binding("d", "design.md", h) == 0
    assert g._marker_state("s1") == "valid" and g.has_marker("s1") is True


def test_s8b_file_binding_mismatch_records_drift(env):
    _write_design(env, "design.md", "DESIGN v2")           # файл уже v2
    rc = g.add_design_file_binding("d", "design.md", _h("DESIGN v1"))  # reviewed_hash от v1
    assert rc == 2                                          # exit 2
    assert g._marker_state("s1") == "drifted"              # маркер невалиден (не «не записан»)
    assert "MISMATCH" in (env / "audit.log").read_text()


# ═══ S3/S4/S5/S10: дрейф файла ═══

def test_s3_valid_then_s4_drift_on_edit(env):
    h = _write_design(env, "design.md", "v1")
    g.add_design_file_binding("d", "design.md", h)
    assert g._marker_state("s1") == "valid"
    (env / "design.md").write_text("v1 + §6a destructive migration")   # правка ПОСЛЕ пометки
    assert g._marker_state("s1") == "drifted"              # S4 — фикс §6a-инцидента


def test_s5_drift_on_missing_file(env):
    h = _write_design(env, "design.md", "v1")
    g.add_design_file_binding("d", "design.md", h)
    (env / "design.md").unlink()
    assert g._marker_state("s1") == "drifted"              # дизайн исчез


def test_s6_other_session_cannot_use_marker(env):
    # пер-сессионные пути: чужая сессия видит СВОЙ (пустой) путь → absent; всё равно deny
    h = _write_design(env, "design.md", "v1")
    g.add_design_file_binding("d", "design.md", h)
    assert g._marker_state("other-session") == "absent"
    assert g.has_marker("other-session") is False          # security: чужой не разблокирует


def test_foreign_branch_mismatched_internal_session(env):
    # защитная ветка: маркер на пути s1, но rec.session=другой → foreign (session ДО дрейфа)
    h = _write_design(env, "design.md", "v1")
    g.add_design_file_binding("d", "design.md", h)
    p = env / ".design-approved-s1"
    rec = json.loads(p.read_text()); rec["session"] = "tampered"; p.write_text(json.dumps(rec))
    assert g._marker_state("s1") == "foreign"


# ═══ S9: ре-ревью самолечит ═══

def test_s9_rereview_heals_drift(env):
    h1 = _write_design(env, "design.md", "v1")
    g.add_design_file_binding("d", "design.md", h1)
    (env / "design.md").write_text(_body("v2"))           # дрейф (контент с BSAC-заголовком)
    assert g._marker_state("s1") == "drifted"
    g.add_design_file_binding("d", "design.md", _h(_body("v2")))   # пере-пометка новым hash
    assert g._marker_state("s1") == "valid"


# ═══ S11 + S8c: мульти-дизайн (набор) ═══

def test_s11_multi_design_edit_first_drifts(env):
    ha = _write_design(env, "A.md", "designA")
    hb = _write_design(env, "B.md", "designB")
    g.add_design_file_binding("A", "A.md", ha)
    g.add_design_file_binding("B", "B.md", hb)
    assert g._marker_state("s1") == "valid"                # оба в наборе
    (env / "A.md").write_text("designA + sneaky")          # правка A ПОСЛЕ пометки B
    assert g._marker_state("s1") == "drifted"              # A в наборе → дрейф (R1-F2)


def test_s8c_mismatch_binding_does_not_leave_prior_valid(env):
    ha = _write_design(env, "A.md", "designA")
    g.add_design_file_binding("A", "A.md", ha)             # A валиден
    _write_design(env, "B.md", "designB v2")               # B уже v2
    rc = g.add_design_file_binding("B", "B.md", _h("designB v1"))   # reviewed от v1
    assert rc == 2
    assert g._marker_state("s1") == "drifted"              # НЕ остаётся валидным на одном A (R2-F1)


def test_binding_merge_preserves_others(env):
    ha = _write_design(env, "A.md", "A")
    hb = _write_design(env, "B.md", "B")
    g.add_design_file_binding("A", "A.md", ha)
    g.add_design_file_binding("B", "B.md", hb)             # не затирает A
    rec = json.loads((env / ".design-approved-s1").read_text())
    files = {b["file"] for b in rec["designs"]}
    assert files == {"A.md", "B.md"}


# ═══ S2/S7: back-compat inline + trivial ═══

def test_s2_inline_backcompat(env):
    g.write_marker("design", "codex approved", design_hash="abc123")
    assert g._marker_state("s1") == "valid"                # легаси inline без дрейф-проверки


def test_inline_empty_hash_invalid(env):
    g.write_marker("design", "no hash", design_hash=None)
    assert g._marker_state("s1") == "invalid"


def test_s7_trivial_valid(env):
    g.write_marker("trivial", "typo fix")
    assert g._marker_state("s1") == "valid"


# ═══ реальный маршрут: gate_edit блокирует правку кода при дрейфе ═══

def test_gate_edit_denies_on_drift(env, monkeypatch):
    monkeypatch.setattr(g, "ONBOARDED", True)
    h = _write_design(env, "design.md", "v1")
    g.add_design_file_binding("d", "design.md", h)
    (env / "design.md").write_text(_body("v1 + new surface"))   # дрейф (контент с заголовком)
    hook = json.dumps({"session_id": "s1", "tool_input": {"file_path": "app/x.py"}})
    assert g.gate_edit_cli(hook) == 2                       # правка кода заблокирована
    g.add_design_file_binding("d", "design.md", _h(_body("v1 + new surface")))   # ре-ревью
    assert g.gate_edit_cli(hook) == 0                       # снова разблокировано


# ═══ CLI write-marker --file ═══

def test_cli_write_marker_file(env):
    h = _write_design(env, "design.md", "v1")
    assert g.main(["write-marker", "design", "approved", h, "--file", "design.md"]) == 0
    assert g.has_marker("s1") is True
    (env / "design.md").write_text("v2")
    assert g.main(["write-marker", "design", "approved", h, "--file", "design.md"]) == 2  # mismatch


# ═══ code-R1: аргумент-ошибки --file + конкурентность ═══

def test_cli_file_flag_without_value_fails_no_marker(env):
    # code-R1 F1: `--file` без пути НЕ должен молча создать валидный inline-маркер
    assert g.main(["write-marker", "design", "d", _h("x"), "--file"]) == 1
    assert g.has_marker("s1") is False              # маркер НЕ записан
    assert g.main(["write-marker", "design", "d", _h("x"), "--file", ""]) == 1
    assert g.has_marker("s1") is False


def test_cli_inline_still_works_and_warns(env, capsys):
    # без --file — inline легаси, но с громким warning
    assert g.main(["write-marker", "design", "approved", "deadbeef"]) == 0
    assert g.has_marker("s1") is True
    assert "защищён от дрейфа" in capsys.readouterr().err


def test_concurrent_bindings_all_survive(env):
    # code-R1 F2: конкурентные add одной сессии не теряют биндинги (per-session lock)
    import threading
    files = [f"d{i}.md" for i in range(12)]
    hashes = {f: _write_design(env, f, f"content-{f}") for f in files}
    barrier = threading.Barrier(len(files))

    def add(f):
        barrier.wait()                              # старт одновременно — провоцируем гонку
        g.add_design_file_binding("d", f, hashes[f])

    threads = [threading.Thread(target=add, args=(f,)) for f in files]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    rec = json.loads((env / ".design-approved-s1").read_text())
    assert {b["file"] for b in rec["designs"]} == set(files)   # ни один не потерян
    assert g._marker_state("s1") == "valid"
