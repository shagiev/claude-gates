"""Гейт обязан САМ убедиться, что ревьюеру есть что смотреть.

Иначе ревьюер получает пустоту, честно отвечает «дифф пустой / не могу прочитать» — и это
приходит с КОДОМ 0 и валидным `Verdict:`, то есть ревью, которого не было, неотличимо от
ревью без замечаний. За один день 2026-08-14 так вышло трижды.
"""
import subprocess

import pytest

import codex_review_gate as g


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    r = tmp_path / "proj"
    r.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"],
           "HOME": str(tmp_path)}
    def git(*a):
        subprocess.run(["git", *a], cwd=r, env=env, check=True, capture_output=True)
    git("init", "-q", "-b", "main", ".")
    (r / "a.py").write_text("x = 1\n")
    git("add", "-A")
    git("commit", "-qm", "init")
    monkeypatch.setattr(g, "REPO_ROOT", r)
    return r, git


def test_short_sha_base_is_refused(repo):
    """Ровно та опечатка, что случилась: сокращённый sha не разрешался в диапазон, и ревьюер
    вернул «Branch Diff (none)» с кодом 0."""
    r, _ = repo
    head_short = subprocess.run(["git", "rev-parse", "--short", "HEAD"], cwd=r,
                                capture_output=True, text=True).stdout.strip()
    why = g.review_input_empty(["--base", head_short[:7] + "zz", "--scope", "branch"], None)
    assert why and "не разрешается в коммит" in why


def test_empty_range_is_refused(repo):
    """`HEAD == base`: артефакт не закоммичен, показывать нечего."""
    r, _ = repo
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r,
                          capture_output=True, text=True).stdout.strip()
    why = g.review_input_empty(["--base", head, "--scope", "branch"], None)
    assert why and "ПУСТ" in why


def test_nonempty_range_passes(repo):
    r, git = repo
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r,
                          capture_output=True, text=True).stdout.strip()
    (r / "b.py").write_text("y = 2\n")
    git("add", "-A")
    git("commit", "-qm", "second")
    assert g.review_input_empty(["--base", head, "--scope", "branch"], None) is None


def test_base_together_with_working_tree_scope_is_refused(repo):
    """Движок при заданном `--base` возвращает `mode: branch` ДО того, как посмотрит на scope
    (`lib/git.mjs`), то есть рабочее дерево молча игнорируется. Гейт не должен «проверить
    что-нибудь» — он обязан отказать: пользователь просит одно, движок сделает другое.
    Именно эта комбинация трижды за день давала ревью пустоты с кодом 0."""
    r, _ = repo
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r,
                          capture_output=True, text=True).stdout.strip()
    (r / "a.py").write_text("x = 999\n")
    why = g.review_input_empty(["--base", head, "--scope", "working-tree"], None)
    assert why and "--base" in why and "ТОЛЬКО КОММИТЫ" in why


def test_working_tree_scope_counts_untracked_files(repo):
    """Незакоммиченный НОВЫЙ файл — главный сценарий working-tree (новый модуль, новый тест,
    ещё не закоммиченный дизайн). `git diff` его не показывает, поэтому проверять надо
    `status --untracked-files=all`, иначе гвард блокирует законную работу."""
    r, _ = repo
    assert g.review_input_empty(["--scope", "working-tree"], None)      # дерево чисто
    (r / "новый_модуль.py").write_text("y = 2\n")                       # ТОЛЬКО untracked
    assert g.review_input_empty(["--scope", "working-tree"], None) is None


def test_inline_scope_form_is_parsed(repo):
    """`--scope=working-tree` разбирался не везде: гвард уходил в дефолт `branch` и проверял
    диапазон, пока движок ревьюил рабочее дерево — исходный инцидент в обход гварда."""
    r, _ = repo
    assert g.review_input_empty(["--scope=working-tree"], None)          # чисто → отказ
    (r / "a.py").write_text("x = 5\n")
    assert g.review_input_empty(["--scope=working-tree"], None) is None


def test_single_dash_form_is_refused_rather_than_guessed(repo):
    """Парсер companion принимает и `-base`. Угадывать чужой парсер — денилист; отказываем."""
    r, _ = repo
    why = g.review_input_empty(["-base", "HEAD"], None)
    assert why and "неоднозначная форма" in why


def test_design_file_does_not_excuse_an_empty_range(repo, tmp_path):
    """`--design-file` — флаг ГЕЙТА, до движка он не доезжает: ревьюер всё равно получит
    ДИАПАЗОН. Ветка, которая при наличии файла возвращала None, пропускала ровно инцидент №2
    (незакоммиченный дизайн при HEAD == base) — тот, ради которого гвард и писался."""
    r, _ = repo
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r,
                          capture_output=True, text=True).stdout.strip()
    d = tmp_path / "дизайн.md"
    d.write_text("# дизайн\nтекст\n")
    why = g.review_input_empty(["--base", head, "--scope", "branch"], str(d))
    assert why and "ПУСТ" in why


def test_missing_or_empty_design_file_is_refused(repo, tmp_path):
    assert "не существует" in g.review_input_empty([], str(tmp_path / "нет.md"))
    empty = tmp_path / "пусто.md"
    empty.write_text("   \n")
    assert "пуст" in g.review_input_empty([], str(empty))
    real = tmp_path / "есть.md"
    real.write_text("# дизайн\nтекст\n")
    assert g.review_input_empty([], str(real)) is None


def test_cli_refuses_without_burning_a_round(repo, monkeypatch, capsys):
    """Отказ гейта не должен жечь бюджет: иначе три промаха подряд отказали бы в ПЕРВОМ же
    состоявшемся ревью."""
    r, _ = repo
    head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=r,
                          capture_output=True, text=True).stdout.strip()
    called = []
    monkeypatch.setattr(g, "_exec_companion", lambda *a, **k: called.append(a))
    assert g.main(["companion-review", "--base", head, "--scope", "branch", "фокус"]) == 2
    err = capsys.readouterr().err
    assert "ревью НЕ запускается" in err and "Раунд бюджета НЕ израсходован" in err
    assert not called, "движок ревью был запущен на пустом входе"
