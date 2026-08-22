"""Телеметрия фаз гейта: измеритель обязан быть точным и НЕ ЛОМАТЬ измеряемое.

Дизайн: docs/2026-08-14-gate-telemetry-design.md.
"""
import json
import os
import re
import subprocess
import sys
import time
import textwrap

import pytest

import codex_review_gate as g
import ladder_gate as lg


@pytest.fixture()
def tele_repo(tmp_path, monkeypatch):
    """Репо БЕЗ .gitignore на logs/ — именно в нём видно, пишет ли телеметрия внутрь дерева."""
    r = tmp_path / "r"
    r.mkdir()
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"], "HOME": str(tmp_path)}
    subprocess.run(["git", "init", "-q", "-b", "main", "."], cwd=r, env=env, check=True,
                   capture_output=True)
    (r / "app.py").write_text("x = 1\n")
    subprocess.run(["git", "add", "-A"], cwd=r, env=env, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=r, env=env, check=True,
                   capture_output=True)
    monkeypatch.setattr(g, "REPO_ROOT", r)
    # Каталог состояния уже изолирован autouse-фикстурой conftest (`_gate_state_dir` → tmp).
    # А вот шард кэшируется на ПРОЦЕСС — в бою это верно (процесс одноразовый), но тесты
    # делят процесс, поэтому кэш и стек сбрасываем явно, иначе состояние течёт между тестами.
    monkeypatch.setattr(g, "_TELEMETRY_SHARD", "?")
    g._SPAN_STACK.clear()
    return r


def _events(shard):
    return [json.loads(x) for x in shard.read_text().splitlines() if x.strip()]


# ── S1/S2: событие пишется, и оно НЕ меняет то, что измеряет ────────────────────────────
def test_event_has_closed_schema_and_scale(tele_repo):
    lg.compute_tree(tele_repo)
    ev = _events(g._telemetry_shard())
    assert ev, "событие не записано"
    e = ev[-1]
    assert set(e) == set(g._TELEMETRY_KEYS)
    assert e["phase"] == "compute_tree" and e["outcome"] == "ok"
    assert isinstance(e["scale"], int) and e["scale"] >= 1        # масштаб обязателен


def test_telemetry_does_not_change_the_hash_it_measures(tele_repo):
    """Самая опасная ловушка: `compute_tree` учитывает НЕотслеживаемые файлы, поэтому запись
    внутрь репозитория изменила бы сам хэш, который он в этот момент считает. Репо тут — без
    `.gitignore` на `logs/`, где в бою это маскируется."""
    first = lg.compute_tree(tele_repo)
    second = lg.compute_tree(tele_repo)
    assert first == second
    st = subprocess.run(["git", "status", "--porcelain"], cwd=tele_repo,
                        capture_output=True, text=True).stdout
    assert st.strip() == "", f"телеметрия наследила в репозитории: {st!r}"


# ── S3 + регресс: сток обязан быть ТОТАЛЬНЫМ ────────────────────────────────────────────
def test_broken_sink_never_breaks_the_measured_phase(tele_repo, monkeypatch):
    """Найдено на живом примере: `AttributeError` из резолва каталога состояния прошёл мимо
    перечня `(OSError, TypeError, ValueError)` и уронил `begin_pass` — измеритель сломал
    измеряемое, то есть ровно то, что дизайн объявил невозможным."""
    def boom():
        raise AttributeError("сток сломан неожиданным образом")
    monkeypatch.setattr(g, "_telemetry_shard", boom)
    assert lg.compute_tree(tele_repo)                    # фаза отработала как ни в чём не бывало


def test_sink_failure_during_another_exception_does_not_replace_it(tele_repo, monkeypatch):
    """Сток падает, когда исключение тела уже летит: подменить его собой он не вправе."""
    monkeypatch.setattr(g, "_telemetry_shard",
                        lambda: (_ for _ in ()).throw(OSError("сток недоступен")))
    with pytest.raises(ZeroDivisionError):
        with g._timed("compute_tree"):
            1 / 0


# ── S9: падающая фаза измеряется, исключение не теряется ────────────────────────────────
def test_failing_phase_is_measured_and_exception_preserved(tele_repo):
    marker = RuntimeError("падение тела")
    with pytest.raises(RuntimeError) as caught:
        with g._timed("prepush_fetch"):
            raise marker
    assert caught.value is marker                        # то же исключение, не подменено
    assert _events(g._telemetry_shard())[-1]["outcome"] == "error"


# ── S7/S7b: вложенность и self-время ────────────────────────────────────────────────────
def test_nested_phases_do_not_double_count(tele_repo, monkeypatch):
    """`begin_pass` содержит `compute_tree`; суммирование `dur_ms` посчитало бы одну работу
    дважды и указало бы на неверную цель оптимизации."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    lg.begin_pass(tele_repo, "simplify")
    ev = _events(g._telemetry_shard())
    child = next(e for e in ev if e["phase"] == "compute_tree")
    root = next(e for e in ev if e["phase"] == "begin_pass")
    assert child["parent"] == root["span"]               # реальный граф, не синтетика
    assert child["trace"] == root["trace"]
    assert root["parent"] is None
    assert root["self_ms"] < child["dur_ms"]             # родитель не присвоил время ребёнка
    assert root["dur_ms"] >= child["dur_ms"]


def test_summary_separates_self_time_from_root_latency(tele_repo, monkeypatch):
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    lg.begin_pass(tele_repo, "simplify")
    out = g.telemetry_summary([g._telemetry_shard()])
    assert "compute_tree" in out and "begin_pass" in out
    assert "латентность КОРНЕВЫХ фаз" in out             # отдельной строкой, не смешано


def test_summary_rejects_corrupt_records(tele_repo):
    shard = g._telemetry_shard()
    shard.parent.mkdir(parents=True, exist_ok=True)
    good = {"ts": "2026-08-14T00:00:00+00:00", "host": "0" * 16, "session": "abc123",
            "trace": "a" * 32,
            "span": "b" * 32, "parent": None, "phase": "compute_tree",
            "dur_ms": 5, "self_ms": 5, "scale": 3, "outcome": "ok", "pid": 1}
    shard.write_text(
        json.dumps({"phase": "compute_tree"}) + "\n"          # неполная схема
        + "не json\n"
        + json.dumps({**good, "self_ms": -5}) + "\n"          # валиден ВЕЗДЕ, кроме self_ms
        + json.dumps(good) + "\n")                            # и одна хорошая рядом
    out = g.telemetry_summary([shard])
    assert "отброшено: 3" in out                             # точная строка, без «или»
    assert "событий: 1" in out                               # хорошая запись выжила


# ── S12: закрытая схема ─────────────────────────────────────────────────────────────────
def test_unknown_field_is_not_written(tele_repo):
    g._telemetry_write({**{k: 1 for k in g._TELEMETRY_KEYS}, "phase": "compute_tree",
                        "путь": "/секретный/путь"})
    shard = g._telemetry_shard()
    assert not shard.exists() or "секретный" not in shard.read_text()


def test_unknown_phase_is_not_written(tele_repo):
    with g._timed("не-такая-фаза"):
        pass
    shard = g._telemetry_shard()
    assert not shard.exists() or not _events(shard)


# ── S5: в телеметрии нет строк из окружения ─────────────────────────────────────────────
def test_every_string_value_is_from_an_allowlist(tele_repo, monkeypatch):
    """Позитивная проверка вместо денилиста из двух подстрок: денилист такой формы в этом
    репозитории проваливался четырежды. `session` приходит из ОКРУЖЕНИЯ вызывающего, то есть
    туда можно положить приватный путь — санитайзер обязан его срезать."""
    monkeypatch.setenv("CLAUDE_CODE_SESSION_ID", "/Users/lenar/clients/acme-private")
    lg.begin_pass(tele_repo, "simplify")
    allowed = re.compile(r"^[A-Za-z0-9_.:+-]*$")
    for e in _events(g._telemetry_shard()):
        for k, v in e.items():
            if isinstance(v, str):
                assert v in g._TELEMETRY_PHASES or allowed.match(v), f"{k}={v!r} — сырая строка"
    body = g._telemetry_shard().read_text()
    assert "acme-private" not in body and "/Users" not in body


# ── S6c: удаляющий код обязан быть под тестом ───────────────────────────────────────────
def test_prune_removes_only_shards_older_than_window(tele_repo):
    old = g._telemetry_dir() / "2026-01-01-1.jsonl"
    fresh = g._telemetry_dir() / "2026-08-14-2.jsonl"
    old.parent.mkdir(parents=True, exist_ok=True)
    for f in (old, fresh):
        f.write_text("{}\n")
    os.utime(old, (0, time.time() - 40 * 86400))
    assert g.telemetry_prune([old, fresh], days=14) == 1
    assert not old.exists() and fresh.exists()            # недавний не тронут


def test_writer_never_deletes_anything(tele_repo, monkeypatch):
    """M9: пока писатель вправе удалять, он однажды удалит шард ЖИВОГО процесса, и тот
    продолжит писать в невидимый inode, теряя события без ошибки."""
    monkeypatch.setenv("CLAUDE_SESSION_ID", "s1")
    stale = g._telemetry_dir() / "2020-01-01-999.jsonl"
    stale.parent.mkdir(parents=True, exist_ok=True)
    stale.write_text("{}\n")
    os.utime(stale, (0, 0))
    lg.begin_pass(tele_repo, "simplify")
    assert stale.exists(), "писатель удалил чужой шард"


def test_reporting_commands_do_not_raise_on_hardened_state(tele_repo, monkeypatch):
    """Отчётные команды роли в вердикте не имеют и не должны падать на хардненинге."""
    monkeypatch.setattr(g, "_gate_state_dir",
                        lambda: (_ for _ in ()).throw(g.TrustedGitError("состояние внутри репо")))
    assert g._telemetry_shards() == []
    assert "телеметрии нет" in g.telemetry_summary([])


def test_scale_of_wrong_type_is_rejected_before_write(tele_repo):
    """Схема закрыта не только по ключам, но и по ТИПАМ: `_span.scale` проставляет тело, и
    `scale = str(path)` протащил бы путь в файл мимо всех заявленных защит."""
    with g._timed("compute_tree") as span:
        span.scale = "/секретный/путь"
    shard = g._telemetry_shard()
    assert not shard.exists() or "секретный" not in shard.read_text()


# ── S6b: параллельные писатели, РЕАЛЬНЫЕ процессы ───────────────────────────────────────
def test_parallel_processes_lose_no_events(tmp_path):
    """Шард на процесс: гонка не «обработана аккуратно», а не существует. Проверяем реальными
    процессами — потоки этого не показали бы."""
    scripts = str(__import__("pathlib").Path(g.__file__).parent)
    home = tmp_path / "home"
    # Каталоги создаём заранее: `_inside_repo` при НЕсуществующем пути поднимается до первого
    # существующего предка, и без этого подъём дошёл бы до общего родителя с repo, а гейт
    # честно отказался бы («состояние внутри проверяемого репозитория») — артефакт стенда.
    (home / ".local" / "state" / "claude-gates").mkdir(parents=True)
    (tmp_path / "repo").mkdir()
    prog = textwrap.dedent(f"""
        import sys, pathlib
        sys.path.insert(0, {scripts!r})
        import codex_review_gate as g
        g._trusted_home = lambda: pathlib.Path({str(home)!r})
        g.REPO_ROOT = pathlib.Path({str(tmp_path / "repo")!r})
        for _ in range(25):
            with g._timed("compute_tree"):
                pass
    """)
    procs = [subprocess.Popen([sys.executable, "-c", prog], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, text=True) for _ in range(4)]
    for p in procs:
        out, _ = p.communicate(timeout=60)
        assert p.returncode == 0, f"дочерний процесс упал: {out}"
    shards = list((home / ".local" / "state" / "claude-gates").rglob("telemetry/*.jsonl"))
    assert len(shards) == 4, f"ожидался шард на процесс, найдено {len(shards)}"
    total = [json.loads(x) for s in shards for x in s.read_text().splitlines() if x.strip()]
    assert len(total) == 100                              # ни одно событие не потеряно
    assert len({e["span"] for e in total}) == 100         # и ни одно не задвоено


def test_context_manager_phase_measures_the_body(tele_repo):
    """Декоратор на генераторной функции оборачивает её ВЫЗОВ, а он лишь создаёт объект —
    тело выполняется позже, в `with` вызывающего, и фаза отчитывалась нулём. Порядок
    декораторов не помогает: оборачивается создание объекта в обоих случаях. Самая дорогая
    локальная фаза (`worktree add` = настоящий checkout) была бы «бесплатной» в релизе,
    цель которого — измерить."""
    import contextlib as cl

    @cl.contextmanager
    def inner():
        time.sleep(0.15)
        yield "w"

    @cl.contextmanager
    def probe():
        with g._timed("merge_probe"), inner() as got:
            yield got

    with probe():
        time.sleep(0.05)
    e = [x for x in _events(g._telemetry_shard()) if x["phase"] == "merge_probe"][-1]
    assert e["dur_ms"] >= 150, f"фаза измерена как {e['dur_ms']} мс — мерится не тело"


def test_one_trace_per_process_not_per_top_level_phase(tele_repo):
    """В одной CLI-инвокации несколько фаз верхнего уровня, и они ПОСЛЕДОВАТЕЛЬНЫ, не вложены
    (`_prepush_fetch` → `predict_merge` → `merge_probe`). Наследование trace только через
    непустой стек давало каждой свой trace: «инвокаций» завышалось кратно, а именно эта цифра
    пошла бы в довод «гейты добавляют N секунд на инвокацию»."""
    for ph in ("prepush_fetch", "predict_merge", "merge_probe"):
        with g._timed(ph):
            pass
    ev = _events(g._telemetry_shard())
    assert len(ev) == 3
    assert len({e["trace"] for e in ev}) == 1


def test_prune_floor_protects_a_live_writer(tele_repo):
    """`telemetry-prune 0` обнулял окно, и «недавние пропускаем» переставало существовать —
    удалялся шард ЖИВОГО писателя, а тот на POSIX продолжал писать в невидимый inode."""
    with g._timed("compute_tree"):
        pass
    shard = g._telemetry_shard()
    assert g.telemetry_prune([shard], days=0) == 0
    assert shard.exists()


def test_host_groups_machines_and_never_mixes_them(tele_repo):
    """12-секундный compute_tree мака и 2,6-секундный на Linux в одной медиане дали бы число,
    которого нет ни на одной машине — замерено на двух реальных машинах."""
    shard = g._telemetry_shard()
    shard.parent.mkdir(parents=True, exist_ok=True)
    base = {"ts": "2026-08-14T00:00:00+00:00", "session": "c" * 16, "parent": None,
            "phase": "compute_tree", "self_ms": 0, "scale": 500, "outcome": "ok", "pid": 1}
    lines = []
    for host, dur in (("a" * 16, 12275), ("b" * 16, 2627)):
        lines.append(json.dumps({**base, "host": host, "trace": host * 2, "span": host * 2,
                                 "dur_ms": dur, "self_ms": dur}))
    shard.write_text("\n".join(lines) + "\n")
    out = g.telemetry_summary([shard])
    assert "машина " + "a" * 16 in out and "машина " + "b" * 16 in out
    assert "12275" in out and "2627" in out          # обе цифры видны, не усреднены


def test_machine_id_is_stable_and_carries_no_name(tele_repo, monkeypatch):
    """Id переживает переименование машины (в отличие от hostname) и имени не несёт."""
    first = g._machine_id()
    assert re.fullmatch(r"[0-9a-f]{16}", first)
    monkeypatch.setattr(g.os, "uname", lambda: type("U", (), {"nodename": "другое-имя"})())
    assert g._machine_id() == first                  # переименование не дробит историю
    import socket
    # machine-id общий на все репозитории машины: он лежит РЯДОМ с per-repo каталогами
    stored = (g._gate_state_dir().parent / "machine-id").read_text()
    assert stored.strip() == first
    assert socket.gethostname() not in stored


def test_two_installations_get_distinct_ids(tmp_path):
    """S7b: разные установки — разные id. Иначе сводка снова смешает машины."""
    a = g._machine_id_at(str(tmp_path / "инсталляция-a"))
    b = g._machine_id_at(str(tmp_path / "инсталляция-b"))
    assert a and b and a != b


def test_existing_machine_id_is_never_overwritten(tmp_path):
    """Детерминированная проверка вместо гоночной: прошлый тест был зелёным и на НАИВНОЙ
    реализации (импорт модуля сериализует процессы, окно гонки не открывается), то есть
    утверждал гарантию, которой не проверял. Суть свойства — «второй не перезаписывает
    первого» — проверяется прямо."""
    state = tmp_path / "state"
    first = g._machine_id_at(str(state))
    g._machine_id_at.cache_clear()               # как будто это другой процесс
    assert g._machine_id_at(str(state)) == first
    (state / "machine-id").write_text("0123456789abcdef")
    g._machine_id_at.cache_clear()
    assert g._machine_id_at(str(state)) == "0123456789abcdef"


def test_pre_0_9_4_events_are_not_discarded(tele_repo):
    """События 0.9.3 не знают поля `host`, а схема закрыта по ключам — они выбрасывались
    целиком. Это ровно те двухмашинные замеры, которыми обоснован релиз: база «до»."""
    shard = g._telemetry_shard()
    shard.parent.mkdir(parents=True, exist_ok=True)
    old_event = {"ts": "2026-08-14T00:00:00+00:00", "session": "a" * 16, "trace": "b" * 32,
                 "span": "c" * 32, "parent": None, "phase": "compute_tree",
                 "dur_ms": 12275, "self_ms": 12275, "scale": 579, "outcome": "ok", "pid": 1}
    shard.write_text(json.dumps(old_event) + "\n")
    out = g.telemetry_summary([shard])
    assert "событий: 1" in out and "12275" in out


# --- раунды ревью (0.9.5): узкое место переехало сюда ---

def _fake_review(monkeypatch, repo, stdout, rc=0):
    monkeypatch.setattr(g, "REPO_ROOT", repo)
    monkeypatch.setattr(g, "_exec_companion",
                        lambda *a, **k: subprocess.CompletedProcess(a, rc, stdout, ""))


def test_review_round_is_measured_with_kind_and_findings(tele_repo, monkeypatch):
    """Путь `companion-review` не измерялся ВООБЩЕ: все события `reviewer_call` приходили из
    деплой-панели, а десятки раундов дизайн- и код-ревью были невидимы — тот же провал, что
    был у лесенки до 0.9.3."""
    (tele_repo / "b.py").write_text("y = 2\n")
    real = ("Verdict: needs-attention\n\nFindings:\n"
            "- [high] Раз (a.py:1)\n  Т.\n- [medium] Два (a.py:2)\n  Т.\n")
    _fake_review(monkeypatch, tele_repo, real)
    g.main(["companion-review", "--scope", "working-tree", "фокус"])
    e = [x for x in _events(g._telemetry_shard()) if x["phase"] == "review_round"][-1]
    assert e["kind"] == "code" and e["round"] == 1 and e["findings"] == 2
    assert e["outcome"] == "ok"


def test_design_round_is_tagged_design(tele_repo, monkeypatch):
    """`kind` обязателен: у дизайн-ревью потолок ОДИН раунд, у кодового три, и без разделения
    «доля раундов 3+» отражала бы состав работы, а не сходимость цикла."""
    d = tele_repo / "дизайн.md"
    d.write_text("# д\nтекст\n")
    _fake_review(monkeypatch, tele_repo, "Verdict: needs-attention\n\n## Замечание\nX.\n")
    g.main(["companion-review", "--design-file", str(d), "--scope", "working-tree", "ф"])
    e = [x for x in _events(g._telemetry_shard()) if x["phase"] == "review_round"][-1]
    assert e["kind"] == "design"


def test_budget_refusal_is_measured_as_error(tele_repo, monkeypatch):
    """«Сколько раз бюджет реально кусался» иначе нигде не видно."""
    (tele_repo / "b.py").write_text("y = 2\n")
    monkeypatch.setattr(g, "REPO_ROOT", tele_repo)
    monkeypatch.setattr(g, "review_round_check", lambda a: (4, 3, "[codex-gate] бюджет исчерпан"))
    assert g.main(["companion-review", "--scope", "working-tree", "ф"]) == 2
    e = [x for x in _events(g._telemetry_shard()) if x["phase"] == "review_round"][-1]
    assert e["outcome"] == "error" and e["round"] == 4


def test_summary_stratifies_rounds_by_kind(tele_repo, monkeypatch):
    """Фикстура строится ПРОГОНОМ, а не литералами: прошлая версия собирала дизайн-строку
    руками с `round=1` — значением, которое код произвести не мог (`review_round_check` для
    дизайна отдаёт 0, а ноль ложен, и сводка выбрасывала ВСЕ дизайн-события). Тест был
    зелёным на неработающей фиче — ровно «мок скрывает сломанное»."""
    (tele_repo / "b.py").write_text("y = 2\n")
    d = tele_repo / "дизайн.md"
    d.write_text("# д\nтекст\n")
    real = "Verdict: needs-attention\n\nFindings:\n- [high] Раз (a.py:1)\n  Т.\n"
    _fake_review(monkeypatch, tele_repo, real)
    g.main(["companion-review", "--scope", "working-tree", "ф"])
    _fake_review(monkeypatch, tele_repo, "Verdict: needs-attention\n\n## Замечание\nX.\n")
    g.main(["companion-review", "--design-file", str(d), "--scope", "working-tree", "ф"])
    out = g.telemetry_summary([g._telemetry_shard()])
    assert "раунды code:" in out and "раунды design:" in out


def test_checklist_does_not_inflate_findings(tele_repo, monkeypatch):
    """`Verdict: approve` с прогонным чеклистом уезжал как `findings=3` и выпадал из счётчика
    пустых раундов — метрики, ради которой поле и заводилось."""
    (tele_repo / "b.py").write_text("y = 2\n")
    _fake_review(monkeypatch, tele_repo,
                 "Verdict: approve\n\nЧеклист:\n- [x] дифф прочитан\n- [ ] бенчмарк\n\n"
                 "No material findings.\n")
    g.main(["companion-review", "--scope", "working-tree", "ф"])
    e = [x for x in _events(g._telemetry_shard()) if x["phase"] == "review_round"][-1]
    assert e["findings"] == 0, "чеклист-маркеры посчитаны находками"


def test_rejected_rounds_do_not_inflate_the_distribution(tele_repo, monkeypatch):
    """Отклонённая попытка бюджет не жжёт, поэтому повтор идёт под ТЕМ ЖЕ номером: считая их
    все, «доля раундов 3+» занижается, а «ожидание» смешивает минуты с мгновенными отказами."""
    (tele_repo / "b.py").write_text("y = 2\n")
    blind = "Verdict: needs-attention\n\nНе смог.\n"
    _fake_review(monkeypatch, tele_repo, blind)
    g.main(["companion-review", "--scope", "working-tree", "ф"])
    g.main(["companion-review", "--scope", "working-tree", "ф"])
    _fake_review(monkeypatch, tele_repo,
                 "Verdict: needs-attention\n\nFindings:\n- [high] Раз (a.py:1)\n  Т.\n")
    g.main(["companion-review", "--scope", "working-tree", "ф"])
    out = g.telemetry_summary([g._telemetry_shard()])
    assert "пустых 0/1" in out and "отклонено 2" in out


def test_pre_0_9_5_events_still_read(tele_repo):
    """Схема закрыта по ключам: без значений по умолчанию события 0.9.4 отбрасывались бы
    целиком вместе с базой «до» — так уже случилось при вводе `host`."""
    shard = g._telemetry_shard()
    shard.parent.mkdir(parents=True, exist_ok=True)
    old = {"ts": "2026-08-20T00:00:00+00:00", "host": "b" * 16, "session": "c" * 16,
           "trace": "d" * 32, "span": "e" * 32, "parent": None, "phase": "compute_tree",
           "dur_ms": 275, "self_ms": 275, "scale": 621, "outcome": "ok", "pid": 1}
    shard.write_text(json.dumps(old) + "\n")
    out = g.telemetry_summary([shard])
    assert "событий: 1" in out and "275" in out


def test_rejected_round_is_not_recorded_as_ok(tele_repo, monkeypatch):
    """Найдено на ЖИВЫХ данных сразу после выката: отвергнутый раунд писал `findings=None` и
    `outcome=ok`, то есть телеметрия говорила «всё хорошо» там, где гейт возвращал 2. Счёт
    находок берётся ДО решений, а каждый путь отказа помечает исход."""
    (tele_repo / "b.py").write_text("y = 2\n")
    blind = "Verdict: needs-attention\n\nНе смог прочитать дерево.\n"
    _fake_review(monkeypatch, tele_repo, blind)
    assert g.main(["companion-review", "--scope", "working-tree", "ф"]) == 2
    e = [x for x in _events(g._telemetry_shard()) if x["phase"] == "review_round"][-1]
    assert e["outcome"] == "error", "отвергнутый раунд помечен как успешный"
    assert e["findings"] == 0, "у отвергнутого раунда потерян счёт находок"


def test_engine_failure_round_is_marked_error(tele_repo, monkeypatch):
    """Ненулевой код движка — тоже не «ok»: иначе сводка покажет успешные раунды там, где
    ревью не состоялось ни разу."""
    (tele_repo / "b.py").write_text("y = 2\n")
    _fake_review(monkeypatch, tele_repo, "", rc=1)
    assert g.main(["companion-review", "--scope", "working-tree", "ф"]) == 2
    e = [x for x in _events(g._telemetry_shard()) if x["phase"] == "review_round"][-1]
    assert e["outcome"] == "error"


@pytest.mark.parametrize("stdout,rc,want_rc,want_outcome", [
    ("Verdict: needs-attention\n\nFindings:\n- [high] X (a.py:1)\n  T.\n", 0, 0, "ok"),
    ("Verdict: needs-attention\n\nНе смог прочитать.\n", 0, 2, "error"),
    ("просто проза без вердикта\n", 0, 0, "error"),
    ("", 1, 2, "error"),
])
def test_blocking_is_never_recorded_as_a_successful_round(
        tele_repo, monkeypatch, stdout, rc, want_rc, want_outcome):
    """Инвариант: код 2 НИКОГДА не записывается как состоявшийся раунд, иначе сводка покажет
    успешные раунды там, где ревью не состоялось. Обратное намеренно неверно: «ответ не по
    контракту» возвращает 0 (гейт не блокирует, отдаёт текст агенту), но раунд НЕ засчитан —
    в данных это не успех."""
    (tele_repo / "b.py").write_text("y = 2\n")
    _fake_review(monkeypatch, tele_repo, stdout, rc=rc)
    assert g.main(["companion-review", "--scope", "working-tree", "ф"]) == want_rc
    e = [x for x in _events(g._telemetry_shard()) if x["phase"] == "review_round"][-1]
    assert e["outcome"] == want_outcome
    assert not (want_rc == 2 and e["outcome"] == "ok")
