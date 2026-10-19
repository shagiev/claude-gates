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


def test_s11_oversized_design_is_refused_before_review(env, monkeypatch, capsys):
    (env / "docs/big.md").write_text("## Альтернативы\n" + "x" * (g._DESIGN_SNAPSHOT_MAX + 1))
    _fake_companion(monkeypatch, "echo CALLED")
    assert g.main(["companion-review", "--design-file", "docs/big.md", "фокус"]) == 2
    captured = capsys.readouterr()
    assert "CALLED" not in captured.out and "раздел" in captured.err
    assert _receipts() == {}


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


def test_input_guard_sees_the_snapshotted_file_not_a_cwd_relative_one(env, monkeypatch, tmp_path):
    """Относительный путь разрешался от cwd, снимок — от корня: два разных файла."""
    _put(env, "docs/d.md", GOOD)
    seen = {}
    monkeypatch.setattr(g, "review_input_empty",
                        lambda passthrough, design_file: seen.setdefault("f", design_file) and None)
    monkeypatch.chdir(tmp_path)                                   # cwd ≠ корень репозитория
    _fake_companion(monkeypatch, 'printf "Verdict: approve\\n\\nNo material findings.\\n"')
    g.main(["companion-review", "--design-file", "docs/d.md", "фокус"])
    assert seen["f"] == str(env / "docs/d.md")
