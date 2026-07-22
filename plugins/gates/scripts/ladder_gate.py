"""Ladder gate: tree-хэш «всё изменённое» (временный индекс) + begin/mark протокол
проходов /simplify → /code-review; pre-commit/post-commit энфорсмент; деплой range-проверка
`check_range` — покрытие ВСЕГО `baseline..HEAD`, канонические проходы независимо от config.
Chain-семантика, анти-replay (R7), consume-then-publish (R8). Порт из боевого проекта-источника в плагин
gates: конфиг — `.codex-gate.yaml` (было config.yaml), эпоха — из конфига root'а с
module-оверрайдом для тестов.
Спека: docs/2026-07-22-gates-plugin-port-design.md (+ docs/methodology/)."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import traceback
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:            # PyYAML может отсутствовать в системном python3 (решение 3):
    yaml = None                # конфиг нечитаем → безопасные строгие дефолты

# is_code_path — sibling-модуль в каталоге плагина: прямой импорт работает, когда каталог
# уже в sys.path (тесты через conftest, запуск обоих скриптов из одного каталога), фолбэк —
# при запуске как голый скрипт из произвольного cwd.
try:
    from codex_review_gate import is_code_path
except ImportError:                                    # запуск как голый скрипт
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from codex_review_gate import is_code_path  # type: ignore[no-redef]

DEPLOY_REQUIRED_PASSES = ("simplify", "code-review")
GATE_CONFIG_NAME = ".codex-gate.yaml"

# Точные пути ladder-бухгалтерии для исключения из tree-хэша (ревью Task 1: широкий glob
# `.ladder-*` прятал бы от хэша и ПРОИЗВОЛЬНЫЙ файл/каталог под этим префиксом — сузено
# до 4 конкретных литералов; всё прочее под .claude/.ladder-* попадает в хэш). Плоские пути
# (не pathspec-негации, см. compute_tree fix Task 4) — потребляются `git rm --cached`.
_BOOKKEEPING_PATHS = tuple(
    f".claude/.ladder-{name}"
    for p in DEPLOY_REQUIRED_PASSES for name in (p, f"pending-{p}")
)


class LadderError(Exception):
    """Нарушение протокола begin/mark (неизвестный проход, mark без begin, разрыв цепочки)."""


def _env_session() -> str:
    # Тот же конвенционный chokepoint, что _env_session в codex_review_gate.py.
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID", "")


def _marker_path(root: Path, pass_name: str) -> Path:
    return root / ".claude" / f".ladder-{pass_name}"


def _pending_path(root: Path, pass_name: str) -> Path:
    return root / ".claude" / f".ladder-pending-{pass_name}"


def _gate_config(root: Path) -> "dict | None":
    """Парсит `<root>/.codex-gate.yaml`. None = нет файла / битый YAML / нет PyYAML /
    симлинк / не dict — вызывающие трактуют по принципу «строже» (лесенка вкл, эпоха выкл)."""
    p = root / GATE_CONFIG_NAME
    if p.is_symlink() or not p.exists() or yaml is None:
        return None
    try:
        data = yaml.safe_load(p.read_text())
    except (yaml.YAMLError, OSError, UnicodeError):   # не-UTF-8 = битый, не крэш (Codex code-R1)
        return None
    return data if isinstance(data, dict) else None


def _config_blob(root: Path, ref: str) -> "bytes | None":
    """Содержимое .codex-gate.yaml в index (ref='' → ':<path>') или коммите (ref='HEAD').
    None = файла там нет / git-сбой (консервативно «не совпало» у вызывающего)."""
    r = subprocess.run(["git", "show", f"{ref}:{GATE_CONFIG_NAME}"], cwd=root,
                       capture_output=True)
    return r.stdout if r.returncode == 0 else None


def _worktree_config_bytes(root: Path) -> "bytes | None":
    p = root / GATE_CONFIG_NAME
    if p.is_symlink():
        return b"<symlink>"        # заведомо не совпадёт с блобом — классификации не верить
    if not p.exists():
        return None
    try:
        return p.read_bytes()
    except OSError:
        return b"<unreadable>"


def classification_trustworthy(root: Path, ref: str) -> bool:
    """Codex code-R1 (лаундеринг): классификация код/не-код читает КОНФИГ ИЗ WORKTREE
    (import-time), а коммитится содержимое index/HEAD. Если worktree-конфиг отличается от
    коммитуемого (`ref`), незастейдженное ослабление могло сделать код-коммит «не-кодом» —
    exempt-решению верить нельзя (консервативно считаем коммит кодом; fail-safe).
    Застейдженная/закоммиченная правка конфига — легитимный канал: она видна Codex-ревью
    деплой-диффа (принятый аргумент спеки, решение 1)."""
    return _worktree_config_bytes(root) == _config_blob(root, ref)


def compute_tree(root: Path) -> str:
    """TREE-хэш «всё изменённое»: ПУСТОЙ временный индекс, GIT_INDEX_FILE=<tmp>
    git add -A && git write-tree (полный re-hash). Реальный индекс НЕ мутируется.

    Ladder-бухгалтерия (`.claude/.ladder-*`) исключена: иначе запись pending/маркера
    самим протоколом меняла бы дерево между соседними begin/mark-вызовами
    (self-referential — маркер о состоянии кода не должен зависеть от файла,
    описывающего этот же маркер). Исключение — через `git rm --cached --ignore-unmatch`
    ПОСЛЕ `add -A`, а не через негативные pathspec-и в самом `add -A` (фикс smoke-теста
    Task 4): если бухгалтерский файл УЖЕ существует на диске и подпадает под реальный
    `.gitignore` (`.claude/*`), git трактует `:!path`-негацию на такой путь как явную
    попытку добавить игнорируемый файл и падает `fatal: ... ignored by .gitignore ...
    use -f` — воспроизводится начиная со ВТОРОГО вызова begin/mark в жизни репозитория
    (после первого `mark` маркер уже лежит на диске). `git rm --cached --ignore-unmatch`
    не подвержен этой проверке и идемпотентен, если путь и не был застейджен (тестовые
    tmp-репо без .gitignore)."""
    with tempfile.TemporaryDirectory() as td:
        # ПУСТОЙ tmp-индекс, НЕ копия реального (ревью Task 3): shutil.copy сбрасывал mtime
        # индекса в «сейчас», отключая git-детекцию racy-правок — same-size правка в ту же
        # секунду (типичный фикс /simplify) могла отдать STALE blob → ложный tree-хэш на
        # chokepoint'е целостности. Пустой индекс = полный re-hash рабочего дерева (репо
        # маленький, цена мизерна), иммунно к racy-index по построению.
        tmp_index = Path(td) / "index"
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(tmp_index)
        subprocess.run(["git", "add", "-A", "--", "."],
                       cwd=root, env=env, check=True, capture_output=True)
        subprocess.run(["git", "rm", "--cached", "--ignore-unmatch", "-q", "--",
                        *_BOOKKEEPING_PATHS],
                       cwd=root, env=env, check=True, capture_output=True)
        r = subprocess.run(["git", "write-tree"], cwd=root, env=env, check=True,
                           capture_output=True, text=True)
        return r.stdout.strip()


def index_tree(root: Path) -> str:
    """git write-tree РЕАЛЬНОГО индекса (для pre-commit — ровно то, что закоммитится)."""
    r = subprocess.run(["git", "write-tree"], cwd=root, check=True,
                       capture_output=True, text=True)
    return r.stdout.strip()


def read_marker(root: Path, pass_name: str) -> dict | None:
    """Читает маркер прохода. Битый JSON → None (fail-closed выше по стеку)."""
    p = _marker_path(root, pass_name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _read_pending(root: Path, pass_name: str) -> dict | None:
    p = _pending_path(root, pass_name)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _atomic_write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(path)   # атомарная публикация


def begin_pass(root: Path, pass_name: str) -> None:
    """Снимает tree_before ДО прохода, пишет pending (перезапись своего — ок).
    Для code-review валидирует chain-start: текущий tree == simplify.tree_after,
    иначе LadderError (ручная правка между проходами / /simplify не запускался)."""
    if pass_name not in DEPLOY_REQUIRED_PASSES:
        raise LadderError(f"неизвестный проход {pass_name!r} — ожидается один из "
                          f"{DEPLOY_REQUIRED_PASSES}")
    tree = compute_tree(root)
    if pass_name == "code-review":
        simplify_marker = read_marker(root, "simplify")
        if simplify_marker is None or simplify_marker.get("tree_after") != tree:
            raise LadderError(
                "code-review: старт цепочки не совпадает с simplify.tree_after — "
                "сначала /simplify (или ручная правка сломала цепочку между проходами)")
    _atomic_write(_pending_path(root, pass_name), {
        "tree_before": tree,
        "ts": datetime.now(timezone.utc).isoformat(),
    })


def mark_pass(root: Path, pass_name: str) -> None:
    """pending обязателен (LadderError если нет). Consume-then-publish (R8):
    удаляет pending ПЕРВЫМ, затем атомарно публикует маркер {tree_before, tree_after,
    session, ts}. Повторный mark без нового begin → LadderError (R7, анти-replay)."""
    if pass_name not in DEPLOY_REQUIRED_PASSES:   # симметрия с begin_pass (ревью Task 1)
        raise LadderError(f"неизвестный проход {pass_name!r} — ожидается один из "
                          f"{DEPLOY_REQUIRED_PASSES}")
    pending_path = _pending_path(root, pass_name)
    pending = _read_pending(root, pass_name)
    if pending is None:
        raise LadderError(f"mark {pass_name!r} без begin — нет pending "
                          f"(или он уже потреблён предыдущим mark)")
    tree_before = pending["tree_before"]
    pending_path.unlink(missing_ok=True)   # consume ПЕРВЫМ (crash после — fail-closed, не replay)
    tree_after = compute_tree(root)
    _atomic_write(_marker_path(root, pass_name), {
        "tree_before": tree_before,
        "tree_after": tree_after,
        "session": _env_session(),
        "ts": datetime.now(timezone.utc).isoformat(),
    })


# --- pre-commit / post-commit (спека Фазы 1.5 §2/§3) ---

_AUDIT_LOG_RELPATH = Path("logs") / "codex_review_audit.log"
_LEDGER_DIR_RELPATH = Path("logs") / "ladder_ledger"

_CHAIN_INSTRUCTIONS = (
    "[ladder-gate] цепочка /simplify → /code-review не подтверждена для этого коммита.\n"
    "Прогони протокол: `ladder_gate.py begin simplify` → /simplify → "
    "`ladder_gate.py mark simplify` → `ladder_gate.py begin code-review` → /code-review → "
    "`ladder_gate.py mark code-review`, затем закоммить снова.\n"
    "Обход (осознанно, с аудитом): LADDER_SKIP=1 [LADDER_SKIP_REASON=\"...\"] git commit ..."
)


def _audit_line(root: Path, msg: str) -> None:
    # Тот же формат, что audit() в codex_review_gate.py (iso-ts + сообщение), но параметризован
    # по root — audit-путь codex_review_gate вычислен от cwd-репо и непригоден в tmp-репо тестов.
    log = root / _AUDIT_LOG_RELPATH
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")


def changed_paths_staged(root: Path) -> list[str]:
    """Пути, застейдженные относительно HEAD (`git diff --cached --name-only`)."""
    r = subprocess.run(["git", "diff", "--cached", "--name-only"], cwd=root, check=True,
                       capture_output=True, text=True)
    return [line for line in r.stdout.splitlines() if line]


def commit_touches_code(paths: list[str]) -> bool:
    """True если хоть один путь — код-путь (`is_code_path` из codex_review_gate).

    NB: пути из git diff/diff-tree ОТНОСИТЕЛЬНЫЕ (к корню репо коммита) — is_code_path
    в tmp-репо тестов их не абсолютизирует, что и требуется."""
    return any(is_code_path(p) for p in paths)


def ladder_enabled(root: Path) -> bool:
    """`<root>/.codex-gate.yaml` секция `ladder.enabled`. Файла нет / битый YAML / ключа нет →
    True (строже — лесенка требуется по умолчанию). Только явный `enabled: false` отключает."""
    data = _gate_config(root)
    if not isinstance(data, dict):
        return True
    ladder = data.get("ladder")
    if not isinstance(ladder, dict):
        return True
    return ladder.get("enabled", True) is not False


def _chain_valid_against(root: Path, expected_tree: str) -> bool:
    """Chain-валидность (спека §1/§2): simplify.tree_after == codereview.tree_before И
    codereview.tree_after == expected_tree (индекс на pre-commit, HEAD^{tree} на post-commit)."""
    simplify = read_marker(root, "simplify")
    codereview = read_marker(root, "code-review")
    return (simplify is not None and codereview is not None
            and simplify.get("tree_after") == codereview.get("tree_before")
            and codereview.get("tree_after") == expected_tree)


def check_precommit(root: Path) -> int:
    """Pre-commit гейт (спека §2). Порядок: (1) staged не трогает код → exempt; (2)
    ladder.enabled=false → пропуск; (3) LADDER_SKIP=1 → пропуск + аудит; (4) chain-валидация
    против РЕАЛЬНОГО индекса (`index_tree`) → 0, иначе abort (2) с self-healing инструкцией."""
    paths = changed_paths_staged(root)
    # exempt-у «не-код» верим только при недирти-конфиге (Codex code-R1: незастейдженное
    # ослабление .codex-gate.yaml не должно лаундерить код-коммит в exempt)
    if not commit_touches_code(paths) and classification_trustworthy(root, ""):
        return 0
    # enabled=false чтится тоже только из доверенного (совпадающего с index) конфига
    # (Codex code-R2: незастейдженный enabled:false гасил pre-commit без skip-аудита)
    if not ladder_enabled(root) and classification_trustworthy(root, ""):
        return 0
    if os.environ.get("LADDER_SKIP") == "1":
        reason = os.environ.get("LADDER_SKIP_REASON", "")
        _audit_line(root, f"LADDER_SKIP=1 session={_env_session()!r} reason={reason!r} — "
                          "pre-commit ladder-проверка ПРОПУЩЕНА")
        return 0
    if _chain_valid_against(root, index_tree(root)):
        return 0
    print(_CHAIN_INSTRUCTIONS, file=sys.stderr)
    return 2


def ledger_path(root: Path, sha: str) -> Path:
    return root / _LEDGER_DIR_RELPATH / f"{sha}.json"


def read_ledger(root: Path, sha: str) -> dict | None:
    """Читает post-commit ledger-запись. Битый JSON → None (fail-closed выше по стеку —
    деплой-гейт трактует отсутствие/битую запись как непокрытый коммит)."""
    p = ledger_path(root, sha)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def _write_ledger(root: Path, sha: str, payload: dict) -> None:
    p = ledger_path(root, sha)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(p.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    tmp.replace(p)   # атомарная публикация


def _git_out(root: Path, args: list[str]) -> str:
    r = subprocess.run(["git", *args], cwd=root, check=True, capture_output=True, text=True)
    return r.stdout.strip()


def _commit_parents(root: Path, sha: str) -> list[str]:
    parts = _git_out(root, ["rev-list", "--parents", "-n1", sha]).split()
    return parts[1:]   # parts[0] == sha сам


def _commit_changed_paths(root: Path, sha: str) -> list[str]:
    # --root: для корневого коммита (родителя нет) диффит против пустого дерева — все пути
    # считаются «изменёнными» (тот же критерий commit_touches_code применяется к полному списку).
    out = _git_out(root, ["diff-tree", "--no-commit-id", "--name-only", "-r", "--root", sha])
    return [line for line in out.splitlines() if line]


def _record_commit_impl(root: Path) -> None:
    head = _git_out(root, ["rev-parse", "HEAD"])
    if len(_commit_parents(root, head)) > 1:
        return   # merge-коммит — документированный остаток (спека §4 п.5), ничего не пишем
    tree = _git_out(root, ["rev-parse", "HEAD^{tree}"])
    ts = datetime.now(timezone.utc).isoformat()
    changed = _commit_changed_paths(root, head)
    # exempt-запись — только при недирти-конфиге (Codex code-R1, тот же лаундеринг: иначе
    # worktree-ослабление конфига чеканит exempt-noncode ledger для код-коммита; без записи —
    # fail-closed: деплой-гейт заблокирует диапазон)
    if not commit_touches_code(changed) and classification_trustworthy(root, "HEAD"):
        _write_ledger(root, head, {"passes": ["exempt-noncode"], "tree": tree, "ts": ts})
        return
    if os.environ.get("LADDER_SKIP") == "1":
        reason = os.environ.get("LADDER_SKIP_REASON", "")
        _write_ledger(root, head, {"skipped": True, "reason": reason, "tree": tree, "ts": ts})
        return
    if _chain_valid_against(root, tree):
        codereview = read_marker(root, "code-review") or {}
        _write_ledger(root, head, {
            "passes": list(DEPLOY_REQUIRED_PASSES), "tree": tree,
            "session": codereview.get("session", ""), "ts": ts,
        })
        return
    print(f"[ladder-gate] post-commit: HEAD {head[:12]} — код-коммит без валидной лесенки, "
          "ledger НЕ записан (деплой-гейт заблокирует диапазон, включающий этот коммит; "
          "прогони протокол begin/mark или LADDER_SKIP=1, если это было осознанно)",
          file=sys.stderr)


def record_commit(root: Path) -> None:
    """Post-commit хук (спека §3). Самодостаточно пересчитывает от HEAD — не абортит коммит
    (git не умеет), любой сбой (включая саму запись) — громко в stderr, не исключение наружу."""
    try:
        _record_commit_impl(root)
    except Exception as e:   # post-commit НИКОГДА не должен ронять git commit (спека §3)
        print(f"[ladder-gate] post-commit СБОЙ записи ledger: {type(e).__name__}: {e}",
              file=sys.stderr)
        print(traceback.format_exc(), file=sys.stderr)


# --- деплой-гейт по диапазону (спека Фазы 1.5 §4) ---

# Оверрайд эпохи для тестов: None → читать `ladder.epoch_sha` из `.codex-gate.yaml` root'а
# (решение 2 спеки плагина). Эпоха в конфиге допустима, потому что правка конфига гейтится
# (жёсткий код-путь) и видна Codex-ревью диффа; нет конфига → эпоха выключена (вся история
# проверяется — строже).
LADDER_EPOCH_SHA: str | None = None


def _effective_epoch(root: Path) -> "str | None":
    if LADDER_EPOCH_SHA is not None:
        return LADDER_EPOCH_SHA
    data = _gate_config(root)
    if not isinstance(data, dict):
        return None
    ladder = data.get("ladder")
    if not isinstance(ladder, dict):
        return None
    epoch = ladder.get("epoch_sha")
    return epoch if isinstance(epoch, str) and epoch.strip() else None


def _is_ancestor(root: Path, sha: str, ancestor_of: str) -> bool:
    r = subprocess.run(["git", "merge-base", "--is-ancestor", sha, ancestor_of],
                       cwd=root, capture_output=True)
    return r.returncode == 0


def _passes_complete(passes: object) -> bool:
    return isinstance(passes, list) and all(p in passes for p in DEPLOY_REQUIRED_PASSES)


def check_range(root: Path, baseline: str) -> int:
    """Деплой-гейт диапазона `baseline..HEAD` (спека §4, ML-L6). Для каждого коммита
    диапазона коммит ПОКРЫТ, если выполнено ЛЮБОЕ (порядок — дёшево→дорого):
      1. merge-коммит (>1 родителя) — документированный остаток, exempt с ГРОМКОЙ пометкой
         (итог диапазона ревьюит Codex-гейт независимо);
      2. эпоха задана (конфиг/оверрайд) И коммит — предок эпохи (grandfathering);
      3. ledger-запись существует, её `tree` совпадает с `<sha>^{tree}`, И
         (все КАНОНИЧЕСКИЕ `DEPLOY_REQUIRED_PASSES` присутствуют
          ИЛИ `passes == ["exempt-noncode"]`
          ИЛИ `skipped is True` — ГРОМКИЙ аудит в stderr, обход уже был осознанным на коммите).
    Иначе — НЕ покрыт. Непокрытые коммиты собираются и печатаются в конце; return 2 если хоть
    один есть, иначе 0 (пустой диапазон тоже 0). Намеренно НЕ читает `ladder.enabled` /
    `required_passes` из конфига (спека §4: коммит не должен ослаблять собственную
    проверку мутацией конфига — ML-L7)."""
    epoch = _effective_epoch(root)
    out = _git_out(root, ["rev-list", f"{baseline}..HEAD"])
    shas = [s for s in out.splitlines() if s]
    uncovered: list[str] = []
    for sha in shas:
        if len(_commit_parents(root, sha)) > 1:
            print(f"[ladder-gate] merge-коммит {sha[:12]} exempt — итог ревьюит Codex",
                  file=sys.stderr)
            continue
        if epoch is not None and _is_ancestor(root, sha, epoch):
            continue
        record = read_ledger(root, sha)
        if record is not None:
            tree = _git_out(root, ["rev-parse", f"{sha}^{{tree}}"])
            if record.get("tree") == tree:
                passes = record.get("passes")
                if _passes_complete(passes) or passes == ["exempt-noncode"]:
                    continue
                if record.get("skipped") is True:
                    print(f"[ladder-gate] коммит {sha[:12]} прошёл под LADDER_SKIP "
                          f"(reason={record.get('reason', '')!r}) — осознанный обход",
                          file=sys.stderr)
                    continue
        uncovered.append(sha)
    if uncovered:
        print("[ladder-gate] check-range: непокрытые коммиты диапазона "
              f"{baseline}..HEAD (нет валидной ladder-записи):", file=sys.stderr)
        for sha in uncovered:
            print(f"  {sha}", file=sys.stderr)
        return 2
    return 0


def main(argv: list[str]) -> int:
    if argv and argv[0] == "check-precommit":
        return check_precommit(Path.cwd())
    if argv and argv[0] == "record-commit":
        record_commit(Path.cwd())
        return 0
    if argv and argv[0] == "check-range":
        if len(argv) < 2:
            print("usage: ladder_gate.py check-range <baseline>", file=sys.stderr)
            return 1
        return check_range(Path.cwd(), argv[1])
    if len(argv) < 2 or argv[0] not in ("begin", "mark"):
        print("usage: ladder_gate.py begin|mark <pass> | check-precommit | record-commit | "
              "check-range <baseline>", file=sys.stderr)
        return 1
    cmd, pass_name = argv[0], argv[1]
    root = Path.cwd()
    try:
        if cmd == "begin":
            begin_pass(root, pass_name)
        else:
            mark_pass(root, pass_name)
    except LadderError as e:
        print(f"[ladder-gate] {e}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
