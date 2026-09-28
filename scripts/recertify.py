#!/usr/bin/env python3
"""`make recertify`: перекалибровка Claude-ревьюера на актуальную модель одной командой.

Дизайн: docs/2026-09-29-reviewer-recertify-design.md. Скрипт готовит РАБОЧЕЕ ДЕРЕВО (реестр,
отчёт, версии, CHANGELOG); коммит, лесенка, PR и мерж — как у любой правки: повышение статуса
остаётся ревьюируемой правкой (инвариант `certify_reviewers.py`). На хосты не поставляется.

Транзакция: либо полный новый набор файлов, либо побайтово прежний. Прерывание SIGKILL отката
не даёт — гейт на недоделанном наборе fail-closed, а повторный запуск его не признает
«актуальным» (актуальность проверяется тем же путём валидации связки, что и в проде).
"""
from __future__ import annotations

import argparse
import datetime
import fcntl
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "plugins" / "gates" / "scripts"))
import certify_reviewers as cr                                                   # noqa: E402
import codex_review_gate as g                                                    # noqa: E402
from prepush_gate import _git_common_dir                                         # noqa: E402

#: алиас, по которому вендор публикует актуальную модель; проба спрашивает именно его
PROBE_ALIAS = "opus"
PROBE_TIMEOUT_S = 180
REPETITIONS = 2
#: имя модели становится certification_id и именем файла отчёта — форма узкая
_MODEL_RE = re.compile(r"claude-[a-z0-9]+(?:[.-][a-z0-9]+)*")
_MANIFESTS = ("plugins/gates/.claude-plugin/plugin.json", ".claude-plugin/marketplace.json")
_CODEX_MANIFEST = "plugins/gates/.codex-plugin/plugin.json"


class RecertifyError(Exception):
    """Отказ до или во время перекалибровки; сообщение — для оператора."""


class _Terminated(Exception):
    pass


def _write(path: Path, data: bytes) -> None:
    """Единственная точка мутации файлов (кроме удаления): tmp + os.replace."""
    tmp = path.with_name(f".{path.name}.recertify-tmp")
    try:
        tmp.write_bytes(data)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)     # иначе скрытый tmp уехал бы в коммит через `git add -A`
        raise


def _unlink(path: Path) -> None:
    """Отдельная точка мутации ради fault-injection теста (удаление старого отчёта)."""
    path.unlink()


def _shown(value: object) -> str:
    """Имя модели для сообщения: ключи modelUsage недоверенны (приходят из ответа CLI)."""
    return repr(g.redact_secrets(str(value))[:80])


def validate_model(model: str) -> str:
    if not isinstance(model, str) or not _MODEL_RE.fullmatch(model):
        raise RecertifyError(f"недопустимое имя модели {_shown(model)}: ожидается "
                             "claude-<версия>")
    if g.model_family(model) != "anthropic":
        raise RecertifyError(f"{_shown(model)} — не модель Anthropic")
    return model


def parse_probe(stdout: str) -> str:
    """Модель, НАПИСАВШАЯ ответ пробы: строго наибольший положительный выход в modelUsage
    (рядом штатно бывает служебная haiku — см. S10 в AGENTS.md)."""
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, ValueError) as exc:
        raise RecertifyError("ответ пробы не JSON") from exc
    if not isinstance(envelope, dict) or envelope.get("is_error") is not False:
        raise RecertifyError("проба вернула ошибку CLI")
    usage = envelope.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        raise RecertifyError("в ответе пробы нет modelUsage")
    outs = {}
    for model, u in usage.items():
        v = u.get("outputTokens") if isinstance(u, dict) else None
        if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
            raise RecertifyError(f"у модели {_shown(model)} нет счётчика выхода")
        outs[str(model)] = v
    top = max(outs.values())
    writers = [m for m, v in outs.items() if v == top]
    if len(writers) != 1:
        raise RecertifyError("не определить модель ответа: ничья "
                             + ", ".join(_shown(m) for m in sorted(writers)))
    return validate_model(writers[0])


def probe_alias() -> str:
    """Куда сейчас резолвится алиас вендора. Те же флаги изоляции, что у адаптера ревьюера."""
    binary = g._resolve_claude_bin()
    if binary is None:
        raise RecertifyError("claude CLI не найден")
    cwd = g._sterile_mkdtemp("gates-recertify-probe-")
    if cwd is None:
        raise RecertifyError("не создать стерильный cwd вне репозитория для пробы")
    try:
        result = subprocess.run(
            g.claude_cmd(binary, PROBE_ALIAS), cwd=cwd, input="Reply with the single word: ok", capture_output=True, text=True,
            timeout=PROBE_TIMEOUT_S, env=g._certified_subprocess_env())
    except subprocess.TimeoutExpired as exc:
        raise RecertifyError(f"проба: таймаут {PROBE_TIMEOUT_S}s") from exc
    finally:
        shutil.rmtree(cwd, ignore_errors=True)
    if result.returncode != 0:
        raise RecertifyError("проба: claude вышел с кодом "
                             f"{result.returncode}: {g.redact_secrets(result.stderr)[:300]}")
    return parse_probe(result.stdout)


def _dump(body: dict) -> bytes:
    return (json.dumps(body, ensure_ascii=False, indent=2) + "\n").encode()


def _claude_blocking_index(registry: dict) -> int:
    idx = [i for i, c in enumerate(registry.get("certifications", []))
           if c.get("provider") == "claude" and "blocking" in (c.get("roles") or [])]
    if len(idx) != 1:
        raise RecertifyError(f"в реестре {len(idx)} Claude blocking-записей, нужна ровно одна — "
                             "разберись руками, угадывать нельзя")
    return idx[0]


def _bump(root: Path, today: str) -> tuple[str, str, dict[Path, bytes]]:
    """Новые байты манифестов: patch+1. CLI обновляет по ВЕРСИИ — без поднятия реестр не
    доедет до хостов (урок перекалибровки 29.09.2026)."""
    main = root / _MANIFESTS[0]
    old = json.loads(main.read_text())["version"]
    m = re.fullmatch(r"(\d+)\.(\d+)\.(\d+)", str(old))
    if not m:
        raise RecertifyError(f"версия {old!r} не вида X.Y.Z")
    new = f"{m[1]}.{m[2]}.{int(m[3]) + 1}"
    out = {}
    for rel in _MANIFESTS:
        text = (root / rel).read_text()
        needle = f'"version": "{old}"'
        if needle not in text:
            raise RecertifyError(f"{rel}: нет {needle} — манифесты разъехались")
        out[root / rel] = text.replace(needle, f'"version": "{new}"').encode()
    codex = root / _CODEX_MANIFEST
    text = codex.read_text()
    fixed, n = re.subn(rf'"version": "{re.escape(old)}\+codex\.\d{{8}}"',
                       f'"version": "{new}+codex.{today}"', text)
    if n != 1:
        raise RecertifyError(f"{_CODEX_MANIFEST}: нет версии {old}+codex.<дата>")
    out[codex] = fixed.encode()
    return old, new, out


def _changelog(root: Path, new: str, today: str, old_model: str, model: str,
               cert_id: str, cases: int) -> tuple[Path, bytes]:
    path = root / "CHANGELOG.md"
    text = path.read_text()
    head = "# Changelog\n\n"
    if not text.startswith(head):
        raise RecertifyError("CHANGELOG.md не начинается с '# Changelog'")
    day = f"{today[:4]}-{today[4:6]}-{today[6:]}"
    entry = (f"## {new} — {day}\n\n"
             f"Перекалибровка Claude-ревьюера (`make recertify`): `{old_model}` → `{model}`. "
             f"Корпус {cases} кейсов × {REPETITIONS} повтора пройден целиком одной моделью, "
             f"отчёт `reviewer_corpus/reports/{cert_id}.json`.\n\n")
    return path, (head + entry + text[len(head):]).encode()


def _recertify(model: str, *, root: Path, fail_dir: Path, today: str,
               snapshot: dict[Path, bytes | None]) -> int:
    registry_path = g._CERTIFICATION_REGISTRY
    reports = g._REPORTS_DIR
    registry = json.loads(registry_path.read_text())
    i = _claude_blocking_index(registry)
    old = registry["certifications"][i]
    # тот же путь, что у прода: загрузчик понижает запись с битой связкой отчёта (нет отчёта,
    # другой sha, новый корпус), и тогда certified-модели нет — это НЕ «актуально»
    if g.claude_blocking_model() == model:
        print(f"[recertify] актуально: {model} сертифицирована, связка отчёта валидна")
        return 0
    cert_id = f"{model}-blocking-{today}"
    new_report = reports / f"{cert_id}.json"
    old_report = (reports / Path(old["report"]["path"]).name
                  if isinstance(old.get("report"), dict) and old["report"].get("path") else None)
    touched = [registry_path, new_report, root / "CHANGELOG.md", root / _CODEX_MANIFEST,
               *(root / rel for rel in _MANIFESTS)]
    if old_report is not None:
        touched.append(old_report)
    for p in touched:
        snapshot[p] = p.read_bytes() if p.exists() else None
    old_actual = (old.get("actual_models") or ["?"])[0]
    # миграция с алиаса: «opus (claude-opus-5-5) → claude-opus-5-5», а не «X → X»
    old_model = (old_actual if old.get("requested_model") in (None, old_actual)
                 else f"{old['requested_model']} ({old_actual})")
    _, new_version, manifests = _bump(root, today)       # отказы формы — ДО первой мутации

    candidate = {k: v for k, v in old.items() if k not in ("report", "note")}
    candidate.update(requested_model=model, actual_models=[model], certification_id=cert_id,
                     status="candidate")
    registry["certifications"][i] = candidate
    _write(registry_path, _dump(registry))
    print(f"[recertify] прогон корпуса для {model} ({REPETITIONS} повтора)…", flush=True)
    code, report = cr.run_provider("claude", REPETITIONS)
    report_bytes = json.dumps(report, ensure_ascii=False, indent=2).encode()
    if code != 0 or report.get("pass") is not True or report.get("actual_models") != [model]:
        fail_dir.mkdir(parents=True, exist_ok=True)
        failed = fail_dir / f"{cert_id}.failed.json"
        failed.write_bytes(report_bytes)
        raise RecertifyError(f"{model} не прошла корпус — реестр не тронут. Отчёт: {failed}")

    _write(new_report, report_bytes)
    for path, data in manifests.items():
        _write(path, data)
    path, data = _changelog(root, new_version, today, old_model, model, cert_id,
                            len(cr.load_corpus()["cases"]))
    _write(path, data)
    candidate.update(status="certified", report={
        "path": f"reports/{new_report.name}",
        "sha256": hashlib.sha256(report_bytes).hexdigest(),
        "corpus_sha256": report["corpus_sha256"],
        "repetitions": REPETITIONS,
    })
    _write(registry_path, _dump(registry))
    if old_report is not None and old_report != new_report and old_report.exists():
        _unlink(old_report)
    # постусловие тем же путём, что у прода: связка сошлась и запись однозначна
    if g.claude_blocking_model() != model:
        raise RecertifyError("постусловие не выполнено: гейт не принимает новую запись")
    print(f"[recertify] ✓ {old_model} → {model}, версия {new_version}. Дальше — обычная правка: "
          "make test, лесенка, коммит, PR")
    return 0


def run(target: str | None, *, root: Path, lock_path: Path, fail_dir: Path, today: str,
        probe=probe_alias) -> int:
    snapshot: dict[Path, bytes | None] = {}
    previous = signal.getsignal(signal.SIGTERM)

    def _on_term(_signum, _frame):
        raise _Terminated()

    try:
        with open(lock_path, "w") as lock:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                print("[recertify] ✗ уже идёт другая перекалибровка", file=sys.stderr)
                return 2
            signal.signal(signal.SIGTERM, _on_term)
            try:
                model = validate_model(target if target is not None else probe())
                return _recertify(model, root=root, fail_dir=fail_dir, today=today,
                                  snapshot=snapshot)
            except BaseException as exc:
                # повторный SIGTERM посреди отката оставил бы набор наполовину восстановленным
                signal.signal(signal.SIGTERM, signal.SIG_IGN)
                _rollback(snapshot)
                # ожидаемые отказы — выход 2; прочее (TypeError, KeyError…) — баг, пробрасываем
                if isinstance(exc, (_Terminated, KeyboardInterrupt)):
                    reason = "прервано сигналом"
                elif isinstance(exc, (RecertifyError, ValueError, OSError, RuntimeError)):
                    reason = f"{type(exc).__name__}: {exc}"
                else:
                    raise
                print(f"[recertify] ✗ {reason}. Файлы восстановлены.", file=sys.stderr)
                return 2
    finally:
        signal.signal(signal.SIGTERM, previous)


def _rollback(snapshot: dict[Path, bytes | None]) -> None:
    """Побайтово прежнее; намеренно мимо `_write`/`_unlink` — откат не должен зависеть от
    той точки мутации, которая только что отказала."""
    for path, data in snapshot.items():
        if data is None:
            path.unlink(missing_ok=True)
        else:
            _restore(path, data)


def _restore(path: Path, data: bytes) -> None:
    """Атомарно (tmp + replace): прерванный откат не оставляет усечённый файл."""
    tmp = path.with_name(f".{path.name}.recertify-restore")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--model", help=f"цель явно, минуя пробу алиаса `{PROBE_ALIAS}`")
    args = parser.parse_args(argv)
    if "MODEL" in os.environ:
        # `make recertify MODEL=x` иначе МОЛЧА перекалибровал бы на модель алиаса, а не на x
        print("[recertify] ✗ MODEL из окружения не принимается: цель явно — "
              "`python3 -B scripts/recertify.py --model claude-…`", file=sys.stderr)
        return 2
    state = g._gate_state_dir()
    git_dir = _git_common_dir(ROOT)
    fail_dir = (state / "recertify-failed") if state else git_dir / "gates-recertify-failed"
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%d")
    return run(args.model, root=ROOT, lock_path=git_dir / "gates-recertify.lock",
               fail_dir=fail_dir, today=today)


if __name__ == "__main__":
    sys.exit(main())
