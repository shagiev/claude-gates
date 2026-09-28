"""`make recertify`: перекалибровка Claude-ревьюера одной командой.

Дизайн: docs/2026-09-29-reviewer-recertify-design.md (S1–S6, E1–E6). Все тесты работают на
tmp-копии мира (реестр, отчёты, корпус, манифесты); страж ниже сверяет боевые файлы до/после.
"""
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

import certify_reviewers as cr
import codex_review_gate as g

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import recertify as rc          # noqa: E402

_VT = "Verd" + "ict:"
_CLEAN = f"{_VT} approve\n\nNo material findings.\n"
_BLOCK = f"{_VT} needs-attention\n\n- [high] реальная проблема (app/x.py:1)\n"
CATEGORIES = sorted(cr.REQUIRED_CATEGORIES)
TODAY = "20260929"


def _live_files():
    plug = ROOT / "plugins" / "gates"
    paths = [plug / "reviewer_certifications.json", plug / ".claude-plugin" / "plugin.json",
             plug / ".codex-plugin" / "plugin.json", ROOT / ".claude-plugin" / "marketplace.json",
             ROOT / "CHANGELOG.md", *sorted((plug / "reviewer_corpus" / "reports").iterdir())]
    return {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}


@pytest.fixture(autouse=True)
def _live_state_untouched():
    """E6: ни один тест не пишет в боевой реестр, отчёты или манифесты."""
    before = _live_files()
    yield
    assert _live_files() == before


def _write_json(path, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")


@pytest.fixture
def world(tmp_path, monkeypatch):
    corpus = tmp_path / "corpus" / "cases.json"
    _write_json(corpus, {"schema": 1, "policy_id": "portable-review-v2", "cases": [
        {"id": f"c-{cat}", "category": cat, "diff": "OK" if cat == "benign" else "BLOCK",
         "expect_blocking": cat != "benign", "required": True, "forbidden_output": []}
        for cat in CATEGORIES]})
    registry = tmp_path / "plugin" / "reviewer_certifications.json"
    _write_json(registry, {"schema": 2, "policy_id": "portable-review-v2", "certifications": [
        {"provider": "codex", "adapter": "codex-companion", "requested_model": "gpt-9",
         "actual_models": ["gpt-9"], "family": "openai", "roles": ["blocking"],
         "certification_id": "codex-gpt-9-1", "status": "candidate", "attestation": "declared"},
        # легаси-форма: алиасом, как стояло до 29.09.2026
        {"provider": "claude", "adapter": "claude-cli", "requested_model": "opus",
         "actual_models": ["claude-old-1"], "family": "anthropic", "roles": ["blocking"],
         "certification_id": "claude-old-1-blocking-20260101", "status": "candidate",
         "attestation": "verified"},
    ]})
    reports = tmp_path / "plugin" / "reviewer_corpus" / "reports"
    reports.mkdir(parents=True)
    root = tmp_path / "root"
    _write_json(root / "plugins/gates/.claude-plugin/plugin.json", {"name": "gates",
                                                                     "version": "1.2.3"})
    _write_json(root / "plugins/gates/.codex-plugin/plugin.json",
                {"name": "gates", "version": "1.2.3+codex.20260101"})
    _write_json(root / ".claude-plugin/marketplace.json", {
        "metadata": {"version": "1.2.3"}, "plugins": [{"name": "gates", "version": "1.2.3"}]})
    (root / "CHANGELOG.md").write_text("# Changelog\n\n## 1.2.3 — 2026-01-01\n\nстарое\n")
    for attr, val in (("_CORPUS_PATH", corpus), ("_CERTIFICATION_REGISTRY", registry),
                      ("_REPORTS_DIR", reports)):
        monkeypatch.setattr(g, attr, val)
    monkeypatch.setattr(cr, "CORPUS", corpus)
    state = SimpleNamespace(tmp=tmp_path, root=root, registry=registry, reports=reports,
                            corpus=corpus, lock=tmp_path / "recertify.lock",
                            fail_dir=tmp_path / "failed", calls=0, fail=False, hook=None)

    def fake_review(diff, *, role, allow_candidate):
        state.calls += 1
        if state.hook:
            state.hook()
        model = g.claude_blocking_model(allow_candidate=True)
        assert model is not None, "раннер обязан видеть единственную candidate-запись цели"
        text = _CLEAN if state.fail or "BLOCK" not in diff else _BLOCK
        return (text, model, "", {"models_seen": [model]}, "ok")

    monkeypatch.setattr(g, "run_claude_review_text", fake_review)

    def run(target="claude-old-1", probe=None):
        return rc.run(target, root=root, lock_path=state.lock, fail_dir=state.fail_dir,
                      today=TODAY, probe=probe or (lambda: pytest.fail("проба не ожидалась")))

    state.run = run
    return state


def _snapshot(world):
    return {str(p.relative_to(world.tmp)): p.read_bytes()
            for p in sorted(world.tmp.rglob("*")) if p.is_file() and p != world.lock
            and world.fail_dir not in p.parents
            and p.suffix != ".log"}        # изолированный аудит conftest пишет законно


def _claude_entries(world):
    return [c for c in json.loads(world.registry.read_text())["certifications"]
            if c["provider"] == "claude"]


def _version(world, rel):
    return json.loads((world.root / rel).read_text())


def test_pins_legacy_alias_entry_to_exact_model(world):
    """S2 (+миграция): алиасная запись → закреплённая certified с валидной связкой."""
    assert world.run("claude-old-1") == 0
    [entry] = _claude_entries(world)
    assert entry["requested_model"] == entry["actual_models"][0] == "claude-old-1"
    assert entry["status"] == "certified"
    assert entry["certification_id"] == "claude-old-1-blocking-20260929"
    # постусловие, а не действие: тот же валидирующий путь, что у прода
    assert g.reviewer_certification("claude", "claude-old-1", "blocking") is not None
    assert g.claude_blocking_model() == "claude-old-1"
    assert _version(world, "plugins/gates/.claude-plugin/plugin.json")["version"] == "1.2.4"
    assert (_version(world, "plugins/gates/.codex-plugin/plugin.json")["version"]
            == "1.2.4+codex.20260929")
    market = _version(world, ".claude-plugin/marketplace.json")
    assert market["metadata"]["version"] == market["plugins"][0]["version"] == "1.2.4"
    changelog = (world.root / "CHANGELOG.md").read_text()
    assert changelog.startswith("# Changelog\n\n## 1.2.4 — 2026-09-29")
    assert "`opus (claude-old-1)` → `claude-old-1`" in changelog and "## 1.2.3" in changelog
    codex = json.loads(world.registry.read_text())["certifications"][0]
    assert codex["certification_id"] == "codex-gpt-9-1"              # чужая запись не тронута


def test_upgrade_replaces_entry_and_old_report(world):
    assert world.run("claude-old-1") == 0
    assert world.run("claude-new-2") == 0
    [entry] = _claude_entries(world)
    assert entry["actual_models"] == ["claude-new-2"]
    assert sorted(p.name for p in world.reports.iterdir()) == [
        "claude-new-2-blocking-20260929.json"]
    assert g.claude_blocking_model() == "claude-new-2"
    assert _version(world, "plugins/gates/.claude-plugin/plugin.json")["version"] == "1.2.5"


def test_up_to_date_changes_nothing(world):
    """S4: эффективно действующая сертификация → выход 0, ни байта изменений, без прогона."""
    assert world.run("claude-old-1") == 0
    before, calls = _snapshot(world), world.calls
    assert world.run("claude-old-1") == 0
    assert _snapshot(world) == before and world.calls == calls


@pytest.mark.parametrize("damage", ["report-missing", "report-altered", "corpus-changed"])
def test_declared_but_invalid_certification_is_rerun(world, damage):
    """S4b: запись «certified», но загрузчик её понизит — это НЕ «актуально»."""
    assert world.run("claude-old-1") == 0
    report = world.reports / "claude-old-1-blocking-20260929.json"
    if damage == "report-missing":
        report.unlink()
    elif damage == "report-altered":
        report.write_bytes(report.read_bytes() + b" ")
    else:
        body = json.loads(world.corpus.read_text())
        case = next(c for c in body["cases"] if c["expect_blocking"])
        case["diff"] = "BLOCK changed"
        _write_json(world.corpus, body)
    assert g.reviewer_certification("claude", "claude-old-1", "blocking") is None
    calls = world.calls
    assert world.run("claude-old-1") == 0
    assert world.calls > calls
    assert g.reviewer_certification("claude", "claude-old-1", "blocking") is not None
    assert report.exists()           # тот же день → тот же путь: новый отчёт, не удалён как «старый»


def test_failed_corpus_leaves_everything_byte_identical(world):
    """S3: провал → побайтово прежнее, отчёт провала вне reports/."""
    assert world.run("claude-old-1") == 0
    before = _snapshot(world)
    world.fail = True
    assert world.run("claude-new-2") == 2
    assert _snapshot(world) == before
    failed = list(world.fail_dir.iterdir())
    assert len(failed) == 1 and json.loads(failed[0].read_text())["pass"] is False


def test_runner_exception_rolls_back(world):
    assert world.run("claude-old-1") == 0
    before = _snapshot(world)

    def boom():
        raise RuntimeError("сеть упала")
    world.hook = boom
    assert world.run("claude-new-2") == 2
    assert _snapshot(world) == before


def test_sigterm_mid_run_rolls_back_and_restores_handler(world):
    assert world.run("claude-old-1") == 0
    before = _snapshot(world)
    previous = signal.getsignal(signal.SIGTERM)
    world.hook = lambda: os.kill(os.getpid(), signal.SIGTERM)
    assert world.run("claude-new-2") == 2
    assert _snapshot(world) == before
    assert signal.getsignal(signal.SIGTERM) == previous


def test_fault_at_every_mutation_point_rolls_back(world, monkeypatch):
    """E4: сбой после КАЖДОЙ точки мутации → полный набор побайтово прежний."""
    assert world.run("claude-old-1") == 0
    before = _snapshot(world)
    real_write, real_unlink = rc._write, rc._unlink
    hit = 0
    n = 0
    while True:
        counter = {"i": 0}

        def maybe_fail(real):
            def wrapper(*a, **k):
                if counter["i"] == n:
                    counter["i"] += 1
                    raise OSError("диск кончился")
                counter["i"] += 1
                return real(*a, **k)
            return wrapper
        monkeypatch.setattr(rc, "_write", maybe_fail(real_write))
        monkeypatch.setattr(rc, "_unlink", maybe_fail(real_unlink))
        code = world.run("claude-new-2")
        if counter["i"] <= n:                   # точка n не достигнута: прогон прошёл целиком
            assert code == 0
            break
        assert code == 2, f"сбой в точке {n}"
        assert _snapshot(world) == before, f"откат неполон после сбоя в точке {n}"
        hit += 1
        n += 1
    assert hit >= 5          # реестр ×2, отчёт, 3 манифеста, CHANGELOG, удаление старого отчёта


def test_concurrent_run_is_refused(world):
    """S6: второй запуск при занятом замке — отказ до любой мутации."""
    assert world.run("claude-old-1") == 0
    before, calls = _snapshot(world), world.calls
    with open(world.lock, "w") as held:
        fcntl.flock(held, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert world.run("claude-new-2") == 2
    assert _snapshot(world) == before and world.calls == calls


def test_two_claude_blocking_entries_are_refused(world):
    """E1: неоднозначный реестр не чинится угадыванием."""
    body = json.loads(world.registry.read_text())
    extra = dict(body["certifications"][1], requested_model="claude-x-2",
                 actual_models=["claude-x-2"], certification_id="claude-x-2-blocking-1")
    body["certifications"].append(extra)
    _write_json(world.registry, body)
    before = _snapshot(world)
    assert world.run("claude-new-2") == 2
    assert _snapshot(world) == before and world.calls == 0


@pytest.mark.parametrize("target", ["gpt-9", "../x", "claude-A B", "claude-", "claude-x/y", ""])
def test_invalid_target_is_refused_before_any_write(world, target):
    """E3: цель становится certification_id и путём отчёта — валидируется до мутаций."""
    before = _snapshot(world)
    assert world.run(target) == 2
    assert _snapshot(world) == before and world.calls == 0


def test_probe_resolves_target_and_its_failure_changes_nothing(world):
    assert world.run(None, probe=lambda: "claude-old-1") == 0
    before = _snapshot(world)

    def broken():
        raise rc.RecertifyError("claude CLI не найден")
    assert world.run(None, probe=broken) == 2
    assert _snapshot(world) == before


def _envelope(usage, **extra):
    return json.dumps({"is_error": False, "result": "ok", "modelUsage": usage, **extra})


def test_parse_probe_takes_the_model_that_wrote_the_answer():
    out = _envelope({"claude-new-2": {"outputTokens": 40},
                     "claude-haiku-4-5-20251001": {"outputTokens": 3}})
    assert rc.parse_probe(out) == "claude-new-2"


@pytest.mark.parametrize("stdout", [
    "не json",
    json.dumps({"is_error": True, "modelUsage": {"claude-a-1": {"outputTokens": 5}}}),
    _envelope({}),
    _envelope({"gpt-9": {"outputTokens": 5}}),                                   # чужое семейство
    _envelope({"claude-a-1": {"outputTokens": 5}, "claude-b-2": {"outputTokens": 5}}),  # ничья
    _envelope({"../../x": {"outputTokens": 5}}),
    _envelope({"claude-a-1": {}}),                                                # нет счётчика
])
def test_parse_probe_refuses_unattributable_answers(stdout):
    with pytest.raises(rc.RecertifyError):
        rc.parse_probe(stdout)


def test_postcondition_rejects_a_report_the_gate_would_not_accept(world, monkeypatch):
    """Раннер отчитался «пройдено», но гейт связку не примет (корпус раннера ≠ корпус гейта):
    записать такую «сертификацию» значило бы молча выключить Claude-слот при деплое."""
    assert world.run("claude-old-1") == 0
    before = _snapshot(world)
    real = cr.run_provider

    def skewed(provider, reps):
        code, report = real(provider, reps)
        return code, dict(report, corpus_sha256="0" * 64)
    monkeypatch.setattr(cr, "run_provider", skewed)
    assert world.run("claude-new-2") == 2
    assert _snapshot(world) == before


def test_bug_class_exception_is_raised_after_rollback(world):
    """Исключения не глотаются: TypeError — баг, он всплывает, но файлы уже восстановлены."""
    assert world.run("claude-old-1") == 0
    before = _snapshot(world)

    def bug():
        raise TypeError("баг в раннере")
    world.hook = bug
    with pytest.raises(TypeError):
        world.run("claude-new-2")
    assert _snapshot(world) == before


_SECRET = "sk-" + "Q" * 44


@pytest.mark.parametrize("stdout", [
    _envelope({_SECRET: {"outputTokens": 5}}),                                     # одиночный
    _envelope({"claude-a-1": {"outputTokens": 5}, _SECRET: {"outputTokens": 5}}),  # ничья
    _envelope({_SECRET: {}}),                                                      # без счётчика
])
def test_probe_diagnostics_redact_untrusted_model_names(stdout):
    with pytest.raises(rc.RecertifyError) as exc:
        rc.parse_probe(stdout)
    assert _SECRET not in str(exc.value)


def test_make_target_takes_no_parameters():
    """Через make значение не передаётся вовсе: make раскрывает `$(…)` в нём и в рецепте, и
    в окружении рецепта — дважды латанный класс закрыт отсутствием входа."""
    code = [ln for ln in (ROOT / "Makefile").read_text().splitlines()
            if ln.strip() and not ln.lstrip().startswith("#")]
    target = next(i for i, ln in enumerate(code) if ln.startswith("recertify:"))
    assert code[target + 1] == "\tpython3 -B scripts/recertify.py"
    assert not any("MODEL" in ln for ln in code)


def test_explicit_target_comes_only_from_argv(monkeypatch):
    seen = {}

    def fake_run(target, **_kw):
        seen["t"] = target
        return 0
    monkeypatch.delenv("MODEL", raising=False)
    monkeypatch.setattr(rc, "run", fake_run)
    monkeypatch.setattr(rc, "_git_common_dir", lambda _root: Path("/нет/такого"))
    monkeypatch.setattr(rc.g, "_gate_state_dir", lambda: None)
    assert rc.main(["--model", "claude-x-1"]) == 0 and seen["t"] == "claude-x-1"
    assert rc.main([]) == 0 and seen["t"] is None


def test_model_in_environment_is_refused_not_ignored(monkeypatch):
    """`make recertify MODEL=x` (make экспортирует его в окружение) не должен молча уйти на
    модель алиаса."""
    monkeypatch.setenv("MODEL", "claude-x-1")
    monkeypatch.setattr(rc, "run", lambda *_a, **_k: pytest.fail("перекалибровка не ожидалась"))
    assert rc.main([]) == 2
    assert rc.main(["--model", "claude-x-1"]) == 2


def test_second_sigterm_during_rollback_does_not_break_it(world, monkeypatch):
    assert world.run("claude-old-1") == 0
    before = _snapshot(world)
    real_restore = rc._restore
    fired = []

    def restore_under_fire(path, data):
        if not fired:
            fired.append(1)
            os.kill(os.getpid(), signal.SIGTERM)          # повторный сигнал посреди отката
        real_restore(path, data)
    monkeypatch.setattr(rc, "_restore", restore_under_fire)

    def boom():
        raise RuntimeError("первый отказ")
    world.hook = boom
    assert world.run("claude-new-2") == 2
    assert fired and _snapshot(world) == before


def test_write_leaves_no_temp_file_on_failure(tmp_path, monkeypatch):
    target = tmp_path / "reg.json"
    target.write_bytes(b"old")

    def broken_replace(*_a):
        raise OSError("диск")
    monkeypatch.setattr(rc.os, "replace", broken_replace)
    with pytest.raises(OSError):
        rc._write(target, b"new")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["reg.json"]
    assert target.read_bytes() == b"old"


@pytest.fixture
def probe_env(tmp_path, monkeypatch):
    """Проба без живого CLI: бинарь-заглушка и перехват subprocess.run."""
    sterile = tmp_path / "sterile"
    sterile.mkdir()
    monkeypatch.setattr(rc.g, "_resolve_claude_bin", lambda: "/нет/такого/claude")
    monkeypatch.setattr(rc.g, "_sterile_mkdtemp", lambda _prefix: str(sterile))
    monkeypatch.setattr(rc.g, "_certified_subprocess_env", lambda: {"HOME": "/h", "PATH": "/p"})
    seen = {}

    def answer(result):
        def fake_run(cmd, **kw):
            seen.update(cmd=cmd, **kw)
            if isinstance(result, BaseException):
                raise result
            return result
        monkeypatch.setattr(rc.subprocess, "run", fake_run)
    return SimpleNamespace(seen=seen, answer=answer, sterile=sterile)


def test_probe_runs_isolated_like_the_reviewer_adapter(probe_env):
    probe_env.answer(SimpleNamespace(returncode=0, stderr="", stdout=_envelope(
        {"claude-new-2": {"outputTokens": 7}})))
    assert rc.probe_alias() == "claude-new-2"
    seen = probe_env.seen
    assert seen["cmd"] == rc.g.claude_cmd("/нет/такого/claude", rc.PROBE_ALIAS)
    assert seen["cwd"] == str(probe_env.sterile)             # не репозиторий
    assert seen["env"] == {"HOME": "/h", "PATH": "/p"}         # аллоулист, не окружение вызывающего
    assert not probe_env.sterile.exists()                      # стерильный cwd убран


@pytest.mark.parametrize("case", ["no-bin", "no-sterile", "timeout", "nonzero"])
def test_probe_failures_are_refusals(probe_env, monkeypatch, case):
    if case == "no-bin":
        monkeypatch.setattr(rc.g, "_resolve_claude_bin", lambda: None)
    elif case == "no-sterile":
        monkeypatch.setattr(rc.g, "_sterile_mkdtemp", lambda _prefix: None)
    elif case == "timeout":
        probe_env.answer(rc.subprocess.TimeoutExpired("claude", 1))
    else:
        probe_env.answer(SimpleNamespace(returncode=1, stdout="", stderr="auth " + _SECRET))
    with pytest.raises(rc.RecertifyError) as exc:
        rc.probe_alias()
    assert _SECRET not in str(exc.value)
    if case in ("no-bin", "no-sterile"):
        assert "cmd" not in probe_env.seen                     # до CLI не дошли
