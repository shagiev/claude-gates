"""Деплой плагина на флот. Дизайн: docs/2026-08-23-plugin-deploy-design.md.

Свойство, которое здесь проверяется: КАЖДЫЙ шаг утверждает ровно то, что проверил. За две
недели флот трижды разъехался молча именно потому, что «команда сказала updated» и «скрипт не
упал» принимались за доказательства. Поэтому половина тестов — мутационные: они ломают гейт
или возвращают закрытую дыру и требуют, чтобы деплой это ЗАМЕТИЛ.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import deploy_gates as dg          # noqa: E402
import deploy_verify as dv         # noqa: E402


@pytest.fixture()
def install(tmp_path):
    """Полная копия плагина — «установленная версия» на фейковом хосте (нужны и scripts, и
    templates/githooks/gates-run: smoke ходит через шим)."""
    d = tmp_path / "install"
    shutil.copytree(ROOT / "plugins" / "gates", d,
                    ignore=shutil.ignore_patterns("__pycache__"))
    return d


@pytest.fixture()
def harness(monkeypatch, tmp_path):
    """Прогон `main()` со всем зелёным. Тест переопределяет ровно свою строку."""
    state = tmp_path / "state"
    monkeypatch.setattr(dg, "STATE", state)
    monkeypatch.setattr(dg, "LOCK", state / "lock")
    monkeypatch.delenv("GATES_HOSTS", raising=False)
    monkeypatch.setattr(dg, "pre_deploy_gate", lambda: None)
    monkeypatch.setattr(dg, "load_agent", lambda sha: None)
    monkeypatch.setattr(dg, "read_inventory", lambda: [
        {"id": "canary", "channels": ["claude"]}, {"id": "vps", "channels": ["claude"]}])
    monkeypatch.setattr(dg, "pinned_identity", lambda: {
        "version": {"claude": "9.9.9", "codex": "9.9.9+codex"}, "sha": "s" * 40,
        "tree": {"scripts/x.py": "100644 abc"}, "digest": "dig"})
    monkeypatch.setattr(dg, "snapshot", lambda h, c: {
        "host": h, "channels": {"claude": {"state": "installed", "version": "9.9.8",
                                           "sha": "o" * 40, "path": "/p"}}})
    monkeypatch.setattr(dg, "check_rollback_target", lambda *a: None)
    monkeypatch.setattr(dg, "update", lambda h, c: None)
    monkeypatch.setattr(dg, "verify", lambda *a: {"ok": True, "problems": []})
    monkeypatch.setattr(dg, "rollback", lambda h, s: [])
    monkeypatch.setattr(dg, "_drop_mux", lambda hosts: None)
    return monkeypatch


# ── инвентарь: канал объявлен, а не угадан ─────────────────────────────────────────────────
def test_inventory_lives_in_the_repo_and_declares_channels():
    hosts = dg.read_inventory()
    assert hosts and all(h["channels"] for h in hosts)
    assert (ROOT / "deploy-hosts.yaml").exists(), "инвентарь в gitignore не прошёл бы те же гейты"


def test_block_form_channels_are_parsed(monkeypatch, tmp_path):
    """Построчная регулярка понимала только `channels: [a, b]`, а на блочной молча отдавала
    пустой список — забытый канал выглядел бы как «его тут и не было»."""
    monkeypatch.setattr(dg, "ROOT", tmp_path)
    (tmp_path / "deploy-hosts.yaml").write_text(
        "hosts:\n  - id: h1\n    channels:\n      - claude\n      - codex\n")
    assert dg.read_inventory() == [{"id": "h1", "channels": ["claude", "codex"]}]


def test_unknown_channel_is_rejected_before_any_mutation(monkeypatch, tmp_path):
    """С неявным `else` опечатка трактовалась бы как codex и была бы МУТИРОВАНА."""
    monkeypatch.setattr(dg, "ROOT", tmp_path)
    (tmp_path / "deploy-hosts.yaml").write_text("hosts:\n  - id: h1\n    channels: [claud]\n")
    with pytest.raises(SystemExit):
        dg.read_inventory()


def test_host_without_channels_is_rejected(monkeypatch, tmp_path):
    monkeypatch.setattr(dg, "ROOT", tmp_path)
    (tmp_path / "deploy-hosts.yaml").write_text("hosts:\n  - id: h1\n")
    with pytest.raises(SystemExit):
        dg.read_inventory()


def test_gates_hosts_typo_is_named_not_silently_dropped(harness, monkeypatch):
    monkeypatch.setenv("GATES_HOSTS", "vpss")
    with pytest.raises(SystemExit):
        dg.main([])


# ── идентичность выкатки ───────────────────────────────────────────────────────────────────
def test_expected_tree_comes_from_the_commit_not_the_working_tree(tmp_path):
    """Ожидаемое дерево, снятое с диска, было верно лишь потому, что ЧУЖОЙ файл проверял
    чистоту. Смысл единственной идентичности в том, чтобы грязное дерево структурно не могло
    определять, что стоит на флоте.

    Прежняя версия этого теста рабочее дерево не пачкала вовсе и сверяла два ОДИНАКОВЫХ
    значения (`plugins/gates` побайтово совпадает в HEAD и HEAD~1), поэтому не ловила даже
    полное игнорирование аргумента `sha`."""
    head, old = NEW_SHA, OLD_SHA
    now, before = dg.expected_tree(head), dg.expected_tree(old)
    assert now != before, "аргумент sha обязан влиять на результат"
    assert all(len(v.split()[1]) == 40 for v in now.values())

    victim = ROOT / "plugins" / "gates" / "scripts" / "ladder_gate.py"
    backup = victim.read_bytes()
    try:
        victim.write_bytes(backup + "\n# грязь в рабочем дереве\n".encode())
        assert dg.expected_tree(head) == now, "дерево поехало вслед за диском"
    finally:
        victim.write_bytes(backup)


def test_tree_covers_the_whole_plugin_not_just_top_level_scripts():
    """Прежний дайджест брал `*.py` верхнего уровня — 4 файла из 20 — и мимо него проходили
    `hooks.json`, шим `gates-run` и тексты скиллов, объявленные поверхностью гейта."""
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                          capture_output=True, text=True).stdout.strip()
    tree = dg.expected_tree(head)
    assert "hooks/hooks.json" in tree
    assert "templates/githooks/gates-run" in tree
    assert any(k.startswith("skills/") for k in tree)


def test_agent_source_comes_from_the_commit(monkeypatch):
    """`make gates-restore` увозил на хост НЕЗАКОММИЧЕННЫЙ рабочий код и исполнял его там:
    путь `--restore` не зовёт pre-deploy гейт, а агент читался с диска."""
    seen = {}
    def fake_git(*a, **k):
        seen["args"] = a
        return subprocess.CompletedProcess(a, 0, "# из коммита\n", "")

    monkeypatch.setattr(dg.g, "_trusted_git", fake_git)
    monkeypatch.setattr(dg, "_AGENT_SRC", None)
    dg.load_agent("c" * 40)
    assert seen["args"] == ("show", f"{'c' * 40}:scripts/deploy_verify.py")
    assert dg._AGENT_SRC == "# из коммита\n"


def test_agent_missing_from_the_commit_is_fatal(monkeypatch):
    monkeypatch.setattr(dg.g, "_trusted_git",
                        lambda *a, **k: subprocess.CompletedProcess(a, 128, "", "нет такого"))
    with pytest.raises(SystemExit):
        dg.load_agent("c" * 40)


def test_agent_refuses_to_run_before_it_is_loaded(monkeypatch):
    monkeypatch.setattr(dg, "_AGENT_SRC", None)
    with pytest.raises(SystemExit):
        dg.agent("h", "installed", "claude")


def test_version_mismatch_across_manifests_blocks(monkeypatch, tmp_path):
    monkeypatch.setattr(dg, "ROOT", tmp_path)
    for rel, ver in (("plugins/gates/.claude-plugin/plugin.json", "1.0.0"),
                     ("plugins/gates/.codex-plugin/plugin.json", "0.9.9+codex")):
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps({"version": ver}))
    (tmp_path / ".claude-plugin").mkdir(exist_ok=True)
    (tmp_path / ".claude-plugin/marketplace.json").write_text(json.dumps(
        {"metadata": {"version": "1.0.0"}, "plugins": [{"version": "1.0.0"}]}))
    monkeypatch.setattr(dg.g, "_trusted_git", lambda *a, **k: subprocess.CompletedProcess(a, 0,
                                                                                         "x", ""))
    monkeypatch.setattr(dg, "_git_out", lambda *a: "abc")
    with pytest.raises(SystemExit):
        dg.pinned_identity()


# ── состояние установки: три значения, не два ──────────────────────────────────────────────
def _fake_home(tmp_path, meta_text=None, cached=False):
    home = tmp_path / "home"
    (home / ".claude" / "plugins").mkdir(parents=True)
    if meta_text is not None:
        (home / ".claude" / "plugins" / "installed_plugins.json").write_text(meta_text)
    if cached:
        d = home / ".claude/plugins/cache/lenar-gates/gates/0.9.4/scripts"
        d.mkdir(parents=True)
        (d / "ladder_gate.py").write_text("")
    return home


def test_unreadable_metadata_is_undetermined_when_the_shim_would_fall_back(monkeypatch,
                                                                          tmp_path):
    """«Хост не ответил» и «плагин не установлен» — разные факты. Шим на том же отказе уходит в
    глоб-фолбэк и ЗАПУСКАЕТ старшую версию из кэша: назвать это absent значило бы сказать
    «первая установка» про машину, где прямо сейчас работает гейт."""
    monkeypatch.setenv("HOME", str(_fake_home(tmp_path, "{битый", cached=True)))
    assert dv.installed("claude")["state"] == "undetermined"


def test_missing_metadata_is_undetermined_when_a_cached_install_exists(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(_fake_home(tmp_path, cached=True)))
    assert dv.installed("claude")["state"] == "undetermined"


def test_missing_metadata_with_empty_cache_is_absent(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(_fake_home(tmp_path)))
    assert dv.installed("claude")["state"] == "absent"


def test_schema_drift_is_undetermined_not_a_crash(monkeypatch, tmp_path):
    """Дрейф схемы вероятнее синтаксической ошибки, а ловился только второй случай."""
    monkeypatch.setenv("HOME", str(_fake_home(tmp_path, json.dumps(["не тот верхний уровень"]))))
    assert dv.installed("claude")["state"] == "undetermined"


def test_entry_with_unresolvable_path_is_undetermined(monkeypatch, tmp_path):
    """Шим в этом случае уходит в глоб-фолбэк и что-то запускает — версия не определена."""
    monkeypatch.setenv("HOME", str(_fake_home(tmp_path, json.dumps(
        {"plugins": {"gates@lenar-gates": [{"installPath": "/нет/такого", "version": "1.0"}]}}))))
    assert dv.installed("claude")["state"] == "undetermined"


_CODEX_ROWS = """Marketplace `lenar-gates`
/home/u/.codex/.tmp/marketplaces/lenar-gates/.agents/plugins/marketplace.json

PLUGIN             STATUS              VERSION               PATH
gates@lenar-gates  not installed       0.9.5+codex.20260822  /home/u/mkt/plugins/gates
"""


def test_codex_not_installed_is_not_read_as_installed(monkeypatch):
    """`"installed" in line` совпадало с `not installed`: снесённый плагин объявлялся
    установленным, цель отката становилась фикцией, а код `not-installed` для этого канала не
    мог сработать никогда."""
    monkeypatch.setattr(dv.subprocess, "run", lambda argv, **k:
                        subprocess.CompletedProcess(argv, 0, _CODEX_ROWS, ""))
    assert dv.installed("codex")["state"] == "absent"


def test_codex_unparsable_output_is_undetermined(monkeypatch):
    monkeypatch.setattr(dv.subprocess, "run", lambda argv, **k:
                        subprocess.CompletedProcess(argv, 0, "какой-то другой вывод\n", ""))
    assert dv.installed("codex")["state"] == "undetermined"


def test_codex_path_with_a_space_is_parsed(monkeypatch, tmp_path):
    """Путь брался последним токеном: `/Users/John Smith/...` намертво заклинивал деплой."""
    path = tmp_path / "John Smith" / "gates"
    (path / ".codex-plugin").mkdir(parents=True)
    (path / ".codex-plugin" / "plugin.json").write_text(json.dumps({"version": "1.0+codex"}))
    rows = ("PLUGIN             STATUS     VERSION    PATH\n"
            f"gates@lenar-gates  installed  1.0+codex  {path}\n")
    monkeypatch.setattr(dv.subprocess, "run", lambda argv, **k:
                        subprocess.CompletedProcess(argv, 0, "lenar-gates\n" + rows, ""))
    monkeypatch.setattr(dv, "git", lambda cwd, *a: subprocess.CompletedProcess(
        a, 0, str(path) if a[0] == "rev-parse" and "--show-toplevel" in a else "a" * 40, ""))
    got = dv.installed("codex")
    assert got["state"] == "installed" and got["path"] == str(path)


def test_codex_restore_reports_a_failed_checkout(monkeypatch):
    """Иначе откат рапортовал успех, ничего не откатив."""
    monkeypatch.setattr(dv, "git", lambda cwd, *a: subprocess.CompletedProcess(a, 1, "", "нет"))
    out = dv.cmd_restore(json.dumps({"channel": "codex", "snapshot": {
        "state": "installed", "sha": "b" * 40, "toplevel": "/top", "version": "1"}}))
    assert not out["ok"] and out["problems"][0]["code"] == "restore-failed"


def test_unmerged_head_is_refused(monkeypatch, tmp_path):
    """Хосты ставят плагин ИЗ GITHUB: незамерженное выкатить нельзя."""
    monkeypatch.setattr(dg.g, "_trusted_git",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(dg, "_git_out", lambda *a: "a" * 40 if a[-1] == "origin/main" else "b" * 40)
    # Дерево подменено намеренно: иначе тест краснел от «дерево не прочитано» и про сверку
    # HEAD с origin/main не доказывал ничего.
    monkeypatch.setattr(dg, "expected_tree", lambda sha: {"x": "100644 " + "0" * 40})
    with pytest.raises(SystemExit):
        dg.pinned_identity()


def test_host_id_form_is_validated(monkeypatch, tmp_path):
    """id идёт и в `ssh <host>`, и в имя файла снапшота: `../` воспроизводимо уводил запись за
    пределы каталога состояния."""
    monkeypatch.setattr(dg, "ROOT", tmp_path)
    (tmp_path / "deploy-hosts.yaml").write_text(
        "hosts:\n  - id: ../../подброшено\n    channels: [claude]\n")
    with pytest.raises(SystemExit):
        dg.read_inventory()


def test_duplicate_host_ids_are_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(dg, "ROOT", tmp_path)
    (tmp_path / "deploy-hosts.yaml").write_text(
        "hosts:\n  - id: h\n    channels: [claude]\n  - id: h\n    channels: [codex]\n")
    with pytest.raises(SystemExit):
        dg.read_inventory()


def test_snapshot_refuses_when_the_agent_cannot_answer(monkeypatch, tmp_path):
    """DeployAbort, а не SystemExit: отказ на НЕ первом хосте обязан попасть в когортный откат,
    а `fail()` уходил из процесса, оставляя канарейку обновлённой, а второй хост старым."""
    monkeypatch.setattr(dg, "STATE", tmp_path / "s")
    monkeypatch.setattr(dg, "agent", lambda h, c, a: {"error": "ssh timeout"})
    with pytest.raises(dg.DeployAbort):
        dg.snapshot("vps", ["claude"])


# ── верификация по дереву и по поведению ───────────────────────────────────────────────────
def _spec(install, **over):
    want = {"version": "0.10.0", "sha": "s" * 40, "tree": None,
            "checks": ["version", "sha", "tree", "smoke"]}
    want.update(over)
    return json.dumps({"channels": {"claude": want}})


def test_verify_catches_version_skew(install, monkeypatch):
    monkeypatch.setattr(dv, "installed", lambda ch: {
        "state": "installed", "version": "0.9.3", "sha": "s" * 40, "path": str(install)})
    rep = dv.cmd_verify(_spec(install))
    assert not rep["ok"] and rep["problems"][0]["code"] == "version"


def test_verify_problems_are_structured_codes_not_prose(install, monkeypatch):
    """Вердикт отката раньше решался фильтром по подстроке «sha»: чужая проблема с этим словом
    молча зачлась бы как успешный откат, а переформулировка текста перевернула бы вердикт."""
    monkeypatch.setattr(dv, "installed", lambda ch: {
        "state": "installed", "version": "0.9.3", "sha": "z" * 40, "path": str(install)})
    rep = dv.cmd_verify(_spec(install))
    assert {p["code"] for p in rep["problems"]} == {"version", "sha"}
    assert all(set(p) == {"code", "channel", "text"} for p in rep["problems"])


def _full_tree(install):
    """Полная карта установки. Частичная карта делала проверки ложно-зелёными: остальные файлы
    считались лишними, и `problems` был непуст ещё до всякой мутации."""
    tree = {}
    for dp, _dn, fn in os.walk(install):
        for f in fn:
            rel = os.path.relpath(os.path.join(dp, f), install)
            tree[rel] = " ".join(dv.blob_entry(Path(dp) / f))
    return tree


def test_clean_install_has_no_problems(install):
    assert dv.check_tree(install, _full_tree(install)) == []


def test_tree_check_catches_a_tampered_file(install):
    tree = _full_tree(install)
    (install / "scripts" / "ladder_gate.py").write_text("# подменено\n")
    problems = dv.check_tree(install, tree)
    assert any("ladder_gate.py" in p for p in problems), "подмена при верной версии не найдена"


def test_tree_check_catches_exec_bit(install):
    """Прежний sha256-по-содержимому был слеп: тот же класс, что BUG-0.9.0-symlink-tree-hash."""
    tree = _full_tree(install)
    os.chmod(install / "scripts" / "codex_review_gate.py", 0o755)
    assert any("100755" in p for p in dv.check_tree(install, tree))


def test_tree_check_catches_symlink_swap(install):
    tree = _full_tree(install)
    f = install / "scripts" / "codex_review_gate.py"
    (install / "scripts" / "twin.py").write_bytes(f.read_bytes())
    f.unlink()
    os.symlink("twin.py", f)
    problems = dv.check_tree(install, tree)
    assert any("120000" in p for p in problems), "подмена файла симлинком не обнаружена"
    assert any("twin.py" in p for p in problems), "подброшенный файл не обнаружен"


def test_extra_file_under_scripts_is_a_problem(install):
    """`scripts` первым лежит в sys.path того, что из него запускается: подброшенный
    `scripts/json.py` затеняет stdlib при полностью сошедшихся mode+oid."""
    tree = _full_tree(install)
    (install / "scripts" / "json.py").write_text("raise SystemExit(0)\n")
    assert any("json.py" in p for p in dv.check_tree(install, tree))


def test_installer_metadata_is_tolerated(install):
    """Замерено на живых установках обоих каналов: без этого каждый запуск гейта красил бы
    деплой."""
    tree = _full_tree(install)
    (install / ".in_use").mkdir(exist_ok=True)
    (install / ".in_use" / "12345").write_text("")
    (install / "scripts" / "__pycache__").mkdir(exist_ok=True)
    (install / "scripts" / "__pycache__" / "x.cpython-313.pyc").write_text("")
    (install / ".orphaned_at").write_text("2026")
    assert dv.check_tree(install, tree) == []


def test_poisoned_bytecode_is_removed_before_the_proof(install):
    """Подделанный `.pyc` грузится ВМЕСТО исходника: CPython при штатной инвалидации сверяет
    только 8 байт заголовка (mtime и размер), содержимое — нет. Воспроизведено: произвольный
    код исполнялся внутри гейта при зелёном дереве и обеих пройденных smoke."""
    import py_compile
    import struct
    tree = _full_tree(install)
    src = install / "scripts" / "codex_review_gate.py"
    before = src.read_bytes()
    poison = install.parent / "poison.py"
    poison.write_bytes(before + b"\nraise SystemExit('payload')\n")
    tag = f"cpython-{sys.version_info.major}{sys.version_info.minor}"
    pyc = install / "scripts" / "__pycache__" / f"codex_review_gate.{tag}.pyc"
    pyc.parent.mkdir(exist_ok=True)
    py_compile.compile(str(poison), cfile=str(pyc), dfile=str(src))
    st = src.stat()
    raw = bytearray(pyc.read_bytes())
    raw[8:12] = struct.pack("<I", int(st.st_mtime) & 0xFFFFFFFF)
    raw[12:16] = struct.pack("<I", st.st_size & 0xFFFFFFFF)
    pyc.write_bytes(bytes(raw))

    assert src.read_bytes() == before, "атака не трогает исходник — в этом и суть"
    assert dv.check_tree(install, tree) == []
    assert not pyc.exists(), "байткод остался: исполнится он, а захэширован был исходник"


def test_symlinked_directory_hiding_extra_files_is_caught(install):
    """`os.walk` симлинк-каталоги не обходит, а цикл ожиданий читает файлы СКВОЗЬ них:
    подменённый `scripts/` проходил проверку зелёным."""
    tree = _full_tree(install)
    shadow = install.parent / "shadow"
    shutil.copytree(install / "scripts", shadow)
    (shadow / "json.py").write_text("raise SystemExit(0)\n")
    shutil.rmtree(install / "scripts")
    os.symlink(shadow, install / "scripts")
    assert any("симлинк" in p for p in dv.check_tree(install, tree))


def test_the_shipped_gate_never_writes_bytecode():
    """Структурный корень: пока гейт пишет `__pycache__` в каталог установки, слепое пятно
    возвращается при первом же запуске хука."""
    shim = (ROOT / "plugins/gates/templates/githooks/gates-run").read_text()
    assert "python3 -B" in shim and "exec python3 -B" in shim
    hooks = (ROOT / "plugins/gates/hooks/hooks.json").read_text()
    assert "python3 \\\n" not in hooks and "python3 -B" in hooks


def test_missing_file_is_reported(install):
    tree = _full_tree(install)
    (install / "scripts" / "ladder_gate.py").unlink()
    assert any("отсутствует" in p for p in dv.check_tree(install, tree))


def test_path_outside_the_install_is_refused(install):
    """`cmd_verify` — argv-точка входа на удалённом хосте; `..` в карте читал бы чужой файл."""
    assert any("вне установки" in p
               for p in dv.check_tree(install, {"../OUTSIDE.py": "100644 " + "0" * 40}))


# ── поведенческие smoke и их мутации ───────────────────────────────────────────────────────
def test_smoke_proves_the_gate_actually_blocks(install):
    """Сердцевина. «Точка входа не упала» ничего не доказывает: no-op с нулевым кодом такую
    проверку проходит. Гейт обязан ЗАБЛОКИРОВАТЬ код-правку без ревью и ПРОПУСТИТЬ её после
    честной цепочки — и всё это через настоящий шим, которым ходят хуки."""
    assert dv.smoke_gate_blocks(install) is None


def test_smoke_detects_a_gate_that_never_blocks(install):
    src = (install / "scripts" / "ladder_gate.py").read_text()
    (install / "scripts" / "ladder_gate.py").write_text(
        src.replace("def check_precommit(root: Path) -> int:",
                    "def check_precommit(root: Path) -> int:\n    return 0  # сломано", 1))
    why = dv.smoke_gate_blocks(install)
    assert why and "не заблокирована" in why


def test_smoke_detects_a_broken_shim(install):
    """Шим — часть поставки и часть поверхности отказа: сломанный резолв четыре раза оставлял
    обязательного ревьюера слепым."""
    (install / "templates" / "githooks" / "gates-run").write_text("#!/bin/sh\nexit 0\n")
    assert dv.smoke_gate_blocks(install), "нерабочий шим не обнаружен"


def test_smoke_compute_tree_on_hostile_shapes(install):
    assert dv.smoke_compute_tree(install) is None


def test_smoke_detects_restored_filter_hole(install):
    """Если кто-то вернёт применение clean-фильтра, деплой обязан покраснеть."""
    src = (install / "scripts" / "ladder_gate.py").read_text()
    (install / "scripts" / "ladder_gate.py").write_text(src.replace('"--no-filters", ', "", 1))
    assert dv.smoke_compute_tree(install), "подмена содержимого фильтром не обнаружена"


def test_smoke_is_skipped_once_the_version_is_already_wrong(install, monkeypatch):
    """Заведомо не та версия — smoke проверял бы не то, что деплоят, и стоил бы ~2 с на канал."""
    monkeypatch.setattr(dv, "installed", lambda ch: {
        "state": "installed", "version": "0.0.1", "sha": "s" * 40, "path": str(install)})
    ran = []
    monkeypatch.setattr(dv, "SMOKES", (lambda root: ran.append(1),))
    dv.cmd_verify(_spec(install))
    assert not ran


# ── цель отката ────────────────────────────────────────────────────────────────────────────
def test_rollback_target_is_proven_before_mutation(monkeypatch):
    """Прежний `test -f ladder_gate.py` доказывал существование ОДНОГО файла, тогда как
    обещано было совпадение дерева и прохождение smoke."""
    calls = []
    snap = {"channels": {"claude": {"state": "installed", "version": "0.9.4",
                                    "sha": "a" * 40, "path": "/p"}}}
    monkeypatch.setattr(dg.g, "_trusted_git",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, "", ""))
    monkeypatch.setattr(dg, "expected_tree", lambda sha: {"scripts/x.py": "100644 " + "0" * 40})
    monkeypatch.setattr(dg, "agent", lambda h, c, a: calls.append(json.loads(a)) or
                        {"ok": False, "problems": [{"code": "smoke_gate_blocks",
                                                    "channel": "claude", "text": "не блокирует"}]})
    why = dg.check_rollback_target("h", snap)
    assert why and "цель отката НЕ работает" in why
    assert calls[0]["channels"]["claude"]["checks"] == ["version", "sha", "tree", "smoke"]


def test_undetermined_channel_blocks_the_deploy(monkeypatch):
    snap = {"channels": {"claude": {"state": "undetermined", "why": "метаданные битые"}}}
    why = dg.check_rollback_target("h", snap)
    assert why and "не определено" in why


def test_first_install_needs_no_rollback_target(monkeypatch):
    snap = {"channels": {"claude": {"state": "absent", "why": "нет"}}}
    assert dg.check_rollback_target("h", snap) is None


def test_rollback_target_with_unknown_commit_refuses(monkeypatch):
    """Коммита нет локально → ожидаемое дерево не построить → доказать цель нечем."""
    snap = {"channels": {"claude": {"state": "installed", "version": "0.9.4",
                                    "sha": "f" * 40, "path": "/p"}}}
    monkeypatch.setattr(dg.g, "_trusted_git",
                        lambda *a, **k: subprocess.CompletedProcess(a, 1, "", ""))
    why = dg.check_rollback_target("h", snap)
    assert why and "не найден локально" in why


# ── откат ──────────────────────────────────────────────────────────────────────────────────
def test_rollback_restores_every_snapshotted_channel(monkeypatch):
    """Снапшот берётся по всем каналам именно затем, чтобы восстановление одного не оставило
    смешанное состояние — а восстанавливался раньше только Claude."""
    restored, verified = [], []

    def fake_agent(host, cmd, arg):
        spec = json.loads(arg)
        if cmd == "restore":
            restored.append(spec["channel"])
            return {"ok": True, "problems": []}
        verified.append(sorted(spec["channels"]))
        return {"ok": True, "problems": []}

    monkeypatch.setattr(dg, "agent", fake_agent)
    monkeypatch.setattr(dg, "expected_tree", lambda sha: {})
    snap = {"channels": {
        "claude": {"state": "installed", "version": "0.9.4", "sha": "a" * 40, "path": "/p"},
        "codex": {"state": "installed", "version": "0.9.4+c", "sha": "a" * 40, "path": "/q",
                  "toplevel": "/top"}}}
    assert dg.rollback("h", snap) == []
    assert sorted(restored) == ["claude", "codex"]
    assert verified == [["claude", "codex"]], "откат не подтверждён по обоим каналам"


def test_rollback_reports_failure_of_any_channel(monkeypatch):
    monkeypatch.setattr(dg, "agent", lambda h, c, a: {"error": "хост недоступен"})
    snap = {"channels": {"codex": {"state": "installed", "version": "1", "sha": "a" * 40,
                                   "path": "/q", "toplevel": "/top"}}}
    assert dg.rollback("h", snap)


def test_codex_restore_checks_out_the_snapshotted_sha(monkeypatch, tmp_path):
    """У codex нет версионного кэша: каталог плагина — рабочее дерево git-клона маркетплейса,
    поэтому возврат делается checkout'ом снапшотного sha."""
    seen = []
    monkeypatch.setattr(dv, "git", lambda cwd, *a: seen.append((cwd, a)) or
                        subprocess.CompletedProcess(a, 0, "", ""))
    out = dv.cmd_restore(json.dumps({"channel": "codex", "snapshot": {
        "state": "installed", "sha": "b" * 40, "toplevel": "/top", "version": "1"}}))
    assert out["ok"] and seen == [("/top", ("checkout", "-q", "--detach", "b" * 40))]


def test_restore_without_a_target_fails_loudly():
    out = dv.cmd_restore(json.dumps({"channel": "claude",
                                     "snapshot": {"state": "undetermined"}}))
    assert not out["ok"]


def test_restore_to_absence_removes_the_channel(monkeypatch):
    """Канал, установленный ЭТИМ деплоем, обязан быть снят: пропуск таких каналов возвращал
    пустой список проблем, и вызывающий печатал «откат проверен»."""
    seen = []
    monkeypatch.setattr(dv.subprocess, "run", lambda argv, **k: seen.append(argv) or
                        subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(dv, "installed", lambda ch: {"state": "absent"})
    out = dv.cmd_restore(json.dumps({"channel": "codex", "snapshot": {
        "state": "absent", "toplevel": "/песочница"}}))
    assert out["ok"] and seen == [["codex", "plugin", "remove", "gates@lenar-gates"]]


def test_restore_without_an_explicit_target_refuses_to_mutate():
    """Реальный инцидент 23.08.2026: спек без адреса цели по умолчанию бил в текущий `~`, и
    тест снёс живой плагин на рабочей машине командой `claude plugin uninstall`."""
    out = dv.cmd_restore(json.dumps({"channel": "claude", "snapshot": {"state": "absent"}}))
    assert not out["ok"] and out["problems"][0]["code"] == "no-target"


def test_restore_to_absence_reports_failure_if_the_channel_survives(monkeypatch):
    monkeypatch.setattr(dv.subprocess, "run", lambda argv, **k:
                        subprocess.CompletedProcess(argv, 0, "", ""))
    monkeypatch.setattr(dv, "installed", lambda ch: {"state": "installed", "version": "1"})
    out = dv.cmd_restore(json.dumps({"channel": "codex", "snapshot": {
        "state": "absent", "toplevel": "/песочница"}}))
    assert not out["ok"] and out["problems"][0]["code"] == "remove-failed"


def test_installed_channel_without_sha_blocks_rollback_confirmation(monkeypatch):
    """Иначе канал молча выпадал из постпроверки, и «откат проверен» печаталось про канал,
    который не проверяли вовсе."""
    monkeypatch.setattr(dg, "agent", lambda h, c, a: {"ok": True, "problems": []})
    snap = {"channels": {"claude": {"state": "installed", "version": "1", "path": "/p"}}}
    assert dg.rollback("h", snap)


# ── порядок выкатки ────────────────────────────────────────────────────────────────────────
def test_canary_failure_leaves_other_hosts_untouched(harness):
    touched = []
    harness.setattr(dg, "update", lambda h, c: touched.append(h) or None)
    harness.setattr(dg, "verify", lambda *a: {"ok": False, "problems": [
        {"code": "smoke", "channel": "claude", "text": "сломано"}]})
    assert dg.main([]) == 2
    assert touched == ["canary"], f"тронуты лишние хосты: {touched}"


def test_second_host_failure_rolls_back_the_whole_cohort(harness):
    rolled = []
    harness.setattr(dg, "verify", lambda h, *a: {
        "ok": h == "canary",
        "problems": [] if h == "canary" else [{"code": "tree", "channel": "claude",
                                               "text": "сломано"}]})
    harness.setattr(dg, "rollback", lambda h, s: rolled.append(h) or [])
    assert dg.main([]) == 2
    assert set(rolled) == {"canary", "vps"}, f"откачена не вся когорта: {rolled}"


def test_stable_is_written_only_when_every_host_is_green(harness, tmp_path):
    assert dg.main([]) == 0
    stable = json.loads((tmp_path / "state" / "stable").read_text())
    assert stable["hosts"] == ["canary", "vps"] and stable["sha"] == "s" * 40


def test_subset_run_does_not_certify_the_fleet(harness, monkeypatch, capsys, tmp_path):
    monkeypatch.setenv("GATES_HOSTS", "canary")
    assert dg.main([]) == 3
    assert not (tmp_path / "state" / "stable").exists()
    assert "ЧАСТИЧНО" in capsys.readouterr().err


def test_concurrent_deploy_fails_loudly(harness, tmp_path):
    (tmp_path / "state").mkdir(parents=True)
    (tmp_path / "state" / "lock").write_text("pid=1 sha=abc")
    with pytest.raises(SystemExit):
        dg.main([])


def test_lock_is_taken_atomically(monkeypatch, tmp_path):
    """`exists()` + `write_text()` оставляли окно между проверкой и записью, куда помещается
    второй прогон."""
    monkeypatch.setattr(dg, "STATE", tmp_path)
    monkeypatch.setattr(dg, "LOCK", tmp_path / "lock")
    dg._take_lock("a" * 40)
    with pytest.raises(SystemExit):
        dg._take_lock("b" * 40)


# ── pre-deploy гейт принадлежит мутирующему скрипту ────────────────────────────────────────
def test_pre_deploy_gate_refuses_to_run_under_pytest():
    """Иначе прогон тестов рекурсивно запустил бы сам себя — и тест, забывший подменить гейт,
    молча висел бы вместо того, чтобы упасть."""
    with pytest.raises(SystemExit):
        dg.pre_deploy_gate()
    # ...и именно на своей причине: без `match` тест был зелёным от чужого `fail()` («дерево
    # грязное»), а снятие guard'а в чистом дереве не покраснело бы, а ПОВЕСИЛО прогон.
    import io
    import contextlib
    err = io.StringIO()
    with contextlib.redirect_stderr(err), pytest.raises(SystemExit):
        dg.pre_deploy_gate()
    assert "pytest" in err.getvalue()


def test_direct_invocation_still_runs_the_pre_deploy_gate(harness):
    """`python3 scripts/deploy_gates.py` — документированный прямой вызов; когда гейт жил в
    Makefile, через него флот мутировался без проверки дерева и тестов вовсе."""
    called = []
    harness.setattr(dg, "pre_deploy_gate", lambda: called.append(1))
    dg.main([])
    assert called == [1]


# ── интеграция: настоящий деплой на подставной хост ────────────────────────────────────────
#
# Мутационное ревью показало, что 32 правки из 63 оставляли модульные тесты зелёными: `agent`,
# `on_host`, `update`, `verify`, `cmd_verify`, `cmd_installed`, `_installed_codex` и claude-ветка
# отката не исполнялись НИ РАЗУ — они везде были замоканы. Здесь подменяется ровно одна
# функция, `dg.on_host`: единственная точка выхода наружу. Всё остальное настоящее.

NEW_SHA = subprocess.run(["git", "rev-parse", "HEAD"], cwd=ROOT,
                         capture_output=True, text=True).stdout.strip()
OLD_SHA = "0150db00655fe0eeaa9fca9b37b8a6d7e34f8fe8"          # 0.9.4, дерево плагина отличается

_FAKE_CLAUDE = '''#!/usr/bin/env python3
"""Подставной `claude`, воспроизводящий задокументированный инцидент: `plugin update` при
УСТАРЕВШЕМ каталоге печатает пустой вывод с кодом 0, ничего не изменив."""
import json, os, pathlib, sys
home = pathlib.Path(os.environ["HOME"])
(home / "cli.log").open("a").write(" ".join(sys.argv[1:]) + "\\n")
fresh = home / ".mkt-fresh"
if sys.argv[1:4] == ["plugin", "marketplace", "update"]:
    fresh.write_text("x"); sys.exit(0)
if sys.argv[1:3] == ["plugin", "update"]:
    if not fresh.exists():
        sys.exit(0)                       # каталог устарел: тишина и код 0
    meta = home / ".claude/plugins/installed_plugins.json"
    d = json.loads(meta.read_text())
    d["plugins"]["gates@lenar-gates"][0].update(
        {"installPath": os.environ["FAKE_NEW"], "version": os.environ["FAKE_NEW_VER"],
         "gitCommitSha": os.environ["FAKE_NEW_SHA"]})
    meta.write_text(json.dumps(d)); sys.exit(0)
if sys.argv[1:3] == ["plugin", "uninstall"]:
    meta = home / ".claude/plugins/installed_plugins.json"
    meta.write_text(json.dumps({"plugins": {}})); sys.exit(0)
sys.exit(1)
'''


def _extract(sha, dest):
    dest.mkdir(parents=True)
    tar = subprocess.run(["git", "archive", sha, "plugins/gates"], cwd=ROOT,
                         capture_output=True, check=True).stdout
    subprocess.run(["tar", "-x", "-C", str(dest), "--strip-components=2"],
                   input=tar, check=True)
    return dest


@pytest.fixture()
def fake_host(tmp_path, monkeypatch):
    """Хост целиком: дом, реестр установок, две версии в кэше и CLI, который РЕАЛЬНО правит
    реестр — и врёт ровно так, как врал настоящий."""
    home = tmp_path / "home"
    cache = home / ".claude" / "plugins" / "cache" / "lenar-gates" / "gates"
    old = _extract(OLD_SHA, cache / "0.9.4")
    new = _extract(NEW_SHA, cache / "0.9.5")
    old_ver = json.loads((old / ".claude-plugin" / "plugin.json").read_text())["version"]
    new_ver = json.loads((new / ".claude-plugin" / "plugin.json").read_text())["version"]
    meta = home / ".claude" / "plugins" / "installed_plugins.json"
    meta.write_text(json.dumps({"plugins": {"gates@lenar-gates": [
        {"scope": "user", "installPath": str(old), "version": old_ver,
         "gitCommitSha": OLD_SHA}]}}))
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "claude").write_text(_FAKE_CLAUDE)
    os.chmod(bin_dir / "claude", 0o755)
    env = {**os.environ, "HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
           "FAKE_NEW": str(new), "FAKE_NEW_VER": new_ver, "FAKE_NEW_SHA": NEW_SHA}
    for var in ("GIT_DIR", "GIT_INDEX_FILE", "GIT_WORK_TREE"):
        env.pop(var, None)

    monkeypatch.setattr(dg, "on_host", lambda host, argv, stdin=None: subprocess.run(
        argv, capture_output=True, text=True, input=stdin, env=env))
    monkeypatch.setattr(dg, "STATE", tmp_path / "state")
    monkeypatch.setattr(dg, "LOCK", tmp_path / "state" / "lock")
    monkeypatch.setattr(dg, "pre_deploy_gate", lambda: None)
    monkeypatch.delenv("GATES_HOSTS", raising=False)
    monkeypatch.setattr(dg, "read_inventory", lambda: [{"id": "h", "channels": ["claude"]}])
    # В проде агент едет из коммита; здесь проверяется рабочая копия, поэтому загрузка
    # подменяется явно. Сам `load_agent` покрыт отдельно — иначе подмена скрыла бы, что он
    # вообще не читает коммит.
    monkeypatch.setattr(dg, "load_agent", lambda sha: monkeypatch.setattr(
        dg, "_AGENT_SRC", (ROOT / "scripts" / "deploy_verify.py").read_text()))
    tree = dg.expected_tree(NEW_SHA)
    monkeypatch.setattr(dg, "pinned_identity", lambda: {
        "version": {"claude": new_ver}, "sha": NEW_SHA, "tree": tree,
        "digest": dv.tree_digest(tree)})
    return {"home": home, "old": old, "new": new, "meta": meta, "state": tmp_path / "state",
            "old_ver": old_ver, "new_ver": new_ver, "monkeypatch": monkeypatch}


def _registry(fh):
    return json.loads(fh["meta"].read_text())["plugins"]["gates@lenar-gates"][0]


def test_end_to_end_deploy_updates_the_host_and_certifies_it(fake_host):
    """Полный маршрут настоящим кодом: снапшот → доказанная цель отката → обновление →
    верификация деревом и обеими smoke → `stable`."""
    assert dg.main([]) == 0
    assert _registry(fake_host)["version"] == fake_host["new_ver"]
    assert _registry(fake_host)["gitCommitSha"] == NEW_SHA
    stable = json.loads((fake_host["state"] / "stable").read_text())
    assert stable["sha"] == NEW_SHA and stable["hosts"] == ["h"]
    log = (fake_host["home"] / "cli.log").read_text().splitlines()
    assert log[0].startswith("plugin marketplace update"), "каталог обновляется ПЕРВЫМ"
    assert log[1].startswith("plugin update")


def test_cli_that_reports_success_without_updating_is_caught_and_rolled_back(fake_host):
    """Тот самый инцидент: без обновления каталога `plugin update` печатает пустой вывод с кодом
    0 и ничего не меняет. Раньше это и был «успешный деплой»."""
    fake_host["monkeypatch"].setitem(
        dv.CHANNELS["claude"], "update", (["plugin", "update", "gates@lenar-gates"],))
    assert dg.main([]) == 2
    assert _registry(fake_host)["version"] == fake_host["old_ver"], "хост не возвращён"
    assert not (fake_host["state"] / "stable").exists()


def test_tampered_file_on_the_host_fails_the_deploy(fake_host):
    """Верная версия, верный sha, подменённый файл — деплой обязан покраснеть и откатиться."""
    (fake_host["new"] / "scripts" / "ladder_gate.py").write_text("# подменено\n")
    assert dg.main([]) == 2
    assert _registry(fake_host)["version"] == fake_host["old_ver"]


def test_extra_shadowing_module_fails_the_deploy(fake_host):
    """`scripts/json.py` затеняет stdlib для всего, что запускается из этого каталога: mode+oid
    всех 20 закреплённых файлов сходятся, а исполняется чужой код."""
    (fake_host["new"] / "scripts" / "json.py").write_text("raise SystemExit(0)\n")
    assert dg.main([]) == 2
    assert _registry(fake_host)["version"] == fake_host["old_ver"]


def test_broken_rollback_target_stops_before_touching_the_host(fake_host):
    """Цель отката не работает → мутации не происходит вовсе."""
    (fake_host["old"] / "templates" / "githooks" / "gates-run").unlink()
    assert dg.main([]) == 2
    assert _registry(fake_host)["version"] == fake_host["old_ver"]
    assert (fake_host["home"] / "cli.log").exists() is False, "хост тронут до проверки цели"


def test_transport_failure_is_never_read_as_absent(fake_host):
    """Главный заявленный инвариант агента. Отказ подсовывается ТОЛЬКО агенту: если ронять всё
    подряд, тест зеленеет от падения `update` и про агента не доказывает ничего."""
    real = dg.on_host

    def only_agent_fails(host, argv, stdin=None):
        if argv[:2] == ["python3", "-"]:
            return subprocess.CompletedProcess(argv, 255, "", "ssh: connection timed out")
        return real(host, argv, stdin)

    fake_host["monkeypatch"].setattr(dg, "on_host", only_agent_fails)
    assert dg.main([]) == 2
    assert not (fake_host["home"] / "cli.log").exists(), "мутация при неотвечающем агенте"
    assert not (fake_host["state"] / "stable").exists()


def test_failing_cli_stops_the_deploy(fake_host):
    """`update` обязан читать код возврата: молчаливое игнорирование rc — это ровно «команда
    сказала updated»."""
    real = dg.on_host
    fake_host["monkeypatch"].setattr(dg, "on_host", lambda h, argv, stdin=None:
                                     subprocess.CompletedProcess(argv, 3, "", "CLI упал")
                                     if argv[0] == "claude" else real(h, argv, stdin))
    assert dg.main([]) == 2
    assert _registry(fake_host)["version"] == fake_host["old_ver"]


def test_update_reports_a_failing_cli(monkeypatch):
    """Прямо, а не сквозь деплой: сквозной тест зеленел от последующего расхождения версии и
    про чтение rc не доказывал ничего."""
    monkeypatch.setattr(dg, "on_host", lambda h, argv, stdin=None:
                        subprocess.CompletedProcess(argv, 3, "", "CLI упал"))
    why = dg.update("h", ["claude"])
    assert why and "rc=3" in why


def test_broken_git_on_the_host_is_caught_by_smoke_alone(fake_host):
    """Дерево сходится побайтово, а гейт на этом хосте неисполним. Только поведенческая
    проверка это видит — и без неё деплой объявил бы хост зелёным."""
    real_git = subprocess.run(["which", "git"], capture_output=True, text=True).stdout.strip()
    bin_dir = fake_host["home"].parent / "bin"
    (bin_dir / "git").write_text(f'#!/bin/sh\ncase "$1" in init) exit 1 ;; esac\n'
                                 f'exec {real_git} "$@"\n')
    os.chmod(bin_dir / "git", 0o755)
    assert dg.main([]) == 2
    assert _registry(fake_host)["version"] == fake_host["old_ver"]


def test_tampered_skill_text_is_caught_by_the_tree_alone(fake_host):
    """Тексты скиллов — объявленная поверхность гейта, и ни одна smoke их не исполняет. Только
    сверка дерева отличает подмену инструкции от исправной поставки."""
    victim = next((fake_host["new"] / "skills").rglob("*.md"))
    victim.write_text(victim.read_text() + "\n<!-- подменённая инструкция -->\n")
    assert dg.main([]) == 2
    assert _registry(fake_host)["version"] == fake_host["old_ver"]


def _verify_install(install, monkeypatch, checks=("smoke",)):
    monkeypatch.setattr(dv, "installed", lambda ch: {
        "state": "installed", "version": "v", "sha": "s", "path": str(install)})
    return dv.cmd_verify(json.dumps({"channels": {"claude": {
        "version": "v", "sha": "s", "tree": None, "checks": list(checks)}}}))


def test_delivery_without_the_shim_fails_the_smoke_itself(install, monkeypatch):
    """Фолбэк на свой лаунчер включался ровно на том отказе, ради которого smoke написана."""
    (install / "templates" / "githooks" / "gates-run").unlink()
    rep = _verify_install(install, monkeypatch)
    assert not rep["ok"], "поставка без шима прошла smoke зелёной"


def test_cmd_verify_runs_the_blocking_smoke(install, monkeypatch):
    """Через `cmd_verify`, а не прямым вызовом: в проде smoke зовёт только он, и выпадение
    проверки из набора обязано быть видно. Подменённый шим ломает ИМЕННО эту smoke — вторая
    его не касается."""
    (install / "templates" / "githooks" / "gates-run").write_text("#!/bin/sh\nexit 0\n")
    rep = _verify_install(install, monkeypatch)
    assert [p["code"] for p in rep["problems"]] == ["smoke_gate_blocks"]


def test_cmd_verify_runs_the_tree_hash_smoke(install, monkeypatch):
    """Симметрично: возвращённая дыра clean-фильтра ломает ТОЛЬКО проверку compute_tree."""
    src = (install / "scripts" / "ladder_gate.py").read_text()
    (install / "scripts" / "ladder_gate.py").write_text(src.replace('"--no-filters", ', "", 1))
    rep = _verify_install(install, monkeypatch)
    assert "smoke_compute_tree" in [p["code"] for p in rep["problems"]]
