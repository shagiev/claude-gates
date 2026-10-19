"""Дизайн-ревью оспаривает архитектуру: секция альтернатив, задание ревьюеру, квитанция на снимок.

Дизайн: docs/2026-10-18-design-alternatives-design.md (Р1–Р5, S1–S12, E1–E4).
"""
import hashlib
import json
import os
import shlex

import pytest

import codex_review_gate as g

GOOD = """# Дизайн

## Решения и альтернативы

- Вариант A — выбран, потому что …
- Вариант B — отвергнут, потому что …

## Сценарии (BSAC)

- S1 …
"""
NO_ALTS = """# Дизайн

Тут упоминается альтернатива, но секции нет.

## Сценарии (BSAC)

- S1 …
"""


def _h(text):
    return hashlib.sha256(text.encode()).hexdigest()


@pytest.fixture()
def env(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    monkeypatch.setattr(g, "REPO_ROOT", root)
    monkeypatch.setattr(g, "DESIGN_MARKER", root / ".claude" / ".design-approved")
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    monkeypatch.setattr(g, "review_input_empty", lambda *a, **k: None)
    return root


def _put(root, rel, text):
    (root / rel).write_text(text)
    return _h(text)


def _receipt(rel, text, policy=None):
    g._record_design_receipt(rel, _h(text), policy or g.DESIGN_REVIEW_POLICY)


def _mark(rel, h, *extra):
    return g.main(["write-marker", "design", "codex approved", h, "--file", rel, *extra])


def _fake_companion(monkeypatch, script):
    monkeypatch.setenv("CODEX_COMPANION_CMD", shlex.join(["bash", "-c", script, "--"]))


# ═══ Р1: распознавание секции — заголовок, а не слово в прозе ═══

@pytest.mark.parametrize("text", [
    "## Решения и альтернативы\n", "### Альтернативы\n", "# Alternatives considered\n",
    "## Рассмотренные альтернативы\n", "#### alternatives\n",
])
def test_alternatives_heading_recognized(text):
    assert g._has_alternatives(text) is True


@pytest.mark.parametrize("text", [
    "Рассматривались альтернативы, но выбрали A.",       # S9: проза без заголовка
    "- альтернатива: B", "```\n## Альтернативы\n```x", "",
])
def test_alternatives_in_prose_is_not_a_section(text):
    assert g._has_alternatives(text) is False


# ═══ S1/S2/S6: маркер требует секцию и квитанцию на ровно этот снимок ═══

def test_s2_section_and_receipt_unblock(env):
    h = _put(env, "docs/d.md", GOOD)
    _receipt("docs/d.md", GOOD)
    assert _mark("docs/d.md", h) == 0
    assert g._marker_state("s1") == "valid"


def test_s1_missing_section_blocks_regardless_of_tier(env, capsys):
    (env / g.GATE_CONFIG_NAME).write_text("review:\n  tier: convenience\n")
    h = _put(env, "docs/d.md", NO_ALTS)
    _receipt("docs/d.md", NO_ALTS)
    assert _mark("docs/d.md", h) == 2
    assert g._marker_state("s1") != "valid"
    err = capsys.readouterr().err
    assert "альтернатив" in err and "write-marker trivial" in err


def test_s6_no_receipt_blocks(env, capsys):
    h = _put(env, "docs/d.md", GOOD)
    assert _mark("docs/d.md", h) == 2
    assert g._marker_state("s1") != "valid"
    assert "квитанц" in capsys.readouterr().err


def test_s6_receipt_for_other_content_blocks(env):
    """Файл правили после ревью: квитанция на старый снимок, маркер — на новый хэш."""
    _receipt("docs/d.md", GOOD)
    edited = GOOD + "\n- добавленная после ревью поверхность\n"
    h = _put(env, "docs/d.md", edited)
    assert _mark("docs/d.md", h) == 2


def test_s6_receipt_for_other_path_blocks(env):
    _receipt("docs/other.md", GOOD)
    h = _put(env, "docs/d.md", GOOD)
    assert _mark("docs/d.md", h) == 2


def test_s6_receipt_of_old_policy_blocks(env):
    _receipt("docs/d.md", GOOD, policy="alternatives-v0")
    h = _put(env, "docs/d.md", GOOD)
    assert _mark("docs/d.md", h) == 2


def test_e3_path_spellings_share_one_receipt(env):
    _receipt("./docs/d.md", GOOD)
    h = _put(env, "docs/d.md", GOOD)
    assert _mark("docs/d.md", h) == 0


# ═══ S7: аудируемый аварийный выход, причина редактируется ═══

def test_s7_no_receipt_escape_is_audited_and_redacted(env, capsys):
    secret = "sk-" + "Z" * 40
    h = _put(env, "docs/d.md", GOOD)
    assert _mark("docs/d.md", h, "--no-receipt", f"codex недоступен, токен {secret}") == 0
    assert g._marker_state("s1") == "valid"
    audit = (env.parent / "audit.log").read_text()
    assert "DESIGN-NO-RECEIPT" in audit and "docs/d.md" in audit
    assert secret not in audit and secret not in capsys.readouterr().err


@pytest.mark.parametrize("extra", [["--no-receipt"], ["--no-receipt", "  "]])
def test_s7_escape_requires_a_reason(env, extra):
    h = _put(env, "docs/d.md", GOOD)
    assert _mark("docs/d.md", h, *extra) != 0
    assert g._marker_state("s1") != "valid"


def test_s7_escape_still_requires_the_section(env):
    h = _put(env, "docs/d.md", NO_ALTS)
    assert _mark("docs/d.md", h, "--no-receipt", "codex недоступен") == 2


# ═══ S12/S8: inline-маркер не создаётся, уже записанный читается ═══

def test_s12_inline_design_marker_is_refused(env, capsys):
    assert g.main(["write-marker", "design", "codex approved", "a" * 64]) == 2
    assert not g._marker_path("s1").exists()
    assert "--file" in capsys.readouterr().err


def test_s8_existing_inline_marker_still_reads_valid(env):
    g._write_marker_payload("s1", {"kind": "design", "detail": "old", "design_hash": "h",
                                   "session": "s1", "ts": "2026-10-01T00:00:00+00:00"})
    assert g._marker_state("s1") == "valid"


def test_trivial_marker_unchanged(env):
    assert g.main(["write-marker", "trivial", "опечатка в README"]) == 0
    assert g._marker_state("s1") == "valid"


# ═══ S3/S4/S5/S10: задание и снимок уходят ревьюеру, квитанция — на эти байты ═══

def _receipts():
    p = g._design_receipts_path()
    return json.loads(p.read_text()) if p and p.exists() else {}


def test_s3_s10_design_review_sends_task_and_full_snapshot_and_records_receipt(env, monkeypatch,
                                                                              capsys):
    _put(env, "docs/d.md", GOOD)
    _fake_companion(monkeypatch, 'printf "%s\\n" "$@"; printf "Verdict: approve\\n\\nNo material findings.\\n"')
    rc = g.main(["companion-review", "--design-file", "docs/d.md", "--scope", "working-tree",
                 "фокус агента"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "фокус агента" in out                         # фокус агента не тронут
    assert "STRONGEST ALTERNATIVE" in out                 # фиксированное задание
    assert GOOD.strip().splitlines()[-1] in out and "Вариант B — отвергнут" in out  # снимок целиком
    assert "--design-file" not in out                     # флаг гейта до движка не доезжает
    assert _receipts() == {"docs/d.md": {"sha256": _h(GOOD), "policy": g.DESIGN_REVIEW_POLICY}}


def test_e1_task_survives_missing_focus(env, monkeypatch, capsys):
    _put(env, "docs/d.md", GOOD)
    _fake_companion(monkeypatch, 'printf "%s\\n" "$@"; printf "Verdict: approve\\n\\nNo material findings.\\n"')
    assert g.main(["companion-review", "--design-file", "docs/d.md", "--scope",
                   "working-tree"]) == 0
    assert "STRONGEST ALTERNATIVE" in capsys.readouterr().out


def test_s5_code_review_gets_no_task_and_no_receipt(env, monkeypatch, capsys):
    _fake_companion(monkeypatch, 'printf "%s\\n" "$@"; printf "Verdict: approve\\n\\nNo material findings.\\n"')
    assert g.main(["companion-review", "--scope", "working-tree", "фокус"]) == 0
    assert "STRONGEST ALTERNATIVE" not in capsys.readouterr().out
    assert _receipts() == {}


def test_s4_outage_writes_no_receipt(env, monkeypatch):
    _put(env, "docs/d.md", GOOD)
    envelope = ('{"codex":{"status":0,"stdout":"You have hit your usage limit."},'
                '"result":null,"parseError":"model did not return valid JSON"}')
    _fake_companion(monkeypatch, "printf %s " + shlex.quote(envelope))
    assert g.main(["companion-review", "--design-file", "docs/d.md", "фокус"]) == 2
    assert _receipts() == {}


def test_s4_empty_output_writes_no_receipt(env, monkeypatch):
    _put(env, "docs/d.md", GOOD)
    _fake_companion(monkeypatch, "exit 0")
    assert g.main(["companion-review", "--design-file", "docs/d.md", "фокус"]) == 2
    assert _receipts() == {}


def test_s4_failed_companion_writes_no_receipt(env, monkeypatch):
    _put(env, "docs/d.md", GOOD)
    _fake_companion(monkeypatch, "echo boom >&2; exit 3")
    assert g.main(["companion-review", "--design-file", "docs/d.md", "фокус"]) == 2
    assert _receipts() == {}


def test_s11_large_design_is_reviewed_by_path_without_receipt(env, monkeypatch, capsys):
    """Гибрид Р2а: больше предела — ревью ИДЁТ (задание просит прочитать файл целиком по пути),
    снимок не вкладывается, квитанции нет, гейт печатает команду маркера с `--no-receipt`."""
    big = GOOD + "\n" + ("абзац дизайна " * 9000)          # > 100 КБ в UTF-8
    _put(env, "docs/big.md", big)
    assert len(big.encode()) > g._DESIGN_SNAPSHOT_MAX
    _fake_companion(monkeypatch, 'printf "%s\\n" "$@"; printf "Verdict: approve\\n\\nNo material findings.\\n"')
    assert g.main(["companion-review", "--design-file", "docs/big.md", "фокус"]) == 0
    captured = capsys.readouterr()
    assert "STRONGEST ALTERNATIVE" in captured.out              # задание ушло
    assert "READ THE FILE" in captured.out and "docs/big.md" in captured.out
    assert "абзац дизайна абзац дизайна" not in captured.out    # снимок НЕ вложен
    assert _receipts() == {}
    assert "--no-receipt" in captured.err and "предел" in captured.err


def test_s11_large_design_marker_needs_the_audited_escape(env, capsys):
    big = GOOD + "\n" + ("абзац дизайна " * 9000)
    h = _put(env, "docs/big.md", big)
    assert _mark("docs/big.md", h) == 2                          # квитанции быть не может
    assert "больше предела" in capsys.readouterr().err           # и сказано почему, а не «нет квитанции»
    assert _mark("docs/big.md", h, "--no-receipt", "дизайн больше предела снимка") == 0
    assert g._marker_state("s1") == "valid"


def test_s11_large_design_still_needs_the_alternatives_section(env):
    big = NO_ALTS + "\n" + ("абзац дизайна " * 9000)
    h = _put(env, "docs/big.md", big)
    assert _mark("docs/big.md", h, "--no-receipt", "больше предела") == 2


def test_s11_large_committed_design_on_clean_tree_is_reviewed(tmp_path, monkeypatch, capsys):
    """Как и для малого: вход — указание читать непустой файл, проверка диапазона не нужна."""
    import subprocess
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs/big.md").write_text(GOOD + "\n" + ("абзац " * 30000))
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null"}
    for a in (["init", "-q"], ["add", "-A"],
              ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "d"]):
        subprocess.run(["git", "-C", str(root), *a], check=True, env=env, capture_output=True)
    monkeypatch.setattr(g, "REPO_ROOT", root)
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.chdir(root)
    _fake_companion(monkeypatch, 'printf "Verdict: approve\\n\\nNo material findings.\\n"')
    assert g.main(["companion-review", "--design-file", "docs/big.md", "--scope", "working-tree",
                   "фокус"]) == 0, capsys.readouterr().err


def test_design_file_beyond_the_hard_limit_is_refused(env, monkeypatch, capsys):
    (env / "docs/huge.md").write_text("## Альтернативы\n" + "x" * (g._DESIGN_FILE_MAX + 1))
    _fake_companion(monkeypatch, "echo CALLED")
    assert g.main(["companion-review", "--design-file", "docs/huge.md", "фокус"]) == 2
    assert "CALLED" not in capsys.readouterr().out


# ═══ E3a: снимок читается без следования symlink ═══

@pytest.fixture()
def outside_secret(tmp_path):
    p = tmp_path / "id_ed25519"
    p.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nСЕКРЕТ\n")
    return p


@pytest.mark.parametrize("make", ["file-symlink-out", "file-symlink-in", "dir-symlink",
                                  "absolute", "dotdot", "directory", "missing"])
def test_e3a_unsafe_design_path_never_reaches_reviewer(env, monkeypatch, capsys, outside_secret,
                                                       make):
    if make == "file-symlink-out":
        os.symlink(outside_secret, env / "docs/d.md"); rel = "docs/d.md"
    elif make == "file-symlink-in":
        _put(env, "docs/real.md", GOOD); os.symlink(env / "docs/real.md", env / "docs/d.md")
        rel = "docs/d.md"
    elif make == "dir-symlink":
        (outside_secret.parent / "ext").mkdir(); (outside_secret.parent / "ext/d.md").write_text(GOOD)
        os.symlink(outside_secret.parent / "ext", env / "linked"); rel = "linked/d.md"
    elif make == "absolute":
        rel = str(outside_secret)
    elif make == "dotdot":
        rel = "../id_ed25519"
    elif make == "directory":
        rel = "docs"
    else:
        rel = "docs/нет.md"
    _fake_companion(monkeypatch, "echo CALLED")
    assert g.main(["companion-review", "--design-file", rel, "фокус"]) == 2
    captured = capsys.readouterr()
    assert "CALLED" not in captured.out
    assert "СЕКРЕТ" not in captured.out + captured.err
    assert _receipts() == {}


def test_e3a_marker_path_is_held_to_the_same_rules(env, outside_secret):
    os.symlink(outside_secret, env / "docs/d.md")
    assert _mark("docs/d.md", _h(outside_secret.read_text()), "--no-receipt", "x") == 2


def test_shipped_template_passes_the_gates_own_section_checks():
    """Шаблон, на который ссылается скилл, обязан сам проходить проверки секций."""
    from pathlib import Path
    tpl = (Path(g.__file__).resolve().parent.parent / "templates" / "design-doc.template.md")
    text = tpl.read_text()
    assert g._has_alternatives(text) and g._has_bsac(text)


# ═══ код-ревью 19.10.2026: квитанция только за СОСТОЯВШЕЕСЯ ревью ═══

@pytest.mark.parametrize("body", [
    "You have hit your usage limit. Try again at 8:06 PM",      # квота простым текстом, код 0
    "Traceback (most recent call last):\n  File x\nError: boom",
    "Ревью: всё выглядит разумно, но вердикта нет.",
])
def test_s4_output_without_verdict_writes_no_receipt(env, monkeypatch, capsys, body):
    _put(env, "docs/d.md", GOOD)
    _fake_companion(monkeypatch, "printf %s " + shlex.quote(body))
    g.main(["companion-review", "--design-file", "docs/d.md", "фокус"])
    captured = capsys.readouterr()
    assert _receipts() == {}
    assert body.splitlines()[0] in captured.out          # ответ всё равно показан
    assert "квитанц" in captured.err


def test_receipt_write_failure_does_not_swallow_the_review(env, monkeypatch, capsys):
    _put(env, "docs/d.md", GOOD)
    _fake_companion(monkeypatch, 'echo "Verdict: needs-attention"; echo "- [high] находка"')

    def broken(*_a, **_k):
        raise OSError("диск")
    monkeypatch.setattr(g, "_atomic_write_json", broken)
    g.main(["companion-review", "--design-file", "docs/d.md", "фокус"])
    captured = capsys.readouterr()
    assert "[high] находка" in captured.out and "квитанц" in captured.err


def test_snapshot_cap_fits_a_single_linux_argument():
    """Linux MAX_ARG_STRLEN = 128 КБ на ОДИН аргумент: снимок + задание обязаны влезать."""
    worst = g._design_challenge("docs/" + "x" * 200, "f" * 64, "я" * (g._DESIGN_SNAPSHOT_MAX // 2))
    assert len(worst.encode()) < 128 * 1024


@pytest.mark.parametrize("spelling", ["as-is", "realpath"])
def test_absolute_path_inside_repo_is_accepted(env, monkeypatch, capsys, spelling):
    _put(env, "docs/d.md", GOOD)
    root = str(env) if spelling == "as-is" else os.path.realpath(env)
    _fake_companion(monkeypatch, 'printf "Verdict: approve\\n\\nNo material findings.\\n"')
    assert g.main(["companion-review", "--design-file", f"{root}/docs/d.md", "фокус"]) == 0
    assert _receipts() == {"docs/d.md": {"sha256": _h(GOOD), "policy": g.DESIGN_REVIEW_POLICY}}


def test_heading_inside_unclosed_fence_is_not_a_section():
    assert g._has_alternatives("# D\n```\n## Альтернативы\nконец без закрытия\n") is False


def test_receipt_from_overridden_engine_is_marked_in_audit(env, monkeypatch, capsys):
    """Подменённый движок ревью не доказывает: квитанция выдаётся, но след обязан остаться."""
    _put(env, "docs/d.md", GOOD)
    _fake_companion(monkeypatch, 'printf "Verdict: approve\\n\\nNo material findings.\\n"')
    assert g.main(["companion-review", "--design-file", "docs/d.md", "фокус"]) == 0
    assert "engine=override" in (env.parent / "audit.log").read_text()
    assert "CODEX_COMPANION_CMD" in capsys.readouterr().err


def test_receipt_state_dir_failure_is_reported_not_raised(env, monkeypatch, capsys):
    _put(env, "docs/d.md", GOOD)
    _fake_companion(monkeypatch, 'printf "Verdict: approve\\n\\nNo material findings.\\n"')

    def no_state():
        raise g.TrustedGitError("доверенный git недоступен")
    monkeypatch.setattr(g, "_gate_state_dir", no_state)
    assert g.main(["companion-review", "--design-file", "docs/d.md", "фокус"]) == 0
    assert "квитанц" in capsys.readouterr().err


def test_input_guard_is_not_consulted_when_the_snapshot_is_the_input(env, monkeypatch, tmp_path):
    """Снимок — сам вход: проверка диапазона не нужна (и раньше рассогласовывалась с cwd)."""
    _put(env, "docs/d.md", GOOD)
    called = []
    monkeypatch.setattr(g, "review_input_empty", lambda *a, **k: called.append(a))
    monkeypatch.chdir(tmp_path)                                   # cwd ≠ корень репозитория
    _fake_companion(monkeypatch, 'printf "Verdict: approve\\n\\nNo material findings.\\n"')
    assert g.main(["companion-review", "--design-file", "docs/d.md", "фокус"]) == 0
    assert called == []


def test_committed_design_on_clean_tree_is_reviewable(tmp_path, monkeypatch, capsys):
    """Живая проверка 0.10.5 (19.10.2026): повторное ревью ЗАКОММИЧЕННОГО дизайна на чистом
    дереве отказывало «ревьюеру нечего показать» — хотя снимок целиком уже во входе. Без ревью
    нет квитанции, без квитанции нет маркера: маршрут для закоммиченного дизайна был мёртв.
    Настоящий git и настоящая проверка входа (без заглушки)."""
    import subprocess
    root = tmp_path / "repo"
    (root / "docs").mkdir(parents=True)
    (root / "docs/d.md").write_text(GOOD)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null"}
    for a in (["init", "-q"], ["add", "-A"],
              ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "d"]):
        subprocess.run(["git", "-C", str(root), *a], check=True, env=env, capture_output=True)
    monkeypatch.setattr(g, "REPO_ROOT", root)
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.chdir(root)
    _fake_companion(monkeypatch, 'printf "Verdict: approve\\n\\nNo material findings.\\n"')
    assert g.main(["companion-review", "--design-file", "docs/d.md", "--scope", "working-tree",
                   "фокус"]) == 0, capsys.readouterr().err
    assert _receipts() == {"docs/d.md": {"sha256": _h(GOOD), "policy": g.DESIGN_REVIEW_POLICY}}


def test_code_review_on_clean_tree_is_still_refused(tmp_path, monkeypatch, capsys):
    """Пропуск проверки входа — ТОЛЬКО при снимке: кодовое ревью пустоты по-прежнему отказ."""
    import subprocess
    root = tmp_path / "repo"
    root.mkdir()
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp_path), "GIT_CONFIG_GLOBAL": "/dev/null"}
    for a in (["init", "-q"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q",
                               "--allow-empty", "-m", "e"]):
        subprocess.run(["git", "-C", str(root), *a], check=True, env=env, capture_output=True)
    monkeypatch.setattr(g, "REPO_ROOT", root)
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.chdir(root)
    _fake_companion(monkeypatch, "echo CALLED")
    assert g.main(["companion-review", "--scope", "working-tree", "фокус"]) == 2
    assert "CALLED" not in capsys.readouterr().out


@pytest.mark.parametrize("content", ["", "   \n\t\n"])
def test_empty_design_snapshot_is_refused_end_to_end(env, monkeypatch, capsys, content):
    (env / "docs/d.md").write_text(content)
    _fake_companion(monkeypatch, 'echo CALLED; printf "Verdict: approve\\n\\nNo material findings.\\n"')
    assert g.main(["companion-review", "--design-file", "docs/d.md", "фокус"]) == 2
    captured = capsys.readouterr()
    assert "CALLED" not in captured.out and "пуст" in captured.err
    assert _receipts() == {}
    assert _mark("docs/d.md", _h(content), "--no-receipt", "x") == 2


EVIL = "docs/a $(touch PWNED) `id`;x.md"


def test_large_design_marker_hint_is_shell_safe(env, monkeypatch, capsys):
    """Подсказку копирует оператор: путь из имени файла не должен стать подстановкой команды."""
    big = GOOD + "\n" + ("абзац дизайна " * 9000)
    (env / EVIL).write_text(big)
    _fake_companion(monkeypatch, 'printf "Verdict: approve\\n\\nNo material findings.\\n"')
    assert g.main(["companion-review", "--design-file", EVIL, "фокус"]) == 0
    err = capsys.readouterr().err
    assert shlex.quote(EVIL) in err
    cmd = next(l for l in err.splitlines() if l.strip().startswith("write-marker design"))
    args = shlex.split(cmd)
    assert args[args.index("--file") + 1] == EVIL                 # разбор шеллом — ровно путь


def test_no_receipt_hint_is_shell_safe(env, capsys):
    h = _put(env, EVIL, GOOD)
    assert _mark(EVIL, h) == 2
    err = capsys.readouterr().err
    cmd = next(l for l in err.splitlines() if l.strip().startswith("companion-review"))
    assert shlex.split(cmd)[shlex.split(cmd).index("--design-file") + 1] == EVIL


def test_design_review_runs_the_reviewer_from_the_repo_root(env, monkeypatch, capsys):
    """Путь в задании — от корня: ревьюер, запущенный из подкаталога, его не нашёл бы."""
    _put(env, "docs/d.md", GOOD)
    monkeypatch.chdir(env / "docs")
    _fake_companion(monkeypatch, 'echo "CWD=$(pwd -P)"; printf "Verdict: approve\\n\\nNo material findings.\\n"')
    assert g.main(["companion-review", "--design-file", "docs/d.md", "фокус"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert f"CWD={os.path.realpath(env)}" in lines       # строка целиком: подстрока «…/repo/docs» ложно проходила


def test_large_design_reviewer_reads_a_gate_owned_copy_not_the_worktree(env, monkeypatch, capsys,
                                                                         outside_secret):
    """Код-ревью 0.10.7: указание читать путь В РАБОЧЕМ ДЕРЕВЕ открывало окно подмены — файл
    после безопасного чтения заменяют symlink'ом на секрет. Фейковый ревьюер делает ровно это,
    а затем читает файл, на который указало задание: обязан получить снимок, а не секрет."""
    big = GOOD + "\n" + ("абзац дизайна " * 9000)
    _put(env, "docs/big.md", big)
    script = (
        'rm -f docs/big.md; ln -s ' + shlex.quote(str(outside_secret)) + ' docs/big.md; '
        'p=$(printf "%s\\n" "$@" | sed -n "s/.*READ THE FILE \\`\\([^\\`]*\\)\\`.*/\\1/p" | head -1); '
        'echo "READ=$p"; head -n 5 "$p"; '
        'printf "Verdict: approve\\n\\nNo material findings.\\n"')
    _fake_companion(monkeypatch, script)
    assert g.main(["companion-review", "--design-file", "docs/big.md", "фокус"]) == 0
    out = capsys.readouterr().out
    read_path = next(l for l in out.splitlines() if l.startswith("READ="))[5:]
    assert read_path and not read_path.startswith(str(env))      # не путь в рабочем дереве
    assert "Решения и альтернативы" in out and "СЕКРЕТ" not in out
    assert not os.path.exists(read_path)                          # копия убрана после ревью


def test_partial_design_copy_is_removed_on_write_failure(env, monkeypatch):
    real_fdopen = os.fdopen

    class Broken:
        def __init__(self, f): self.f = f
        def __enter__(self): return self
        def __exit__(self, *a): self.f.close()
        def write(self, _b): raise OSError(28, "No space left on device")
    monkeypatch.setattr(g.os, "fdopen", lambda fd, mode: Broken(real_fdopen(fd, mode)))
    made = g._materialize_design_copy("a" * 64, "текст")
    assert isinstance(made, str) and "не записать" in made
    assert list((g._gate_state_dir() / "design-snapshots").glob("*.md")) == []


def test_stale_design_copies_are_swept(env):
    d = g._gate_state_dir() / "design-snapshots"
    d.mkdir(parents=True)
    old, fresh = d / "old.md", d / "fresh.md"
    old.write_text("x"); fresh.write_text("y")
    os.utime(old, (1, 1))
    made = g._materialize_design_copy("b" * 64, "текст")
    assert not old.exists() and fresh.exists() and made.exists()
