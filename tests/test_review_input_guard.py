"""Гейт обязан САМ убедиться, что ревьюеру есть что смотреть.

Иначе ревьюер получает пустоту, честно отвечает «дифф пустой / не могу прочитать» — и это
приходит с КОДОМ 0 и валидным `Verdict:`, то есть ревью, которого не было, неотличимо от
ревью без замечаний. За один день 2026-08-14 так вышло трижды.
"""
import json
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


# --- «не ship без находок» = ревью не состоялось ---

def test_verdict_without_findings_is_treated_as_no_review():
    """Ревьюер, не сумевший прочитать артефакт (устаревший кэш своего плагина, недоступный
    инструмент), пишет «не смог» ПРОЗОЙ и возвращает код 0 с валидным `Verdict:`. За один день
    это случилось трижды, и механика не ловила — ловили глаза. Разбирать прозу нельзя, а
    «не ship и ноль находок» — самопротиворечие, проверяемое машинно."""
    blind = ("# Codex Adversarial Review\n\nVerdict: needs-attention\n\n"
             "Не удалось прочитать рабочее дерево.\n\nNo material findings.\n")
    why = g._review_says_nothing(blind)
    assert why and "без единой находки" in why


def test_clean_approve_verdict_is_not_flagged():
    """Обратное — чистый `approve` — законный вердикт, и блокировать его нельзя. Первая версия
    исключала выдуманный «ship», а словарь вердиктов ровно один: `RECOGNIZED_VERDICTS`."""
    assert g._review_says_nothing("Verdict: approve\n\nNo material findings.\n") is None
    assert g._review_says_nothing(json.dumps({"verdict": "approve", "findings": []})) is None


def test_verdict_with_findings_passes():
    real = ("Verdict: needs-attention\n\nFindings:\n"
            "- [high] Что-то сломано (file.py:10)\n  Подробности.\n")
    assert g._review_says_nothing(real) is None


def test_cli_refuses_a_blind_review_end_to_end(repo, monkeypatch, capsys):
    """Тест на УРОВНЕ CLI, а не хелпера: первая версия правки оставила `_review_says_nothing`
    без единого вызова — функция была мёртвой, а модульные тесты этого не видели, потому что
    звали её напрямую (находка код-ревью 14.08.2026)."""
    r, _ = repo
    (r / "b.py").write_text("y = 2\n")
    monkeypatch.setattr(g, "REPO_ROOT", r)
    blind = ("# Codex Adversarial Review\n\nVerdict: needs-attention\n\n"
             "Не удалось прочитать рабочее дерево.\n\nNo material findings.\n")
    monkeypatch.setattr(g, "_exec_companion",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, blind, ""))
    assert g.main(["companion-review", "--scope", "working-tree", "фокус"]) == 2
    assert "ревью НЕ выполнено" in capsys.readouterr().err


def test_cli_accepts_a_real_review_end_to_end(repo, monkeypatch, capsys):
    """Обратная сторона: настоящее ревью с находками обязано проходить."""
    r, _ = repo
    (r / "b.py").write_text("y = 2\n")
    monkeypatch.setattr(g, "REPO_ROOT", r)
    real = ("Verdict: needs-attention\n\nFindings:\n"
            "- [high] Что-то сломано (b.py:1)\n  Подробности.\n")
    monkeypatch.setattr(g, "_exec_companion",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, real, ""))
    assert g.main(["companion-review", "--scope", "working-tree", "фокус"]) == 0


@pytest.mark.parametrize("tail", ["- [ ] перезапустить с чистым кэшем",
                                  "- [1] см. лог companion",
                                  "  - [x] проверить кэш"])
def test_markdown_checklist_is_not_a_finding(tail):
    """`_FINDING_RE` матчит ЛЮБОЙ ярлык в скобках (намеренно: неизвестная severity обязана
    блокировать). Слепой ревьюер, оформивший «что делать дальше» чеклистом, проходил гвард
    насквозь — а это типовая форма сообщения «я не смог, вот что сделать»."""
    blind = f"Verdict: needs-attention\n\nНе удалось прочитать дерево.\n\n{tail}\n"
    assert g._review_says_nothing(blind), f"чеклист {tail!r} принят за находку"


@pytest.mark.parametrize("label", ["high", "critical", "low", "urgent", "blocker", "note",
                                   "P1", "sev-1", "high-impact", "b"])
def test_word_like_label_counts_as_a_finding(label):
    """Не только ИЗВЕСТНЫЕ severity: сначала сверка шла с `KNOWN_SEVERITIES`, и находка
    `[urgent]` объявлялась «слепым ревью» — раунд не засчитывался, а сама находка НЕ попадала
    в реестр, то есть настоящая претензия молча исчезала (найдено security-проходом).
    Сначала сверка шла с `KNOWN_SEVERITIES` (отсекала `[urgent]`), потом требовалось «слово
    из трёх букв» (отсекало `[P1]`, `[sev-1]`, `[high-impact]`). Каждый раз настоящая находка
    НЕ попадала в реестр: гвард возвращает 2 ДО записи. Теперь наоборот — находкой считается
    всё, кроме узкого закрытого перечня чеклист-маркеров."""
    real = f"Verdict: needs-attention\n\n- [{label}] Что-то сломано (a.py:1)\n  Текст.\n"
    assert g._review_says_nothing(real) is None


def test_design_review_prose_is_not_treated_as_blind(repo, monkeypatch):
    """Дизайн-ревью возвращает прозу по построению, и терять его единственный оплаченный
    раунд нельзя. Но исключение действует, ТОЛЬКО если ревьюер получил один дизайн-файл."""
    r, git = repo
    d = r / "дизайн.md"
    d.write_text("# дизайн\nтекст\n")
    monkeypatch.setattr(g, "REPO_ROOT", r)
    prose = "Verdict: needs-attention\n\n## Замечание 1\nПодумай про X.\n"
    monkeypatch.setattr(g, "_exec_companion",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, prose, ""))
    assert g.main(["companion-review", "--design-file", str(d),
                   "--scope", "working-tree", "фокус"]) == 0


def test_design_flag_does_not_launder_a_blind_code_review(repo, monkeypatch, capsys):
    """Флагу `--design-file` верить нельзя: он снимается до вызова движка, а тот всё равно
    ревьюит ДИАПАЗОН. Приложив любой дизайн-файл к правке КОДА, можно было протащить слепое
    кодовое ревью как дизайнерскую прозу — и мой прошлый тест это закреплял (находка
    код-ревью 14.08.2026). Судим по разрешённому входу, а не по флагу."""
    r, _ = repo
    d = r / "дизайн.md"
    d.write_text("# дизайн\nтекст\n")
    (r / "b.py").write_text("y = 2\n")                # рядом лежит КОД
    monkeypatch.setattr(g, "REPO_ROOT", r)
    blind = "Verdict: needs-attention\n\nНе смог прочитать дерево.\n"
    monkeypatch.setattr(g, "_exec_companion",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, blind, ""))
    assert g.main(["companion-review", "--design-file", str(d),
                   "--scope", "working-tree", "фокус"]) == 2
    assert "ревью НЕ выполнено" in capsys.readouterr().err


def test_blind_review_prints_the_body_it_rejected(repo, monkeypatch, capsys):
    """Без тела оператор не отличит слепого ревьюера от законного ревью, чьи находки не
    распарсились, — и не сможет ничего сделать."""
    r, _ = repo
    (r / "b.py").write_text("y = 2\n")
    monkeypatch.setattr(g, "REPO_ROOT", r)
    blind = "Verdict: needs-attention\n\nХук ссылается на несуществующий кэш.\n"
    monkeypatch.setattr(g, "_exec_companion",
                        lambda *a, **k: subprocess.CompletedProcess(a, 0, blind, ""))
    assert g.main(["companion-review", "--scope", "working-tree", "фокус"]) == 2
    err = capsys.readouterr().err
    assert "ревью НЕ выполнено" in err and "несуществующий кэш" in err
