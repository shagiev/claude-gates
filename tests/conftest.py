"""Общая обвязка портированных тестов гейтов (спека плагина, раздел «Тесты»).

sys.path: скрипты живут в plugins/gates/scripts (не в пакете `scripts` целевого репо,
как в проекте-источнике) — тесты импортируют их напрямую.
"""
import json
import os
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


#: CLI, мутирующие установку плагина на РЕАЛЬНОЙ машине. Тесты уже ломали установку четыре
#: раза: трижды снесли плагин целиком, один раз переписали реестр значениями фикстуры
#: (23–24.08.2026): сперва потому, что спек отката без явной цели бил в текущий `~`, потом —
#: потому, что регресс-тест на сам страж исполнял разрушительную команду по-настоящему.
_FORBIDDEN_CLI = {"claude", "codex"}

#: ГРАНИЦА — аллоулист формы, а не денилист написаний. Каталог со стабами встаёт первым в PATH
#: НА ИМПОРТЕ (не в фикстуре: на этапе сбора тестов фикстур ещё нет, а `subprocess` там уже
#: зовут) И подставляется в окружение КАЖДОГО потомка, даже если тест заменил PATH целиком.
#: Стабы отказывают всегда, поэтому перекрыты формы, до которых разбор argv не достаёт в
#: принципе: внуки (`python3 -` → агент → `claude plugin …`), `sh -c`, `env`-обёртки,
#: подстановки, симлинк с другим именем. Тесту, которому нужен рабочий подставной CLI, кладёт
#: свой каталог раньше в PATH И СОХРАНЯЕТ там `os.environ["PATH"]` (см. фикстуру `fake_host`):
#: наш каталог доклеивается всегда, когда его в PATH нет, поэтому PATH, собранный с нуля,
#: получит стаб впереди собственного фейка.
#:
#: ⚠️ ЧЕГО ГРАНИЦА НЕ ЗАКРЫВАЕТ, и это надо знать, а не думать, что закрыто:
#:  • `bash -lc` — login-профиль ПЕРЕСОБИРАЕТ PATH (`~/.profile` на дев-сервере кладёт
#:    `~/.local/bin` впереди), и там лежат настоящие бинари;
#:  • удалённые хосты — на них `tests/stub_bin` не существует; ssh-вектор закрыт отдельно,
#:    инертной подменой `deploy_gates.on_host` ниже;
#:  • боевой код, который РЕЗОЛВИТ АБСОЛЮТНЫЙ путь мимо PATH (`_resolve_claude_bin`) или
#:    задаёт свой `PATH` константой — от него тесты держит подмена резолверов ниже.
_STUB_BIN = Path(__file__).resolve().parent / "stub_bin"
os.environ["PATH"] = f"{_STUB_BIN}{os.pathsep}{os.environ.get('PATH', '')}"


def _mentions_forbidden_cli(argv) -> "str | None":
    """Дешёвая растяжка ПОВЕРХ границы: ловит прямую форму `[…, "claude", "plugin", …]` и даёт
    внятную ошибку сразу, вместо `rc=97` из стаба где-то в глубине.

    Намеренно смотрит ТОЛЬКО элементы argv и не разбирает строки команд: разбор свободного
    текста уже ронял четыре давних теста — промпт ревьюера содержит слова «claude plugin».
    Полноту обеспечивает PATH, а не это правило."""
    if isinstance(argv, (str, bytes, Path)):
        return None
    names = [Path(a.decode("utf-8", "replace") if isinstance(a, bytes) else str(a)).name
             for a in argv if isinstance(a, (str, bytes, Path))]
    for i, cli in enumerate(names):
        # `plugin` ищется ДАЛЬШЕ по argv, а не строго следующим: `["claude","--debug","plugin"]`
        # проходил мимо. Только элементы argv — свободный текст промпта так не разбирается.
        if cli in _FORBIDDEN_CLI and "plugin" in names[i + 1:]:
            return cli
    return None


@pytest.fixture(autouse=True)
def _no_real_plugin_cli(monkeypatch):
    import subprocess as _sp
    real = _sp.Popen

    class Guarded(real):
        def __init__(self, argv, *a, **k):
            if not isinstance(argv, (str, bytes, Path)):
                argv = list(argv)          # генератор нельзя вычерпать и отдать дальше пустым
            # Граница едет ВМЕСТЕ С РЕБЁНКОМ. База — `os.environ`, когда `env` не передан:
            # `monkeypatch.setenv("PATH", …)` вычищает наш каталог ИЗ `os.environ`, и потомок с
            # `env=None` наследовал уже очищенный PATH (так делают три теста набора).
            # Подстановка каталога, где только два инертных стаба, ничего кроме этих двух имён
            # затенить не может.
            base = k.get("env")
            base = dict(os.environ) if base is None else dict(base)
            if str(_STUB_BIN) not in (base.get("PATH") or "").split(os.pathsep):
                base["PATH"] = f"{_STUB_BIN}{os.pathsep}{base.get('PATH', '')}"
            k["env"] = base
            name = _mentions_forbidden_cli(argv)
            if name:
                home = (k.get("env") or {}).get("HOME") or os.environ.get("HOME", "")
                if not home or Path(home).resolve() == Path(os.path.expanduser("~")).resolve():
                    raise AssertionError(
                        f"тест запускает {name} plugin против настоящего ~ ({home}) — "
                        "установка плагина на этой машине изменилась бы по-настоящему")
            super().__init__(argv, *a, **k)

    monkeypatch.setattr(_sp, "Popen", Guarded)


@pytest.fixture(autouse=True)
def _no_remote_mutation(monkeypatch):
    """ssh-вектор: `deploy_gates.on_host` увозит на хост `python3 - restore …`, и уже ВНУК там
    зовёт `claude plugin uninstall`. Граница локальна, растяжка видит только `python3 -`, а в
    инвентаре стоит реально достижимый `smartape-vps-1`. Единственной защитой оставалась
    дисциплина «каждый тест сам мокает on_host» — то есть забытый мок мутировал бы боевой хост.

    Инертная подмена делает забытый мок невозможным; тесту, которому нужен транспорт, подменяет
    его сам (фикстура `fake_host` так и делает).

    Два кавета, чтобы формулировка не была шире факта: (а) фикстура — no-op, если
    `deploy_gates` не импортирован (прогон одного файла помимо `test_deploy.py`); (б) закрыт
    транспорт ДЕПЛОЯ, а не ssh вообще — прямой `subprocess.run(["ssh", host, "claude plugin …"])`
    из произвольного теста не ловит никто: растяжка строку команды не разбирает (разбор
    свободного текста ронял четыре давних теста), а на удалённом хосте стабов нет."""
    dg = sys.modules.get("deploy_gates")
    if dg is None:
        return

    def refuse(host, argv, stdin=None):
        raise AssertionError(
            f"тест зовёт on_host({host!r}, …) без подмены — на реальный хост уехала бы "
            f"мутация установки плагина: {' '.join(map(str, argv))[:120]}")

    monkeypatch.setattr(dg, "on_host", refuse)


@pytest.fixture(autouse=True)
def _no_real_install_mutation(monkeypatch, tmp_path):
    """Мутация установки БЕЗ subprocess — маршрут, который не видит ни один из трёх слоёв.

    `_installed_claude` читает реестр ЧИСТЫМ ЧТЕНИЕМ и честно возвращает боевой адрес
    (`registry=~/.claude/plugins/installed_plugins.json`); `cmd_restore` затем в него пишет, а
    `cmd_verify` идёт `rmtree`'ом по `__pycache__` живой установки. Ни `Popen`, ни PATH, ни
    `on_host` тут не участвуют. Проверка «адрес не боевой» в самом `cmd_restore` была бы
    неверна: на хосте писать в боевой реестр — это и есть его работа. Поэтому chokepoint
    ставится со стороны тестов, как уже сделано для `on_host` и резолверов бинарей."""
    home = Path(os.path.expanduser("~")).resolve()

    def _not_production(addr, what):
        if addr and Path(addr).resolve().is_relative_to(home):
            raise AssertionError(f"{what} указывает на НАСТОЯЩУЮ установку: {addr}")

    dv = sys.modules.get("deploy_verify")
    if dv is not None:
        real_installed = dv.installed

        def guarded_installed(channel):
            # Блокируется не вызов, а РЕЗУЛЬТАТ с боевым адресом: тесты самого `installed`
            # законны и работают на поддельном `HOME`, а опасен тот случай, когда функция
            # честно вернула адрес рабочей машины и он поехал дальше в запись.
            got = real_installed(channel)
            for key in ("registry", "toplevel", "path"):
                _not_production(got.get(key), f"installed({channel!r})[{key!r}]")
            return got

        monkeypatch.setattr(dv, "installed", guarded_installed)
        real_restore = dv.cmd_restore

        def guarded_restore(spec):
            snap = (json.loads(spec) or {}).get("snapshot") or {}
            for key in ("registry", "toplevel", "path"):
                _not_production(snap.get(key), f"cmd_restore snapshot[{key!r}]")
            return real_restore(spec)

        monkeypatch.setattr(dv, "cmd_restore", guarded_restore)
        real_drop = dv._drop_stale_bytecode

        def guarded_drop(root):
            # `check_tree` безусловно `rmtree`'ит `__pycache__` под своим аргументом, а он
            # приходит НИ из `installed()`, НИ из спека — то есть мимо остальных слоёв.
            _not_production(root, "_drop_stale_bytecode(root)")
            return real_drop(root)

        monkeypatch.setattr(dv, "_drop_stale_bytecode", guarded_drop)

    dg = sys.modules.get("deploy_gates")
    if dg is not None:
        # Состояние деплоя связывается на импорте с боевым каталогом; забытый патч в тесте,
        # зовущем `main()`, записал бы боевой `stable` — сфабрикованную сертификацию флота.
        monkeypatch.setattr(dg, "STATE", tmp_path / "deploy-state")
        monkeypatch.setattr(dg, "LOCK", tmp_path / "deploy-state" / "lock")
        dg._REAL_DROP_MUX = dg._drop_mux            # чтобы тест проверял ЕГО, а не заглушку
        monkeypatch.setattr(dg, "_drop_mux", lambda hosts: None)   # ходит наружу мимо on_host


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
    # Состояние гейта (счётчик раундов ревью и всё, что рядом) — тоже НЕ боевое: иначе
    # тесты жгли бы реальный бюджет и зависели бы от порядка запуска. Тот же инвариант,
    # что для FINDINGS_DIR/LEDGER_DIR ниже.
    monkeypatch.setattr(g, "_gate_state_dir", lambda: tmp_path / "gate_state")
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
