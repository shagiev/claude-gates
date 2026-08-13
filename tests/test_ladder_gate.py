import json
import os
import shutil
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
    # Возвращает результат, а не глотает его: иначе тесту, которому нужен stdout, приходится
    # звать subprocess напрямую — в обход HOME=tmp, то есть с ~/.gitconfig разработчика.
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True,
                          env={"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
                               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
                               "PATH": os.environ["PATH"], "HOME": str(repo.parent)})


def _head(repo) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


def _head_tree(repo) -> str:
    return subprocess.run(["git", "rev-parse", "HEAD^{tree}"], cwd=repo,
                          capture_output=True, text=True, check=True).stdout.strip()


def _grow_valid_chain(repo, rel="app/x.py"):
    """Полная цепочка канонических проходов с правкой между звеньями.

    Проходы берутся из `DEPLOY_REQUIRED_PASSES`, а не перечисляются литералами: второй
    хелпер со своим списком разошёлся бы с тем, что гейт считает валидной цепочкой, ровно
    в том файле, который это и стережёт."""
    for name in lg.DEPLOY_REQUIRED_PASSES:
        lg.begin_pass(repo, name)
        (repo / rel).write_text(f"x = 2  # {name}\n")
        lg.mark_pass(repo, name)


def _tree_paths(repo, tree) -> list[str]:
    return _git(repo, "ls-tree", "-r", "--name-only", tree).stdout.split()


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
    assert rec == {"passes": ["exempt-noncode"], "tree": tree, "ts": rec["ts"],
                   "ladder_schema": lg.LADDER_SCHEMA}


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
    assert rec["passes"] == ["simplify", "code-review", "security"]
    assert rec["ladder_schema"] == lg.LADDER_SCHEMA
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


def test_range_skips_lists_only_skipped(repo, monkeypatch):
    # inframon-интерфейс (R1-F3): исторические LADDER_SKIP-коммиты диапазона — видимы
    baseline = _head(repo)
    (repo / "app" / "x.py").write_text("x = 2\n")
    _git(repo, "add", "-A")
    monkeypatch.setenv("LADDER_SKIP", "1")
    _git(repo, "commit", "-m", "skip commit")
    lg.record_commit(repo)
    skipped_sha = _head(repo)
    monkeypatch.delenv("LADDER_SKIP")
    (repo / "README.md").write_text("hi\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "docs")
    lg.record_commit(repo)                                # exempt-noncode — НЕ skip
    assert lg.range_skips(repo, baseline) == [skipped_sha]


# ═══ Третий проход security (спека docs/2026-07-25-security-ladder-pass-design.md) ═══

def test_sec1_full_three_pass_chain_precommit_and_record(repo):
    _grow_valid_chain(repo)
    _git(repo, "add", "app/x.py")
    assert lg.check_precommit(repo) == 0                       # SEC1: цепочка из трёх звеньев
    _git(repo, "commit", "-m", "three-pass change")
    lg.record_commit(repo)
    rec = lg.read_ledger(repo, _head(repo))
    assert rec["passes"] == ["simplify", "code-review", "security"]
    assert rec["ladder_schema"] == lg.LADDER_SCHEMA


def test_sec2_missing_security_blocks_precommit(repo, capsys):
    lg.begin_pass(repo, "simplify")
    (repo / "app" / "x.py").write_text("x = 2\n")
    lg.mark_pass(repo, "simplify")
    lg.begin_pass(repo, "code-review")
    (repo / "app" / "x.py").write_text("x = 2  # reviewed\n")
    lg.mark_pass(repo, "code-review")                          # security НЕ пройден
    _git(repo, "add", "app/x.py")
    capsys.readouterr()                                        # выбросить begin-баннеры: иначе
    # проверка ниже могла бы пройти по слову «security» из ЧУЖОГО вывода и перестать сторожить
    # третье звено (ревью 2026-07-26)
    assert lg.check_precommit(repo) == 2                       # SEC2
    err = capsys.readouterr().err
    assert "security" in err                                   # инструкция включает третий шаг
    assert lg._chain_instructions(repo) in err                 # именно блок-сообщение, не эхо


def test_sec3_begin_security_validates_chain_start(repo):
    lg.begin_pass(repo, "simplify")
    (repo / "app" / "x.py").write_text("x = 2\n")
    lg.mark_pass(repo, "simplify")
    lg.begin_pass(repo, "code-review")
    lg.mark_pass(repo, "code-review")
    (repo / "app" / "x.py").write_text("x = 3\n")              # ручная правка МЕЖДУ проходами
    with pytest.raises(lg.LadderError):                        # SEC3
        lg.begin_pass(repo, "security")


def test_sec4_security_mark_without_begin_and_replay(repo):
    with pytest.raises(lg.LadderError):                        # SEC4: mark без begin
        lg.mark_pass(repo, "security")
    lg.begin_pass(repo, "simplify"); lg.mark_pass(repo, "simplify")
    lg.begin_pass(repo, "code-review"); lg.mark_pass(repo, "code-review")
    lg.begin_pass(repo, "security"); lg.mark_pass(repo, "security")
    with pytest.raises(lg.LadderError):                        # анти-replay
        lg.mark_pass(repo, "security")


def test_sec17_break_second_link_blocks(repo):
    # все три маркера есть, но code-review.after != security.before (правка между 2-м и 3-м)
    _grow_valid_chain(repo)
    mp = repo / ".claude" / ".ladder-security"
    m = json.loads(mp.read_text())
    m["tree_before"] = "0" * 40                                # искусственный разрыв звена
    mp.write_text(json.dumps(m))
    _git(repo, "add", "app/x.py")
    assert lg.check_precommit(repo) == 2                       # SEC17


def test_sec18_stale_security_marker_blocks(repo):
    # цепочка связна, но код правили ПОСЛЕ mark security -> security.after != индекс
    _grow_valid_chain(repo)
    (repo / "app" / "x.py").write_text("x = 999  # правка ПОСЛЕ security\n")
    _git(repo, "add", "app/x.py")
    assert lg.check_precommit(repo) == 2                       # SEC18


def _commit_with_record(repo, record, msg):
    """Код-коммит + подложенная ledger-запись (tree подставляется настоящий).
    Содержимое уникально по msg: hash() рандомизирован по процессу (PYTHONHASHSEED), и при
    коллизии по модулю содержимое не менялось → пустой коммит → нестабильное падение."""
    (repo / "app" / "x.py").write_text(f"# {msg}\nx = 1\n")
    _git(repo, "add", "app/x.py")
    _git(repo, "commit", "-m", msg)
    head, tree = _head(repo), _head_tree(repo)
    rec = dict(record); rec["tree"] = tree
    lg._write_ledger(repo, head, rec)
    return head


def test_sec7_sec8_schema2_requires_canonical_set(repo):
    baseline = _head(repo)
    _commit_with_record(repo, {"passes": ["simplify", "code-review", "security"],
                               "ladder_schema": 2, "ts": "t"}, "schema2 full")
    assert lg.check_range(repo, baseline) == 0                 # SEC7
    _commit_with_record(repo, {"passes": ["simplify", "code-review"],
                               "ladder_schema": 2, "ts": "t"}, "schema2 partial")
    assert lg.check_range(repo, baseline) == 2                 # SEC8


def test_sec9_legacy_record_two_passes_covered(repo):
    # СЕРДЦЕ обратной совместимости: коммит до фичи (запись без ladder_schema) остаётся покрыт
    baseline = _head(repo)
    _commit_with_record(repo, {"passes": ["simplify", "code-review"], "ts": "t"}, "legacy full")
    assert lg.check_range(repo, baseline) == 0                 # SEC9


def test_sec10_legacy_record_one_pass_blocks(repo):
    baseline = _head(repo)
    _commit_with_record(repo, {"passes": ["simplify"], "ts": "t"}, "legacy partial")
    assert lg.check_range(repo, baseline) == 2                 # SEC10


def test_sec19_sec20_legacy_exempt_and_skipped_covered(repo):
    baseline = _head(repo)
    _commit_with_record(repo, {"passes": ["exempt-noncode"], "ts": "t"}, "legacy exempt")
    assert lg.check_range(repo, baseline) == 0                 # SEC19
    _commit_with_record(repo, {"skipped": True, "reason": "old", "ts": "t"}, "legacy skipped")
    assert lg.check_range(repo, baseline) == 0                 # SEC20


def test_sec11_config_cannot_weaken_canonical_set(repo):
    # урезанный required_passes в конфиге не ослабляет деплой (существующий инвариант ML-L7)
    (repo / ".codex-gate.yaml").write_text("ladder:\n  required_passes: [simplify]\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-m", "config")
    baseline = _head(repo)
    _commit_with_record(repo, {"passes": ["simplify"], "ladder_schema": 2, "ts": "t"}, "one pass")
    assert lg.check_range(repo, baseline) == 2                 # SEC11


def test_required_for_record_provenance():
    assert lg._required_for_record({"ladder_schema": 2}) == lg.DEPLOY_REQUIRED_PASSES
    assert lg._required_for_record({}) == lg.LEGACY_REQUIRED_PASSES          # легаси
    for junk in ({"ladder_schema": True}, {"ladder_schema": "2"}, {"ladder_schema": 1}):
        assert lg._required_for_record(junk) == lg.LEGACY_REQUIRED_PASSES, junk  # fail-safe


# ═══ Текст протокола: реестр проходов и баннер begin (ревью B2, 2026-07-26) ═══

def test_pass_runner_covers_all_passes():
    """Реестр обязан покрывать канонические проходы: расхождение раньше печатало заглушку
    оператору, а теперь роняет импорт — тест ловит его до релиза, а не у пользователя."""
    assert set(lg._PASS_RUNNER) == set(lg.DEPLOY_REQUIRED_PASSES)
    for p in lg.DEPLOY_REQUIRED_PASSES:
        assert lg._PASS_RUNNER[p].strip(), p


def test_chain_instructions_mention_every_pass_and_runner(repo):
    """Мутация, выкинувшая звено из блок-сообщения, раньше оставляла тесты зелёными."""
    shim = repo / ".githooks" / "gates-run"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text("#!/usr/bin/env bash\n")
    msg = lg._chain_instructions(repo)
    for p in lg.DEPLOY_REQUIRED_PASSES:
        assert f"begin {p}" in msg, p
        assert f"mark {p}" in msg, p
        assert lg._PASS_RUNNER[p] in msg, p
    assert lg._RUNNER_RULE in msg
    # команды печатаются в исполнимой форме (через шим), иначе copy-paste даёт command not found
    assert "bash .githooks/gates-run ladder_gate.py begin" in msg


def test_chain_instructions_do_not_invent_shim_path(repo):
    """Тот же инвариант, что у баннера begin: сообщение блокировки копируют чаще всего,
    поэтому неисполнимый путь в нём вреднее всего (ревью 2026-07-26)."""
    assert not (repo / ".githooks" / "gates-run").exists()
    msg = lg._chain_instructions(repo)
    assert ".githooks/gates-run" not in msg
    assert lg._RUN_UNKNOWN in msg


def test_begin_prints_runner_banner_in_runnable_form(repo, capsys):
    """Новый code path: баннер begin. Содержание, исполнимость команды и чистый stdout —
    в одном тесте: фикстура repo поднимает git-репо, дробить её по одному ассерту дорого."""
    shim = repo / ".githooks" / "gates-run"
    shim.parent.mkdir(parents=True, exist_ok=True)
    shim.write_text("#!/usr/bin/env bash\n")
    lg.begin_pass(repo, "simplify")
    captured = capsys.readouterr()
    assert "begin simplify" in captured.err
    assert lg._PASS_RUNNER["simplify"] in captured.err
    assert "bash .githooks/gates-run ladder_gate.py mark simplify" in captured.err
    assert captured.out == ""          # на пустом stdout держится машиночитаемость CLI


def test_begin_banner_does_not_invent_shim_path(repo, capsys):
    """Проект мог поставить хуки мимо .githooks/ (gates-init это поддерживает). Печатать туда
    путь — выдать строку, дающую command not found, то есть ровно тот провал, ради которого
    подсказка и существует."""
    assert not (repo / ".githooks" / "gates-run").exists()
    lg.begin_pass(repo, "simplify")
    err = capsys.readouterr().err
    assert ".githooks/gates-run" not in err
    assert lg._RUN_UNKNOWN in err and "mark simplify" in err


def test_repo_root_used_instead_of_cwd(repo, monkeypatch):
    """Ревью 2026-07-26: все точки входа CLI брали Path.cwd(), поэтому begin/mark из
    подкаталога писали бухгалтерию в <subdir>/.claude/, а git запускает pre-commit из корня
    и там её не находил — коммит блокировался «цепочка не подтверждена», хотя проходы были
    выполнены, и оператор шёл за LADDER_SKIP."""
    sub = repo / "services" / "api"
    sub.mkdir(parents=True)
    monkeypatch.chdir(sub)
    assert lg._repo_root() == repo.resolve()
    assert lg.main(["begin", "simplify"]) == 0
    assert (repo / ".claude" / ".ladder-pending-simplify").is_file()      # в КОРНЕ
    assert not (sub / ".claude").exists()                                 # не в подкаталоге


def test_repo_root_falls_back_to_cwd_outside_git(tmp_path, monkeypatch):
    """Вне git-репо поведение прежнее — гейт не обязан падать там, где его просто нет."""
    monkeypatch.chdir(tmp_path)
    assert lg._repo_root() == Path.cwd()


# --- tree-хэш на нерегулярных записях (BUG-0.9.0-symlink-tree-hash + находки дизайн-ревью) ---
# Общий корень всех случаев ниже: compute_tree считал, что запись дерева — это регулярный
# файл, хэшируемый по пути. Каждое отступление (симлинк, gitlink, сырые байты, запись без
# файла на диске) давало либо падение движка, либо молча неверный tree-хэш. Проверка везде
# ЭТАЛОННАЯ — равенство настоящему дереву git'а, а не «не упало»: неверный хэш означает, что
# маркер лесенки привязан к дереву, которого не существует.

def _mkrepo(tmp_path, name):
    r = tmp_path / name
    r.mkdir(parents=True)
    _git(r, "init", "-q", "-b", "main", ".")
    r.joinpath(".gitignore").write_text(".claude/\n")     # как в реальных репо: бухгалтерия вне git
    return r


def _commit_all(r, msg="init"):
    _git(r, "add", "-A")
    _git(r, "commit", "-qm", msg)


@pytest.fixture()
def symlink_repo(tmp_path):
    """Три вида tracked-симлинков. `linkdir` — на директорию: ровно он валил движок
    (`hash-object` по пути разыменовывает ссылку, а на директории git отвечает
    `fatal: Unable to add (null) to database`). `linkfile` — на файл: он НЕ падал, а молча
    получал блоб СОДЕРЖИМОГО цели при режиме 120000, то есть tree-хэш врал. `broken` — на
    несуществующий путь."""
    r = _mkrepo(tmp_path, "sl")
    (r / "real" / "sub").mkdir(parents=True)
    (r / "real" / "sub" / "f").write_text("x\n")
    os.symlink("real/sub", r / "linkdir")
    os.symlink("real/sub/f", r / "linkfile")
    os.symlink("nowhere/missing", r / "broken")
    _commit_all(r)
    return r


def test_compute_tree_matches_git_tree_with_symlinks(symlink_repo):
    assert lg.compute_tree(symlink_repo) == _head_tree(symlink_repo)


def test_compute_tree_symlink_to_file_hashes_link_text_not_target(tmp_path):
    """Тихая половина бага: симлинк на ФАЙЛ не роняет hash-object — он отдаёт блоб цели.
    Падения нет, хэш неверен. Отдельным тестом, чтобы регресс ловился и без симлинка
    на директорию."""
    r = _mkrepo(tmp_path, "sf")
    (r / "real").mkdir()
    (r / "real" / "f").write_text("target-content\n")
    os.symlink("real/f", r / "link")
    _commit_all(r)
    assert lg.compute_tree(r) == _head_tree(r)


def test_compute_tree_handles_non_utf8_symlink_target(tmp_path):
    """Цель симлинка — любые байты кроме NUL. `os.readlink` отдаёт str с surrogate-escape, и
    текстовый режим subprocess падал бы UnicodeEncodeError — мимо диагностики, потому что это
    не TrustedGitError (находка дизайн-ревью, раунд 1)."""
    r = _mkrepo(tmp_path, "nonutf")
    (r / "plain.txt").write_text("p\n")
    os.symlink(b"real/\xff\xfename", os.fsencode(r / "link"))
    _commit_all(r)
    assert lg.compute_tree(r) == _head_tree(r)


def test_ladder_runs_in_repo_with_dir_symlink(symlink_repo, monkeypatch):
    """Маршрут оператора целиком в затронутом репо: до фикса begin падал TrustedGitError."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    lg.begin_pass(symlink_repo, "simplify")
    (symlink_repo / "real" / "sub" / "f").write_text("y\n")
    lg.mark_pass(symlink_repo, "simplify")
    m = lg.read_marker(symlink_repo, "simplify")
    assert m["tree_before"] != m["tree_after"]


def test_symlink_retarget_changes_tree(symlink_repo):
    """Хэш обязан быть чувствителен к перенаправлению ссылки: подмена цели — правка кода."""
    t0 = lg.compute_tree(symlink_repo)
    (symlink_repo / "real" / "other").mkdir()
    (symlink_repo / "linkdir").unlink()
    os.symlink("real/other", symlink_repo / "linkdir")
    assert lg.compute_tree(symlink_repo) != t0


@pytest.fixture()
def submodule_repo(tmp_path):
    sub = _mkrepo(tmp_path, "sub")
    (sub / "a.txt").write_text("a\n")
    _commit_all(sub, "s1")
    top = _mkrepo(tmp_path, "top")
    (top / "app").mkdir()
    (top / "app" / "x.py").write_text("x = 1\n")
    _commit_all(top)
    _git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "sub")
    _git(top, "commit", "-qm", "add submodule")
    return top, sub


def test_compute_tree_matches_git_tree_with_submodule(submodule_repo):
    top, _ = submodule_repo
    assert lg.compute_tree(top) == _head_tree(top)


def test_ladder_completes_in_repo_with_submodule(submodule_repo, monkeypatch):
    """Находка дизайн-ревью (critical), воспроизведена: gitlink выпадал из compute_tree, а в
    index_tree он есть всегда — значит честная цепочка НИКОГДА не могла дать 0, и репозиторий
    с подмодулем жил на LADDER_SKIP. Сквозной тест: полная цепочка обязана пропустить коммит."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    top, _ = submodule_repo
    _grow_valid_chain(top)
    _git(top, "add", "app/x.py")
    assert lg.check_precommit(top) == 0


def test_submodule_head_move_changes_tree(submodule_repo, monkeypatch):
    """Бамп указателя подмодуля обязан ломать цепочку, а не проскакивать под старым маркером."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    top, sub = submodule_repo
    _grow_valid_chain(top)
    _git(top, "add", "app/x.py")
    assert lg.check_precommit(top) == 0

    (sub / "b.txt").write_text("b\n")
    _commit_all(sub, "s2")
    new_head = _head(sub)
    _git(top / "sub", "-c", "protocol.file.allow=always", "fetch", "-q", "origin")
    _git(top / "sub", "checkout", "-q", new_head)
    # Незастейдженный сдвиг в коммит не попадает: check_precommit сверяет маркер с ИНДЕКСОМ.
    assert lg.check_precommit(top) == 0
    _git(top, "add", "sub")                     # а вот теперь он ЧАСТЬ коммита
    assert lg.check_precommit(top) == 2


def test_uninitialized_submodule_completes_ladder(submodule_repo, monkeypatch):
    """Неинициализированный подмодуль: `git add -A` его не трогает, дерево обязано совпасть
    с индексом — иначе снова неисполнимая лесенка."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    top, _ = submodule_repo
    _git(top, "submodule", "deinit", "-f", "-q", "sub")
    assert lg.compute_tree(top) == _head_tree(top)
    _grow_valid_chain(top)
    _git(top, "add", "app/x.py")
    assert lg.check_precommit(top) == 0


def test_removed_submodule_directory_drops_entry(submodule_repo):
    """Удалённый каталог подмодуля выпадает из дерева, как выпадает удалённый файл."""
    top, _ = submodule_repo
    shutil.rmtree(top / "sub")
    assert "sub" not in _tree_paths(top, lg.compute_tree(top))


def test_ladder_completes_in_sparse_checkout(tmp_path, monkeypatch):
    """Находка дизайн-ревью (раунд 2), воспроизведена и шире заявленного: SKIP_WORKTREE-запись
    отсутствует на диске, но остаётся в индексе. Трактовка «нет файла = удалён» разводила
    compute_tree и index_tree в ЛЮБОМ sparse-checkout репозитории (на обычных файлах, не
    только на подмодулях) — лесенка была неисполнима."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    r = _mkrepo(tmp_path, "sparse")
    for d in ("keep", "excluded"):
        (r / d).mkdir()
        (r / d / "f.py").write_text(f"# {d}\n")
    _commit_all(r)
    _git(r, "sparse-checkout", "init", "--cone")
    _git(r, "sparse-checkout", "set", "keep")
    assert not (r / "excluded").exists()                  # запись есть в индексе, файла нет
    assert lg.compute_tree(r) == lg.index_tree(r)

    _grow_valid_chain(r, "keep/f.py")
    _git(r, "add", "keep/f.py")
    assert lg.check_precommit(r) == 0


def test_sparse_plus_assume_unchanged_completes_ladder(tmp_path, monkeypatch):
    """`skip-worktree` и `assume-unchanged` — независимые биты, и `ls-files -v` кодирует
    второй РЕГИСТРОМ буквы первого: у записи с обоими флаг `s`, а не `S`. Наивное сравнение
    `tag == "S"` вернуло бы такую запись в ветку «удалён» и воспроизвело неисполнимую
    лесенку (находка дизайн-ревью, раунд 3)."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    r = _mkrepo(tmp_path, "combined")
    for d in ("keep", "excluded"):
        (r / d).mkdir()
        (r / d / "f.py").write_text(f"# {d}\n")
    _commit_all(r)
    _git(r, "sparse-checkout", "init", "--cone")
    _git(r, "sparse-checkout", "set", "keep")
    _git(r, "update-index", "--assume-unchanged", "excluded/f.py")
    flags = {e.split()[0] for e in _git(r, "ls-files", "-v", "-s").stdout.splitlines() if e}
    assert "s" in flags, f"ожидался комбинированный флаг 's', получены {flags}"
    assert lg.compute_tree(r) == lg.index_tree(r)

    _grow_valid_chain(r, "keep/f.py")
    _git(r, "add", "keep/f.py")
    assert lg.check_precommit(r) == 0


def test_sparse_checkout_still_tracks_in_cone_edits(tmp_path):
    """Sparse checkout не должен стать дырой: правка ВНУТРИ конуса меняет tree-хэш."""
    r = _mkrepo(tmp_path, "sparse2")
    for d in ("keep", "excluded"):
        (r / d).mkdir()
        (r / d / "f.py").write_text(f"# {d}\n")
    _commit_all(r)
    _git(r, "sparse-checkout", "init", "--cone")
    _git(r, "sparse-checkout", "set", "keep")
    t0 = lg.compute_tree(r)
    (r / "keep" / "f.py").write_text("# changed\n")
    assert lg.compute_tree(r) != t0


def test_deleted_file_still_changes_tree(tmp_path):
    """Настоящее удаление обязано менять дерево — его нельзя спутать со skip-worktree."""
    r = _mkrepo(tmp_path, "del")
    (r / "a.py").write_text("a\n")
    (r / "b.py").write_text("b\n")
    _commit_all(r)
    t0 = lg.compute_tree(r)
    (r / "b.py").unlink()
    tree = lg.compute_tree(r)
    assert tree != t0
    assert "b.py" not in _tree_paths(r, tree)


def test_assume_unchanged_does_not_hide_edit(tmp_path):
    """`assume-unchanged` прячет правку от самого git'а; гейт целостности обязан её видеть."""
    r = _mkrepo(tmp_path, "assume")
    (r / "a.py").write_text("a\n")
    _commit_all(r)
    _git(r, "update-index", "--assume-unchanged", "a.py")
    t0 = lg.compute_tree(r)
    (r / "a.py").write_text("MALICIOUS\n")
    assert lg.compute_tree(r) != t0


def test_unmerged_entry_collapses_to_worktree_content(tmp_path):
    """Конфликтный merge даёт stage 1/2/3 и НИ ОДНОЙ stage-0 записи. Путь не должен потеряться:
    `git add -A` застейджил бы содержимое рабочего дерева одной stage-0 записью."""
    r = _mkrepo(tmp_path, "merge")
    (r / "c.txt").write_text("base\n")
    _commit_all(r, "base")
    _git(r, "checkout", "-q", "-b", "other")
    (r / "c.txt").write_text("other\n")
    _commit_all(r, "other")
    _git(r, "checkout", "-q", "main")
    (r / "c.txt").write_text("mine\n")
    _commit_all(r, "mine")
    subprocess.run(["git", "merge", "other"], cwd=r, capture_output=True)   # конфликт
    stages = _git(r, "ls-files", "-s", "c.txt").stdout.splitlines()
    assert len(stages) == 3, f"ожидался конфликт со stage 1/2/3, получено {stages}"

    tree = lg.compute_tree(r)
    entries = _git(r, "ls-tree", "-r", tree).stdout
    assert entries.count("\tc.txt") == 1                  # ровно одна запись, не три
    on_disk = _git(r, "hash-object", "--no-filters", "--", "c.txt").stdout.strip()
    assert on_disk in entries                             # и это содержимое С ДИСКА


# --- отказ движка ≠ непройденная лесенка ---

def _break_engine(monkeypatch, exc):
    def boom(root):
        raise exc
    monkeypatch.setattr(lg, "compute_tree", boom)


def test_precommit_reports_broken_engine_not_missing_passes(repo, monkeypatch, capsys):
    """Инцидент 0.9.0 прожил двое суток именно из-за этого: отказ движка печатался как
    штатное «цепочка не подтверждена», а подсказка в конце вела прямо к LADDER_SKIP —
    инфраструктурный отказ читался как собственная забывчивость оператора."""
    (repo / "app" / "x.py").write_text("x = 42\n")
    subprocess.run(["git", "add", "app/x.py"], cwd=repo, check=True)
    _break_engine(monkeypatch, lg.TrustedGitError(
        "не посчитать хэш симлинка 'linkdir' (git rc=128): "
        "fatal: Unable to add (null) to database — tree-хэш не посчитать"))
    rc = lg.check_precommit(repo)
    err = capsys.readouterr().err
    assert rc == 2                                        # коммит по-прежнему блокирован
    assert "ДВИЖОК ГЕЙТА СЛОМАН" in err
    assert "Unable to add (null)" in err                  # stderr git'а доехал до оператора
    assert "цепочка" not in err                           # и это НЕ штатное сообщение


def test_precommit_reports_broken_engine_for_any_exception(repo, monkeypatch, capsys):
    """Перечислять формы отказа бессмысленно: их нашлось четыре за один день, а не-UTF-8 имя
    файла на APFS даже не воспроизводится. Громким обязано быть ЛЮБОЕ исключение."""
    (repo / "app" / "x.py").write_text("x = 42\n")
    subprocess.run(["git", "add", "app/x.py"], cwd=repo, check=True)
    _break_engine(monkeypatch, UnicodeEncodeError("utf-8", "x", 0, 1, "surrogates"))
    rc = lg.check_precommit(repo)
    err = capsys.readouterr().err
    assert rc == 2
    assert "ДВИЖОК ГЕЙТА СЛОМАН" in err
    assert "Traceback" in err                             # трейсбек не проглочен, а озаглавлен


def test_main_reports_broken_engine_exit_3(repo, monkeypatch, capsys):
    """begin/mark на сломанном движке: код 3 (отличим и от 0, и от 2 = «гейт блокирует»)."""
    _break_engine(monkeypatch, lg.TrustedGitError("синтетический отказ"))
    monkeypatch.setattr(lg, "_repo_root", lambda: repo)
    assert lg.main(["begin", "simplify"]) == 3
    assert "ДВИЖОК ГЕЙТА СЛОМАН" in capsys.readouterr().err


def test_main_still_reports_ladder_errors_plainly(repo, monkeypatch, capsys):
    """Нарушение протокола — не отказ движка: оно обязано остаться обычным сообщением и 2."""
    monkeypatch.setattr(lg, "_repo_root", lambda: repo)
    assert lg.main(["mark", "simplify"]) == 2             # mark без begin
    err = capsys.readouterr().err
    assert "ДВИЖОК ГЕЙТА СЛОМАН" not in err
    assert "без begin" in err


def test_tree_hash_error_carries_git_stderr(repo, monkeypatch):
    """`не посчитать хэш X` без stderr git'а недиагностируем — причину инцидента пришлось
    воспроизводить руками."""
    real = lg._git_mutate

    def failing(root, env, *args, stdin_bytes=None):
        if args and args[0] == "hash-object":
            return subprocess.CompletedProcess(args, 128, "", "fatal: синтетический отказ\n")
        return real(root, env, *args, stdin_bytes=stdin_bytes)

    monkeypatch.setattr(lg, "_git_mutate", failing)
    with pytest.raises(lg.TrustedGitError, match="синтетический отказ"):
        lg.compute_tree(repo)


# --- инвариант как исполняемое свойство ---
# Заявленный инвариант: compute_tree == дерево, которое дал бы `git add -A && git write-tree`.
# Пока он жил только в прозе докстринга, сравнения шли против _head_tree на ЧИСТОМ репо, где
# он тождествен, — то есть проверялось свойство слабее заявленного. Здесь он проверяется в том
# единственном состоянии, в котором compute_tree реально зовут из begin/mark: на ГРЯЗНОМ дереве.

def _add_all_tree(repo) -> str:
    """Эталон: что реально застейджил бы `git add -A`, во ВРЕМЕННОМ индексе (реальный цел)."""
    idx = repo.parent / f".idx-inv-{repo.name}"
    idx.unlink(missing_ok=True)
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "PATH": os.environ["PATH"], "HOME": str(repo.parent),
           "GIT_INDEX_FILE": str(idx)}
    def g(*a):
        return subprocess.run(["git", *a], cwd=repo, env=env, capture_output=True, text=True)
    g("read-tree", "HEAD")
    g("add", "-A")
    tree = g("write-tree").stdout.strip()
    idx.unlink(missing_ok=True)
    return tree


def test_compute_tree_equals_git_add_all_on_dirty_symlink_tree(symlink_repo):
    r = symlink_repo
    (r / "untracked.py").write_text("u = 1\n")            # новый неотслеживаемый файл
    (r / "real" / "sub" / "f").write_text("edited\n")     # правка отслеживаемого
    (r / "real" / "other").mkdir()
    (r / "linkfile").unlink()
    os.symlink("real/other", r / "linkfile")              # ссылка перенацелена на КАТАЛОГ
    (r / "broken").unlink()                               # симлинк удалён
    assert lg.compute_tree(r) == _add_all_tree(r)


def test_compute_tree_equals_git_add_all_on_dirty_submodule_tree(submodule_repo):
    top, _ = submodule_repo
    (top / "app" / "new.py").write_text("n = 1\n")
    (top / "app" / "x.py").write_text("x = 99\n")
    assert lg.compute_tree(top) == _add_all_tree(top)


def test_untracked_nested_git_repo_becomes_gitlink(tmp_path):
    """Вендоренный клон приходит из `ls-files -o` ОДНИМ путём со слэшем, и вид записи у него
    определяет ДИСК (каталог с .git → gitlink), а не индекс, где его нет. Пока вид брался из
    индекса, каталог молча выпадал из дерева со всем поддеревом: правка внутри него не меняла
    tree-хэш вовсе — та же тихая слепота, что у симлинка на файл."""
    r = _mkrepo(tmp_path, "nested")
    (r / "app").mkdir()
    (r / "app" / "x.py").write_text("x = 1\n")
    _commit_all(r)
    vendor = r / "vendor"
    vendor.mkdir()
    _git(vendor, "init", "-q", "-b", "main", ".")
    (vendor / "lib.py").write_text("v = 1\n")
    _git(vendor, "add", "-A")
    _git(vendor, "commit", "-qm", "v1")

    assert lg.compute_tree(r) == _add_all_tree(r)          # инвариант держится
    t0 = lg.compute_tree(r)
    (vendor / "lib.py").write_text("v = 999\n")           # правка ВНУТРИ вложенного репо
    _git(vendor, "add", "-A")
    _git(vendor, "commit", "-qm", "v2")
    assert lg.compute_tree(r) != t0                        # и она видна


def test_gate_refusal_is_not_engine_breakage_and_offers_no_bypass(repo, monkeypatch, capsys):
    """Отказ доверия («похоже на подмену git») — не поломка движка. До разведения он печатался
    тем же баннером и с готовым рецептом LADDER_SKIP, то есть сигнал ОБНАРУЖЕННОЙ подмены
    приходил вместе с инструкцией, как её обойти."""
    def refuse():
        raise lg.GateRefusal("git выдал корень /other, не содержащий текущий каталог — "
                             "похоже на подмену git; решения лесенки не принимаются")
    monkeypatch.setattr(lg, "_repo_root", refuse)
    assert lg.main(["begin", "simplify"]) == 3
    err = capsys.readouterr().err
    assert "ГЕЙТ ОТКАЗАЛСЯ РЕШАТЬ" in err
    assert "ДВИЖОК ГЕЙТА СЛОМАН" not in err
    assert "LADDER_SKIP" not in err              # рецепта обхода тут быть не должно
    assert "Обходить НЕЛЬЗЯ" in err


def test_engine_breakage_still_offers_audited_bypass(repo, monkeypatch, capsys):
    """Обратная сторона: настоящая поломка обход предлагать обязана — иначе оператор заперт."""
    _break_engine(monkeypatch, lg.TrustedGitError("не посчитать хэш — tree-хэш не посчитать"))
    monkeypatch.setattr(lg, "_repo_root", lambda: repo)
    assert lg.main(["begin", "simplify"]) == 3
    err = capsys.readouterr().err
    assert "ДВИЖОК ГЕЙТА СЛОМАН" in err
    assert "LADDER_SKIP" in err


# --- находки код-ревью 2026-08-13 ---

def test_skip_worktree_cannot_launder_staged_bytes(tmp_path):
    """Канал подмены через skip-worktree (находка код-ревью, high).

    Бит skip-worktree ставит ПРОВЕРЯЕМАЯ сторона. Атака: застейджить злое, вернуть на диск
    безобидное, поставить `--skip-worktree`. Если дереву брать oid из индекса безусловно, то
    маркеры лесенки хэшируют злой блоб, ревьюер читает добрый файл, а pre-commit сходится —
    два разных рабочих дерева получают один хэш. Индексу верим ТОЛЬКО когда файла нет."""
    r = _mkrepo(tmp_path, "tamper")
    (r / "app.py").write_text("safe = 1\n")
    _commit_all(r)

    (r / "app.py").write_text("MALICIOUS = 'payload'\n")
    _git(r, "add", "app.py")
    evil = _git(r, "rev-parse", ":app.py").stdout.strip()
    (r / "app.py").write_text("safe = 1\n")               # ревьюер увидит это
    good = _git(r, "hash-object", "--no-filters", "--", "app.py").stdout.strip()
    _git(r, "update-index", "--skip-worktree", "app.py")
    assert evil != good

    entries = _git(r, "ls-tree", "-r", lg.compute_tree(r)).stdout
    assert good in entries, "tree-хэш обязан отражать ДИСК, а не застейдженные байты"
    assert evil not in entries
    # и, как следствие, честная цепочка на таком дереве не сойдётся с индексом — fail-closed
    assert lg.compute_tree(r) != lg.index_tree(r)


def test_file_replaced_by_empty_directory_matches_git(tmp_path):
    """Смена типа файл→каталог: раньше фабриковался gitlink `160000` поверх блоб-oid'а."""
    r = _mkrepo(tmp_path, "f2dir")
    (r / "thing").write_text("i am a file\n")
    (r / "keep.py").write_text("k = 1\n")
    _commit_all(r)
    (r / "thing").unlink()
    (r / "thing").mkdir()
    assert lg.compute_tree(r) == _add_all_tree(r)
    assert "thing" not in _tree_paths(r, lg.compute_tree(r))


def test_file_replaced_by_directory_with_contents_matches_git(tmp_path):
    """Тот же случай, но каталог НЕ пуст: `update-index` падал
    `appears as both a file and as a directory`, то есть лесенка была неисполнима."""
    r = _mkrepo(tmp_path, "f2dir2")
    (r / "thing").write_text("file\n")
    _commit_all(r)
    (r / "thing").unlink()
    (r / "thing").mkdir()
    (r / "thing" / "inner.py").write_text("inner = 1\n")
    assert lg.compute_tree(r) == _add_all_tree(r)
    assert "thing/inner.py" in _tree_paths(r, lg.compute_tree(r))


def test_skip_worktree_absent_file_cannot_launder_staged_bytes(tmp_path):
    """Форма 2 того же канала (код-ревью, раунд 2): застейджить злое, skip-worktree, удалить
    файл — ревьюер не видит НИЧЕГО, а в коммит уезжает злой блоб."""
    r = _mkrepo(tmp_path, "launder2")
    (r / "app.py").write_text("safe = 1\n")
    _commit_all(r)

    (r / "app.py").write_text("MALICIOUS = 'payload'\n")
    _git(r, "add", "app.py")
    _git(r, "update-index", "--skip-worktree", "app.py")
    (r / "app.py").unlink()

    with pytest.raises(lg.TrustedGitError, match="skip-worktree"):
        lg.compute_tree(r)


def test_skip_worktree_unchanged_from_head_is_accepted(tmp_path):
    """Обратная сторона: законный sparse-checkout обязан продолжать работать — там
    невидимая запись совпадает с HEAD, ревьюить в ней нечего."""
    r = _mkrepo(tmp_path, "legit_sparse")
    for d in ("keep", "excluded"):
        (r / d).mkdir()
        (r / d / "f.py").write_text(f"# {d}\n")
    _commit_all(r)
    _git(r, "sparse-checkout", "init", "--cone")
    _git(r, "sparse-checkout", "set", "keep")
    assert lg.compute_tree(r) == lg.index_tree(r)


def test_deinitialized_submodule_cannot_launder_staged_pointer(tmp_path):
    """Форма 3 (код-ревью, раунд 3): переключить подмодуль на злой коммит, застейджить
    указатель, деинициализировать — содержимого на диске нет, ревьюеру нечего смотреть."""
    sub = _mkrepo(tmp_path, "sub")
    (sub / "a.txt").write_text("benign\n")
    _commit_all(sub, "good")
    top = _mkrepo(tmp_path, "top")
    (top / "app.py").write_text("x = 1\n")
    _commit_all(top)
    _git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor")
    _git(top, "commit", "-qm", "add submodule")

    (sub / "a.txt").write_text("MALICIOUS\n")
    _commit_all(sub, "evil")
    evil = _head(sub)
    _git(top / "vendor", "-c", "protocol.file.allow=always", "fetch", "-q", "origin")
    _git(top / "vendor", "checkout", "-q", evil)
    _git(top, "add", "vendor")                       # злой указатель в индексе
    _git(top, "submodule", "deinit", "-f", "-q", "vendor")   # и содержимое спрятано

    with pytest.raises(lg.TrustedGitError, match="разошёлся с HEAD"):
        lg.compute_tree(top)


def test_skip_worktree_on_brand_new_file_cannot_launder(tmp_path):
    """Форма, которой ревью НЕ называло — доказательство, что закрыт класс, а не экземпляры:
    файла не было в HEAD вовсе, сравнивать не с чем, значит проходить он не должен."""
    r = _mkrepo(tmp_path, "sw_new")
    (r / "keep.py").write_text("k = 1\n")
    _commit_all(r)
    (r / "secret.py").write_text("BACKDOOR = 1\n")
    _git(r, "add", "secret.py")
    _git(r, "update-index", "--skip-worktree", "secret.py")
    (r / "secret.py").unlink()

    with pytest.raises(lg.TrustedGitError, match="разошёлся с HEAD"):
        lg.compute_tree(r)


def test_deinitialized_submodule_with_untouched_pointer_is_accepted(tmp_path):
    """Обратная сторона: законный deinit (указатель не трогали) обязан работать."""
    sub = _mkrepo(tmp_path, "sub2")
    (sub / "a.txt").write_text("a\n")
    _commit_all(sub, "s1")
    top = _mkrepo(tmp_path, "top2")
    (top / "app.py").write_text("x = 1\n")
    _commit_all(top)
    _git(top, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(sub), "vendor")
    _git(top, "commit", "-qm", "add submodule")
    _git(top, "submodule", "deinit", "-f", "-q", "vendor")
    assert lg.compute_tree(top) == lg.index_tree(top)


def test_moving_head_cannot_bless_invisible_content(tmp_path, monkeypatch):
    """Находка security-ревью: базис правила «невидимое не менялось» — HEAD, а HEAD это
    обычная ссылка, которую проверяемая сторона переставляет `git symbolic-ref` НЕ трогая ни
    индекс, ни рабочее дерево. Схема: спрятать бэкдор в индекс, перевести HEAD на старую ветку,
    где он «не менялся», провести всю лесенку, вернуть HEAD и закоммитить. Маркеры честные,
    ledger полный — а содержимое не видел никто. Поэтому исключение перепроверяется на
    pre-commit против того HEAD, на который коммит реально ложится."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "atk")
    r = _mkrepo(tmp_path, "headatk")
    (r / "app.py").write_text("v1\n")
    _commit_all(r, "base")
    _git(r, "checkout", "-q", "-b", "legacy")
    (r / "auth.py").write_text("BACKDOOR = True\n")
    _commit_all(r, "legacy")
    backdoor = _git(r, "rev-parse", "legacy:auth.py").stdout.strip()
    _git(r, "checkout", "-q", "main")

    (r / "app.py").write_text("v2\n")                    # видимая безобидная правка
    _git(r, "add", "app.py")
    _git(r, "update-index", "--add", "--cacheinfo", f"100644,{backdoor},auth.py")
    _git(r, "update-index", "--skip-worktree", "auth.py")
    assert not (r / "auth.py").exists()                   # ревьюер этого не увидит

    with pytest.raises(lg.TrustedGitError, match="разошёлся с HEAD"):
        lg.compute_tree(r)                                # с честным HEAD — отказ

    _git(r, "symbolic-ref", "HEAD", "refs/heads/legacy")  # подменяем базис
    for name in lg.DEPLOY_REQUIRED_PASSES:
        lg.begin_pass(r, name)
        lg.mark_pass(r, name)
    _git(r, "symbolic-ref", "HEAD", "refs/heads/main")    # и возвращаем

    assert lg.check_precommit(r) == 2                     # цепочка честна, но коммит блокирован


def test_precommit_unseen_recheck_leaves_normal_flow_alone(repo, monkeypatch):
    """Обратная сторона: перепроверка не должна трогать обычный ход дел. Незастейдженная
    правка в дереве законна, коммита не касается и блокировать его не обязана."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    _grow_valid_chain(repo)
    _git(repo, "add", "app/x.py")
    (repo / "app" / "x.py").write_text("правка ПОСЛЕ mark, не застейджена\n")
    assert lg.check_precommit(repo) == 0
