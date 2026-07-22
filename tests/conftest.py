"""Общая обвязка портированных тестов гейтов (спека плагина, раздел «Тесты»).

sys.path: скрипты живут в plugins/gates/scripts (не в пакете `scripts` целевого репо,
как в проекте-источнике) — тесты импортируют их напрямую.
"""
import sys
from pathlib import Path

import pytest

_SCRIPTS = Path(__file__).resolve().parent.parent / "plugins" / "gates" / "scripts"
if str(_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(_SCRIPTS))

import codex_review_gate as g  # noqa: E402


@pytest.fixture(autouse=True)
def _gates_test_isolation(monkeypatch, tmp_path):
    # Детерминированный конфиг код-путей: тесты не должны зависеть от .codex-gate.yaml
    # репо-носителя (иначе правка конфига плагин-репо ломала бы тест-матрицу).
    monkeypatch.setattr(g, "CODE_PATH_PREFIXES",
                        ("app/", "tests/", "scripts/", "lib/", ".githooks/"))
    monkeypatch.setattr(g, "CODE_PATH_EXACT",
                        {"Dockerfile", "docker-compose.yml", "config.yaml", "Makefile",
                         "requirements.txt", "requirements-dev.txt", "pyproject.toml"})
    monkeypatch.setattr(g, "HARD_CAP_ROUNDS", 8)
    monkeypatch.setattr(g, "ONBOARDED", True)
    # Гигиена (инцидент проекта-источника: тест заархивировал боевую findings-серию): даже тест
    # с забытым точечным моком не должен трогать файлы репо-носителя.
    monkeypatch.setattr(g, "AUDIT_LOG", tmp_path / "audit_auto.log")
    monkeypatch.setattr(g, "LAST_REVIEWED", tmp_path / ".last-reviewed-sha-auto")
    monkeypatch.setattr(g, "LAST_DEPLOYED", tmp_path / ".last-deployed-sha-auto")
    monkeypatch.setattr(g, "DESIGN_MARKER", tmp_path / ".design-approved-auto")
    # ambient EMPIRICAL_SKIP (напр. из ручного `EMPIRICAL_SKIP=1 make deploy`) не должен
    # контаминировать gate-тесты (как LADDER_SKIP/CODEX_REVIEW_SKIP).
    monkeypatch.delenv("EMPIRICAL_SKIP", raising=False)
    # реальный CLAUDE_CODE_SESSION_ID не должен перебивать сессию, которую тест задаёт через
    # CLAUDE_SESSION_ID (иначе _env_session вернёт реальный id и маркер-тесты сломаются).
    monkeypatch.delenv("CLAUDE_CODE_SESSION_ID", raising=False)
