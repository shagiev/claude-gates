import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))
import deploy_gates as dg  # noqa: E402


def test_make_command_line_cannot_disable_bytecode_prevention(tmp_path):
    probe = tmp_path / "probe.mk"
    probe.write_text(
        f"include {ROOT / 'Makefile'}\n"
        "prevention-probe:\n"
        "\t@python3 -c 'import os; "
        "print(os.environ.get(\"PYTHONDONTWRITEBYTECODE\", \"missing\"))'\n")
    result = subprocess.run(
        ["make", "--no-print-directory", "-f", str(probe),
         "prevention-probe", "PYTHONDONTWRITEBYTECODE="],
        cwd=ROOT, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "1"


def test_all_make_python_recipes_use_configured_interpreter_with_dash_b():
    recipes = [line.strip() for line in (ROOT / "Makefile").read_text().splitlines()
               if line.startswith("\t") and ("python3" in line or "$(PYTHON)" in line)]
    assert recipes
    for recipe in recipes:
        assert "$(PYTHON) -B" in recipe, recipe
        assert "python3 -B" not in recipe, recipe


@pytest.mark.parametrize("path,anchor", [
    ("scripts/deploy_gates.py", "import codex_review_gate as g"),
    ("scripts/recertify.py", "import certify_reviewers as cr"),
    ("scripts/kimi_acceptance.py", "def directory_digest"),
])
def test_direct_entrypoints_force_prevention_before_local_code(path, anchor):
    text = (ROOT / path).read_text()
    assignment = 'os.environ["PYTHONDONTWRITEBYTECODE"] = "1"'
    assert "sys.dont_write_bytecode = True" in text
    assert assignment in text, "setdefault сохраняет пустое env значение и отключает защиту"
    assert text.index("sys.dont_write_bytecode = True") < text.index(anchor)
    assert text.index(assignment) < text.index(anchor)
    assert "setdefault(\"PYTHONDONTWRITEBYTECODE\"" not in text


def _project_pyc(root: Path):
    return [p for p in root.rglob("*.pyc") if str(ROOT) in str(p)]


def test_direct_deploy_status_writes_no_bytecode_with_empty_env(tmp_path):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="", PYTHONPYCACHEPREFIX=str(tmp_path))
    result = subprocess.run([sys.executable, str(ROOT / "scripts/deploy_gates.py"), "--status"],
                            cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert _project_pyc(tmp_path) == []


def test_direct_recertify_help_writes_no_bytecode_with_empty_env(tmp_path):
    env = dict(os.environ, PYTHONDONTWRITEBYTECODE="", PYTHONPYCACHEPREFIX=str(tmp_path))
    result = subprocess.run([sys.executable, str(ROOT / "scripts/recertify.py"), "--help"],
                            cwd=ROOT, env=env, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert _project_pyc(tmp_path) == []


def test_deploy_pytest_child_has_dash_b():
    text = (ROOT / "scripts/deploy_gates.py").read_text()
    assert '[sys.executable, "-B", "-m", "pytest", "tests/", "-q"]' in text


def test_remote_agent_has_dash_b(monkeypatch):
    seen = []
    monkeypatch.setattr(dg, "_AGENT_SRC", "print('{}')")
    monkeypatch.setattr(dg, "on_host", lambda host, argv, stdin=None: seen.append(argv)
                        or subprocess.CompletedProcess(argv, 0, "{}\n", ""))
    assert dg.agent("example.invalid", "status", "{}") == {}
    assert seen and seen[0][:3] == ["python3", "-B", "-"]
