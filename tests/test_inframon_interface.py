"""Локальная Фаза 2: интерфейс к inframon (спека docs/2026-07-23-inframon-interface-design.md).
Ф1 authoritative baseline (B1-B12): pin секции deploy, no-fallback, env-переходы с аудитом.
Ф2 вердикт деплой-гейта (V1-V8): delete-then-write под локом, скипы видимы."""
import json

import pytest

import codex_review_gate as g

H = "a" * 40           # валидный «SHA» от команды
HEAD_SHA = "b" * 40


def _sec(cmd="echo " + H, to=None):
    s = {"baseline_command": cmd}
    if to is not None:
        s["baseline_timeout_s"] = to             # именно baseline_timeout_s (code-R1 F3)
    return s


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(g, "DEPLOY_PIN", tmp_path / ".deploy-section-pin")
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(g, "VERDICT_DIR", tmp_path / "verdicts")
    return tmp_path


def _states(monkeypatch, head_state, base_state=("absent", None)):
    """Стаб _config_section_at_ref: head_ref=HEAD_SHA → head_state, прочее → base_state."""
    monkeypatch.setattr(g, "_config_section_at_ref",
                        lambda root, ref, section: head_state if ref == HEAD_SHA else base_state)


# ═══ Ф1: resolve_baseline_gate (B1-B12) ═══

def test_b1_env_only_used(env, monkeypatch):
    _states(monkeypatch, ("absent", None))
    monkeypatch.setenv("CODEX_DEPLOY_BASELINE", H)
    assert g._resolve_baseline_gate(HEAD_SHA) == (H, 0)


def test_b2_command_valid_sha_wins_over_file(env, monkeypatch):
    sec = _sec(cmd=f"echo {H}")
    _states(monkeypatch, ("enabled", sec))
    g._write_pin(g._deploy_section_hash(sec))                    # pin одобрен
    monkeypatch.setattr(g, "resolve_baseline", lambda: "stale-file-sha")
    baseline, rc = g._resolve_baseline_gate(HEAD_SHA)
    assert rc == 0 and baseline == H                             # файл игнорируется


def test_b3_command_fails_blocks_no_fallback(env, monkeypatch, capsys):
    for cmd in ("false", "definitely-not-a-cmd-xyz", "sleep 5"):
        sec = _sec(cmd=cmd, to=1)
        _states(monkeypatch, ("enabled", sec))
        g._write_pin(g._deploy_section_hash(sec))
        monkeypatch.setattr(g, "resolve_baseline", lambda: "stale")   # фолбэк ЗАПРЕЩЁН
        baseline, rc = g._resolve_baseline_gate(HEAD_SHA)
        assert rc == 2 and baseline is None, cmd
    assert "ЗАПРЕЩЁН" in capsys.readouterr().err


def test_b4_command_garbage_output_blocks(env, monkeypatch):
    for cmd in ("echo not-a-sha", "true", f"echo {H[:20]}"):     # мусор/пусто/короткий
        sec = _sec(cmd=cmd)
        _states(monkeypatch, ("enabled", sec))
        g._write_pin(g._deploy_section_hash(sec))
        assert g._resolve_baseline_gate(HEAD_SHA)[1] == 2, cmd


def test_b5_b12_absent_both_legacy_and_bootstrap_pin(env, monkeypatch):
    _states(monkeypatch, ("absent", None), ("absent", None))     # head И baseline absent
    monkeypatch.setattr(g, "resolve_baseline", lambda: H)
    baseline, rc = g._resolve_baseline_gate(HEAD_SHA)
    assert rc == 0 and baseline == H                             # легаси-файл (B5)
    assert g._read_pin() == "disabled"                           # bootstrap-сентинел записан (B12)


def test_b6_nothing_none_passthrough(env, monkeypatch):
    _states(monkeypatch, ("absent", None))
    monkeypatch.setattr(g, "resolve_baseline", lambda: None)
    assert g._resolve_baseline_gate(HEAD_SHA) == (None, 0)       # решает существующий R1-2
    assert g._read_pin() is None                                 # без baseline bootstrap не завершён


def test_b7_env_with_command_audited(env, monkeypatch):
    sec = _sec()
    _states(monkeypatch, ("enabled", sec))
    monkeypatch.setenv("CODEX_DEPLOY_BASELINE", H)
    assert g._resolve_baseline_gate(HEAD_SHA) == (H, 0)
    audit = (env / "audit.log").read_text()
    assert "перебил authoritative" in audit                      # B7
    assert "pin переход" in audit                                # EARS-3b (активация через env)
    assert g._read_pin() == g._deploy_section_hash(sec)          # pin записан


def test_b8_head_unreadable_blocks(env, monkeypatch):
    _states(monkeypatch, ("unreadable", None))
    assert g._resolve_baseline_gate(HEAD_SHA)[1] == 2


def test_b9_first_activation_without_env_blocks(env, monkeypatch):
    _states(monkeypatch, ("enabled", _sec()))                    # pin отсутствует
    assert g._resolve_baseline_gate(HEAD_SHA)[1] == 2


def test_b9_section_change_vs_pin_blocks(env, monkeypatch):
    sec = _sec(cmd=f"echo {H}")
    _states(monkeypatch, ("enabled", sec))
    g._write_pin(g._deploy_section_hash(_sec(cmd="echo other")))  # pin от ДРУГОЙ секции
    assert g._resolve_baseline_gate(HEAD_SHA)[1] == 2


def test_b9b_corrupt_pin_blocks(env, monkeypatch):
    sec = _sec()
    _states(monkeypatch, ("enabled", sec))
    (env / ".deploy-section-pin").write_text("{broken")
    assert g._resolve_baseline_gate(HEAD_SHA)[1] == 2


def test_b10_self_hiding_change_caught_before_command(env, monkeypatch):
    # изменённая команда вернула бы HEAD (пустой диапазон) — но pin ловит ДО исполнения
    evil = _sec(cmd=f"echo {HEAD_SHA}")                          # возвращает сам HEAD
    _states(monkeypatch, ("enabled", evil))
    g._write_pin(g._deploy_section_hash(_sec()))                 # pin от прежней секции
    baseline, rc = g._resolve_baseline_gate(HEAD_SHA)
    assert rc == 2                                               # заблокировано pin-сверкой


def test_b11_removed_section_with_enabled_pin_blocks(env, monkeypatch):
    _states(monkeypatch, ("absent", None))
    g._write_pin(g._deploy_section_hash(_sec()))                 # pin помнит enabled
    assert g._resolve_baseline_gate(HEAD_SHA)[1] == 2


def test_b11_bootstrap_baseline_had_command_blocks(env, monkeypatch):
    # pin absent (новая машина), секция absent на head, но на легаси-baseline была enabled
    _states(monkeypatch, ("absent", None), ("enabled", _sec()))
    monkeypatch.setattr(g, "resolve_baseline", lambda: H)
    assert g._resolve_baseline_gate(HEAD_SHA)[1] == 2            # удаление без перехода


def test_removal_transition_via_env_audited(env, monkeypatch):
    # EARS-3b: удаление секции через env — pin переход АУДИРУЕТСЯ (command на head отсутствует)
    _states(monkeypatch, ("absent", None))
    g._write_pin(g._deploy_section_hash(_sec()))
    monkeypatch.setenv("CODEX_DEPLOY_BASELINE", H)
    assert g._resolve_baseline_gate(HEAD_SHA) == (H, 0)
    assert "pin переход" in (env / "audit.log").read_text()
    assert g._read_pin() == "disabled"


# ═══ юниты _run_baseline_command / _deploy_section_hash ═══

def test_run_baseline_command_validation(env, monkeypatch):
    monkeypatch.setattr(g, "REPO_ROOT", env)
    assert g._run_baseline_command(f"echo {H}", 10) == H
    assert g._run_baseline_command(f"echo {H.upper()}", 10) == H   # регистр нормализуется
    assert g._run_baseline_command("echo nope", 10) is None
    assert g._run_baseline_command("false", 10) is None
    assert g._run_baseline_command("", 10) is None


def test_section_hash_stable_ordering():
    a = g._deploy_section_hash({"baseline_command": "x", "baseline_timeout_s": 5})
    b = g._deploy_section_hash({"baseline_timeout_s": 5, "baseline_command": "x"})
    assert a == b                                                # sort_keys → порядок не важен


# ═══ Ф2: вердикт (V1-V8) ═══

def _allow_env(monkeypatch, tmp_path):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path / "ledger")
    monkeypatch.setattr(g, "LAST_REVIEWED", tmp_path / ".lr")
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "_resolve_baseline_gate", lambda head: ("HEAD~1", 0))
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 0)
    monkeypatch.setattr(g, "_empirical_gate", lambda baseline, head: 0)
    monkeypatch.setattr(g, "_empirical_config", lambda root, ref: ("absent", None, 600))


def _read_verdict():
    files = [p for p in g.VERDICT_DIR.iterdir() if p.suffix == ".json"]
    assert len(files) == 1
    return json.loads(files[0].read_text())


def test_v1_fresh_allow_writes_verdict(env, tmp_path, monkeypatch):
    _allow_env(monkeypatch, tmp_path)
    import pathlib
    fix = pathlib.Path(__file__).parent / "fixtures" / "stub_companion_pass.sh"
    monkeypatch.setenv("CODEX_COMPANION_CMD", f"bash {fix}")
    assert g.check_reviewed_cli() == 0
    v = _read_verdict()
    assert v["schema"] == 1 and v["gates"]["codex"] == "allow"
    assert v["gates"]["ladder"] == "covered" and v["gates"]["empirical"] == "not-configured"
    assert v["head_sha"] == g.git_head() and v["run_id"]


def test_v2_cached_allow_writes_verdict(env, tmp_path, monkeypatch):
    _allow_env(monkeypatch, tmp_path)
    head, diff = g.git_head(), g.diff_sha256("HEAD~1")
    g.write_ledger(head, diff, "HEAD~1",
                   g.parse_review_output("Verdict: approve\nNo material findings.\n"))
    assert g.check_reviewed_cli() == 0
    assert _read_verdict()["gates"]["codex"] == "cached"


def test_v3_all_skips_visible(env, tmp_path, monkeypatch):
    _allow_env(monkeypatch, tmp_path)
    monkeypatch.setattr(g, "_resolve_baseline_gate", lambda head: (None, 0))
    monkeypatch.setenv("LADDER_SKIP", "1")
    monkeypatch.setenv("CODEX_REVIEW_SKIP", "1")
    monkeypatch.setenv("EMPIRICAL_SKIP", "1")
    assert g.check_reviewed_cli() == 0
    gates = _read_verdict()["gates"]
    assert gates == {"ladder": "skipped", "empirical": "skipped", "codex": "skipped"}


def test_v4_block_no_verdict(env, tmp_path, monkeypatch):
    _allow_env(monkeypatch, tmp_path)
    monkeypatch.setenv("CODEX_COMPANION_CMD", "bash -c 'exit 7'")   # outage → блок
    assert g.check_reviewed_cli() == 2
    assert not g.VERDICT_DIR.exists() or not list(g.VERDICT_DIR.glob("*.json"))


def test_v5_write_oserror_loud_but_allows(env, monkeypatch, capsys):
    def boom(path, obj, indent=None):
        raise OSError("disk full")
    monkeypatch.setattr(g, "_atomic_write_json", boom)
    rc = g._write_deploy_verdict(HEAD_SHA, H, "d" * 64, "covered", "pass", "allow")
    assert rc == 0                                               # allow стоит
    assert "вердикт НЕ записан" in capsys.readouterr().err
    assert not (g.VERDICT_DIR / f"{HEAD_SHA}.json").exists()     # файла нет → consumer честен


def test_v5b_unlink_failure_with_existing_blocks(env, monkeypatch):
    g.VERDICT_DIR.mkdir(parents=True, exist_ok=True)
    old = g.VERDICT_DIR / f"{HEAD_SHA}.json"
    old.write_text('{"schema":1,"gates":{"codex":"allow"}}')     # старый чистый вердикт
    real_unlink = type(old).unlink
    def bad_unlink(self, missing_ok=False):
        if self.name == f"{HEAD_SHA}.json":
            raise OSError("EPERM")
        return real_unlink(self, missing_ok=missing_ok)
    monkeypatch.setattr(type(old), "unlink", bad_unlink)
    rc = g._write_deploy_verdict(HEAD_SHA, H, "d" * 64, "skipped", "skipped", "skipped")
    assert rc == 2                                               # старый маскировал бы скипы


def test_v7_historical_ladder_skips_visible(env, tmp_path, monkeypatch):
    _allow_env(monkeypatch, tmp_path)
    monkeypatch.setattr(g, "_ladder_range_skips", lambda baseline: ["deadbeef"])
    head, diff = g.git_head(), g.diff_sha256("HEAD~1")
    g.write_ledger(head, diff, "HEAD~1",
                   g.parse_review_output("Verdict: approve\nNo material findings.\n"))
    assert g.check_reviewed_cli() == 0
    assert _read_verdict()["gates"]["ladder"] == "covered-with-skips"


def test_invalid_command_key_blocks_not_legacy(env, monkeypatch):
    # code-R1 F2: присутствующий невалидный baseline_command НЕ откатывает тихо к легаси
    for bad in ([1, 2], "", "   ", 42):
        _states(monkeypatch, ("enabled", {"baseline_command": bad}))
        g._write_pin("disabled")                            # даже при disabled-pin
        monkeypatch.setattr(g, "resolve_baseline", lambda: "stale")
        assert g._resolve_baseline_gate(HEAD_SHA)[1] == 2, bad


def test_v5b_any_unlink_oserror_blocks(env, monkeypatch):
    # code-R1 F1: любой OSError unlink = блок, без exists()-проверки (stat может врать)
    g.VERDICT_DIR.mkdir(parents=True, exist_ok=True)
    target = g.VERDICT_DIR / f"{HEAD_SHA}.json"
    def bad_unlink(self, missing_ok=False):
        raise OSError("I/O error")
    monkeypatch.setattr(type(target), "unlink", bad_unlink)
    assert g._write_deploy_verdict(HEAD_SHA, H, "d" * 64, "covered", "pass", "allow") == 2


def test_b3_timeout_branch_really_times_out(env, monkeypatch):
    # code-R1 F3: подтверждаем вход именно в TimeoutExpired-ветку
    import subprocess as sp
    calls = {}
    def fake_run(argv, cwd=None, capture_output=None, text=None, timeout=None):
        calls["timeout"] = timeout
        raise sp.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(g.subprocess, "run", fake_run)
    assert g._run_baseline_command("sleep 5", 1) is None
    assert calls["timeout"] == 1                            # переданный baseline_timeout_s
