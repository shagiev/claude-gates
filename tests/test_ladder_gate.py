import json
import os
import subprocess
from pathlib import Path

import pytest

import ladder_gate as lg


@pytest.fixture(autouse=True)
def _isolate_session(monkeypatch):
    # как в test_codex_review_gate: реальный CLAUDE_CODE_SESSION_ID не должен
    # перебивать сессию, которую задаёт тест через CLAUDE_SESSION_ID
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)


@pytest.fixture()
def repo(tmp_path):
    r = tmp_path / "r"
    r.mkdir()
    def git(*a):
        subprocess.run(["git", *a], cwd=r, check=True, capture_output=True,
                       env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                            "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                            "PATH": __import__("os").environ["PATH"],
                            "HOME": str(tmp_path)})
    git("init", "-b", "main")
    (r / "app").mkdir()
    (r / "app" / "x.py").write_text("x = 1\n")
    git("add", "-A"); git("commit", "-m", "init")
    return r


def test_compute_tree_includes_untracked_and_keeps_index(repo):
    t0 = lg.compute_tree(repo)
    (repo / "app" / "new.py").write_text("n = 1\n")   # untracked
    t1 = lg.compute_tree(repo)
    assert t0 != t1                                    # untracked учтён
    st = subprocess.run(["git", "status", "--porcelain"], cwd=repo,
                        capture_output=True, text=True).stdout
    assert "?? app/new.py" in st                       # реальный индекс НЕ тронут (не застейджен)


def test_begin_mark_protocol(repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    lg.begin_pass(repo, "simplify")
    (repo / "app" / "x.py").write_text("x = 2\n")      # «фиксы прохода»
    lg.mark_pass(repo, "simplify")
    m = lg.read_marker(repo, "simplify")
    assert m["tree_before"] != m["tree_after"]

def test_mark_without_begin_errors(repo):
    with pytest.raises(lg.LadderError):
        lg.mark_pass(repo, "simplify")

def test_mark_consumes_pending_no_replay(repo):
    lg.begin_pass(repo, "simplify")
    lg.mark_pass(repo, "simplify")
    with pytest.raises(lg.LadderError):                # R7: повторный mark без begin
        lg.mark_pass(repo, "simplify")

def test_begin_codereview_validates_chain_start(repo):
    lg.begin_pass(repo, "simplify"); lg.mark_pass(repo, "simplify")
    (repo / "app" / "x.py").write_text("x = 3\n")      # ручная правка МЕЖДУ проходами
    with pytest.raises(lg.LadderError):                # R7: ловится на begin
        lg.begin_pass(repo, "code-review")

def test_full_chain_ok(repo):
    lg.begin_pass(repo, "simplify")
    (repo / "app" / "x.py").write_text("x = 2\n")
    lg.mark_pass(repo, "simplify")
    lg.begin_pass(repo, "code-review")                 # старт == simplify.after
    (repo / "app" / "x.py").write_text("x = 2  # reviewed\n")   # фиксы code-review
    lg.mark_pass(repo, "code-review")
    s, c = lg.read_marker(repo, "simplify"), lg.read_marker(repo, "code-review")
    assert s["tree_after"] == c["tree_before"]         # цепочка
    assert c["tree_after"] == lg.compute_tree(repo)

def test_unknown_pass_errors(repo):
    with pytest.raises(lg.LadderError):
        lg.begin_pass(repo, "bogus")
    with pytest.raises(lg.LadderError):      # симметрия (ревью Task 1)
        lg.mark_pass(repo, "bogus")


def test_marker_session_recorded(repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-1")
    lg.begin_pass(repo, "simplify"); lg.mark_pass(repo, "simplify")
    assert lg.read_marker(repo, "simplify")["session"] == "sess-1"


def test_bookkeeping_exclusion_is_narrow(repo):
    # ревью Task 1: исключены только 4 литеральных файла бухгалтерии; произвольный файл
    # под .claude/.ladder-* ОБЯЗАН влиять на tree-хэш (иначе им можно спрятать дифф)
    t0 = lg.compute_tree(repo)
    d = repo / ".claude" / ".ladder-foo"
    d.mkdir(parents=True)
    (d / "nested.py").write_text("hidden = 1\n")
    assert lg.compute_tree(repo) != t0               # НЕ спрятан
    # а сами маркеры протокола — не влияют
    lg.begin_pass(repo, "simplify")
    t1 = lg.compute_tree(repo)
    lg.mark_pass(repo, "simplify")
    assert lg.compute_tree(repo) == t1               # маркер/pending не меняют хэш


def test_full_chain_ok_with_real_gitignore(repo):
    # Task 4 smoke-test finding: реальный репо гитигнорит `.claude/*`. Второй вызов
    # compute_tree (begin code-review), когда .claude/.ladder-simplify УЖЕ существует на
    # диске и подпадает под этот .gitignore, раньше падал CalledProcessError — git трактует
    # негативный pathspec `:!.claude/.ladder-simplify` на существующий игнорируемый путь как
    # явную попытку добавить игнорируемый файл (`fatal: ... ignored ... use -f`). Фикс:
    # исключение через `git rm --cached --ignore-unmatch` после `add -A`, не через pathspec.
    (repo / ".gitignore").write_text(".claude/*\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "add gitignore"], cwd=repo, check=True,
                   capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": os.environ["PATH"], "HOME": str(repo.parent)})
    lg.begin_pass(repo, "simplify")
    (repo / "app" / "x.py").write_text("x = 2\n")
    lg.mark_pass(repo, "simplify")
    lg.begin_pass(repo, "code-review")   # раньше крашилось здесь (marker уже на диске)
    (repo / "app" / "x.py").write_text("x = 2  # reviewed\n")
    lg.mark_pass(repo, "code-review")
    s, c = lg.read_marker(repo, "simplify"), lg.read_marker(repo, "code-review")
    assert s["tree_after"] == c["tree_before"]
    assert c["tree_after"] == lg.compute_tree(repo)


# --- Task 2: pre-commit / post-commit ---
# NB: реальный проектный репо гитигнорит `.claude/*` — поэтому begin/mark маркеры никогда не
# попадают в реальный staged-индекс. Тестовый repo-фикстура .gitignore не заводит (нужен Task 1
# test_bookkeeping_exclusion_is_narrow, где .claude/.ladder-foo ОБЯЗАН влиять на compute_tree) —
# поэтому здесь стейджим явные пути кода (не `git add -A`), воспроизводя тот же эффект.
def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                   env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                        "PATH": os.environ["PATH"], "HOME": str(repo.parent)})


def _head(repo) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


def _head_tree(repo) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


def _grow_valid_chain(repo):
    """begin/mark simplify → begin/mark code-review, оставляя финальные правки в app/x.py."""
    lg.begin_pass(repo, "simplify")
    (repo / "app" / "x.py").write_text("x = 2\n")
    lg.mark_pass(repo, "simplify")
    lg.begin_pass(repo, "code-review")
    (repo / "app" / "x.py").write_text("x = 2  # reviewed\n")
    lg.mark_pass(repo, "code-review")


# --- changed_paths_staged / commit_touches_code ---
def test_changed_paths_staged_and_commit_touches_code(repo):
    (repo / "app" / "x.py").write_text("x = 3\n")
    subprocess.run(["git", "add", "app/x.py"], cwd=repo, check=True)
    paths = lg.changed_paths_staged(repo)
    assert paths == ["app/x.py"]
    assert lg.commit_touches_code(paths) is True
    assert lg.commit_touches_code(["README.md", "docs/x.md"]) is False


# --- ladder_enabled ---
def test_ladder_enabled_default_true_no_config(repo):
    assert lg.ladder_enabled(repo) is True


def test_ladder_enabled_false_via_config(repo):
    (repo / ".codex-gate.yaml").write_text("ladder:\n  enabled: false\n")
    assert lg.ladder_enabled(repo) is False


def test_ladder_enabled_malformed_yaml_defaults_true(repo):
    (repo / ".codex-gate.yaml").write_text("ladder: [unclosed\n")
    assert lg.ladder_enabled(repo) is True


def test_ladder_enabled_missing_key_defaults_true(repo):
    (repo / ".codex-gate.yaml").write_text("ladder:\n  required_passes: [simplify]\n")
    assert lg.ladder_enabled(repo) is True


# --- check_precommit ---
def test_precommit_exempt_noncode(repo):
    (repo / "README.md").write_text("hi\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    assert lg.check_precommit(repo) == 0


def test_precommit_disabled_via_config(repo):
    (repo / ".codex-gate.yaml").write_text("ladder:\n  enabled: false\n")
    # enabled=false чтится только из доверенного конфига (worktree == index) — стейджим
    subprocess.run(["git", "add", ".codex-gate.yaml"], cwd=repo, check=True)
    (repo / "app" / "x.py").write_text("x = 99\n")
    subprocess.run(["git", "add", "app/x.py"], cwd=repo, check=True)
    assert lg.check_precommit(repo) == 0


def test_precommit_ladder_skip_audited(repo, monkeypatch):
    (repo / "app" / "x.py").write_text("x = 99\n")
    subprocess.run(["git", "add", "app/x.py"], cwd=repo, check=True)
    monkeypatch.setenv("LADDER_SKIP", "1")
    monkeypatch.setenv("LADDER_SKIP_REASON", "hotfix")
    assert lg.check_precommit(repo) == 0
    audit_log = repo / "logs" / "codex_review_audit.log"
    assert audit_log.exists()
    assert "hotfix" in audit_log.read_text()


def test_precommit_valid_chain_allows(repo):
    _grow_valid_chain(repo)
    subprocess.run(["git", "add", "app/x.py"], cwd=repo, check=True)
    assert lg.check_precommit(repo) == 0


def test_precommit_blocks_broken_chain_with_instructions(repo, capsys):
    (repo / "app" / "x.py").write_text("x = 42\n")   # нет ни одного маркера
    subprocess.run(["git", "add", "app/x.py"], cwd=repo, check=True)
    assert lg.check_precommit(repo) == 2
    err = capsys.readouterr().err
    assert "begin simplify" in err and "mark simplify" in err
    assert "begin code-review" in err and "mark code-review" in err


def test_precommit_blocks_manual_edit_between_passes(repo):
    lg.begin_pass(repo, "simplify")
    (repo / "app" / "x.py").write_text("x = 2\n")
    lg.mark_pass(repo, "simplify")
    (repo / "app" / "x.py").write_text("x = 3\n")     # ручная правка МЕЖДУ проходами
    subprocess.run(["git", "add", "app/x.py"], cwd=repo, check=True)
    assert lg.check_precommit(repo) == 2              # code-review так и не запускался


# --- record_commit / ledger ---
def test_record_commit_merge_no_ledger(repo):
    _git(repo, "checkout", "-b", "feature")
    (repo / "app" / "y.py").write_text("y = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature")
    _git(repo, "checkout", "main")
    (repo / "app" / "z.py").write_text("z = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main change")
    _git(repo, "merge", "--no-ff", "feature", "-m", "merge")
    head = _head(repo)
    lg.record_commit(repo)
    assert lg.read_ledger(repo, head) is None
    assert not lg.ledger_path(repo, head).exists()


def test_record_commit_exempt_noncode(repo):
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "docs")
    head, tree = _head(repo), _head_tree(repo)
    lg.record_commit(repo)
    rec = lg.read_ledger(repo, head)
    assert rec == {"passes": ["exempt-noncode"], "tree": tree, "ts": rec["ts"]}


def test_record_commit_skipped(repo, monkeypatch):
    (repo / "app" / "x.py").write_text("x = 7\n")
    _git(repo, "add", "-A")
    monkeypatch.setenv("LADDER_SKIP", "1")
    monkeypatch.setenv("LADDER_SKIP_REASON", "hotfix")
    _git(repo, "commit", "-m", "skip commit")
    head, tree = _head(repo), _head_tree(repo)
    lg.record_commit(repo)
    rec = lg.read_ledger(repo, head)
    assert rec["skipped"] is True
    assert rec["reason"] == "hotfix"
    assert rec["tree"] == tree


def test_record_commit_valid_chain_full_record(repo):
    _grow_valid_chain(repo)
    _git(repo, "add", "app/x.py")
    _git(repo, "commit", "-m", "reviewed change")
    head, tree = _head(repo), _head_tree(repo)
    lg.record_commit(repo)
    rec = lg.read_ledger(repo, head)
    assert rec["passes"] == ["simplify", "code-review"]
    assert rec["tree"] == tree


def test_record_commit_invalid_chain_no_record_loud_stderr(repo, capsys):
    (repo / "app" / "x.py").write_text("x = 55\n")    # никакого begin/mark
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "unreviewed change")
    head = _head(repo)
    lg.record_commit(repo)
    assert lg.read_ledger(repo, head) is None
    err = capsys.readouterr().err
    assert head[:12] in err


# --- CLI ---
def test_cli_check_precommit_and_record_commit(repo, monkeypatch):
    monkeypatch.chdir(repo)
    (repo / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    assert lg.main(["check-precommit"]) == 0
    _git(repo, "commit", "-m", "docs cli")
    assert lg.main(["record-commit"]) == 0
    assert lg.read_ledger(repo, _head(repo))["passes"] == ["exempt-noncode"]


# --- ревью Task 2: непокрытые ветки ---
def test_record_commit_never_raises_on_write_failure(repo, monkeypatch, capsys):
    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(lg, "_write_ledger", boom)
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "README.md"); _git(repo, "commit", "-m", "docs")
    lg.record_commit(repo)                               # не должен поднять
    err = capsys.readouterr().err
    assert "OSError" in err                              # громко, с типом
    assert lg.read_ledger(repo, _head(repo)) is None     # записи нет (fail-closed ниже по стеку)


def test_precommit_branch_order_no_audit_on_shortcircuit(repo, monkeypatch, tmp_path):
    # exempt (не-код) и enabled=false срабатывают РАНЬШЕ LADDER_SKIP → аудит-строки НЕТ
    audit = repo / "logs" / "codex_review_audit.log"
    monkeypatch.setenv("LADDER_SKIP", "1")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "README.md")
    assert lg.check_precommit(repo) == 0                 # exempt, не skip
    assert not audit.exists()
    (repo / ".codex-gate.yaml").write_text("ladder:\n  enabled: false\n")
    _git(repo, "add", ".codex-gate.yaml")                # доверенный (staged) конфиг
    (repo / "app" / "x.py").write_text("x = 42\n")
    _git(repo, "add", "app/x.py")
    assert lg.check_precommit(repo) == 0                 # disabled, не skip
    assert not audit.exists()


# --- Task 3: check_range (спека §4, деплой-гейт по диапазону) ---

def test_check_range_intermediate_commit_uncovered(repo, capsys):
    # (а) промежуточный код-коммит БЕЗ ladder-записи → 2, даже если у HEAD запись валидна;
    # промежуточный sha назван в stderr
    baseline = _head(repo)
    (repo / "app" / "x.py").write_text("x = 2\n")        # ни begin/mark, ни хук не запускались
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "intermediate unreviewed")
    intermediate = _head(repo)
    _grow_valid_chain(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "final reviewed")
    lg.record_commit(repo)
    assert lg.check_range(repo, baseline) == 2
    err = capsys.readouterr().err
    assert intermediate[:12] in err


def test_check_range_tree_mismatch(repo):
    # (б) запись есть, но tree чужой (протухла/подделана) → 2
    baseline = _head(repo)
    (repo / "app" / "x.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")
    head = _head(repo)
    lg._write_ledger(repo, head, {
        "passes": list(lg.DEPLOY_REQUIRED_PASSES), "tree": "0" * 40, "ts": "x",
    })
    assert lg.check_range(repo, baseline) == 2


def test_check_range_passes_incomplete(repo):
    # (в) tree совпал, но не все канонические проходы → 2
    baseline = _head(repo)
    (repo / "app" / "x.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")
    head, tree = _head(repo), _head_tree(repo)
    lg._write_ledger(repo, head, {"passes": ["simplify"], "tree": tree, "ts": "x"})
    assert lg.check_range(repo, baseline) == 2


def test_check_range_all_valid_zero(repo):
    # (г) все коммиты диапазона с валидными полными записями → 0 (интеграция с record_commit)
    baseline = _head(repo)
    _grow_valid_chain(repo)
    _git(repo, "add", "app/x.py")
    _git(repo, "commit", "-m", "reviewed change")
    lg.record_commit(repo)
    assert lg.check_range(repo, baseline) == 0


def test_check_range_exempt_noncode_covered(repo):
    # (д) exempt-noncode запись → 0
    baseline = _head(repo)
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "docs")
    lg.record_commit(repo)
    assert lg.check_range(repo, baseline) == 0


def test_check_range_skipped_covered_with_audit(repo, monkeypatch, capsys):
    # (д) skipped-запись → 0 + громкий stderr-аудит (reason виден)
    baseline = _head(repo)
    (repo / "app" / "x.py").write_text("x = 9\n")
    _git(repo, "add", "-A")
    monkeypatch.setenv("LADDER_SKIP", "1")
    monkeypatch.setenv("LADDER_SKIP_REASON", "hotfix")
    _git(repo, "commit", "-m", "skip commit")
    lg.record_commit(repo)
    assert lg.check_range(repo, baseline) == 0
    err = capsys.readouterr().err
    assert "hotfix" in err


def test_check_range_merge_commit_exempt_with_mark(repo, capsys):
    # (д) merge-коммит без записи → 0 + громкая пометка; обе стороны merge покрыты записями,
    # записанными напрямую (не через begin/mark — независимые нетронутые файлы на каждой ветке,
    # чтобы merge был бесконфликтным и единственной переменной теста был сам merge-коммит)
    baseline = _head(repo)
    _git(repo, "checkout", "-b", "feature")
    (repo / "app" / "y.py").write_text("y = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "feature change")
    feature_head, feature_tree = _head(repo), _head_tree(repo)
    lg._write_ledger(repo, feature_head,
                     {"passes": list(lg.DEPLOY_REQUIRED_PASSES), "tree": feature_tree, "ts": "x"})
    _git(repo, "checkout", "main")
    (repo / "app" / "z.py").write_text("z = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "main change")
    main_head, main_tree = _head(repo), _head_tree(repo)
    lg._write_ledger(repo, main_head,
                     {"passes": list(lg.DEPLOY_REQUIRED_PASSES), "tree": main_tree, "ts": "x"})
    _git(repo, "merge", "--no-ff", "feature", "-m", "merge")
    merge_sha = _head(repo)
    assert lg.check_range(repo, baseline) == 0
    err = capsys.readouterr().err
    assert merge_sha[:12] in err and "merge" in err.lower()


def test_check_range_epoch_grandfathers_pre_epoch_commits(repo, monkeypatch, capsys):
    # эпоха: monkeypatch LADDER_EPOCH_SHA = SHA первого (пред-эпохового) коммита диапазона →
    # он и всё до него exempt без записи; пост-эпоховый код-коммит без записи всё равно → 2
    baseline = _head(repo)
    (repo / "app" / "x.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "pre-epoch code change")
    epoch_sha = _head(repo)
    monkeypatch.setattr(lg, "LADDER_EPOCH_SHA", epoch_sha)
    (repo / "app" / "x.py").write_text("x = 3\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "post-epoch code change")
    post_epoch_sha = _head(repo)
    assert lg.check_range(repo, baseline) == 2
    err = capsys.readouterr().err
    assert post_epoch_sha[:12] in err
    assert epoch_sha[:12] not in err                     # grandfathered — не в списке непокрытых


def test_check_range_config_required_passes_ignored(repo):
    # (е) config-independence (R3): урезанный .codex-gate.yaml required_passes=[] НЕ ослабляет —
    # запись с неполными проходами всё равно блокирует
    baseline = _head(repo)
    (repo / ".codex-gate.yaml").write_text("ladder:\n  required_passes: []\n")
    (repo / "app" / "x.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")
    head, tree = _head(repo), _head_tree(repo)
    lg._write_ledger(repo, head, {"passes": ["simplify"], "tree": tree, "ts": "x"})
    assert lg.check_range(repo, baseline) == 2


def test_check_range_ladder_enabled_false_ignored(repo):
    # (ж) flag-independence (R1): ladder.enabled=false в config НЕ отключает деплой-проверку
    baseline = _head(repo)
    (repo / ".codex-gate.yaml").write_text("ladder:\n  enabled: false\n")
    (repo / "app" / "x.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "change")               # никакой ladder-записи
    assert lg.check_range(repo, baseline) == 2


def test_check_range_empty_range_zero(repo):
    # (з) baseline == HEAD → пустой диапазон → 0
    baseline = _head(repo)
    assert lg.check_range(repo, baseline) == 0


def test_cli_check_range(repo, monkeypatch):
    baseline = _head(repo)
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "docs cli")
    lg.record_commit(repo)
    monkeypatch.chdir(repo)
    assert lg.main(["check-range", baseline]) == 0


def test_compute_tree_immune_to_racy_same_size_edit(repo):
    # ревью Task 3: same-size правка в ту же секунду (типичный фикс /simplify) не должна
    # отдавать stale blob — tmp-индекс строится с нуля (полный re-hash), не копия реального
    (repo / "app" / "x.py").write_text("x = 1\n")
    t0 = lg.compute_tree(repo)
    (repo / "app" / "x.py").write_text("x = 2\n")   # same size, та же секунда
    assert lg.compute_tree(repo) != t0
