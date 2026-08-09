"""Локальная Фаза 2: интерфейс к inframon (спека docs/2026-07-23-inframon-interface-design.md).
Ф1 authoritative baseline (B1-B12): pin секции deploy, no-fallback, env-переходы с аудитом.
Ф2 вердикт деплой-гейта (V1-V8): delete-then-write под локом, скипы видимы."""
import json
from types import SimpleNamespace

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
    # REPO_ROOT здесь — не git-репозиторий, а закреплённый git работает именно в нём (F19).
    # Эти тесты про запись вердикта, git им безразличен.
    monkeypatch.setattr(g, "git_head", lambda: HEAD_SHA)
    monkeypatch.setattr(g, "diff_sha256", lambda base, head=None: "d" * 64)
    # ancestry теперь тоже идёт через доверенный слой (обход через шим закрыт), а REPO_ROOT
    # здесь — не репозиторий: отвечаем успехом, эти тесты про запись вердикта
    _real_tg = g._trusted_git
    # baseline закрепляется в неизменяемый OID (подвижная ссылка не может быть границей
    # ревью), поэтому `rev-parse ...^{commit}` тоже отвечаем сами: REPO_ROOT здесь не репозиторий.
    def _tg(*a, **kw):
        if a and a[0] == "merge-base":
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        if a and a[0] == "rev-parse" and str(a[-1]).endswith("^{commit}"):
            return SimpleNamespace(returncode=0, stdout=str(a[-1])[:-len("^{commit}")] + "\n",
                                   stderr="")
        return _real_tg(*a, **kw)

    monkeypatch.setattr(g, "_trusted_git", _tg)
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
    adapter = env.parent / "b2-adapter"
    adapter.write_text(f"#!/bin/sh\necho {H}\n")
    adapter.chmod(0o755)
    sec = _sec(cmd=str(adapter))
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
    # Адаптер — исполняемый файл ВНЕ репозитория, без аргументов (см. тест ниже).
    adapter = env.parent / "adapter"
    adapter.write_text(f"#!/bin/sh\necho {H}\n")
    adapter.chmod(0o755)
    assert g._run_baseline_command(str(adapter), 10) == H
    adapter.write_text(f"#!/bin/sh\necho {H.upper()}\n")
    assert g._run_baseline_command(str(adapter), 10) == H       # регистр нормализуется
    adapter.write_text("#!/bin/sh\necho nope\n")
    assert g._run_baseline_command(str(adapter), 10) is None
    assert g._run_baseline_command("/usr/bin/false", 10) is None
    assert g._run_baseline_command("", 10) is None


def test_baseline_command_form_is_closed(env, monkeypatch, capsys):
    """Форма закрыта аллоулистом, а не проверками: РОВНО один абсолютный исполняемый файл вне
    репозитория, без аргументов. Три раунда security-ревью подряд обходили лексические
    проверки — последним шелл-пейлоадом, в котором ни один аргумент не похож на путь."""
    monkeypatch.setattr(g, "REPO_ROOT", env)
    (env / "ops").mkdir(parents=True, exist_ok=True)
    script = env / "ops" / "deployed-sha"
    script.write_text(f"#!/bin/sh\necho {H}\n")
    script.chmod(0o755)

    for cmd in ("/bin/sh ops/deployed-sha", f"/bin/sh {script}", f"/usr/bin/python3 {script}",
                "/bin/sh -c 'cd \"$HOME\"; cd src; cd claude-gates; cd ops; sh deployed-sha'",
                f"/bin/echo {H}"):
        assert g._run_baseline_command(cmd, 10) is None, cmd
        assert "РОВНО один аргумент" in capsys.readouterr().err, cmd

    assert g._run_baseline_command("./ops/deployed-sha", 10) is None      # относительный
    assert "АБСОЛЮТНЫМ" in capsys.readouterr().err
    assert g._run_baseline_command(str(script), 10) is None               # внутри репозитория
    assert "ВНУТРЬ проверяемого" in capsys.readouterr().err

    # `/bin/sh` — валидный «один абсолютный файл без аргументов». С унаследованным stdin он
    # становится диспетчером, а с cwd=каталог состояния — ещё и подделывает baseline-маркер.
    import subprocess as _sp
    state = env.parent / "state"
    state.mkdir(exist_ok=True)
    monkeypatch.setattr(g, "_gate_state_dir", lambda: state)
    monkeypatch.setattr(g, "GATE_BASELINE", state / "review-baseline")
    payload = f"printf %s {H} > review-baseline; printf %s {H}\n"
    r, w = __import__("os").pipe()
    __import__("os").write(w, payload.encode()); __import__("os").close(w)
    old_stdin = _sp.sys.stdin if hasattr(_sp, "sys") else None
    import os as _os
    saved = _os.dup(0)
    _os.dup2(r, 0)
    try:
        out = g._run_baseline_command("/bin/sh", 10)
    finally:
        _os.dup2(saved, 0); _os.close(saved); _os.close(r)
    assert out is None, "шелл прочитал stdin и вернул подделанный baseline"
    assert not (state / "review-baseline").exists(), "payload записал гейтовый маркер"

    # cwd не должен выбираться вызывающим: `TemporaryDirectory()` уважает TMPDIR, и
    # репозиторный Makefile ставил его внутрь репозитория — инструмент, ищущий конфиг вверх
    # по дереву, подхватывал бы файл проверяемой стороны.
    monkeypatch.setenv("TMPDIR", str(env))
    probe = env.parent / "cwd-probe"
    probe.write_text("#!/bin/sh\npwd\n")
    probe.chmod(0o755)
    monkeypatch.setattr(g.subprocess, "run", _sp.run)
    seen = {}
    real_run = _sp.run

    def capture(argv, **kw):
        seen["cwd"] = kw.get("cwd")
        return real_run(argv, **kw)

    monkeypatch.setattr(g.subprocess, "run", capture)
    g._run_baseline_command(str(probe), 10)
    assert seen["cwd"] == "/", f"cwd выбирается вызывающим: {seen['cwd']}"

    # Симлинк ВНУТРИ репозитория, указывающий наружу: цель прошла бы проверку, а исполнялся
    # бы изменяемый симлинк — значит отвергаем и по лексическому пути.
    link = env / "gitshim"
    link.symlink_to("/bin/echo")
    assert g._run_baseline_command(str(link), 10) is None
    assert "ВНУТРЬ проверяемого" in capsys.readouterr().err


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


def test_v1_fresh_allow_writes_verdict(env, tmp_path, monkeypatch, clean_pair):
    _allow_env(monkeypatch, tmp_path)
    import pathlib
    fix = pathlib.Path(__file__).parent / "fixtures" / "stub_companion_pass.sh"
    monkeypatch.setenv("CODEX_COMPANION_CMD", f"bash {fix}")
    assert g.check_reviewed_cli() == 0
    v = _read_verdict()
    assert v["schema"] == 2 and v["gates"]["codex"] == "allow"   # схема 2: + providers
    assert v["gates"]["ladder"] == "covered" and v["gates"]["empirical"] == "not-configured"
    assert v["head_sha"] == g.git_head() and v["run_id"]


def test_v2_cached_allow_writes_verdict(env, tmp_path, monkeypatch, clean_pair):
    _allow_env(monkeypatch, tmp_path)
    head, diff = g.git_head(), g.diff_sha256("HEAD~1")
    # Кэш обязан быть снят ТОЙ ЖЕ панелью: легаси-запись без `reviewers` больше не годится
    # (B13 — approve другой панели не удовлетворяет обязательную пару).
    panel, err = g.resolve_portable_review_plan("portable")
    assert panel is not None, err
    g.write_ledger(head, diff, "HEAD~1",
                   g.parse_review_output("Verdict: approve\nNo material findings.\n"),
                   reviewers=[g._cert_cache_record(c, "blocking") for c in panel])
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


def test_v7_historical_ladder_skips_visible(env, tmp_path, monkeypatch, clean_pair):
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
    def fake_run(argv, cwd=None, capture_output=None, text=None, timeout=None, env=None,
                 stdin=None):
        calls["timeout"] = timeout
        raise sp.TimeoutExpired(argv, timeout)
    monkeypatch.setattr(g.subprocess, "run", fake_run)
    sleeper = env.parent / "sleeper"
    sleeper.write_text("#!/bin/sh\nsleep 5\n")
    sleeper.chmod(0o755)
    assert g._run_baseline_command(str(sleeper), 1) is None
    assert calls["timeout"] == 1                            # переданный baseline_timeout_s
