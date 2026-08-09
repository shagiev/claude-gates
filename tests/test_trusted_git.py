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
