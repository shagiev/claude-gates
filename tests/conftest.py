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

# Протокольный токен собирается из частей: буквальная строка в фикстурах
# попадала в ревьюируемый дифф и создавала неоднозначность, которую строгий
# парсер обязан отвергать (находка F12).
_VT = "Verd" + "ict:"


@pytest.fixture(autouse=True)
def _gates_test_isolation(monkeypatch, tmp_path):
    # Hook CLI — одноразовый процесс, но unit-тесты делят импортированный модуль. Codex payload.cwd
    # теперь может переключить REPO_ROOT; снапшот через monkeypatch не даёт этому контексту утечь
    # в следующий тест и направить его git-команды/аудит в чужой tmp-репозиторий.
    monkeypatch.setattr(g, "REPO_ROOT", g.REPO_ROOT)
    monkeypatch.setattr(g, "_GATE_CFG", g._GATE_CFG)
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
    # Portable reviewer tests must never inherit real credentials and accidentally spend money.
    # Each adapter test opts in with a synthetic key and a mocked transport.
    for _secret_var in ("GOOGLE_API_KEY", "GEMINI_API_KEY", "XAI_API_KEY"):
        monkeypatch.delenv(_secret_var, raising=False)
    monkeypatch.delenv("GEMINI_REVIEW_MODEL", raising=False)
    # Та же защита от случайного live-spend, что CODEX_COMPANION_CMD ниже: тест, забывший
    # подменить Claude adapter, обязан получить deterministic unavailable, а не запустить CLI.
    monkeypatch.setattr(g, "_resolve_claude_bin", lambda: None)
    monkeypatch.setattr(g, "_resolve_cursor_bin", lambda: None)
    # В проде git закреплён абсолютным путём и санированным окружением (F19/F22). Тесты же
    # подменяют subprocess.run и опознают вызов по имени `git`, поэтому здесь _trusted_git
    # делегирует привычной форме — иначе пришлось бы переписывать все фейки, не проверяя
    # ничего нового. Сам хардненинг проверяется точечными тестами F19/F22.
    monkeypatch.setattr(g, "_trusted_git", lambda *args, cwd=None: g.subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else g.REPO_ROOT,
        capture_output=True, text=True))
    monkeypatch.setattr(g, "_trusted_git_bytes", lambda *args, cwd=None: g.subprocess.run(
        ["git", *args], cwd=str(cwd) if cwd else g.REPO_ROOT, capture_output=True))
    # Легаси-значения (codex|cursor|both) сняты с деплой-пути: панель Codex+Claude обязательна
    # и переменной окружения не понижается (§4 дизайна 2026-08-07). Дефолт тестов — portable.
    monkeypatch.setenv("REVIEW_PROVIDER", "portable")
    # см. F11: resolved-by-user принимается только из интерактивного терминала;
    # тестам протокола он эмулируется, запрет проверяется точечным тестом.
    monkeypatch.setattr(g.sys.stdin, "isatty", lambda: True)
    # ИНЦИДЕНТ 2026-07-25: FINDINGS_DIR/LEDGER_DIR были изолированы только в фикстуре
    # ОДНОГО тест-файла — новый тест-файл без локального мока писал в БОЕВОЙ
    # logs/review_findings (создал мусорную серию с critical-находкой из фикстуры, уронил
    # 7 соседних тестов). Инвариант «тесты не трогают боевые ledger'ы» — в ОБЩЕМ chokepoint.
    monkeypatch.setattr(g, "FINDINGS_DIR", tmp_path / "rf_conftest")
    monkeypatch.setattr(g, "LEDGER_DIR", tmp_path / "ledger_conftest")
    # Резолв companion не должен зависеть от машины. ВАЖНО: CODEX_COMPANION_CMD именно
    # ВЫСТАВЛЯЕТСЯ в инертное значение, а не удаляется. Удаление снимало бы последний барьер:
    # тест без своего мока провалился бы в глоб кэша, нашёл РЕАЛЬНЫЙ codex-companion.mjs и
    # запустил живое ревью — реальные траты и вис до _REVIEW_TIMEOUT_S (900 с). Инертная
    # команда делает забытый мок невозможным (правило «тесты не ходят в production-сервисы»).
    monkeypatch.setenv("CODEX_COMPANION_CMD", "bash -c 'exit 99'")
    # Blocking-путь СОЗНАТЕЛЬНО игнорирует CODEX_COMPANION_CMD (иначе вызывающий подставляет
    # approve-шим вместо обязательного Codex). Значит инертной переменной уже недостаточно:
    # подменяем сам резолвер — это атрибут модуля, а не окружение, поэтому агент им управлять
    # не может, а тест забыть мок — не должен.
    _real_resolve = g.resolve_companion_cmd
    monkeypatch.setattr(
        g, "resolve_companion_cmd",
        lambda **kw: (["bash", "-c", "exit 99"] if kw.get("allow_env_override") is False
                      else _real_resolve(**kw)))
    for _root_var in ("CODEX_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        monkeypatch.delenv(_root_var, raising=False)   # override выше их и так перебивает
    # pre-push гейт: ambient INTEGRATION_* (напр. из ручного `INTEGRATION_SKIP=1 git push`)
    # не должен контаминировать тесты — тот же класс, что EMPIRICAL_SKIP/LADDER_SKIP выше.
    for _v in ("INTEGRATION_SKIP", "INTEGRATION_SKIP_REASON", "INTEGRATION_TESTS_SKIP",
               "INTEGRATION_TESTS_SKIP_REASON", "INTEGRATION_CONFIG_CHANGE",
               "INTEGRATION_CONFIG_CHANGE_REASON"):
        monkeypatch.delenv(_v, raising=False)
    # inframon-интерфейс: pin/вердикты — в tmp (не трогать боевые), range_skips детерминирован
    monkeypatch.setattr(g, "DEPLOY_PIN", tmp_path / ".deploy-section-pin")
    monkeypatch.setattr(g, "VERDICT_DIR", tmp_path / "verdicts")
    monkeypatch.setattr(g, "_ladder_range_skips", lambda baseline: [])
    monkeypatch.delenv("CODEX_DEPLOY_BASELINE", raising=False)


@pytest.fixture()
def clean_pair(monkeypatch):
    """Обязательная blocking-пара (Codex+Claude) отрабатывает чисто.

    Нужна тестам деплой-пути, которые проверяют НЕ ревью, а его последствия (вердикт, ledger,
    reviewed-sha). Раньше им хватало одного shell-стаба companion; теперь панель обязательна,
    поэтому «чисто» должны отработать ОБА — иначе гейт честно блокирует (§3)."""
    clean = f"{_VT} approve\n\nNo material findings.\n"

    def _run(cert, _base, _head):
        return g.ReviewerRun("blocking", cert.provider, cert.requested_model,
                             cert.actual_models, cert.family, cert.certification_id,
                             "ok", verdict=g.parse_review_output(clean))

    monkeypatch.setattr(g, "run_certified_reviewer", _run)
    return _run
