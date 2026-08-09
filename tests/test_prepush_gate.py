"""Pre-push интеграционный гейт (спека docs/2026-07-26-prepush-integration-gate-design.md,
ред. 5, цикл ревью закрыт на хард-капе).

Матрица — сценарии С1-С20 спеки. Каждый тест ссылается на свой сценарий/EARS: спека и есть
тест-матрица, и расхождение между ними должно быть видно по имени.

Гейт обещает ОДНО: слияние с базой не конфликтует. Тесты на пробе ничего не гарантируют (И5),
поэтому проверяется не «зелёные тесты = можно пушить», а наличие обязательной пометки.
"""
import os
import subprocess

import pytest

import prepush_gate as pg

ZEROS = "0" * 40


def _git(root, *args, check=True):
    return subprocess.run(["git", *args], cwd=root, check=check, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                               "PATH": os.environ["PATH"], "HOME": str(root.parent)})


@pytest.fixture()
def repo(tmp_path):
    """Репо с bare-remote «origin» и веткой main: гейт целиком про git, мокать его нечем."""
    remote = tmp_path / "remote.git"
    remote.mkdir()
    subprocess.run(["git", "init", "-q", "--bare", "."], cwd=remote, check=True)
    r = tmp_path / "work"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "app").mkdir()
    (r / "app" / "x.py").write_text("x = 1\n")
    (r / ".codex-gate.yaml").write_text("code_paths:\n  prefixes: [app/]\n")
    _git(r, "add", "-A")
    _git(r, "commit", "-m", "init")
    _git(r, "remote", "add", "origin", str(remote))
    _git(r, "push", "-q", "origin", "main")
    return r


def _sha(repo, rev="HEAD"):
    return _git(repo, "rev-parse", rev).stdout.strip()


def _line(local_sha, remote_sha, local_ref="refs/heads/feature", remote_ref=None):
    return f"{local_ref} {local_sha} {remote_ref or local_ref} {remote_sha}\n"


def _set_integration(repo, body, message="cfg"):
    """Дописывает секцию integration в .codex-gate.yaml и коммитит."""
    cfg = repo / ".codex-gate.yaml"
    base = "code_paths:\n  prefixes: [app/]\n"
    cfg.write_text(base + body)
    _git(repo, "add", ".codex-gate.yaml")
    _git(repo, "commit", "-m", message)
    return _sha(repo)


# ═══ Разбор входа и таксономия строк (С10-С12, E1/E2/E2а) ═══

def test_parse_lines_handles_multiple_refs():
    text = (f"refs/heads/a aaa1 refs/heads/a {ZEROS}\n"
            f"refs/tags/v1 bbb2 refs/tags/v1 {ZEROS}\n")
    lines = pg.parse_lines(text)
    assert [l.local_ref for l in lines] == ["refs/heads/a", "refs/tags/v1"]
    assert lines[0].remote_sha == ZEROS


def test_classify_delete_tag_base_branch():
    """С10/С12: пропуск по проверкам, а не по строке целиком."""
    d = pg.parse_lines(f"refs/heads/x {ZEROS} refs/heads/x abc\n")[0]
    assert pg.classify(d, remote="origin", base_ref="origin/main") == "delete"
    t = pg.parse_lines(f"refs/tags/v1 abc refs/tags/v1 {ZEROS}\n")[0]
    assert pg.classify(t, remote="origin", base_ref="origin/main") == "tag"
    b = pg.parse_lines("refs/heads/main abc refs/heads/main def\n")[0]
    assert pg.classify(b, remote="origin", base_ref="origin/main") == "base"
    f = pg.parse_lines("refs/heads/feature abc refs/heads/feature def\n")[0]
    assert pg.classify(f, remote="origin", base_ref="origin/main") == "branch"


def test_base_identity_compares_remote_and_branch_not_name_only():
    """E2а: у форка (пуш в origin, база в upstream) совпадение по одному имени ветки
    пропустило бы предсказание слияния — fail-open единственного обещания."""
    line = pg.parse_lines("refs/heads/main abc refs/heads/main def\n")[0]
    assert pg.classify(line, remote="origin", base_ref="upstream/main") == "branch"
    assert pg.classify(line, remote="upstream", base_ref="upstream/main") == "base"


# ═══ Анти-самоослабление И4 (С9, С9а, С9в, E4/E4а/E4в/E4г) ═══

def test_c9a_first_push_of_new_branch_introduces_section(repo):
    """С9а: нули И нет tracking-ref'а → введение гейта, проход."""
    head = _set_integration(repo, "integration:\n  on_fetch_failure: block\n")
    verdict = pg.self_weaken_verdict(repo, remote_sha=ZEROS, remote="origin",
                                     branch="brandnew", local_sha=head)
    assert verdict.ok, verdict.reason


def test_c9_weakening_already_enabled_section_blocks(repo):
    """С9/E4: секция включена в предыдущем принятом tip'е и изменена → блок."""
    before = _set_integration(repo, "integration:\n  on_fetch_failure: block\n")
    after = _set_integration(repo, "integration:\n  on_fetch_failure: skip-audited\n", "weaken")
    verdict = pg.self_weaken_verdict(repo, remote_sha=before, remote="origin",
                                     branch="feature", local_sha=after)
    assert not verdict.ok
    assert "изменена" in verdict.reason or "ослабл" in verdict.reason


def test_c9b_weaken_on_second_push_while_base_has_no_section(repo):
    """С9б — причина смены референса: сравнение с base_ref пропускало этот случай,
    потому что в origin/main секции нет вовсе."""
    before = _set_integration(repo, "integration:\n  on_fetch_failure: block\n")
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/feature")
    after = _set_integration(repo, "integration:\n  on_fetch_failure: skip-audited\n", "weaken")
    base_state, _ = pg.integration_section(repo, "origin/main")
    assert base_state == "absent"           # в базе секции нет — ред. 3 здесь пропускала
    verdict = pg.self_weaken_verdict(repo, remote_sha=before, remote="origin",
                                     branch="feature", local_sha=after)
    assert not verdict.ok


def test_e4v_unreadable_previous_tip_blocks(repo):
    """E4в (блокирующая находка ревью ред. 4): непустой remote_sha, который локально
    не читается, — НЕ доказательство отсутствия секции. Раньше это был проход."""
    head = _set_integration(repo, "integration:\n  on_fetch_failure: skip-audited\n")
    missing = "0123456789abcdef0123456789abcdef01234567"   # объекта нет в локальной ODB
    verdict = pg.self_weaken_verdict(repo, remote_sha=missing, remote="origin",
                                     branch="feature", local_sha=head)
    assert not verdict.ok
    assert "прочитать" in verdict.reason or "unreadable" in verdict.reason


def test_e4g_branch_recreation_blocks(repo):
    """E4г/С9в: нули, но локальный tracking-ref существует → пересоздание ветки,
    то есть тихий сброс референса И4."""
    before = _set_integration(repo, "integration:\n  on_fetch_failure: block\n")
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/feature")
    assert (repo / ".git" / "refs" / "remotes" / "origin" / "feature").exists()
    after = _set_integration(repo, "integration:\n  on_fetch_failure: skip-audited\n", "weaken")
    verdict = pg.self_weaken_verdict(repo, remote_sha=ZEROS, remote="origin",
                                     branch="feature", local_sha=after)
    assert not verdict.ok
    assert "пересозд" in verdict.reason


def test_audited_ack_unblocks_config_change(repo, monkeypatch):
    """E4: аудируемое подтверждение — тот же класс обхода, что EMPIRICAL_SKIP."""
    before = _set_integration(repo, "integration:\n  on_fetch_failure: block\n")
    after = _set_integration(repo, "integration:\n  on_fetch_failure: skip-audited\n", "weaken")
    monkeypatch.setenv("INTEGRATION_CONFIG_CHANGE", "1")
    monkeypatch.setenv("INTEGRATION_CONFIG_CHANGE_REASON", "осознанно, тикет X")
    verdict = pg.self_weaken_verdict(repo, remote_sha=before, remote="origin",
                                     branch="feature", local_sha=after)
    assert verdict.ok


def test_unreadable_pushed_config_blocks(repo):
    """Нечитаемый конфиг в ПУШИМОМ коммите — тоже блок (состояние гейта не подтвердить)."""
    (repo / ".codex-gate.yaml").write_text("code_paths: [unbalanced\n")
    _git(repo, "add", ".codex-gate.yaml")
    _git(repo, "commit", "-m", "broken")
    state, _ = pg.integration_section(repo, "HEAD")
    assert state == "unreadable"


# ═══ Предсказание слияния (С2, С3, С13, E6/E8) ═══

def test_c3_conflict_blocks_and_names_paths(repo):
    """С3 — единственное, что гейт обещает."""
    base = _sha(repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app" / "x.py").write_text("x = 'feature'\n")
    _git(repo, "commit", "-qam", "feat")
    head = _sha(repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "app" / "x.py").write_text("x = 'main'\n")
    _git(repo, "commit", "-qam", "main-change")
    result = pg.predict_merge(repo, base=_sha(repo), sha=head)
    assert result.status == "conflict"
    assert "app/x.py" in result.paths


def test_c2_clean_merge_returns_tree(repo):
    base = _sha(repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app" / "new.py").write_text("n = 1\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "feat")
    head = _sha(repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "app" / "x.py").write_text("x = 2\n")
    _git(repo, "commit", "-qam", "main-change")
    result = pg.predict_merge(repo, base=_sha(repo), sha=head)
    assert result.status == "clean" and len(result.tree) == 40


def test_c13_unknown_merge_tree_result_blocks(repo):
    """С13/И3: нечитаемая база — «неизвестно», а не «чисто» и не «конфликт».

    ИЗМЕРЕНО: битый OID даёт rc=1 с ПУСТЫМ stdout (сообщение в stderr), тогда как настоящий
    конфликт печатает OID дерева первой строкой. Спека ред. 5 §3 утверждала «код вне {0,1}» —
    неверно; без различения по выводу отсутствие объекта выдавалось бы за конфликт с пустым
    списком файлов."""
    result = pg.predict_merge(repo, base="0123456789abcdef0123456789abcdef01234567",
                              sha=_sha(repo))
    assert result.status == "unknown"


def test_dirty_worktree_survives_prediction(repo):
    """С3/И1: предсказание не касается рабочего дерева, включая незакоммиченное."""
    (repo / "dirty.txt").write_text("не трогать\n")
    base = _sha(repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app" / "x.py").write_text("x = 9\n")
    _git(repo, "commit", "-qam", "feat")
    pg.predict_merge(repo, base=base, sha=_sha(repo))
    assert (repo / "dirty.txt").read_text() == "не трогать\n"


# ═══ Проба и тесты на слиянии (С4-С6, С14, С15, E10/E11/E12/E14) ═══

def test_probe_worktree_contains_merged_content_and_is_cleaned(repo):
    base = _sha(repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app" / "f.py").write_text("from-feature\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "feat")
    head = _sha(repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "app" / "x.py").write_text("from-main\n")
    _git(repo, "commit", "-qam", "main-change")
    merged = pg.predict_merge(repo, base=_sha(repo), sha=head)
    seen = {}
    with pg.probe_worktree(repo, base=_sha(repo), sha=head, tree=merged.tree) as (w, _env):
        seen["f"] = (w / "app" / "f.py").read_text()
        seen["x"] = (w / "app" / "x.py").read_text()
        path = w
    assert seen == {"f": "from-feature\n", "x": "from-main\n"}
    assert not path.exists()                                  # E12: убран
    assert str(path) not in _git(repo, "worktree", "list").stdout


def test_c14_hooks_are_neutralised_in_probe(repo):
    """С14/И6: вложенный git commit внутри тест-команды не должен запускать наши хуки."""
    hooks = repo / ".githooks"
    hooks.mkdir(exist_ok=True)
    marker = repo / "hook-ran.txt"
    (hooks / "pre-commit").write_text(f"#!/bin/sh\necho ran > {marker}\nexit 0\n")
    (hooks / "pre-commit").chmod(0o755)
    _git(repo, "config", "core.hooksPath", ".githooks")
    result, tail = pg.run_merge_tests(repo, "git commit --allow-empty -m probe-inner",
                                      timeout_s=60)
    assert not marker.exists(), f"хук выполнился: {tail}"


def test_c4_red_tests_block(repo):
    result, tail = pg.run_merge_tests(repo, "sh -c 'exit 3'", timeout_s=60)
    assert result == "fail"


def test_c5_green_tests_carry_mandatory_caveat():
    """С5/И5/E11: зелёный НИЧЕГО не доказывает, и пометка печатается ВСЕГДА."""
    out = pg.report_tests_green()
    assert "НЕ доказывает" in out


def test_c6_noop_command_is_not_treated_as_coverage():
    """С6: /bin/true в ред. 2 было хуже отсутствия команды (тише). Теперь — та же пометка."""
    assert pg.report_tests_green() == pg.report_tests_green()   # один и тот же контракт
    assert "НЕ доказывает" in pg.report_tests_green()


def test_c15_side_effect_on_real_tree_blocks(repo):
    """С15/E13: команда, изменившая ОТСЛЕЖИВАЕМЫЙ файл настоящего дерева, негерметична."""
    snap = pg.worktree_snapshot(repo)
    (repo / "app" / "x.py").write_text("изменено тест-командой\n")
    assert pg.worktree_snapshot(repo) != snap


def test_tests_timeout_is_not_pass(repo):
    """Актуатор-урок: «зависло» ≠ «прошло»."""
    result, _ = pg.run_merge_tests(repo, "sh -c 'sleep 5'", timeout_s=1)
    assert result == "timeout"


# ═══ Политики и обходы (С7, С8, С17-С19а, E5/E5а/E16/E16а) ═══

def test_c8_fetch_failure_skip_audited_does_not_claim_nothing_to_integrate():
    """С8/И3: при неосвежённой базе гейт НЕ имеет права заключить «интегрировать нечего»
    (ред. 2 именно так и делала — тихий пропуск)."""
    out = pg.report_fetch_failed(policy="skip-audited", reason="сеть недоступна")
    assert "НЕ проверялось" in out
    assert "нечего" not in out


def test_c7_fetch_failure_block_is_default():
    cfg = pg.integration_config({})
    assert cfg.on_fetch_failure == "block"


def test_unknown_fetch_policy_blocks():
    """Неизвестное значение политики — блок, а не тихий фолбэк (как REVIEW_PROVIDER)."""
    with pytest.raises(pg.ConfigError):
        pg.integration_config({"on_fetch_failure": "warn"})     # значение переименовано в ред. 4


def test_c19a_tests_skip_keeps_conflict_check(monkeypatch):
    """С19а: ложные красные тесты не должны стоить единственного обещания гейта."""
    monkeypatch.setenv("INTEGRATION_TESTS_SKIP", "1")
    monkeypatch.setenv("INTEGRATION_TESTS_SKIP_REASON", "негерметичное окружение")
    assert pg.tests_skipped() is not None
    assert pg.gate_skipped() is None


def test_c19_whole_gate_skip_requires_reason(monkeypatch):
    monkeypatch.setenv("INTEGRATION_SKIP", "1")
    assert pg.gate_skipped() is None            # без причины обход не действует
    monkeypatch.setenv("INTEGRATION_SKIP_REASON", "аварийно")
    assert pg.gate_skipped() == "аварийно"


# ═══ Опт-ин (С18, E17) ═══

def test_c18_not_onboarded_exits_zero(tmp_path):
    r = tmp_path / "plain"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / "f.txt").write_text("x\n")
    _git(r, "add", "-A"); _git(r, "commit", "-m", "init")
    assert pg.main([], stdin_text=_line(_sha(r), ZEROS), root=r) == 0


# ═══ Сквозные проверки через main(): ревью 2026-07-26 указало, что проверки чистых функций
# не доказывают поведение гейта — блокирующие ветки не выполнялись ни в одном тесте ═══

AUDIT = "logs/codex_review_audit.log"


def _diverge(repo):
    """Разводит ветку и базу: origin/main уходит вперёд, feature отстаёт. Возвращает sha ветки."""
    base = _sha(repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app" / "y.py").write_text("y = 1\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "feat")
    feat = _sha(repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "app" / "x.py").write_text("x = 'main moved'\n")
    _git(repo, "commit", "-qam", "main-move")
    _git(repo, "push", "-q", "origin", "main")
    _git(repo, "checkout", "-q", "feature")
    return feat


def _run(repo, sha, remote="origin", ref="refs/heads/feature", remote_sha=ZEROS):
    return pg.main([remote], stdin_text=_line(sha, remote_sha, local_ref=ref), root=repo)


def test_e2e_conflict_blocks_push(repo, capsys):
    """С3 через main(): единственное обещание гейта, проверенное на настоящем блоке."""
    base = _sha(repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app" / "x.py").write_text("x = 'feature'\n")
    _git(repo, "commit", "-qam", "feat")
    feat = _sha(repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "app" / "x.py").write_text("x = 'main'\n")
    _git(repo, "commit", "-qam", "main-move")
    _git(repo, "push", "-q", "origin", "main")
    assert _run(repo, feat) == 2
    err = capsys.readouterr().err
    assert "конфликтует" in err and "app/x.py" in err


def test_e2e_clean_merge_without_test_command_notes_tests_not_run(repo, capsys):
    """С5/E12: пометка обязательна, иначе оператор решит, что тесты на слиянии зелёные."""
    feat = _diverge(repo)
    assert _run(repo, feat) == 0
    assert "тесты на слиянии НЕ запускались" in capsys.readouterr().err


def test_e2e_green_tests_print_mandatory_caveat(repo, capsys):
    """С5/И5/E11 через main(): раньше проверялся возврат функции, а не факт печати."""
    _set_integration(repo, "integration:\n  merge_test_command: 'true'\n")
    feat = _diverge(repo)
    assert _run(repo, feat) == 0
    err = capsys.readouterr().err
    assert "НЕ доказывает" in err


def test_e2e_red_tests_block(repo, capsys):
    """С4 через main(): блокирующая ветка реально выполняется."""
    _set_integration(repo, "integration:\n  merge_test_command: \"sh -c 'exit 7'\"\n")
    feat = _diverge(repo)
    assert _run(repo, feat) == 2
    assert "тесты на пробе" in capsys.readouterr().err


def test_e2e_non_hermetic_command_mutating_real_tree_blocks(repo, capsys):
    """С15/E13: команда, изменившая ОТСЛЕЖИВАЕМЫЙ файл настоящего дерева, негерметична."""
    script = repo / "mutate.sh"
    script.write_text(f"#!/bin/sh\necho mutated > {repo}/app/x.py\n")
    script.chmod(0o755)
    _set_integration(repo, f"integration:\n  merge_test_command: 'sh {script}'\n")
    feat = _diverge(repo)
    assert _run(repo, feat) == 2
    assert "негерметична" in capsys.readouterr().err


def test_e2e_fetch_failure_blocks_by_default(repo, capsys):
    """С7/E5: дефолт — блок, причина видна."""
    _set_integration(repo, "integration:\n  base_ref: nosuchremote/main\n")
    feat = _diverge(repo)
    assert _run(repo, feat) == 2
    assert "база не обновлена" in capsys.readouterr().err


def test_e2e_fetch_failure_skip_audited_passes_and_writes_audit(repo, capsys):
    """С8/E5: пропуск разрешён политикой, но ОБЯЗАН оставить след — ревью нашло, что политика
    называлась skip-audited, а аудита не было вообще."""
    _set_integration(repo,
                     "integration:\n  base_ref: nosuchremote/main\n"
                     "  on_fetch_failure: skip-audited\n")
    feat = _diverge(repo)
    assert _run(repo, feat) == 0
    err = capsys.readouterr().err
    assert "НЕ проверялось" in err and "нечего" not in err
    audit = (repo / AUDIT).read_text()
    assert "on_fetch_failure=skip-audited" in audit and "nosuchremote" in audit


def test_e2e_gate_skip_writes_audit(repo, monkeypatch, capsys):
    """С19/E16: обход всего гейта оставляет запись, а не только строчку в stderr хука."""
    monkeypatch.setenv("INTEGRATION_SKIP", "1")
    monkeypatch.setenv("INTEGRATION_SKIP_REASON", "аварийно, инцидент N")
    feat = _diverge(repo)
    assert _run(repo, feat) == 0
    assert "INTEGRATION_SKIP=1" in (repo / AUDIT).read_text()


def test_e2e_tests_skip_keeps_conflict_check_and_audits(repo, monkeypatch, capsys):
    """С19а/E16а: пропуск тестов НЕ отключает проверку конфликтов."""
    monkeypatch.setenv("INTEGRATION_TESTS_SKIP", "1")
    monkeypatch.setenv("INTEGRATION_TESTS_SKIP_REASON", "негерметичное окружение")
    _set_integration(repo, "integration:\n  merge_test_command: \"sh -c 'exit 7'\"\n")
    base = _sha(repo)
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app" / "x.py").write_text("x = 'feature'\n")
    _git(repo, "commit", "-qam", "feat")
    feat = _sha(repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "app" / "x.py").write_text("x = 'main'\n")
    _git(repo, "commit", "-qam", "main-move")
    _git(repo, "push", "-q", "origin", "main")
    assert _run(repo, feat) == 2                      # конфликт всё равно блокирует
    assert "конфликтует" in capsys.readouterr().err
    # аудита здесь НЕТ намеренно: конфликт блокирует ДО шага тестов, поэтому подтверждение
    # пропуска не вычисляется. Порядок проверок важнее записи об обходе, который не применялся.
    assert not (repo / AUDIT).exists()


def test_e2e_tests_skip_on_clean_merge_audits(repo, monkeypatch, capsys):
    """С19а/E16а: когда шаг тестов ДОСТИГНУТ и пропущен — обход обязан оставить след."""
    monkeypatch.setenv("INTEGRATION_TESTS_SKIP", "1")
    monkeypatch.setenv("INTEGRATION_TESTS_SKIP_REASON", "негерметичное окружение")
    _set_integration(repo, "integration:\n  merge_test_command: \"sh -c 'exit 7'\"\n")
    feat = _diverge(repo)
    assert _run(repo, feat) == 0                      # красные тесты не запускались вовсе
    assert "тесты пропущены" in capsys.readouterr().err
    assert "INTEGRATION_TESTS_SKIP=1" in (repo / AUDIT).read_text()


def test_e2e_config_change_ack_writes_audit(repo, monkeypatch, capsys):
    """E4: подтверждение ослабления — аудируемый обход того же класса, что EMPIRICAL_SKIP."""
    before = _set_integration(repo, "integration:\n  on_fetch_failure: block\n")
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/feature")
    after = _set_integration(repo, "integration:\n  on_fetch_failure: skip-audited\n", "weaken")
    monkeypatch.setenv("INTEGRATION_CONFIG_CHANGE", "1")
    monkeypatch.setenv("INTEGRATION_CONFIG_CHANGE_REASON", "осознанно")
    pg.self_weaken_verdict(repo, remote_sha=before, remote="origin", branch="feature",
                           local_sha=after)
    assert "INTEGRATION_CONFIG_CHANGE=1" in (repo / AUDIT).read_text()


def test_e2e_ack_without_reason_does_not_unblock(repo, monkeypatch):
    """Флаг без причины обходом не является (как LADDER_SKIP)."""
    before = _set_integration(repo, "integration:\n  on_fetch_failure: block\n")
    after = _set_integration(repo, "integration:\n  on_fetch_failure: skip-audited\n", "weaken")
    monkeypatch.setenv("INTEGRATION_CONFIG_CHANGE", "1")
    verdict = pg.self_weaken_verdict(repo, remote_sha=before, remote="origin",
                                     branch="feature", local_sha=after)
    assert not verdict.ok


def test_e2e_fetch_runs_once_for_two_refs(repo, monkeypatch):
    """Эффективность: fetch — единственная сетевая операция, повторять её на каждый ref нельзя."""
    feat = _diverge(repo)
    calls = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd and str(cmd[0]).endswith("git") and "fetch" in cmd:
            calls.append(tuple(cmd))
        return real(cmd, *a, **kw)

    monkeypatch.setattr(pg.subprocess, "run", spy)
    text = _line(feat, ZEROS, local_ref="refs/heads/feature") + \
        _line(feat, ZEROS, local_ref="refs/heads/feature2")
    pg.main(["origin"], stdin_text=text, root=repo)
    assert len(calls) == 1, calls


# ═══ Регрессы на находки кросс-семейного ревью 2026-07-26 ═══

def test_e2e_nested_branch_name_is_not_treated_as_base(repo, capsys):
    """CRITICAL-регресс: `rsplit("/", 1)` превращал refs/heads/release/main в «main», ветка
    считалась базой, и предсказание слияния ПРОПУСКАЛОСЬ. Обычное вложенное имя снимало
    единственное обещание гейта."""
    base = _sha(repo)
    _git(repo, "checkout", "-q", "-b", "release/main")
    (repo / "app" / "x.py").write_text("x = 'release'\n")
    _git(repo, "commit", "-qam", "release change")
    feat = _sha(repo)
    _git(repo, "checkout", "-q", "main")
    (repo / "app" / "x.py").write_text("x = 'main'\n")
    _git(repo, "commit", "-qam", "main-move")
    _git(repo, "push", "-q", "origin", "main")
    assert pg.classify(pg.parse_lines(_line(feat, ZEROS, "refs/heads/release/main"))[0],
                       remote="origin", base_ref="origin/main") == "branch"
    assert _run(repo, feat, ref="refs/heads/release/main") == 2      # конфликт ловится
    assert "конфликтует" in capsys.readouterr().err


def test_e2e_delete_push_is_not_blocked(repo):
    """E2-регресс: у удаления local_sha нулевой, ls-tree по нему падал → «unreadable» → блок,
    и `git push :branch` ломался на онбордженном репо."""
    text = f"refs/heads/gone {ZEROS} refs/heads/gone {_sha(repo)}\n"
    assert pg.main(["origin"], stdin_text=text, root=repo) == 0


def test_e2e_annotated_tag_push_is_not_blocked(repo):
    """Тот же класс: у аннотированного тега local_sha — tag-объект, ls-tree по нему падает."""
    _git(repo, "tag", "-a", "v1", "-m", "release 1")
    tag_sha = _git(repo, "rev-parse", "v1").stdout.strip()
    text = f"refs/tags/v1 {tag_sha} refs/tags/v1 {ZEROS}\n"
    assert pg.main(["origin"], stdin_text=text, root=repo) == 0


def test_e2e_base_skip_is_audited(repo):
    """Пропуск по базовой ветке обязан оставлять след: конфиг, объявивший базой саму ветку,
    иначе отключал бы предсказание слияния бесследно."""
    _set_integration(repo, "integration:\n  base_ref: origin/main\n")
    head = _sha(repo)
    assert pg.main(["origin"], stdin_text=_line(head, head, "refs/heads/main"), root=repo) == 0
    assert "base-skip" in (repo / AUDIT).read_text()


def test_e2e_success_path_audits_base_and_sha(repo):
    """С2 прямым текстом требует «в аудите база и её SHA» — раньше след оставляли только обходы."""
    feat = _diverge(repo)
    assert _run(repo, feat) == 0
    audit = (repo / AUDIT).read_text()
    assert "integration ok" in audit and "base_sha=" in audit


def test_e2e_unreadable_pushed_config_blocks_push(repo, capsys):
    """Раньше тест проверял только возврат integration_section и НЕ утверждал блок."""
    (repo / ".codex-gate.yaml").write_text("integration: [unbalanced\n")
    _git(repo, "add", ".codex-gate.yaml"); _git(repo, "commit", "-qm", "broken")
    assert _run(repo, _sha(repo)) == 2
    assert "нечитаем" in capsys.readouterr().err


def test_e2e_noop_test_command_still_prints_caveat(repo, capsys):
    """С6 через main(): `true` формально «задана», но покрытием не выглядит."""
    _set_integration(repo, "integration:\n  merge_test_command: 'true'\n")
    feat = _diverge(repo)
    assert _run(repo, feat) == 0
    assert "НЕ доказывает" in capsys.readouterr().err


def test_merge_tree_zero_code_without_oid_is_unknown(repo, monkeypatch):
    """Нулевой код с пустым выводом раньше валил IndexError вместо диагностики E8."""
    class R:
        returncode, stdout, stderr = 0, "\n", ""
    monkeypatch.setattr(pg.subprocess, "run", lambda *a, **k: R())
    assert pg.predict_merge(repo, base="x", sha="y").status == "unknown"


def test_e2e_rev_list_failure_blocks(repo, monkeypatch, capsys):
    """И3: сбой сравнения с базой — «неизвестно», а не «есть что интегрировать, разберёмся ниже».
    Без этого теста мутация «сбой rev-list снова не блокирует» выживала."""
    feat = _diverge(repo)
    real = subprocess.run

    def spy(cmd, *a, **kw):
        # слой зовёт АБСОЛЮТНЫЙ git с `-c`-флагами нейтрализации, поэтому подкоманду
        # ищем в argv, а не по фиксированной позиции
        if isinstance(cmd, list) and cmd and str(cmd[0]).endswith("git") and "rev-list" in cmd:
            class R:
                returncode, stdout, stderr = 128, "", "fatal: bad revision"
            return R()
        return real(cmd, *a, **kw)

    monkeypatch.setattr(pg.subprocess, "run", spy)
    assert _run(repo, feat) == 2
    assert "не удалось сравнить" in capsys.readouterr().err


def test_head_source_ref_does_not_trip_recreation_detector(repo):
    """`git push origin HEAD:refs/heads/x` даёт local_ref=HEAD; branch становился «HEAD», а
    refs/remotes/origin/HEAD есть в любом клоне → ПОСТОЯННЫЙ ложный блок без выхода."""
    line = pg.parse_lines(f"HEAD {_sha(repo)} refs/heads/topic {ZEROS}\n")[0]
    assert line.branch == "topic"


def test_absent_previous_section_then_weakening_requires_ack(repo):
    """Fail-open ревью 2026-07-26: отсутствие секции НЕ значит «гейт выключен» — он работает
    на дефолтах, поэтому её появление на уже запушенной ветке есть смена политики."""
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/feature")
    before = _sha(repo)
    after = _set_integration(repo, "integration:\n  base_ref: origin/feature\n", "self-base")
    verdict = pg.self_weaken_verdict(repo, remote_sha=before, remote="origin",
                                     branch="feature", local_sha=after)
    assert not verdict.ok
    assert "ПОЯВИЛАСЬ" in verdict.reason


def test_absent_on_both_sides_is_not_a_change(repo):
    """Обратная сторона: если секции нет ни там, ни тут — менять нечего, блокировать нельзя."""
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/feature")
    before = _sha(repo)
    (repo / "app" / "z.py").write_text("z = 1\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "code only")
    verdict = pg.self_weaken_verdict(repo, remote_sha=before, remote="origin",
                                     branch="feature", local_sha=_sha(repo))
    assert verdict.ok, verdict.reason


def test_push_by_url_blocks_when_remote_unresolvable(repo):
    """Пуш по URL: пространства refs/remotes/<url>/… нет, детектор пересоздания слеп —
    раньше это молча читалось как «новая ветка»."""
    head = _set_integration(repo, "integration:\n  on_fetch_failure: skip-audited\n")
    verdict = pg.self_weaken_verdict(repo, remote_sha=ZEROS, remote="https://example.invalid/r.git",
                                     branch="feature", local_sha=head)
    assert not verdict.ok
    assert "не совпадающему ни с одним" in verdict.reason


def test_push_by_url_matching_configured_remote_is_resolved(repo):
    """URL, совпадающий с настроенным remote, разрешается в его имя — блокировать не за что."""
    url = _git(repo, "remote", "get-url", "origin").stdout.strip()
    assert pg._resolve_remote_name(repo, url) == "origin"


def test_hookless_env_drops_every_inherited_git_config(monkeypatch, tmp_path):
    """И6 + находка 09.08.2026: конфигурация checkout состоит ТОЛЬКО из наших записей.
    Прежнее «дополняем чужие» сохраняло `safe.directory`, но заодно проносило в checkout
    любой `filter.*.smudge` вызывающего — мимо перечисления драйверов, которое идёт через
    доверенный слой. `safe.directory` теперь выставляем сами и только для своего корня."""
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.evil.smudge")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "sh -c 'touch /tmp/pwned'")
    monkeypatch.setenv("GIT_CONFIG_PARAMETERS", "'core.hooksPath=/evil'")
    with pg.hookless_env(tmp_path) as env:
        keys = {env[f"GIT_CONFIG_KEY_{i}"] for i in range(int(env["GIT_CONFIG_COUNT"]))}
        assert keys == {"core.hooksPath", "safe.directory"}, keys
        assert "filter.evil.smudge" not in keys
        assert env["GIT_CONFIG_VALUE_1"] == str(tmp_path)
        assert "GIT_CONFIG_PARAMETERS" not in env


def test_default_base_ref_follows_origin_head(repo):
    """`origin/main` не универсален: на репозитории с другой основной веткой он давал бы
    ложный блок на каждом пуше. Спрашиваем git о фактической основной ветке remote'а."""
    assert pg.default_base_ref(repo) == "origin/main"          # origin/HEAD не задан → литерал
    _git(repo, "branch", "-q", "trunk")
    _git(repo, "push", "-q", "origin", "trunk")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert pg.default_base_ref(repo) == "origin/trunk"


def test_config_default_base_ref_uses_repo_default(repo):
    _git(repo, "branch", "-q", "trunk"); _git(repo, "push", "-q", "origin", "trunk")
    _git(repo, "symbolic-ref", "refs/remotes/origin/HEAD", "refs/remotes/origin/trunk")
    assert pg.integration_config({}, repo).base_ref == "origin/trunk"
    assert pg.integration_config({"base_ref": "upstream/main"}, repo).base_ref == "upstream/main"


# ═══ Регрессы security-прохода 2026-07-26 (оба эксплойта воспроизводились) ═══

@pytest.mark.parametrize("evil", [
    "--upload-pack=touch /tmp/gates-pwned/.",
    "-c/main",
    "--exec=sh/main",
])
def test_base_ref_option_injection_is_rejected(repo, evil):
    """HIGH/RCE: base_ref из ПУШИМОГО коммита уходил позиционным argv в git fetch, а значение
    вида `--upload-pack=<cmd>` git трактует как опцию и ИСПОЛНЯЕТ команду. Валидации
    «строка и есть слэш» не хватало."""
    with pytest.raises(pg.ConfigError) as e:
        pg.integration_config({"base_ref": evil}, repo)
    assert "недопустима" in str(e.value)


def test_base_ref_normal_values_still_accepted(repo):
    for ok in ("origin/main", "upstream/release/2.0", "origin/feature-x_1.2"):
        assert pg.integration_config({"base_ref": ok}, repo).base_ref == ok


def test_merge_test_command_is_not_taken_from_pushed_commit(repo):
    """MEDIUM→структурно: исполняемая команда из чужой ветки запускалась на машине пушащего
    при рядовом `git push`, причём для НОВОЙ ветки подтверждение не требовалось вовсе.
    Теперь команда берётся только из конфига оператора."""
    evil = "sh -c 'touch /tmp/gates-pwned-cmd'"
    cfg = pg.integration_config({"merge_test_command": evil}, repo)
    assert cfg.merge_test_command == ""          # из секции коммита — НЕ берётся


def test_merge_test_command_comes_from_operator_worktree(repo):
    (repo / ".codex-gate.yaml").write_text(
        "code_paths:\n  prefixes: [app/]\nintegration:\n  merge_test_command: 'true'\n")
    assert pg.integration_config({}, repo).merge_test_command == "true"
    # секция коммита политику по-прежнему задаёт (анти-лаундеринг), команду — нет
    cfg = pg.integration_config({"on_fetch_failure": "skip-audited"}, repo)
    assert cfg.on_fetch_failure == "skip-audited" and cfg.merge_test_command == "true"


def test_e2e_pushed_commit_cannot_inject_command(repo, capsys):
    """Сквозная проверка: ветка с исполняемой командой в своём конфиге проходит гейт, но
    команда НЕ исполняется — рабочее дерево оператора её не объявляло."""
    marker = repo / "pwned-marker"
    _set_integration(repo, f"integration:\n  merge_test_command: \"sh -c 'touch {marker}'\"\n")
    feat = _diverge(repo)
    # у оператора в рабочем дереве команды нет: убираем секцию из рабочей копии
    (repo / ".codex-gate.yaml").write_text("code_paths:\n  prefixes: [app/]\n")
    assert _run(repo, feat) == 0
    assert not marker.exists(), "команда из пушимого коммита исполнилась — RCE не закрыт"
    assert "тесты на слиянии НЕ запускались" in capsys.readouterr().err


def test_git_calls_with_config_derived_refs_use_end_of_options(repo, monkeypatch):
    """Defense-in-depth фиксируется тестом: валидатор base_ref уже отсекает `--…`, поэтому
    мутация «убрать --end-of-options» иначе выживала — то есть защита была бы снята незаметно."""
    seen = []
    real = subprocess.run

    def spy(cmd, *a, **kw):
        if isinstance(cmd, list) and cmd and str(cmd[0]).endswith("git"):
            seen.append(cmd)
        return real(cmd, *a, **kw)

    feat = _diverge(repo)
    monkeypatch.setattr(pg.subprocess, "run", spy)
    _run(repo, feat)
    for name in ("fetch", "merge-tree", "rev-parse"):
        calls = [c for c in seen if name in c]
        assert calls, f"вызов git {name} не состоялся — тест перестал проверять то, что заявляет"
        assert all("--end-of-options" in c for c in calls), f"git {name} без --end-of-options"


# ── T16: пути исполнения при checkout (G18/G26/G27), проверка СЕНТИНЕЛОМ ─────────────────

def _probe_with_trap(repo, arm_trap):
    """Ловушка ставится ПОСЛЕ подготовки веток: иначе её срабатывание на служебных
    `git checkout` самого теста читалось бы как исполнение внутри probe."""
    _git(repo, "checkout", "-q", "-b", "feature")
    (repo / "app" / "f.py").write_text("x\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "feat")
    head = _sha(repo)
    _git(repo, "checkout", "-q", "main")
    merged = pg.predict_merge(repo, base=_sha(repo), sha=head)
    arm_trap()
    with pg.probe_worktree(repo, base=_sha(repo), sha=head, tree=merged.tree):
        pass


def test_t16a_arbitrary_process_filter_never_executes(repo, tmp_path):
    """Драйвер может называться КАК УГОДНО — имя приходит из `.gitattributes` проверяемой
    стороны, поэтому гасить поимённо (как было с `filter.lfs.*`) бессмысленно."""
    sentinel = tmp_path / "pwned"

    def arm():
        (repo / ".gitattributes").write_text("*.py filter=evil\n")
        _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "attrs")
        _git(repo, "config", "filter.evil.process", f"sh -c 'touch {sentinel}'")

    with pytest.raises((RuntimeError, pg.TrustedGitError)):
        _probe_with_trap(repo, arm)
    assert not sentinel.exists(), "команда фильтра ИСПОЛНИЛАСЬ правами гейта"


def test_t16b_clean_only_filter_keeps_probe_usable(repo):
    """`clean` checkout не исполняет: блокировать по нему — отказ в функции без выигрыша в
    безопасности. Гейт, который нельзя запустить на легитимном репозитории, выключают целиком."""
    _probe_with_trap(repo, lambda: _git(repo, "config", "filter.lfs.clean", "cat"))


def test_t16c_post_checkout_hook_never_executes(repo, tmp_path):
    """Фильтры — не единственный путь исполнения: `worktree add` делает checkout и без
    нейтрализации запустил бы хук репозитория правами гейта."""
    sentinel = tmp_path / "hooked"

    def arm():
        hooks = repo / ".git" / "hooks"
        hooks.mkdir(parents=True, exist_ok=True)
        hook = hooks / "post-checkout"
        hook.write_text(f"#!/bin/sh\ntouch {sentinel}\n")
        hook.chmod(0o755)

    _probe_with_trap(repo, arm)
    assert not sentinel.exists(), "хук post-checkout исполнился правами гейта"


def test_t16d_partial_clone_fails_closed(repo):
    """Promisor тянет недостающие объекты ВНЕШНИМ transport-helper'ом — гасить нечем."""
    with pytest.raises((RuntimeError, pg.TrustedGitError)):
        _probe_with_trap(repo, lambda: _git(repo, "config", "extensions.partialClone", "origin"))


def test_t16e_inherited_git_config_filter_never_reaches_checkout(repo, tmp_path, monkeypatch):
    """Перечисление драйверов идёт через доверенный слой (он снимает GIT_CONFIG_* вызывающего),
    а checkout раньше получал их дополнением — проверка видела пустоту, команда исполнялась."""
    sentinel = tmp_path / "inherited-pwned"
    (repo / ".gitattributes").write_text("*.py filter=evil\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-qm", "attrs")
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "filter.evil.smudge")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", f"sh -c 'touch {sentinel}'")
    _probe_with_trap(repo, lambda: None)
    assert not sentinel.exists(), "GIT_CONFIG_* вызывающего доехал до checkout мимо проверки"


def test_t16f_remote_uploadpack_is_not_executed_on_fetch(repo, tmp_path):
    """URL-проверки мало: `remote.<name>.uploadpack` исполняется git'ом как КОМАНДА на
    локальном транспорте. Воспроизведено ревью 09.08.2026 — гасим её, `vcs` и `core.gitProxy`."""
    sentinel = tmp_path / "uploadpack-ran"
    _git(repo, "config", "remote.origin.uploadpack", f"sh -c 'touch {sentinel}; false'")
    pg._prepush_fetch(repo, "origin", "main", timeout=30)
    assert not sentinel.exists(), "remote.uploadpack исполнился правами pre-push хука"


def test_t16g_remote_vcs_transport_helper_is_rejected(repo):
    """`remote.<name>.vcs` запускает внешний helper. Гасить пустым значением нельзя (git
    начинает искать `git-remote-`), поэтому конфигурация отклоняется целиком."""
    _git(repo, "config", "remote.origin.vcs", "evil")
    with pytest.raises(pg.TrustedGitError):
        pg._prepush_fetch(repo, "origin", "main", timeout=30)
