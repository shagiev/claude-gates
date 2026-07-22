from pathlib import Path

import pytest

import codex_review_gate as g

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture(autouse=True)
def _isolate_companion(monkeypatch, tmp_path):
    # структурная изоляция: ни один тест не должен случайно позвать реальный codex-companion.mjs
    monkeypatch.setenv("CODEX_COMPANION_CMD", "bash -c 'exit 99'")
    monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PLUGIN_ROOT", raising=False)   # чтобы missing-plugin тест был гермётичен
    # реальный CLAUDE_CODE_SESSION_ID в окружении не должен перебивать сессию, которую задаёт тест
    # через CLAUDE_SESSION_ID (иначе _env_session вернёт реальный id и маркер-тесты сломаются)
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    # ambient CODEX_REVIEW_SKIP / CODEX_DEPLOY_BASELINE (напр. из `SKIP=1 make deploy`) не должны
    # контаминировать gate-тесты (Codex P1: иначе escape-hatch деплой роняет test-quick)
    monkeypatch.delenv("CODEX_REVIEW_SKIP", raising=False)
    monkeypatch.delenv("CODEX_DEPLOY_BASELINE", raising=False)
    # findings-ledger (Фаза 1.6) — в tmp: тесты не должны писать в реальный logs/review_findings
    monkeypatch.setattr(g, "FINDINGS_DIR", tmp_path / "rf_auto")
    # Task 5: ambient LADDER_SKIP (напр. из ручного теста в шелле) не должен просачиваться
    monkeypatch.delenv("LADDER_SKIP", raising=False)


def test_parse_pass_no_blocking():
    v = g.parse_review_output((FIX / "codex_review_rendered_pass.txt").read_text())
    assert v.verdict == "approve" and v.findings == [] and v.blocking is False and v.valid is True


def test_parse_block_flags_critical():
    v = g.parse_review_output((FIX / "codex_review_rendered_block.txt").read_text())
    assert v.verdict == "needs-attention"
    assert "critical" in [s for s, _ in v.findings]
    assert v.blocking is True and v.valid is True


def test_medium_low_only_not_blocking():
    v = g.parse_review_output("Verdict: needs-attention\nFindings:\n- [medium] x (a.py:1)\n- [low] y (b.py:2)\n")
    assert v.blocking is False and v.valid is True


def test_strip_ansi():
    assert g.strip_ansi("\x1b[31mVerdict: approve\x1b[0m") == "Verdict: approve"


def test_decide_block_on_high():
    v = g.parse_review_output("Verdict: needs-attention\nFindings:\n- [high] z (a.py:1)\n")
    assert g.decide_exit(v, fail_closed=True) == 2


def test_decide_allow_on_clean():
    v = g.parse_review_output("Verdict: approve\n\nNo material findings.\n")
    assert g.decide_exit(v, fail_closed=True) == 0


def test_decide_none_fail_closed_blocks():
    assert g.decide_exit(None, fail_closed=True) == 2


def test_decide_none_fail_open_allows():
    assert g.decide_exit(None, fail_closed=False) == 0


# --- R1-1: строгая валидация ---
def test_unknown_verdict_is_invalid_and_blocks_fail_closed():
    v = g.parse_review_output("Verdict: maybe-ok\n\nNo material findings.\n")
    assert v.valid is False
    assert g.decide_exit(v, fail_closed=True) == 2


def test_empty_output_blocks_fail_closed():
    v = g.parse_review_output("")
    assert v.valid is False
    assert g.decide_exit(v, fail_closed=True) == 2


def test_needs_attention_without_findings_is_drift():
    v = g.parse_review_output("Verdict: needs-attention\n\n(findings not rendered)\n")
    assert v.valid is False   # дрейф формата → недоступно
    assert g.decide_exit(v, fail_closed=True) == 2


def test_unknown_severity_is_blocking():   # R1-1b
    v = g.parse_review_output("Verdict: needs-attention\nFindings:\n- [urgent] possible overspend (a.py:1)\n")
    assert v.blocking is True
    assert g.decide_exit(v, fail_closed=True) == 2


def test_non_alpha_severity_labels_block():   # R1-1b: [P0]/[very-high]/пробелы не должны «исчезать»
    for line in ("- [P0] overspend (a.py:1)", "- [very-high] x (a.py:1)", "- [ critical ] x (a.py:1)"):
        v = g.parse_review_output(f"Verdict: needs-attention\nFindings:\n{line}\n")
        assert v.blocking is True, line


def test_approve_with_unknown_finding_still_blocks():
    v = g.parse_review_output("Verdict: approve\nFindings:\n- [P1] sneaky (a.py:1)\n")
    assert v.blocking is True


def test_truncated_finding_bullet_is_malformed():   # R3b: усечённый вывод = fail-closed
    v = g.parse_review_output("Verdict: approve\nFindings:\n- [critical")
    assert v.valid is False
    assert g.decide_exit(v, fail_closed=True) == 2


def test_approve_without_no_findings_marker_is_drift():   # R3b
    v = g.parse_review_output("Verdict: approve\n\nSummary: ok\n")   # нет маркера и находок
    assert v.valid is False
    assert g.decide_exit(v, fail_closed=True) == 2


def test_invalid_verdict_fail_open_allows():
    v = g.parse_review_output("garbage output")
    assert g.decide_exit(v, fail_closed=False) == 0   # design fail-open


# --- Carry-forward from Task 1: realistic renderer output (prefix header + multi-line finding body) ---
def test_realistic_prefixed_output_with_multiline_finding_body():
    text = (
        "# Codex Review\n"
        "Target: feat/codex-review-gates\n"
        "\n"
        "Verdict: needs-attention\n"
        "\n"
        "Findings:\n"
        "- [critical] Unbounded await blows the cycle (app/x.py:10)\n"
        "  This await has no timeout and can hang the whole main_cycle indefinitely\n"
        "  if the downstream call never returns.\n"
        "  Recommendation: wrap in asyncio.wait_for with a bounded timeout.\n"
        "- [medium] Naming nit (app/y.py:3)\n"
        "  Consider renaming `x` to something descriptive.\n"
    )
    v = g.parse_review_output(text)
    assert v.verdict == "needs-attention"
    assert "critical" in [s for s, _ in v.findings]
    assert v.blocking is True
    assert v.valid is True


# --- Task 3: вызов плагина + git-хелперы + escape-hatch/аудит ---
def _stub(name):
    return ["bash", str(FIX / name)]


def test_run_companion_pass(monkeypatch):
    monkeypatch.setenv("CODEX_COMPANION_CMD", " ".join(_stub("stub_companion_pass.sh")))
    out = g.run_companion_review(base="HEAD~1", scope="branch")
    assert out is not None and "Verdict: approve" in out


def test_run_companion_failure_none(monkeypatch):
    monkeypatch.setenv("CODEX_COMPANION_CMD", "bash -c 'exit 7'")
    assert g.run_companion_review(base=None, scope="auto") is None


def test_resolve_companion_env_override(monkeypatch):
    monkeypatch.setenv("CODEX_COMPANION_CMD", "node /x/codex-companion.mjs")
    assert g.resolve_companion_cmd() == ["node", "/x/codex-companion.mjs"]


def test_skip_requested(monkeypatch):
    monkeypatch.setenv("CODEX_REVIEW_SKIP", "1")
    assert g.skip_requested() is True
    monkeypatch.delenv("CODEX_REVIEW_SKIP", raising=False)
    assert g.skip_requested() is False


def test_git_head_diff_sha_clean():
    assert len(g.git_head()) == 40
    assert g.diff_sha256("HEAD~1") == g.diff_sha256("HEAD~1")
    assert isinstance(g.working_tree_clean(), bool)


def test_resolve_companion_cmd_raises_when_no_plugin(monkeypatch):
    monkeypatch.delenv("CODEX_COMPANION_CMD", raising=False)
    monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(g.glob, "glob", lambda *_a, **_k: [])
    with pytest.raises(FileNotFoundError):
        g.resolve_companion_cmd()


# --- Task 4: ledger + check-reviewed CLI (R1-2 baseline fail-closed, R1-3 clean tree) ---
import json


def test_write_read_valid_ledger(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    v = g.parse_review_output("Verdict: approve\nNo material findings.\n")
    g.write_ledger("a" * 40, "d" * 64, "b" * 40, v)
    assert g.read_valid_ledger("a" * 40, "d" * 64) is not None


def test_ledger_rejects_diff_mismatch(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    v = g.parse_review_output("Verdict: approve\nNo material findings.\n")
    g.write_ledger("a" * 40, "d" * 64, "b" * 40, v)
    assert g.read_valid_ledger("a" * 40, "e" * 64) is None


def test_read_ledger_rejects_blocking_record(tmp_path, monkeypatch):   # R1-1
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    v = g.parse_review_output("Verdict: needs-attention\nFindings:\n- [critical] x (a.py:1)\n")
    g.write_ledger("a" * 40, "d" * 64, "b" * 40, v)
    assert g.read_valid_ledger("a" * 40, "d" * 64) is None   # blocking record не разблокирует


def test_read_ledger_rejects_malformed_record(tmp_path, monkeypatch):   # R1-1, проверяет фикс malformed round-trip
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    v = g.parse_review_output("Verdict: approve\nFindings:\n- [critical")   # усечённый → malformed
    g.write_ledger("a" * 40, "d" * 64, "b" * 40, v)
    assert g.read_valid_ledger("a" * 40, "d" * 64) is None   # malformed record не разблокирует (после фикса #1)


def _clean(monkeypatch):
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "resolve_baseline", lambda: "HEAD~1")
    # Task 5: check_reviewed_cli теперь требует ladder-range-покрытие baseline..HEAD;
    # тесты, не проверяющие ladder-логику специально, глушат её нейтральным "покрыт".
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 0)


def test_check_reviewed_blocks_on_critical(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path); _clean(monkeypatch)
    monkeypatch.setenv("CODEX_COMPANION_CMD", " ".join(_stub("stub_companion_block.sh")))
    assert g.check_reviewed_cli() == 2


def test_check_reviewed_passes_and_writes(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path); _clean(monkeypatch)
    monkeypatch.setenv("CODEX_COMPANION_CMD", " ".join(_stub("stub_companion_pass.sh")))
    assert g.check_reviewed_cli() == 0
    assert g.read_valid_ledger(g.git_head(), g.diff_sha256("HEAD~1")) is not None


def test_check_reviewed_dirty_tree_blocks(tmp_path, monkeypatch):   # R1-3
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(g, "working_tree_clean", lambda: False)
    monkeypatch.setattr(g, "resolve_baseline", lambda: "HEAD~1")
    assert g.check_reviewed_cli() == 2


def test_check_reviewed_unknown_baseline_blocks(tmp_path, monkeypatch):   # R1-2
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "resolve_baseline", lambda: None)
    assert g.check_reviewed_cli() == 2


def test_check_reviewed_skip_audits(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)   # skip только на чистом дереве
    # Task 5: CODEX_REVIEW_SKIP больше НЕ пропускает baseline-резолюцию/ladder — baseline
    # нужен обеим частям, глушим ladder нейтрально (не тестируем его здесь).
    monkeypatch.setattr(g, "resolve_baseline", lambda: "HEAD~1")
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 0)
    monkeypatch.setenv("CODEX_REVIEW_SKIP", "1")
    assert g.check_reviewed_cli() == 0
    assert "SKIP" in (tmp_path / "audit.log").read_text()


def test_skip_does_not_bypass_clean_tree(tmp_path, monkeypatch):   # Codex P1
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(g, "working_tree_clean", lambda: False)    # грязное дерево
    monkeypatch.setenv("CODEX_REVIEW_SKIP", "1")
    assert g.check_reviewed_cli() == 2   # skip НЕ пускает грязь (clean-tree проверяется первой)


def test_is_code_path_normalizes_dotdot():   # Codex P2: docs/../app/x.py → код-путь
    assert g.is_code_path("docs/../app/workers/x.py") is True
    assert g.is_code_path(str(g.REPO_ROOT / "docs/../app/x.py")) is True
    assert g.is_code_path("app/../docs/x.md") is False   # реально в docs


def test_bash_mutation_command_position():   # Codex P2: mutator только в командной позиции
    assert g.bash_touches_code("git log --grep patch") is False
    assert g.bash_touches_code("echo patch") is False
    assert g.bash_touches_code("sed -i s/a/b/ docs/design.md") is False   # docs не код
    assert g.bash_touches_code("cat file && patch app/x.py < f.diff") is True   # patch после &&


def test_check_reviewed_outage_emits_emergency(tmp_path, monkeypatch, capsys):   # ML6
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "resolve_baseline", lambda: "HEAD~1")
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 0)   # ladder не тестируется здесь
    monkeypatch.setenv("CODEX_COMPANION_CMD", "bash -c 'exit 7'")   # реальный outage
    assert g.check_reviewed_cli() == 2
    # генерализация плагина: аварийный контур без проект-специфичной команды заморозки, но упоминание
    # kill-switch/freeze обязано остаться (ML6 — не деплой вслепую при активном инциденте)
    assert "freeze" in capsys.readouterr().err


# --- Task 5 (ladder R6): .githooks/ и Makefile — код-пути (спека §2/§4, ML-L7) ---
def test_is_code_path_githooks_and_makefile():
    assert g.is_code_path(".githooks/pre-commit") is True
    assert g.is_code_path("Makefile") is True


def test_is_code_path_dot_prefix_not_stripped_as_charset():
    # Regression: старый `p.lstrip("./")` трактовал аргумент как МНОЖЕСТВО символов, не
    # префикс — снимал ведущую точку с ЛЮБОГО dot-каталога (".githooks/x" → "githooks/x"),
    # ломая и новый .githooks/-префикс, и (случайно совпадавшую по итогу, но по неверной
    # причине) .claude/-экземпцию. os.path.normpath один справляется без lstrip.
    assert g.is_code_path(".githooks/post-commit") is True
    assert g.is_code_path(".claude/settings.json") is False   # экземпция — по правильной причине
    assert g.is_code_path("./app/x.py") is True   # explicit './' по-прежнему схлопывается normpath'ом


def test_gate_edit_denies_githooks_without_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    hook = json.dumps({"session_id": "s1", "tool_input": {"file_path": ".githooks/pre-commit"}})
    assert g.gate_edit_cli(hook) == 2


def test_gate_edit_denies_makefile_without_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    hook = json.dumps({"session_id": "s1", "tool_input": {"file_path": "Makefile"}})
    assert g.gate_edit_cli(hook) == 2


# --- Task 5: маркер (session-bound, R1-6) + gate-edit + gate-bash (R1-5) + main ---
def test_is_code_path():
    assert g.is_code_path("app/workers/worker.py") is True
    assert g.is_code_path("scripts/x.py") is True
    assert g.is_code_path("config.yaml") is True
    assert g.is_code_path("docs/foo.md") is False
    assert g.is_code_path("AGENTS.md") is False
    assert g.is_code_path(".claude/settings.json") is False


def test_marker_lifecycle(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    assert g.has_marker("s1") is False
    g.write_marker("design", "codex approved", design_hash="h1")
    assert g.has_marker("s1") is True
    g.clear_marker()
    assert g.has_marker("s1") is False


def test_marker_rejects_other_session(tmp_path, monkeypatch):   # R1-6
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "sess-A")
    g.write_marker("design", "ok", design_hash="h1")
    assert g.has_marker("sess-B") is False


def test_marker_design_requires_hash(tmp_path, monkeypatch):   # R1-6b
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    g.write_marker("design", "no hash", design_hash=None)
    assert g.has_marker("s1") is False


def test_marker_empty_session_never_valid(tmp_path, monkeypatch):   # R1-6b: закрыть ""=="" дыру
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "")
    g.write_marker("design", "x", design_hash="h1")
    assert g.has_marker("") is False


def test_marker_trivial_audited(tmp_path, monkeypatch):   # R1-6b / BS4
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    g.write_marker("trivial", "typo fix")
    assert g.has_marker("s1") is True
    assert "trivial-marker" in (tmp_path / "audit.log").read_text()


def test_marker_rejects_corrupt(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    (tmp_path / ".design-approved").write_text("{not json")
    assert g.has_marker("s1") is False


def test_gate_edit_denies_code_without_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    hook = json.dumps({"session_id": "s1", "tool_input": {"file_path": "app/x.py"}})
    assert g.gate_edit_cli(hook) == 2


def test_gate_edit_allows_docs(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    hook = json.dumps({"session_id": "s1", "tool_input": {"file_path": "docs/x.md"}})
    assert g.gate_edit_cli(hook) == 0


def test_gate_edit_fail_open_when_session_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    hook = json.dumps({"tool_input": {"file_path": "app/x.py"}})   # нет session_id → fail-open
    assert g.gate_edit_cli(hook) == 0


def test_gate_edit_allows_code_with_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    g.write_marker("design", "ok", design_hash="h1")
    hook = json.dumps({"session_id": "s1", "tool_input": {"file_path": "app/x.py"}})
    assert g.gate_edit_cli(hook) == 0


# --- R1-5: Bash-гейт ---
def test_bash_touches_code():
    assert g.bash_touches_code("sed -i 's/a/b/' app/x.py") is True
    assert g.bash_touches_code("git apply patch.diff && echo app/x.py") is True
    assert g.bash_touches_code("echo hello > /tmp/x") is False
    assert g.bash_touches_code("pytest tests/test_x.py -q") is False   # чтение, не мутация


def test_gate_bash_denies_code_mutation_without_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    hook = json.dumps({"session_id": "s1", "tool_input": {"command": "sed -i s/a/b/ app/x.py"}})
    assert g.gate_bash_cli(hook) == 2


def test_gate_bash_allows_readonly(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    hook = json.dumps({"session_id": "s1", "tool_input": {"command": "make test"}})
    assert g.gate_bash_cli(hook) == 0


# --- Fix 1 (ревью Task 5): is_code_path должен принимать АБСОЛЮТНЫЕ пути ---
def test_is_code_path_absolute(monkeypatch):
    assert g.is_code_path(str(g.REPO_ROOT / "app/x.py")) is True
    assert g.is_code_path(str(g.REPO_ROOT / "docs/x.md")) is False
    assert g.is_code_path("/etc/passwd") is False   # вне репо


def test_gate_edit_denies_absolute_code_path(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    hook = json.dumps({"session_id": "s1", "tool_input": {"file_path": str(g.REPO_ROOT / "app/x.py")}})
    assert g.gate_edit_cli(hook) == 2   # абсолютный код-путь без маркера → deny


# --- Fix 2 (ревью Task 5): main() покрытие ---
def test_main_gate_edit_denies(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(
        json.dumps({"session_id": "s1", "tool_input": {"file_path": "app/x.py"}})))
    assert g.main(["gate-edit"]) == 2


def test_main_clear_and_write_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    assert g.main(["write-marker", "design", "ok", "hash123"]) == 0
    assert g.has_marker("s1") is True
    assert g.main(["clear-marker"]) == 0
    assert g.has_marker("s1") is False


def test_main_unknown_command():
    assert g.main(["bogus"]) == 1


def test_main_gate_bash_denies(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(
        json.dumps({"session_id": "s1", "tool_input": {"command": "sed -i s/a/b/ app/x.py"}})))
    assert g.main(["gate-bash"]) == 2


# --- Fix 3 (ревью Task 5): gate_bash_cli зеркальные тесты + malformed JSON ---
def test_gate_bash_fail_open_when_session_unknown(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.delenv("CLAUDE_SESSION_ID", raising=False)
    assert g.gate_bash_cli(json.dumps({"tool_input": {"command": "sed -i s/a/b/ app/x.py"}})) == 0


def test_gate_bash_allows_mutation_with_marker(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    g.write_marker("design", "ok", "h1")
    hook = json.dumps({"session_id": "s1", "tool_input": {"command": "sed -i s/a/b/ app/x.py"}})
    assert g.gate_bash_cli(hook) == 0


def test_gates_malformed_json_allow():
    assert g.gate_edit_cli("{not json") == 0
    assert g.gate_bash_cli("{not json") == 0


# --- word boundaries + target-aware bash gating ---
def test_bash_word_boundaries_and_targets():
    # ложных срабатываний нет: committee/guarantee/unpatched
    assert g.bash_touches_code("git commit -m 'update committee list'") is False
    assert g.bash_touches_code("cat unpatched_notes.txt") is False
    # mv/cp в код-путь пока НЕ ловим — документированный остаток (Codex P2, Фаза 2)
    assert g.bash_touches_code("mv guarantee.txt app/x.py") is False
    # реальные мутаторы в код-путь ловятся
    assert g.bash_touches_code("patch app/x.py < fix.diff") is True
    assert g.bash_touches_code("tee app/x.py <<< 'content'") is True
    assert g.bash_touches_code("sed -i s/a/b/ app/x.py") is True


def test_bash_gating_codex_fixes():
    # Codex P2: git apply / patch без inline код-пути — безусловно (цель в патч-файле)
    assert g.bash_touches_code("git apply /tmp/fix.patch") is True
    assert g.bash_touches_code("patch -p1 < /tmp/fix.diff") is True
    # Codex P2: нумерованный / &-редирект в КОД-файл ловится
    assert g.bash_touches_code("python gen.py 1> app/generated.py") is True
    assert g.bash_touches_code("python gen.py &> tests/out.py") is True
    # Codex P3: редирект в НЕ-код (диагностика) НЕ блокируется, хоть в аргументах и есть код-путь
    assert g.bash_touches_code("pytest tests/test_x.py > /tmp/pytest.log") is False
    assert g.bash_touches_code("python scripts/x.py 2>&1 | grep app/y.py") is False   # 2>&1 = fd-дуп


def test_parse_json_pass_and_block():   # JSON-first (реальный adversarial-review --json)
    p = g.parse_review_output((FIX / "codex_adv_pass.json").read_text())
    assert p.verdict == "approve" and p.findings == [] and p.blocking is False and p.valid is True
    b = g.parse_review_output((FIX / "codex_adv_block.json").read_text())
    assert b.verdict == "needs-attention" and "critical" in [s for s, _ in b.findings]
    assert b.blocking is True and b.valid is True


def test_parse_json_parseerror_and_status_fail_closed():
    pe = g.parse_review_output((FIX / "codex_adv_parseerror.json").read_text())
    assert pe.valid is False and g.decide_exit(pe, fail_closed=True) == 2   # parseError → fail-closed
    st = g.parse_review_output('{"codex":{"status":1,"stdout":""},"result":{"verdict":"approve","findings":[]}}')
    assert st.valid is False   # процесс ревью упал → fail-closed


def test_text_fallback_still_parses_rendered():
    v = g.parse_review_output("Verdict: approve\n\nNo material findings.\n")
    assert v.valid is True and v.blocking is False


def test_gate_edit_notebook_path(tmp_path, monkeypatch):   # Codex P3: NotebookEdit → notebook_path
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    hook = json.dumps({"session_id": "s1", "tool_input": {"notebook_path": "tests/nb.ipynb"}})
    assert g.gate_edit_cli(hook) == 2   # ноутбук под tests/ без маркера → deny


def test_check_reviewed_non_ancestor_baseline_blocks(tmp_path, monkeypatch):   # Codex P2
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "resolve_baseline", lambda: "0" * 40)   # не предок HEAD
    assert g.check_reviewed_cli() == 2


def test_parse_json_malformed_envelope_is_invalid():   # Codex high: malformed ≠ clean approve
    for bad in ('{"result":{"verdict":"approve"}}',            # findings не list
                '{"result":{"verdict":"maybe","findings":[]}}',  # нераспознанный verdict
                '{"result":null,"parseError":null}',            # result не dict
                '{"foo":"bar"}'):                               # вообще не envelope
        v = g.parse_review_output(bad)
        assert v.valid is False, bad
        assert g.decide_exit(v, fail_closed=True) == 2, bad


def test_check_reviewed_records_reviewed_sha(tmp_path, monkeypatch):   # Codex high: bind approved SHA
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(g, "LAST_REVIEWED", tmp_path / ".last-reviewed-sha")
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "resolve_baseline", lambda: "HEAD~1")
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 0)   # ladder не тестируется здесь
    monkeypatch.setenv("CODEX_COMPANION_CMD", f"bash {FIX / 'stub_companion_pass.sh'}")
    assert g.check_reviewed_cli() == 0
    assert (tmp_path / ".last-reviewed-sha").read_text().strip() == g.git_head()


def test_parse_json_requires_codex_status():   # Codex P2: envelope без codex.status → invalid
    assert g.parse_review_output('{"result":{"verdict":"approve","findings":[]}}').valid is False


def test_parse_json_non_object_finding_invalid():   # Codex P2: findings [null] → invalid
    v = g.parse_review_output('{"codex":{"status":0},"result":{"verdict":"approve","findings":[null]}}')
    assert v.valid is False


def test_parse_json_status_must_be_true_int():   # Codex P2: false/0.0/"0"/null ≠ успех
    for bad in ("false", "0.0", '"0"', "null"):
        env = '{"codex":{"status":%s},"result":{"verdict":"approve","findings":[]}}' % bad
        assert g.parse_review_output(env).valid is False, bad
    ok = '{"codex":{"status":0},"result":{"verdict":"approve","summary":"ok","findings":[],"next_steps":[]}}'
    assert g.parse_review_output(ok).valid is True   # настоящий int 0 + полная схема → ок


def test_parse_json_incomplete_schema_invalid():   # Codex P1: неполная схема result → invalid
    for bad in ('{"codex":{"status":0},"result":{"verdict":"approve","findings":[]}}',
                '{"codex":{"status":0},"result":{"verdict":"approve","summary":"x","next_steps":[],"findings":[{"title":"t"}]}}'):
        assert g.parse_review_output(bad).valid is False, bad


def test_bash_mutation_multiline():   # Codex P2: mutator на новой строке (multiline tool call)
    assert g.bash_touches_code("echo ok\ngit apply /tmp/fix.patch") is True
    assert g.bash_touches_code("cd /x\npatch app/y.py < f") is True


def test_parse_json_full_schema_edges():   # Codex P1: пустой summary / finding вне схемы
    bad_summary = ('{"codex":{"status":0},"result":{"verdict":"approve","summary":"  ",'
                   '"findings":[],"next_steps":[]}}')
    bad_conf = ('{"codex":{"status":0},"result":{"verdict":"needs-attention","summary":"x",'
                '"next_steps":[],"findings":[{"severity":"high","title":"t","body":"b","file":"a.py",'
                '"line_start":1,"line_end":1,"confidence":2.0,"recommendation":"r"}]}}')
    for bad in (bad_summary, bad_conf):
        assert g.parse_review_output(bad).valid is False, bad


def test_bash_sed_inplace_variants():   # Codex P2: -E -i / --in-place / -Ei
    assert g.bash_touches_code("sed -E -i 's/a/b/' app/x.py") is True
    assert g.bash_touches_code("sed --in-place 's/a/b/' app/x.py") is True
    assert g.bash_touches_code("sed -Ei 's/a/b/' app/x.py") is True
    assert g.bash_touches_code("sed -n 's/a/b/p' app/x.py") is False   # без in-place — read-only


def test_resolve_companion_honors_claude_plugin_root(tmp_path, monkeypatch):   # Codex P2
    monkeypatch.delenv("CODEX_COMPANION_CMD", raising=False)
    monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)
    (tmp_path / "scripts").mkdir()
    comp = tmp_path / "scripts" / "codex-companion.mjs"
    comp.write_text("//")
    monkeypatch.setenv("CLAUDE_PLUGIN_ROOT", str(tmp_path))
    assert g.resolve_companion_cmd() == ["node", str(comp)]


def test_markers_are_per_session(tmp_path, monkeypatch):   # Codex P2: сессии не затирают друг друга
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "A")
    g.write_marker("design", "a", "h")
    assert g.has_marker("A") is True
    monkeypatch.setenv("CLAUDE_SESSION_ID", "B")
    g.clear_marker()                    # старт сессии B чистит СВОЙ, не A
    assert g.has_marker("A") is True    # маркер A не тронут
    assert g.has_marker("B") is False


def test_bash_gates_quoted_redirect_target():   # Codex P2: кавычки в цели редиректа/tee
    assert g.bash_touches_code('echo x > "app/generated.py"') is True
    assert g.bash_touches_code("tee 'tests/out.py'") is True
    assert g.bash_touches_code('echo x > "/tmp/log"') is False   # не-код в кавычках — не блок


def test_resolve_baseline_env_precedence(tmp_path, monkeypatch):   # Codex P2
    monkeypatch.setattr(g, "LAST_DEPLOYED", tmp_path / ".last-deployed-sha")
    (tmp_path / ".last-deployed-sha").write_text("stale-sha\n")
    monkeypatch.setenv("CODEX_DEPLOY_BASELINE", "override-sha")
    assert g.resolve_baseline() == "override-sha"   # явный env перебивает протухший файл


def test_run_companion_missing_plugin_returns_none(monkeypatch):   # Codex P2: outage, не traceback
    monkeypatch.delenv("CODEX_COMPANION_CMD", raising=False)
    monkeypatch.delenv("CODEX_PLUGIN_ROOT", raising=False)
    monkeypatch.setattr(g.glob, "glob", lambda *_a, **_k: [])   # плагин не найден
    assert g.run_companion_review(base=None, scope="auto") is None


def test_env_session_prefers_code_var(monkeypatch):   # фикс: CLAUDE_CODE_SESSION_ID приоритетнее
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "real-id")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "legacy-id")
    assert g._env_session() == "real-id"
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
    assert g._env_session() == "legacy-id"   # легаси-фолбэк


def test_gate_bash_allows_stderr_redirect_diagnostic(tmp_path, monkeypatch):
    # R1-5 false-positive fix: read-only диагностика с 2>&1 рядом с код-путём — НЕ мутация
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved")
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")   # сессия известна, маркера нет
    ro = json.dumps({"session_id": "s1",
                     "tool_input": {"command": "python3 scripts/x.py 2>&1 | grep app/y.py"}})
    assert g.gate_bash_cli(ro) == 0   # только stderr-редирект → не блок
    wr = json.dumps({"session_id": "s1",
                     "tool_input": {"command": "echo x > app/y.py"}})
    assert g.gate_bash_cli(wr) == 2   # реальный редирект в код-путь всё ещё ловится


# --- Task 5: check_reviewed_cli реорганизация — ladder-range + два независимых SKIP (спека §4) ---
def test_check_reviewed_ladder_uncovered_blocks(tmp_path, monkeypatch):   # (а)
    # валидный Codex-стаб pass, но ladder-range не покрыт → блок (ladder независим от Codex)
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path); _clean(monkeypatch)
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 2)
    monkeypatch.setenv("CODEX_COMPANION_CMD", " ".join(_stub("stub_companion_pass.sh")))
    assert g.check_reviewed_cli() == 2


def test_check_reviewed_codex_skip_does_not_bypass_ladder(tmp_path, monkeypatch):   # (б)
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path); _clean(monkeypatch)
    monkeypatch.setattr(g, "_ladder_check", lambda baseline: 2)
    monkeypatch.setenv("CODEX_REVIEW_SKIP", "1")
    assert g.check_reviewed_cli() == 2   # CODEX_REVIEW_SKIP не пропускает ladder-проверку


def test_check_reviewed_ladder_skip_does_not_bypass_codex(tmp_path, monkeypatch):   # (в)
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path); _clean(monkeypatch)
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setenv("LADDER_SKIP", "1")
    monkeypatch.setenv("CODEX_COMPANION_CMD", " ".join(_stub("stub_companion_block.sh")))
    assert g.check_reviewed_cli() == 2   # LADDER_SKIP не пропускает Codex-часть
    assert "LADDER_SKIP" in (tmp_path / "audit.log").read_text()


def test_check_reviewed_both_skips_full_bypass(tmp_path, monkeypatch):   # (г)
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path)
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit.log")
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "resolve_baseline", lambda: None)   # оба скипа — даже без baseline
    monkeypatch.setenv("LADDER_SKIP", "1")
    monkeypatch.setenv("CODEX_REVIEW_SKIP", "1")
    assert g.check_reviewed_cli() == 0
    audit_text = (tmp_path / "audit.log").read_text()
    assert "LADDER_SKIP" in audit_text
    assert "CODEX_REVIEW_SKIP" in audit_text


def test_dependency_manifests_are_code_paths():   # Codex final P2: манифесты ставятся в прод
    for p in ("requirements.txt", "requirements-dev.txt", "pyproject.toml"):
        assert g.is_code_path(p) is True, p


# ═══════════ Протокол сходимости (Фаза 1.6, спека 2026-07-22-review-convergence) ═══════════
@pytest.fixture()
def led(tmp_path, monkeypatch):
    monkeypatch.setattr(g, "FINDINGS_DIR", tmp_path / "rf")
    return tmp_path


def test_prefix_parse():
    assert g.parse_finding_prefix("[DUP:F3] same thing") == ("dup", "F3", "same thing")
    assert g.parse_finding_prefix("[DISPUTE:F1] new evidence") == ("dispute", "F1", "new evidence")
    assert g.parse_finding_prefix("plain finding") == (None, None, "plain finding")


def test_merge_round_new_findings_open(led):
    L = g.load_findings_ledger("base123")
    g.merge_round(L, [("high", "Bridge masks spend"), ("critical", "Money leak")])
    assert L["rounds"] == 1
    st = {f["status"] for f in L["findings"].values()}
    assert st == {"open"} and len(L["findings"]) == 2


def test_merge_dup_of_open_links_and_original_stays_open(led):
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Original issue")])          # F1 open
    g.merge_round(L, [("high", "[DUP:F1] restated")])
    dups = [f for f in L["findings"].values() if f["status"] == "duplicate"]
    assert len(dups) == 1 and dups[0]["dup_of"] == "F1"
    assert L["findings"]["F1"]["status"] == "open"           # оригинал блокирует дальше


def test_merge_dup_of_residual_is_reraise_dispute(led):     # спека R1: DUP ≠ согласие
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Edge case")])                # F1
    g.adjudicate(L, "F1", "residual-failsafe", "пере-блок, не пропуск")
    g.merge_round(L, [("high", "[DUP:F1] again")])
    assert L["findings"]["F1"]["status"] == "open"
    assert L["findings"]["F1"]["disputes"] == 1


def test_decision_block_allow_escalate(led):
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Issue A")])
    assert g.convergence_decision(L)[0] == "block"           # open → блок
    g.adjudicate(L, "F1", "refuted", "эмпирика: тест X зелёный")
    assert g.convergence_decision(L)[0] == "allow"           # нет open → сошлись
    g.merge_round(L, [("high", "[DISPUTE:F1] contested")])   # спор по high → человек
    assert g.convergence_decision(L)[0] == "escalate"


def test_decision_hardcap_escalates(led):
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Stuck issue")])
    L["rounds"] = 9                                          # > HARD_CAP_ROUNDS
    assert g.convergence_decision(L)[0] == "escalate"


def test_adjudicate_guards(led):
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("critical", "Direct money loss")])
    with pytest.raises(g.AdjudicationError):                 # ML-C1: critical не residual
        g.adjudicate(L, "F1", "residual-failsafe", "reason")
    with pytest.raises(g.AdjudicationError):                 # причина обязательна
        g.adjudicate(L, "F1", "refuted", "")
    with pytest.raises(g.AdjudicationError):                 # неизвестный id
        g.adjudicate(L, "F99", "fixed", "x")
    g.adjudicate(L, "F1", "resolved-by-user", "решение пользователя")   # человеку можно всё
    assert g.convergence_decision(L)[0] == "allow"


def test_ledger_corrupt_fail_closed(led):
    d = g.FINDINGS_DIR
    d.mkdir(parents=True)
    (d / "current.json").write_text("{broken")
    assert g.load_findings_ledger("b") is None               # ML-C3 → caller блокирует


def test_ledger_persist_roundtrip(led):
    L = g.load_findings_ledger("base-x")
    g.merge_round(L, [("high", "Persisted issue")])
    g.save_findings_ledger(L)
    L2 = g.load_findings_ledger("base-x")
    assert L2["findings"]["F1"]["title"] == "Persisted issue"
    assert L2["baseline"] == "base-x"


def test_merge_dup_of_fixed_is_reraise(led):     # протокол-догфуд F1
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Claimed-fixed issue")])
    g.adjudicate(L, "F1", "fixed", "коммит abc")
    g.merge_round(L, [("high", "[DUP:F1] still broken")])
    assert L["findings"]["F1"]["status"] == "open"       # улика: фикс не сработал
    assert L["findings"]["F1"]["disputes"] == 1


def test_ledger_archived_on_new_baseline(led):   # протокол-догфуд F2
    L = g.load_findings_ledger("base-A")
    g.merge_round(L, [("high", "Old series issue")])
    g.adjudicate(L, "F1", "refuted", "устарело")
    g.save_findings_ledger(L)
    L2 = g.load_findings_ledger("base-B")        # новая серия → архив старой
    assert L2["findings"] == {} and L2["baseline"] == "base-B"
    assert list((g.FINDINGS_DIR / "archive").glob("*.json"))   # старая заархивирована


def test_findings_lock_is_exclusive(led):        # догфуд F3
    import fcntl
    with g.findings_lock():
        lf = open(g.FINDINGS_DIR / ".lock", "w")
        with pytest.raises(BlockingIOError):     # второй эксклюзивный не берётся
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lf.close()


def test_cached_clean_review_respects_open_findings(led, tmp_path, monkeypatch):
    # Codex-спор F3: валидный кэш чистого ревью НЕ обходит open-находку серии
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path / "cl")
    monkeypatch.setattr(g, "LAST_REVIEWED", tmp_path / ".lr")
    monkeypatch.setattr(g, "working_tree_clean", lambda: True)
    monkeypatch.setattr(g, "resolve_baseline", lambda: "HEAD~1")
    v = g.parse_review_output("Verdict: approve\nNo material findings.\n")
    g.write_ledger(g.git_head(), g.diff_sha256("HEAD~1"), "HEAD~1", v)   # кэш чистый
    L = g.load_findings_ledger("HEAD~1")
    g.merge_round(L, [("high", "Concurrent finding")])                   # конкурентная запись
    g.save_findings_ledger(L)
    assert g.check_reviewed_cli() == 2                                   # кэш не обходит


def test_accepted_dispute_does_not_deadlock(led):    # принятый спор + фикс ≠ вечный escalate
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Race issue")])
    g.merge_round(L, [("high", "[DISPUTE:F1] evidence")])   # спор → open, d=1
    assert g.convergence_decision(L)[0] == "escalate"
    g.adjudicate(L, "F1", "fixed", "спор принят, пофикшено")
    assert g.convergence_decision(L)[0] == "allow"          # фикс снимает эскалацию
    g.merge_round(L, [("high", "[DISPUTE:F1] again")])      # d=2, open
    g.adjudicate(L, "F1", "fixed", "снова пофикшено")
    g.merge_round(L, [("high", "[DISPUTE:F1] third")])      # d=3 → жёсткое несогласие
    g.adjudicate(L, "F1", "fixed", "и снова")
    assert g.convergence_decision(L)[0] == "escalate"       # ≥3 споров → человек, даже fixed


def test_adjudication_requires_review_round(led):    # спор F3-2: кэш не минует показ Codex'у
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Issue")])
    g.adjudicate(L, "F1", "refuted", "опровергнуто")
    assert L.get("needs_review_round") is True              # Codex ещё не видел
    g.merge_round(L, [])                                     # реальный раунд показал
    assert L.get("needs_review_round") is False


def test_dup_reraise_escalates_severity(led):    # спор F1-2: critical-DUP повышает оригинал
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Issue")])
    g.adjudicate(L, "F1", "residual-failsafe", "трение")
    g.merge_round(L, [("critical", "[DUP:F1] actually money loss")])
    assert L["findings"]["F1"]["severity"] == "critical"    # эскалировано
    with pytest.raises(g.AdjudicationError):                # residual теперь запрещён (ML-C1)
        g.adjudicate(L, "F1", "residual-failsafe", "x")


def test_resolved_by_user_is_terminal(led):      # решение человека закрывает и d>=3
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Contested race")])
    for _ in range(3):
        g.merge_round(L, [("high", "[DISPUTE:F1] again")])
    assert g.convergence_decision(L)[0] == "escalate"       # d=3 → человек
    g.adjudicate(L, "F1", "resolved-by-user", "юзер: фиксим deploy-lock'ом")
    assert g.convergence_decision(L)[0] == "allow"          # решение терминально


def test_check_decision_cli(led, monkeypatch, capsys):       # ревалидация перед rsync
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Late finding")])
    g.save_findings_ledger(L)
    assert g.main(["check-decision"]) == 2                   # open → устарело → блок
    with g.findings_lock():
        L = g.load_findings_ledger(None)
        g.adjudicate(L, "F1", "refuted", "проверено")
        g.save_findings_ledger(L)
    assert g.main(["check-decision"]) == 2                   # адъюдикация не показана Codex
    with g.findings_lock():
        L = g.load_findings_ledger(None)
        g.merge_round(L, [])                                 # раунд показал
        g.save_findings_ledger(L)
    assert g.main(["check-decision"]) == 0                   # сошлись → ок


def test_resolved_by_user_not_reopenable(led):   # F3 d=5: финальность человека
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Race")])
    g.adjudicate(L, "F1", "resolved-by-user", "юзер решил")
    g.merge_round(L, [("high", "[DISPUTE:F1] still racy")])
    assert L["findings"]["F1"]["status"] == "resolved-by-user"   # не пере-открыт
    assert g.convergence_decision(L)[0] == "allow"
    g.merge_round(L, [("high", "[DUP:F1] again")])               # и DUP не пере-открывает
    assert L["findings"]["F1"]["status"] == "resolved-by-user"


def test_stale_review_does_not_clear_flag(led):  # спор F3-3: ts-guard
    import time
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Issue")])
    t_before_adj = time.time() - 10
    g.adjudicate(L, "F1", "refuted", "проверено")
    g.merge_round(L, [], review_started_ts=t_before_adj)   # старый review (стартовал ДО)
    assert L.get("needs_review_round") is True             # флаг НЕ очищен
    g.merge_round(L, [], review_started_ts=time.time())    # свежий review
    assert L.get("needs_review_round") is False


def test_dispute_reraise_escalates_severity(led):    # спор F1-3: симметрия с DUP
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Issue")])
    g.adjudicate(L, "F1", "refuted", "x")
    g.merge_round(L, [("critical", "[DISPUTE:F1] actually critical")])
    assert L["findings"]["F1"]["severity"] == "critical"
    with pytest.raises(g.AdjudicationError):
        g.adjudicate(L, "F1", "residual-failsafe", "y")


def test_unknown_severity_banned_from_residual(led):   # F4
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("p0", "Unknown-severity money issue")])
    with pytest.raises(g.AdjudicationError):
        g.adjudicate(L, "F1", "residual-failsafe", "x")


def test_check_decision_honors_emergency_skip(led, monkeypatch):   # F5: ML6-путь жив
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("high", "Open issue")])
    g.save_findings_ledger(L)
    monkeypatch.setenv("CODEX_REVIEW_SKIP", "1")
    monkeypatch.setattr(g, "AUDIT_LOG", g.FINDINGS_DIR / "a.log")
    assert g.main(["check-decision"]) == 0


def test_structurally_corrupt_ledger_fail_closed(led):   # F7
    d = g.FINDINGS_DIR
    d.mkdir(parents=True, exist_ok=True)
    for bad in ('{"baseline":"b","rounds":1,"findings":[]}',           # findings-список
                '{"baseline":"b","rounds":"x","findings":{}}',          # rounds не int
                '{"baseline":"b","rounds":1,"findings":{"F1":{"status":5}}}'):  # status не str
        (d / "current.json").write_text(bad)
        assert g.load_findings_ledger("b") is None, bad


def test_unknown_status_in_ledger_fail_closed(led):   # спор F7-2
    d = g.FINDINGS_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "current.json").write_text(
        '{"baseline":"b","rounds":1,"findings":{"F1":{"status":"opne","severity":"high"}}}')
    assert g.load_findings_ledger("b") is None


def test_ledger_without_baseline_fail_closed(led):   # спор F7-3
    d = g.FINDINGS_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "current.json").write_text('{"rounds":1,"findings":{}}')
    assert g.load_findings_ledger("b") is None
    (d / "current.json").write_text('{"baseline":"","rounds":1,"findings":{}}')
    assert g.load_findings_ledger("b") is None


# --- Carry-over (реш. юзера 22.07): пост-hard-cap находки не блокируют срочный деплой ---
def test_carry_over_new_high_post_hardcap(led):
    L = g.load_findings_ledger("b")
    L["rounds"] = 9                                       # за hard-cap
    g.merge_round(L, [("high", "Late nuance")])           # rounds → 10, новая находка
    g.apply_carry_over(L)
    assert L["findings"]["F1"]["status"] == "carried"
    assert g.convergence_decision(L)[0] == "allow"        # деплой едет


def test_carry_over_skips_critical_and_disputed(led):
    L = g.load_findings_ledger("b")
    L["rounds"] = 9
    g.merge_round(L, [("critical", "Money loss"), ("high", "Nuance")])
    g.adjudicate(L, "F2", "refuted", "x")
    g.merge_round(L, [("high", "[DISPUTE:F2] contested")])   # спорная
    g.apply_carry_over(L)
    assert L["findings"]["F1"]["status"] == "open"        # critical НЕ переносится
    assert L["findings"]["F2"]["status"] == "open"        # спорная НЕ переносится


def test_carried_seeds_next_series_open(led):
    L = g.load_findings_ledger("base-A")
    L["rounds"] = 9
    g.merge_round(L, [("high", "Carried issue")])
    g.apply_carry_over(L)
    g.save_findings_ledger(L)
    L2 = g.load_findings_ledger("base-B")                 # новая серия
    f = L2["findings"]["F1"]
    assert f["status"] == "open" and f["carried_from"] == "base-A" and f["carry_count"] == 1
    assert g.convergence_decision(L2)[0] == "block"       # следующий деплой блокируется


def test_carry_rot_escalates_after_two_series(led):
    L = g.load_findings_ledger("b")
    L["findings"]["F1"] = {"severity": "high", "title": "Rotting", "status": "open",
                            "dup_of": None, "disputes": 0, "round": 0,
                            "carried_from": "old", "carry_count": 2}
    assert g.convergence_decision(L)[0] == "escalate"     # 2 серии → человек


def test_dup_does_not_downgrade_unknown_severity(led):   # спор F4-2
    L = g.load_findings_ledger("b")
    g.merge_round(L, [("p0", "Unknown-sev issue")])
    g.merge_round(L, [("high", "[DUP:F1] known-sev restatement")])
    assert L["findings"]["F1"]["severity"] == "p0"       # НЕ понижено
    with pytest.raises(g.AdjudicationError):             # residual всё ещё запрещён
        g.adjudicate(L, "F1", "residual-failsafe", "x")


def test_adjudicated_without_reason_fail_closed(led):    # спор F7-4
    d = g.FINDINGS_DIR
    d.mkdir(parents=True, exist_ok=True)
    (d / "current.json").write_text(
        '{"baseline":"b","rounds":1,"findings":{"F1":{"status":"refuted","severity":"high"}}}')
    assert g.load_findings_ledger("b") is None
