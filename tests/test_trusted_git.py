"""Стерильный git-слой: чистота дерева по СЫРЫМ байтам (спека 2026-08-08, T5..T5d).

Тесты работают на НАСТОЯЩЕМ временном репозитории: подделки, которые они проверяют
(флаги индекса, clean-фильтры, режимы, симлинки), существуют только в реальном git.
"""
import os
import subprocess

import pytest

import codex_review_gate as g


def _git(repo, *args, **kw):
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True,
                          check=kw.pop("check", True), **kw)


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    r = tmp_path / "r"
    r.mkdir()
    _git(r, "init", "-q", ".")
    (r / "a.txt").write_text("original\n")
    _git(r, "add", "a.txt")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    monkeypatch.setattr(g, "REPO_ROOT", r)
    monkeypatch.setattr(g, "_trusted_git", g.__dict__["_trusted_git"])   # настоящий слой
    return r


def test_clean_repository_is_clean(repo):
    assert g.working_tree_clean() is True


def test_t5_assume_unchanged_hides_modification_from_status_but_not_from_us(repo):
    """`status` показывает пустоту — именно поэтому он не может быть предикатом безопасности."""
    _git(repo, "update-index", "--assume-unchanged", "a.txt")
    (repo / "a.txt").write_text("HACKED\n")
    assert _git(repo, "status", "--porcelain").stdout == "", "предпосылка теста сломалась"
    assert g.working_tree_clean() is False


def test_t5_skip_worktree_same(repo):
    _git(repo, "update-index", "--skip-worktree", "a.txt")
    (repo / "a.txt").write_text("HACKED\n")
    assert g.working_tree_clean() is False


def test_t5a_clean_filter_cannot_mask_raw_bytes(repo):
    """Clean-фильтр отдаёт закоммиченное содержимое, а в дереве лежат другие байты —
    именно те, что уедут актуатором."""
    (repo / ".gitattributes").write_text("a.txt filter=mask\n")
    _git(repo, "add", ".gitattributes")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "attrs")
    _git(repo, "config", "filter.mask.clean", "printf 'original\\n'")
    (repo / "a.txt").write_text("HACKED\n")
    assert g.working_tree_clean() is False


def test_t5b_mode_change_is_dirty(repo):
    os.chmod(repo / "a.txt", 0o755)
    assert g.working_tree_clean() is False


def test_t5b_symlink_replacement_is_dirty(repo):
    (repo / "target").write_text("original\n")
    (repo / "a.txt").unlink()
    (repo / "a.txt").symlink_to("target")
    assert g.working_tree_clean() is False


def test_t5d_stat_preserving_write_is_dirty(repo):
    st = (repo / "a.txt").stat()
    (repo / "a.txt").write_text("HACKEDaa\n"[:len("original\n")])
    os.utime(repo / "a.txt", (st.st_atime, st.st_mtime))
    assert g.working_tree_clean() is False


def test_untracked_file_is_dirty(repo):
    (repo / "new.txt").write_text("x")
    assert g.working_tree_clean() is False


def test_deleted_tracked_file_is_dirty(repo):
    (repo / "a.txt").unlink()
    assert g.working_tree_clean() is False


def test_ladder_tree_hash_ignores_repository_clean_filter(tmp_path):
    """`git add` запускает clean-фильтры репозитория, поэтому tree-хэш лесенки считался от
    ПОДСУНУТОГО содержимого. Целостность лесенки держится на этом хэше."""
    import os
    import ladder_gate as L

    r = tmp_path / "f"
    r.mkdir()
    _git(r, "init", "-q", ".")
    (r / "a.txt").write_text("real\n")
    (r / ".gitattributes").write_text("a.txt filter=mask\n")
    _git(r, "add", "-A")
    _git(r, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "init")
    _git(r, "config", "filter.mask.clean", "printf 'FAKE\\n'")

    ours = L.compute_tree(r)

    env = dict(os.environ)
    env["GIT_INDEX_FILE"] = str(tmp_path / "idx")
    subprocess.run(["git", "add", "-A", "--", "."], cwd=r, env=env, capture_output=True)
    filtered = subprocess.run(["git", "write-tree"], cwd=r, env=env,
                              capture_output=True, text=True).stdout.strip()

    assert ours != filtered, "tree-хэш лесенки всё ещё считается через clean-фильтр"


def test_fetch_adapter_rejects_command_executing_transport():
    """`ext::` исполняет ПРОИЗВОЛЬНУЮ команду — это RCE, а не выбор источника."""
    import prepush_gate as pg

    for bad in ("ext::sh -c 'touch /tmp/pwned'", "EXT::whoami", "--upload-pack=evil"):
        assert not pg._fetch_remote_allowed(bad), bad
    for ok in ("https://github.com/x/y.git", "git@github.com:x/y.git",
               "ssh://git@host/x.git", "/srv/bare/repo.git", "file:///srv/bare/repo.git"):
        assert pg._fetch_remote_allowed(ok), ok


def test_verify_deployable_builds_artifact_from_reviewed_commit(repo, monkeypatch, tmp_path,
                                                                capsys):
    """Гейт строит артефакт САМ и подтверждает только его: проверять «чисто ли дерево» и
    надеяться, что актуатор отправит именно его — обещание без подкрепления."""
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    reviewed = tmp_path / "lr"
    reviewed.write_text(head)
    monkeypatch.setattr(g, "LAST_REVIEWED", reviewed)
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "a.log")
    monkeypatch.setattr(g, "_require_repo", lambda: True)
    assert g.main(["verify-deployable"]) == 0
    out = capsys.readouterr().out
    assert "GATES_ARTIFACT=" in out and "GATES_ARTIFACT_SHA256=" in out
    art = [l.split("=", 1)[1] for l in out.splitlines() if l.startswith("GATES_ARTIFACT=")][0]
    assert os.path.isfile(art) and not art.startswith(str(repo)), "артефакт внутри репозитория"


def test_verify_deployable_refuses_when_head_moved(repo, monkeypatch, tmp_path, capsys):
    """HEAD сдвинулся после ревью — строить артефакт не из чего."""
    reviewed = tmp_path / "lr"
    reviewed.write_text("0" * 40)
    monkeypatch.setattr(g, "LAST_REVIEWED", reviewed)
    monkeypatch.setattr(g, "_require_repo", lambda: True)
    assert g.main(["verify-deployable"]) == 2
    assert "не совпадает с одобренным" in capsys.readouterr().err


def test_verify_deployable_refuses_dirty_tree(repo, monkeypatch, tmp_path, capsys):
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    (tmp_path / "lr").write_text(head)
    monkeypatch.setattr(g, "LAST_REVIEWED", tmp_path / "lr")
    monkeypatch.setattr(g, "_require_repo", lambda: True)
    (repo / "a.txt").write_text("dirty\n")
    assert g.main(["verify-deployable"]) == 2
    assert "грязное" in capsys.readouterr().err


def test_deploy_template_has_no_bare_git_in_decision_path():
    """PATH-шим вернул бы одобренный SHA и пустой статус после подмены дерева — финальная
    проверка reviewed≡deployed обязана идти через гейт, а не через голый git."""
    import pathlib as _pl

    tpl = (_pl.Path(g.__file__).resolve().parent.parent
           / "templates" / "Makefile.snippet").read_text()
    deploy = tpl[tpl.index("deploy: check-reviewed"):]
    for bad in ("git rev-parse", "git status", "git diff"):
        assert bad not in deploy, f"в деплой-пути шаблона остался голый {bad!r}"
    assert "verify-deployable" in deploy and "GATES_ARTIFACT" in deploy


def test_artifact_ignores_export_ignore_attribute(repo, monkeypatch, tmp_path, capsys):
    """`git archive` уважает export-ignore, то есть содержимым артефакта управляет
    непроверенное состояние репозитория. Артефакт собирается из дерева коммита."""
    import tarfile

    (repo / "secret.txt").write_text("must ship\n")
    (repo / ".gitattributes").write_text("secret.txt export-ignore\n")
    _git(repo, "add", "-A")
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "attrs")
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True).stdout.strip()
    (tmp_path / "lr").write_text(head)
    monkeypatch.setattr(g, "LAST_REVIEWED", tmp_path / "lr")
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "a.log")
    monkeypatch.setattr(g, "_require_repo", lambda: True)

    assert g.main(["verify-deployable"]) == 0
    art = [l.split("=", 1)[1] for l in capsys.readouterr().out.splitlines()
           if l.startswith("GATES_ARTIFACT=")][0]
    with tarfile.open(art) as tf:
        names = set(tf.getnames())
    assert "secret.txt" in names, "export-ignore выбросил закоммиченный файл из артефакта"
    assert "a.txt" in names


def test_deploy_skeleton_fails_instead_of_reporting_false_success(tmp_path):
    """Прежний скелет ЭХОИЛ команды выкатки и всё равно двигал baseline: `make deploy`
    рапортовал успех для кода, который никуда не уехал. Проверяем ИСПОЛНЕНИЕМ, а не
    поиском подстроки — прошлый тест был ложноположительным."""
    import pathlib as _pl

    tpl = (_pl.Path(g.__file__).resolve().parent.parent
           / "templates" / "Makefile.snippet").read_text()
    proj = tmp_path / "proj"
    (proj / ".claude").mkdir(parents=True)
    (proj / "Makefile").write_text(tpl)

    # оба обязательных таргета НЕ переопределены → деплой обязан упасть
    for target in ("deploy-payload", "verify-deployed"):
        r = subprocess.run(["make", "-s", target], cwd=proj, capture_output=True, text=True)
        assert r.returncode != 0, f"{target} по умолчанию обязан падать"
        assert "не переопределён" in r.stdout + r.stderr

    # baseline не должен появиться от одного лишь запуска
    assert not (proj / ".claude" / ".last-deployed-sha").exists()


def test_submodule_does_not_make_tree_permanently_dirty(tmp_path, monkeypatch):
    """Подмодуль (режим 160000) не файл: без явной ветки он попадал в «удалён или заменён»,
    и дерево считалось грязным ВСЕГДА — гейт был бы неприменим к таким проектам."""
    sub = tmp_path / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", ".")
    (sub / "s.txt").write_text("sub\n")
    _git(sub, "add", "-A")
    _git(sub, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "sub")

    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", ".")
    (main / "a.txt").write_text("a\n")
    _git(main, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor")
    _git(main, "add", "-A")
    _git(main, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "with sub")

    monkeypatch.setattr(g, "REPO_ROOT", main)
    assert g.working_tree_clean() is True, "подмодуль не должен делать дерево вечно грязным"

    # подмодуль сдвинут на другой коммит — это РЕАЛЬНОЕ расхождение
    (sub / "s.txt").write_text("moved\n")
    _git(sub, "add", "-A")
    _git(sub, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "move")
    _git(main / "vendor", "fetch", "-q", "origin")
    _git(main / "vendor", "-c", "protocol.file.allow=always", "pull", "-q", "--ff-only")
    assert g.working_tree_clean() is False


# ── Ревизии 6–10: вход ревьюеров, побайтовая верность, владение baseline ─────────────────

def _commit(repo, msg):
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", msg)


@pytest.fixture()
def delta_repo(repo, monkeypatch):
    monkeypatch.setattr(g, "_trusted_git_bytes", g._REAL_TRUSTED_GIT_BYTES)
    monkeypatch.setattr(g, "_trusted_git", g._REAL_TRUSTED_GIT_FOR_TESTS)
    return repo


def test_t12_gitattributes_nodiff_cannot_hide_source_from_reviewers(delta_repo):
    """`*.py -diff` заставляет `git diff` вернуть «Binary files differ» вместо кода: оба
    обязательных ревьюера получили бы пустышку и одобрили НЕПРОЧИТАННЫЙ payload, а
    `--no-textconv` этого не отменяет (он гасит внешние программы, а не сам атрибут)."""
    repo = delta_repo
    (repo / ".gitattributes").write_text("*.py -diff\n")
    (repo / "app.py").write_text("SAFE = 1\n")
    _git(repo, "add", ".gitattributes", "app.py")
    _commit(repo, "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "app.py").write_text("SAFE = 1\nos.system(ATTACK)\n")
    _git(repo, "add", "app.py")
    _commit(repo, "head")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    plain = _git(repo, "diff", "--no-ext-diff", "--no-textconv", f"{base}..{head}").stdout
    assert "ATTACK" not in plain, "предпосылка сломалась: git diff перестал скрывать код"

    text, err = g._diff_text(base, head)
    assert not err and "ATTACK" in text, text
    assert g.diff_sha256(base, head) == g.hashlib.sha256(
        text.encode("utf-8", "surrogateescape")).hexdigest()


def test_t13_raw_bytes_survive_invalid_utf8(delta_repo):
    """Побайтовая верность: невалидный UTF-8 не роняет сборку и не перекодируется — иначе
    артефакт перестаёт быть равен отревьюенному дереву, сохранив «успешный» sha256."""
    payload = b"\xff\xfe\x00binary\x80\x81"
    (delta_repo / "blob.bin").write_bytes(payload)
    _git(delta_repo, "add", "blob.bin")
    _commit(delta_repo, "binary")
    head = _git(delta_repo, "rev-parse", "HEAD").stdout.strip()
    entries = g._tree_entries(head)
    assert entries is not None and "blob.bin" in entries
    assert g._blob_bytes(entries["blob.bin"][1]) == payload, "байты blob'а изменились"


def test_t15_submodule_pointer_move_is_visible(tmp_path, monkeypatch):
    """РЕГРЕССИЯ 09.08.2026: сборка входа из blob'ов отбрасывала не-blob записи, и перевод
    подмодуля на код атакующего давал обоим ревьюерам ПУСТОЙ дифф."""
    sub = tmp_path / "sub"
    sub.mkdir()
    _git(sub, "init", "-q", ".")
    (sub / "s.txt").write_text("v1\n")
    _git(sub, "add", "-A")
    _commit(sub, "v1")
    main = tmp_path / "main"
    main.mkdir()
    _git(main, "init", "-q", ".")
    (main / "x.txt").write_text("x\n")
    _git(main, "add", "-A")
    _git(main, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor")
    _commit(main, "base")
    base = _git(main, "rev-parse", "HEAD").stdout.strip()
    (sub / "s.txt").write_text("ATTACKER\n")
    _git(sub, "add", "-A")
    _commit(sub, "evil")
    _git(main / "vendor", "fetch", "-q", "origin")
    _git(main / "vendor", "-c", "protocol.file.allow=always", "pull", "-q", "--ff-only")
    _git(main, "add", "vendor")
    _commit(main, "bump")
    head = _git(main, "rev-parse", "HEAD").stdout.strip()
    sub_new = _git(main, "rev-parse", "HEAD:vendor").stdout.strip()

    monkeypatch.setattr(g, "REPO_ROOT", main)
    monkeypatch.setattr(g, "_trusted_git_bytes", g._REAL_TRUSTED_GIT_BYTES)
    monkeypatch.setattr(g, "_trusted_git", g._REAL_TRUSTED_GIT_FOR_TESTS)
    text, err = g._diff_text(base, head)
    assert not err
    assert "vendor" in text and sub_new in text, f"сдвиг подмодуля невидим:\n{text}"


def test_t14_delta_covers_every_change_class(delta_repo):
    """Классы, которые показывал заменённый `git diff`, обязаны остаться видимыми: тип
    симлинка, направление пустого файла, бинарь ТОГО ЖЕ размера с другим содержимым."""
    repo = delta_repo
    (repo / "lnk").symlink_to("a.txt")
    (repo / "bin.dat").write_bytes(b"\x00AAAA")
    (repo / "gone.txt").write_text("bye\n")
    _git(repo, "add", "-A")
    _commit(repo, "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "lnk").unlink()
    (repo / "lnk").symlink_to("/etc/passwd")
    (repo / "bin.dat").write_bytes(b"\x00BBBB")          # тот же размер!
    (repo / "gone.txt").unlink()
    (repo / "empty.txt").write_text("")
    _git(repo, "add", "-A")
    _commit(repo, "head")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    text, err = g._diff_text(base, head)
    assert not err
    assert "link" in text and "/etc/passwd" in text, "ретаргет симлинка неотличим от правки файла"
    assert text.count("sha256:") == 2, "бинарь того же размера схлопнулся в одинаковое описание"
    assert "status: D\npath: gone.txt" in text and "status: A\npath: empty.txt" in text, \
        "добавление и удаление пустого файла неразличимы"


def test_t14b_delta_is_injective_over_tree_pairs(tmp_path, monkeypatch):
    """Контрпример ревью: пары (B1→H1) и (B2→H2) различаются только НЕизменившейся записью
    `c`. Без конверта с OID деревьев их входы совпадают, и два разных перехода становятся
    неотличимы для обоих ревьюеров."""
    monkeypatch.setattr(g, "_trusted_git_bytes", g._REAL_TRUSTED_GIT_BYTES)
    monkeypatch.setattr(g, "_trusted_git", g._REAL_TRUSTED_GIT_FOR_TESTS)
    texts = []
    for i, c_val in enumerate(("C\n", "D\n")):
        r = tmp_path / f"r{i}"
        r.mkdir()
        _git(r, "init", "-q", ".")
        (r / "a").write_text("X\n")
        (r / "c").write_text(c_val)
        _git(r, "add", "-A")
        _commit(r, "base")
        base = _git(r, "rev-parse", "HEAD").stdout.strip()
        (r / "a").write_text("Y\n")
        _git(r, "add", "-A")
        _commit(r, "head")
        head = _git(r, "rev-parse", "HEAD").stdout.strip()
        monkeypatch.setattr(g, "REPO_ROOT", r)
        text, err = g._diff_text(base, head)
        assert not err
        texts.append(text)
    assert texts[0] != texts[1], "два разных перехода дали ОДИН вход ревьюеров"


# ── T19–T21b: владение baseline ревью (G21, G25, G25b) ──────────────────────────────────

def _art(tmp_path, payload=b"tar-bytes"):
    a = tmp_path / "a.tar"
    a.write_bytes(payload)
    return a, g.hashlib.sha256(payload).hexdigest()


def test_t20_artifact_mutation_is_caught_before_actuator(repo, tmp_path, monkeypatch, capsys):
    """Проверка дайджеста ТОЛЬКО в финализации ловит подмену уже ПОСЛЕ того, как
    неотревьюенный payload уехал. check-artifact стоит перед выкаткой."""
    art, digest = _art(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert g.main(["check-artifact", "--artifact", str(art), "--head", head,
                   "--sha256", digest]) == 0
    art.write_bytes(b"SWAPPED")
    assert g.main(["check-artifact", "--artifact", str(art), "--head", head,
                   "--sha256", digest]) == 2
    assert "ИЗМЕНИЛСЯ" in capsys.readouterr().err


def test_t21b_emergency_skip_does_not_advance_baseline(repo, tmp_path, monkeypatch, capsys):
    """G25b: `CODEX_REVIEW_SKIP` даёт и отметку «отревьюено», и allow, НЕ запуская панель.
    Сдвиг baseline по такой записи исключил бы непросмотренный диапазон из ВСЕХ будущих
    ревью — потеря покрытия навсегда, которую аудит не возвращает."""
    art, digest = _art(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(g, "git_head", lambda: head)
    monkeypatch.setattr(g, "LAST_DEPLOYED", repo / ".claude" / ".last-deployed-sha")
    monkeypatch.setattr(g, "PANEL_EVIDENCE", repo / ".claude" / ".panel-evidence.json")
    monkeypatch.setattr(g, "GATE_BASELINE", repo / ".claude" / ".gate-review-baseline")
    monkeypatch.setattr(g, "LAST_REVIEWED", repo / ".claude" / ".last-reviewed-sha")
    g._record_reviewed(head, None)                       # ровно то, что делает skip-путь
    assert g.main(["finalize-deploy", "--artifact", str(art), "--head", head,
                   "--sha256", digest]) == 0            # деплой состоялся
    assert not g.GATE_BASELINE.exists(), "baseline сдвинут без evidence панели"
    assert "НЕ сдвинут" in capsys.readouterr().err


def test_t21_baseline_advances_only_with_full_panel_evidence(repo, tmp_path, monkeypatch):
    art, digest = _art(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(g, "git_head", lambda: head)
    monkeypatch.setattr(g, "LAST_DEPLOYED", repo / ".claude" / ".last-deployed-sha")
    monkeypatch.setattr(g, "PANEL_EVIDENCE", repo / ".claude" / ".panel-evidence.json")
    monkeypatch.setattr(g, "GATE_BASELINE", repo / ".claude" / ".gate-review-baseline")
    monkeypatch.setattr(g, "LAST_REVIEWED", repo / ".claude" / ".last-reviewed-sha")
    (repo / ".claude").mkdir(exist_ok=True)

    half = [{"role": "blocking", "family": "openai", "status": "ok",
             "certification_id": "c1", "actual_models": ["gpt-5.6"]}]
    g._record_reviewed(head, {"head_sha": head, "reviewers": half})
    assert g.main(["finalize-deploy", "--artifact", str(art), "--head", head,
                   "--sha256", digest]) == 0
    assert not g.GATE_BASELINE.exists(), "хватило ОДНОГО семейства — пара необязательна?"

    full = half + [{"role": "blocking", "family": "anthropic", "status": "ok",
                    "certification_id": "c2", "actual_models": ["claude-opus-5"]}]
    g._record_reviewed(head, {"head_sha": head, "reviewers": full})
    assert g.main(["finalize-deploy", "--artifact", str(art), "--head", head,
                   "--sha256", digest]) == 0
    assert g.GATE_BASELINE.read_text().strip() == head


def test_t21_evidence_for_another_commit_is_rejected(repo, tmp_path, monkeypatch):
    """Evidence прошлого прогона не должен разрешать сдвиг baseline на ДРУГОЙ коммит."""
    art, digest = _art(tmp_path)
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.setattr(g, "git_head", lambda: head)
    monkeypatch.setattr(g, "LAST_DEPLOYED", repo / ".claude" / ".last-deployed-sha")
    monkeypatch.setattr(g, "PANEL_EVIDENCE", repo / ".claude" / ".panel-evidence.json")
    monkeypatch.setattr(g, "GATE_BASELINE", repo / ".claude" / ".gate-review-baseline")
    monkeypatch.setattr(g, "LAST_REVIEWED", repo / ".claude" / ".last-reviewed-sha")
    (repo / ".claude").mkdir(exist_ok=True)
    g._record_reviewed(head, {"head_sha": "0" * 40, "reviewers": [
        {"role": "blocking", "family": "openai", "status": "ok",
         "certification_id": "c1", "actual_models": ["gpt-5.6"]},
        {"role": "blocking", "family": "anthropic", "status": "ok",
         "certification_id": "c2", "actual_models": ["claude-opus-5"]}]})
    assert g.main(["finalize-deploy", "--artifact", str(art), "--head", head,
                   "--sha256", digest]) == 0
    assert not g.GATE_BASELINE.exists()


# ── T17/T18: лесенка — корень и эпоха через слой (G19, G20) ─────────────────────────────

def _shim(tmp_path, script):
    """PATH-шим `git`: ровно та подделка, против которой строится слой."""
    d = tmp_path / "shim"
    d.mkdir(exist_ok=True)
    (d / "git").write_text(script)
    (d / "git").chmod(0o755)
    return d


def test_t17_repo_root_shim_cannot_redirect_ladder(repo, tmp_path, monkeypatch):
    """Шим уводит `rev-parse --show-toplevel` во ВТОРОЙ чистый репозиторий: доверенные
    операции добросовестно изучают чужой корень, не находят staged-кода и выдают
    non-code освобождение без аудита."""
    import ladder_gate as L

    other = tmp_path / "other"
    other.mkdir()
    _git(other, "init", "-q", ".")
    shim = _shim(tmp_path, f'#!/bin/sh\nif [ "$2" = "--show-toplevel" ]; then\n'
                           f'  echo "{other}"\nelse\n  exec /usr/bin/git "$@"\nfi\n')
    monkeypatch.chdir(repo)
    monkeypatch.setenv("PATH", f"{shim}:{os.environ['PATH']}")
    root = L._repo_root()
    assert root == repo.resolve(), f"лесенка ушла в чужой корень: {root}"


def test_t17_unresolvable_root_with_git_marker_fails_closed(tmp_path, monkeypatch):
    """Маркер `.git` есть, а корень не разрешился — молчаливый откат на cwd означал бы
    решения лесенки, принятые не на чём."""
    import ladder_gate as L

    r = tmp_path / "broken"
    (r / ".git").mkdir(parents=True)
    monkeypatch.chdir(r)
    monkeypatch.setattr(L, "_bootstrap_git", lambda *a, **k: None)
    with pytest.raises(L.TrustedGitError):
        L._repo_root()


def test_t18_epoch_ancestry_distinguishes_answer_from_failure(repo, monkeypatch):
    """exit 1 у `merge-base --is-ancestor` — легитимный ответ «не предок», а недоступный
    слой — сбой. Смешивать их значило бы выдавать освобождение по эпохе при любом сбое."""
    import ladder_gate as L

    first = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "b.txt").write_text("second\n")
    _git(repo, "add", "-A")
    _commit(repo, "second")
    second = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert L._is_ancestor(repo, first, second) is True       # предок
    assert L._is_ancestor(repo, second, first) is False      # exit 1 — ОТВЕТ, а не сбой
    monkeypatch.setattr(L, "_trusted_git", lambda *a, **k: None)
    with pytest.raises(L.TrustedGitError):                   # слой недоступен — освобождения нет
        L._is_ancestor(repo, first, second)


def test_security_repo_supplied_baseline_cannot_self_exclude(repo, monkeypatch, tmp_path):
    """Ветка, принёсшая маркер baseline со значением HEAD, делала диапазон ПУСТЫМ: оба
    обязательных ревьюера честно одобряли пустоту, и это «evidence» сдвигало baseline на HEAD,
    навсегда исключая весь предшествующий payload из ревью (security-проход 09.08.2026).

    Два независимых барьера: состояние гейта живёт ВНЕ рабочего дерева, и evidence пустого
    диапазона baseline не двигает."""
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    # 1. Подброшенный в репозиторий файл больше не является входом решения.
    (repo / ".claude").mkdir(exist_ok=True)
    (repo / ".claude" / ".gate-review-baseline").write_text(head + "\n")
    (repo / ".claude" / ".last-deployed-sha").write_text(head + "\n")
    monkeypatch.setattr(g, "GATE_BASELINE", tmp_path / "state" / "review-baseline")
    monkeypatch.setattr(g, "LAST_DEPLOYED", repo / ".claude" / ".last-deployed-sha")
    monkeypatch.delenv("CODEX_DEPLOY_BASELINE", raising=False)
    assert g.resolve_baseline() is None, "рецептный/подброшенный маркер всё ещё читается"

    # 2. И даже честный evidence пустого диапазона baseline не двигает.
    art = tmp_path / "a.tar"
    art.write_bytes(b"x")
    digest = g.hashlib.sha256(b"x").hexdigest()
    monkeypatch.setattr(g, "git_head", lambda: head)
    monkeypatch.setattr(g, "PANEL_EVIDENCE", tmp_path / "state" / "panel-evidence.json")
    monkeypatch.setattr(g, "LAST_REVIEWED", tmp_path / "state" / "last-reviewed")
    (tmp_path / "state").mkdir(exist_ok=True)
    g._record_reviewed(head, {"head_sha": head, "baseline_sha": head, "reviewers": [
        {"role": "blocking", "family": "openai", "status": "ok",
         "certification_id": "c1", "actual_models": ["gpt-5.6"]},
        {"role": "blocking", "family": "anthropic", "status": "ok",
         "certification_id": "c2", "actual_models": ["claude-opus-5"]}]})
    led = g.load_findings_ledger(head)
    g.save_findings_ledger(led)
    assert g.main(["finalize-deploy", "--artifact", str(art), "--head", head,
                   "--sha256", digest]) == 0
    assert not g.GATE_BASELINE.exists(), "baseline сдвинут по пустому диапазону"


def test_security_binary_change_blocks_deploy_and_leaves_no_evidence(delta_repo, monkeypatch,
                                                                     capsys):
    """Бинарь ревьюеры видят только как размер+sha256, а в артефакт он уезжает целиком:
    одобрение непрозрачного хэша засчитывалось за полное ревью (security-проход 09.08.2026).
    Обход существует, но он громкий и evidence не оставляет — baseline не двигается."""
    repo = delta_repo
    (repo / "payload.bin").write_bytes(b"\x00safe")
    _git(repo, "add", "-A")
    _commit(repo, "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "payload.bin").write_bytes(b"\x00EVIL")
    _git(repo, "add", "-A")
    _commit(repo, "head")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    text, err = g._diff_text(base, head)
    assert not err and "EVIL" not in text, "предпосылка: содержимое бинаря во вход не попадает"
    assert g.binary_changes(base, head) == ["payload.bin"]

    monkeypatch.setattr(g, "resolve_baseline", lambda: base)
    monkeypatch.setattr(g, "git_head", lambda: head)
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "_require_repo", lambda: True)
    monkeypatch.setattr(g, "_ladder_check", lambda *a, **k: 0)   # лесенка — не предмет теста
    monkeypatch.delenv("GATES_ALLOW_BINARY", raising=False)
    assert g.check_reviewed_cli() == 2
    assert "бинарные изменения" in capsys.readouterr().err


def test_security_moving_baseline_ref_cannot_split_the_range(delta_repo, monkeypatch, capsys):
    """`CODEX_DEPLOY_BASELINE` принимает символическое выражение. Пока каждый потребитель
    резолвил его заново, конкурентный `update-ref` давал хэш по B..H, проверку бинарей по
    H..H и ревью снова по B..H. Теперь baseline закрепляется в OID один раз."""
    repo = delta_repo
    (repo / "p.bin").write_bytes(b"\x00safe")
    _git(repo, "add", "-A")
    _commit(repo, "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "p.bin").write_bytes(b"\x00EVIL")
    _git(repo, "add", "-A")
    _commit(repo, "head")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    _git(repo, "update-ref", "refs/heads/moving", base)

    monkeypatch.setenv("CODEX_DEPLOY_BASELINE", "refs/heads/moving")
    monkeypatch.setattr(g, "git_head", lambda: head)
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "_require_repo", lambda: True)
    monkeypatch.setattr(g, "_ladder_check", lambda *a, **k: 0)
    monkeypatch.delenv("GATES_ALLOW_BINARY", raising=False)
    # Ссылка «уезжает» на HEAD сразу после того, как гейт её выбрал: раньше следующий
    # потребитель резолвил её заново и видел пустой диапазон H..H без бинаря.
    real_resolve = g._resolve_commit
    moved = {"done": False}

    def resolve_then_move(rev):
        out = real_resolve(rev)
        if not moved["done"] and rev == "refs/heads/moving":
            _git(repo, "update-ref", "refs/heads/moving", head)
            moved["done"] = True
        return out

    monkeypatch.setattr(g, "_resolve_commit", resolve_then_move)
    assert g.check_reviewed_cli() == 2, "подвижная ссылка спрятала бинарь от ревью"
    assert "бинарные изменения" in capsys.readouterr().err
    assert moved["done"], "тест не вошёл в проверяемую ветку — ссылка не двигалась"


def test_security_deploy_pin_lives_outside_the_repository(repo, monkeypatch):
    """Pin авторизует `deploy.baseline_command` — команду, назначающую границу ревью.
    Лёжа в `.claude/`, он позволял атакующему закоммитить и команду, и подходящий pin, сделав
    диапазон пустым и исключив собственное изменение из ревью."""
    monkeypatch.setattr(g, "REPO_ROOT", repo)
    monkeypatch.setattr(g, "_trusted_home", lambda: repo.parent / "home")
    (repo.parent / "home" / ".local" / "state").mkdir(parents=True, exist_ok=True)
    pin = g._gate_state_dir() / "deploy-section-pin"
    assert repo.resolve() not in pin.resolve().parents, f"pin внутри репозитория: {pin}"


def test_security_state_dir_inside_repo_fails_closed(tmp_path, monkeypatch):
    """Доверенный HOME сам по себе не спасает: репозиторий, укоренённый в домашнем каталоге,
    снова содержит состояние гейта, и ветка приносит собственный baseline."""
    home = tmp_path / "home"
    (home / ".local" / "state").mkdir(parents=True)
    monkeypatch.setattr(g, "REPO_ROOT", home)
    monkeypatch.setattr(g, "_trusted_home", lambda: home)
    # conftest подменяет `_gate_state_dir` ради изоляции состояния — здесь нужен НАСТОЯЩИЙ.
    monkeypatch.setattr(g, "_gate_state_dir", g._REAL_GATE_STATE_DIR)
    with pytest.raises(g.TrustedGitError):
        g._gate_state_dir()


def test_interrupted_deploy_keeps_evidence_for_retry(delta_repo, tmp_path, monkeypatch):
    """Деплой мог прерваться между реальным прогоном панели и finalize-deploy. Повторная
    попытка идёт по КЭШУ ledger'а и панель уже не запускает — если кэш-путь стирает честный
    evidence, baseline не сдвинется никогда (находка финального код-ревью 09.08.2026).

    Тест входит именно в кэш-ветку check_reviewed_cli, а не проверяет хелпер: прошлая версия
    была зелёной при НЕприменённом фиксе."""
    repo = delta_repo
    (repo / "app.py").write_text("x = 1\n")
    _git(repo, "add", "-A")
    _commit(repo, "base")
    base = _git(repo, "rev-parse", "HEAD").stdout.strip()
    (repo / "app.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _commit(repo, "head")
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()

    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(g, "GATE_BASELINE", state / "review-baseline")
    monkeypatch.setattr(g, "PANEL_EVIDENCE", state / "panel-evidence.json")
    monkeypatch.setattr(g, "LAST_REVIEWED", state / "last-reviewed")
    monkeypatch.setattr(g, "resolve_baseline", lambda: base)
    monkeypatch.setattr(g, "git_head", lambda: head)
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "_require_repo", lambda: True)
    monkeypatch.setattr(g, "_ladder_check", lambda *a, **k: 0)
    monkeypatch.setattr(g, "_run_empirical", lambda *a, **k: ("skipped", ""))

    diff_sha = g.diff_sha256(base, head)
    rows = [{"role": "blocking", "family": "openai", "status": "ok",
             "certification_id": "c1", "actual_models": ["gpt-5.6"]},
            {"role": "blocking", "family": "anthropic", "status": "ok",
             "certification_id": "c2", "actual_models": ["claude-opus-5"]}]
    g._record_reviewed(head, {"head_sha": head, "baseline_sha": base,
                              "diff_sha256": diff_sha, "reviewers": rows})
    # валидный кэш чистого ревью того же диапазона → check_reviewed_cli пойдёт по кэш-ветке
    plan, perr = g.resolve_portable_review_plan("portable")
    assert plan, perr
    reviewers = [g._cert_cache_record(
        c, "supplemental" if "supplemental" in c.roles else "blocking") for c in plan]
    g.write_ledger(head, diff_sha, base,
                   g.ReviewVerdict("approve", [], False, True), reviewers)
    assert g.read_valid_ledger(head, diff_sha, reviewers) is not None, "кэш не принят"

    assert g.check_reviewed_cli() == 0
    assert g._panel_evidence_ok(head, base, diff_sha)[0], \
        "кэш-путь уничтожил честный evidence — baseline больше не сдвинуть никогда"


def test_finalize_after_skip_reports_success_not_failure(repo, tmp_path, monkeypatch, capsys):
    """Аварийный деплой с ОТКРЫТОЙ находкой: finalize вызывается ПОСЛЕ выкатки, поэтому
    возврат 2 читался бы автоматикой как неуспех и мог повторить неидемпотентный актуатор.
    Правильный ответ — успех БЕЗ сдвига baseline."""
    art = tmp_path / "a.tar"
    art.write_bytes(b"y")
    digest = g.hashlib.sha256(b"y").hexdigest()
    head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    state = tmp_path / "state"
    state.mkdir()
    monkeypatch.setattr(g, "git_head", lambda: head)
    monkeypatch.setattr(g, "GATE_BASELINE", state / "review-baseline")
    monkeypatch.setattr(g, "PANEL_EVIDENCE", state / "panel-evidence.json")
    monkeypatch.setattr(g, "LAST_REVIEWED", state / "last-reviewed")
    g._record_reviewed(head, None)                       # ровно то, что делает skip-путь

    led = g.load_findings_ledger("b" * 40)
    g.merge_round(led, [("high", "Открытая находка")])
    g.save_findings_ledger(led)

    assert g.main(["finalize-deploy", "--artifact", str(art), "--head", head,
                   "--sha256", digest]) == 0, "падение ПОСЛЕ выкатки провоцирует повтор"
    assert not g.GATE_BASELINE.exists()
    assert "НЕ сдвинут" in capsys.readouterr().err
