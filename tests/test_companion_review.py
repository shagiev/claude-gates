"""Подкоманда `companion-review` — единственный внешний вход к движку ревью.

Мотив (ревью B2, 2026-07-26): предыдущая попытка выставляла наружу РЕЗОЛВЕР (`companion-path`
печатала argv, а скилл пересобирал команду шеллом). Это (а) обходило редакцию — в argv может
лежать `--api-key=…`, ср. R5-F2, — и (б) переносило таймаут/fail-closed из тестируемого кода в
непроверяемую прозу скилла. Теперь наружу выставлена ОПЕРАЦИЯ.

Изоляция от живого сервиса — общая фикстура conftest: она ВЫСТАВЛЯЕТ инертный
CODEX_COMPANION_CMD (не удаляет — иначе забытый мок нашёл бы реальный companion глобом кэша),
поэтому тест без своего фейка получит exit 99, а не живой Codex.
"""
import shlex

import codex_review_gate as g


def _fake(monkeypatch, script: str) -> None:
    """Фейковый companion. Квотируем через shlex.join: значение переменной парсится
    shlex.split'ом, и наивный f-string с repr() ломался на кавычках внутри скрипта."""
    monkeypatch.setenv("CODEX_COMPANION_CMD", shlex.join(["bash", "-c", script, "--"]))


def test_passes_flags_and_focus_through_and_forces_wait(monkeypatch, capsys):
    _fake(monkeypatch, 'printf "%s\\n" "$@"')
    rc = g.main(["companion-review", "--base", "abc123", "--scope", "branch", "фокус-текст"])
    out = capsys.readouterr().out
    assert rc == 0
    assert "adversarial-review" in out and "--wait" in out      # --wait навязан гейтом
    assert "--base" in out and "abc123" in out
    assert "--scope" in out and "branch" in out
    assert "фокус-текст" in out


def test_stdout_printed_verbatim(monkeypatch, capsys):
    """Пейлоад НАМЕРЕННО содержит то, что редакция порезала бы (64-hex, `token=`, длинный
    идентификатор): иначе тест пуст — вставка redact_secrets в путь stdout его не роняла
    (найдено ревью 2026-07-26, тот же класс, что `sleep 5 --api-key=…`)."""
    body = ("Finding 1: hash mismatch, expected "
            "9f2b7c1d4e6a8b0c3d5f7a9b1c3e5d7f9a1b3c5e7d9f1a3b5c7e9d1f3a5b7c9e\n"
            "Finding 2: config line `token=abcdefghijklmnopqrstuvwxyz0123456789ABCD` unquoted\n")
    _fake(monkeypatch, "printf %s " + shlex.quote(body))
    assert g.main(["companion-review", "фокус"]) == 0
    out = capsys.readouterr().out
    assert out == body                       # дословно, без редакции
    assert "9f2b7c1d" in out and "token=abcdefgh" in out
    assert g.redact_secrets(body) != body    # тест бессмыслен, если редакция тут не сработала бы


def test_outage_envelope_blocks_despite_exit_zero(monkeypatch, capsys):
    """Инцидент с квотой: companion выходит нулём и отдаёт деградировавший конверт.
    Без этой ветки он читался бы как «замечаний нет» и пропускал дизайн-маркер."""
    envelope = ('{"codex":{"status":0,"stdout":"You have hit your usage limit."},'
                '"result":null,"parseError":"model did not return valid JSON"}')
    _fake(monkeypatch, "printf %s " + shlex.quote(envelope))
    rc = g.main(["companion-review", "фокус"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""                       # конверт не выдаётся за ревью
    assert "ревью НЕ выполнено" in captured.err
    assert "usage limit" in captured.err            # причина видна оператору


def test_empty_output_with_exit_zero_blocks(monkeypatch, capsys):
    _fake(monkeypatch, 'true')
    rc = g.main(["companion-review", "фокус"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "пустой вывод" in captured.err


def test_prose_review_is_not_mistaken_for_outage(monkeypatch, capsys):
    """Дизайн-ревью возвращает ПРОЗУ без Verdict: — она обязана проходить."""
    _fake(monkeypatch, 'printf "The design misses a rollback path for the migration.\\n"')
    assert g.main(["companion-review", "фокус"]) == 0
    assert "rollback path" in capsys.readouterr().out


def test_nonzero_exit_shows_outage_reason_from_stdout(monkeypatch, capsys):
    """Причина отказа рендерится в stdout, а не в stderr: выбрасывать stdout здесь значило бы
    вернуть регрессию «причина outage невидима»."""
    _fake(monkeypatch, 'printf "You have hit your usage limit; resets at 15:00"; exit 3')
    rc = g.main(["companion-review", "фокус"])
    err = capsys.readouterr().err
    assert rc == 2
    assert "exit=3" in err and "usage limit" in err


def test_no_args_is_usage_error(capsys):
    assert g.main(["companion-review"]) == 1
    assert "usage" in capsys.readouterr().err


def test_missing_companion_fails_closed(monkeypatch, capsys):
    """Плагина нет → код 2 и пустой stdout. Прошлая форма давала пустой вывод с кодом 0,
    который читается как «замечаний нет» и пропускал маркер без единого ревью."""
    monkeypatch.delenv("CODEX_COMPANION_CMD", raising=False)   # снять инертный мок conftest
    monkeypatch.setattr(g.glob, "glob", lambda *_a, **_k: [])
    rc = g.main(["companion-review", "фокус"])
    captured = capsys.readouterr()
    assert rc == 2
    assert captured.out == ""
    assert "codex-companion" in captured.err


def test_nonzero_exit_blocks_and_redacts_stderr(monkeypatch, capsys):
    _fake(monkeypatch, 'echo "boom api_key=SUPERSECRETVALUE3" >&2; exit 7')
    rc = g.main(["companion-review", "фокус"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "exit=7" in captured.err
    assert "SUPERSECRETVALUE3" not in captured.err


def test_timeout_does_not_leak_argv_secret(monkeypatch, capsys):
    """R5-F2 для нового пути: текст TimeoutExpired несёт ВЕСЬ argv.

    Фейк ОБЯЗАН реально висеть: `sleep 5 --api-key=…` падает мгновенно («invalid time
    interval»), ветка таймаута не выполняется и тест проходит впустую — этим и был порочен
    прежний регресс R5-F2 (найдено мутацией 2026-07-26). Поэтому `bash -c 'sleep 5'`
    с секретом в ОТДЕЛЬНОМ аргументе, плюс проверка, что ветка действительно та."""
    monkeypatch.setenv("CODEX_COMPANION_CMD", "bash -c 'sleep 5' --api-key=SUPERSECRETVALUE4")
    monkeypatch.setattr(g, "_REVIEW_TIMEOUT_S", 1)
    rc = g.main(["companion-review", "фокус"])
    captured = capsys.readouterr()
    assert rc == 2
    assert "TimeoutExpired" in captured.err        # ветка таймаута реально пройдена
    assert "SUPERSECRETVALUE4" not in captured.err
    assert captured.out == ""


def test_companion_timeout_kills_the_whole_process_group(tmp_path, monkeypatch):
    """`subprocess.run(timeout=)` убивает только ПРЯМОГО потомка. Companion — обёртка на node,
    порождающая свои процессы; они наследуют stdout, и после смерти обёртки `communicate()`
    ждёт закрытия пайпа. Замерено 11.08.2026: ревью висело 46 минут при заявленном потолке
    900 с, то есть «жёсткий потолок» не действовал, а зависшее ревью неотличимо от медленного.
    Тест ПАДАЕТ (висит), если убрать `start_new_session=True`."""
    import time

    script = tmp_path / "hanging-companion.sh"
    script.write_text("#!/bin/sh\nsleep 600 &\nsleep 600\n")     # внук держит stdout
    script.chmod(0o755)
    monkeypatch.setattr(g, "_REVIEW_TIMEOUT_S", 3)
    monkeypatch.setattr(g, "resolve_companion_cmd", lambda **kw: ["/bin/sh", str(script)])

    started = time.monotonic()
    result = g._exec_companion(["adversarial-review"])
    elapsed = time.monotonic() - started

    assert result is None, "таймаут не превратился в отказ"
    assert elapsed < 60, f"висело {elapsed:.0f}s при потолке 3s — группа процессов не убита"
