"""Ladder gate: tree-хэш «всё изменённое» (временный индекс) + begin/mark протокол
проходов /simplify → /code-review → security; pre-commit/post-commit энфорсмент; range-проверка
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
from typing import NamedTuple

try:
    import yaml
except ImportError:            # PyYAML может отсутствовать в системном python3 (решение 3):
    yaml = None                # конфиг нечитаем → безопасные строгие дефолты

# is_code_path — sibling-модуль в каталоге плагина: прямой импорт работает, когда каталог
# уже в sys.path (тесты через conftest, запуск обоих скриптов из одного каталога), фолбэк —
# при запуске как голый скрипт из произвольного cwd.
try:
    from codex_review_gate import (is_code_path, _trusted_git, TrustedGitError,
                                   _trusted_git_bin, _trusted_home, _GIT_ENV_ALLOW,
                                   _GIT_SAFE_ENV, _GIT_NEUTRALIZE, _TRUSTED_PATH_DIRS,
                                   _bootstrap_git, _has_git_marker, redact_secrets, _timed)
except ImportError:                                    # запуск как голый скрипт
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from codex_review_gate import (is_code_path, _trusted_git,  # type: ignore[no-redef]
                                   TrustedGitError, _trusted_git_bin, _trusted_home,
                                   _GIT_ENV_ALLOW, _GIT_SAFE_ENV, _GIT_NEUTRALIZE,
                                   _TRUSTED_PATH_DIRS, _bootstrap_git,
                                   _has_git_marker, redact_secrets, _timed)

DEPLOY_REQUIRED_PASSES = ("simplify", "code-review", "security")
# Легаси-набор: записи БЕЗ поля `ladder_schema` физически писал старый код, когда
# security-прохода не существовало — такой коммит не мог его иметь (grandfathering по
# ПРОИСХОЖДЕНИЮ записи, спека 2026-07-25-security-ladder-pass-design.md, EARS-5).
LEGACY_REQUIRED_PASSES = ("simplify", "code-review")
LADDER_SCHEMA = 2                     # текущая схема ledger-записи (пишется новым кодом)
GATE_CONFIG_NAME = ".codex-gate.yaml"

# Чем выполняется проход. НАМЕРЕННО без утверждений о конкретной машине: доступность команды
# и флаг `disable-model-invocation` — свойство окружения, а плагин едет в произвольные репо.
# Ревью 2026-07-26: зашитое «агент вызвать НЕ может» врёт там, где /code-review резолвится в
# вызываемую модель команду, и толкает агента к LADDER_SKIP; зашитое «иначе агент прогоняет
# security-фокус прозой» вообще подменяло проход самоаттестацией.
# Ключи обязаны покрывать DEPLOY_REQUIRED_PASSES: пропуск = KeyError на импорте (громко),
# а не тихая заглушка в операторском тексте (тест test_pass_runner_covers_all_passes).
# Остаётся текстом: подсказка ничего не энфорсит, машиночитаемое описание прохода
# {class, command, source} проектируется в тикете B1.
_PASS_RUNNER = {
    "simplify": "/simplify либо субагенты очистки",
    # Перечисляем ВЫЗЫВАЕМЫЕ АГЕНТОМ движки, а не только слэш-команду: платформа блокирует
    # агенту имя команды, но не движок, который она оборачивает. Прежний текст «/code-review»
    # толкал агента останавливаться и ждать человека там, где ждать не нужно (2026-07-26).
    "code-review": "/code-review, либо тот же движок напрямую (workflow-ревью), либо "
                   "независимый ревьюер: codex_review_gate.py companion-review / cursor",
    "security": "/security-review либо security-фокус по конституции AGENTS.md",
}
_RUN = "bash .githooks/gates-run ladder_gate.py"   # как гейт реально вызывается (см. README)
_RUN_UNKNOWN = "<ваш шим gates-run> ladder_gate.py"


def _run_cmd(root: Path) -> str:
    """Команда гейта в copy-paste форме. gates-init кладёт шим в `.githooks/`, но проект мог
    поставить хуки иначе (gates-init поддерживает чужой core.hooksPath) — там печатать
    `.githooks/gates-run` значит выдать строку, дающую command not found, то есть ровно тот
    провал, ради предотвращения которого подсказка и печатается."""
    return _RUN if (root / ".githooks" / "gates-run").is_file() else _RUN_UNKNOWN


def _repo_root() -> Path:
    """Корень репозитория, а НЕ cwd. `begin`/`mark`, запущенные из подкаталога, писали
    бухгалтерию в <subdir>/.claude/, а git запускает pre-commit из корня и там её не находил:
    коммит блокировался «цепочка не подтверждена», хотя все проходы были выполнены и отмечены,
    и оператор шёл за LADDER_SKIP. Вне git-репо поведение прежнее (cwd).

    Корень ищется доверенным bootstrap-git и обязан СОДЕРЖАТЬ cwd: голый `git rev-parse`
    подменяется PATH-шимом на второй, чистый репозиторий, после чего все доверенные операции
    добросовестно изучают ЧУЖОЙ корень, не находят staged-кода и выдают non-code освобождение
    без аудита (находка ревью 09.08.2026). Если маркер `.git` рядом есть, а корень не
    разрешился — падаем, а не откатываемся на cwd."""
    cwd = Path.cwd().resolve()
    r = _bootstrap_git("rev-parse", "--show-toplevel", cwd=cwd)
    if r is None or r.returncode != 0 or not r.stdout.strip():
        if _has_git_marker(cwd):
            raise GateRefusal("маркер .git есть, но корень репозитория не разрешился — "
                              "решения лесенки принимать не на чем")
        return cwd
    root = Path(r.stdout.strip()).resolve()
    if root != cwd and root not in cwd.parents:
        raise GateRefusal(f"git выдал корень {root}, не содержащий текущий каталог {cwd} — "
                          "похоже на подмену git; решения лесенки не принимаются")
    return root

# Правило одно на все проходы, поэтому печатается один раз, а не копией в каждой строке.
_RUNNER_RULE = (
    "Проход запускает агент, ЕСЛИ платформа даёт ему эту команду. Если платформа отвечает\n"
    "`disable-model-invocation` — агент обязан ПОПРОСИТЬ оператора набрать её и дождаться\n"
    "результата, а НЕ подменять проход собственным вычитыванием и НЕ помечать непройденное."
)

# Точные пути ladder-бухгалтерии для исключения из tree-хэша (ревью Task 1: широкий glob
# `.ladder-*` прятал бы от хэша и ПРОИЗВОЛЬНЫЙ файл/каталог под этим префиксом — сужено до
# конкретных литералов, по два на каждый канонический проход; всё прочее под
# .claude/.ladder-* попадает в хэш). Плоские пути
# (не pathspec-негации, см. compute_tree fix Task 4) — потребляются `git rm --cached`.
_BOOKKEEPING_PATHS = tuple(
    f".claude/.ladder-{name}"
    for p in DEPLOY_REQUIRED_PASSES for name in (p, f"pending-{p}")
)


class GateRefusal(TrustedGitError):
    """Гейт ОТКАЗАЛСЯ принимать решение (подмена git, неразрешимый корень) — это не поломка.

    Разница операционная: сломанный движок чинят и, при нужде, обходят с аудитом; отказ
    доверия обходить НЕЛЬЗЯ — обход и есть то, чего добивается подмена. До 2026-08-13 оба
    случая печатались одним баннером с готовым рецептом LADDER_SKIP."""


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
    r = _trusted_git("show", f"{ref}:{GATE_CONFIG_NAME}", cwd=root)
    return r.stdout.encode() if (r is not None and r.returncode == 0) else None


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


def _git_fail(what: str, r) -> str:
    """Текст отказа git'а вместе с кодом и stderr.

    Без них сообщение недиагностируемо: `не посчитать хэш X` не содержало
    `fatal: Unable to add (null) to database`, и причину инцидента 0.9.0 пришлось
    воспроизводить руками."""
    err = redact_secrets((r.stderr or "").strip())
    return f"{what} (git rc={r.returncode}){f': {err}' if err else ''} — tree-хэш не посчитать"


def _git_ok(root: Path, env: "dict | None", what: str, *args: str,
            stdin_bytes: "bytes | None" = None):
    """git-вызов, для которого ненулевой код — отказ движка, а не ответ."""
    r = _git_mutate(root, env, *args, stdin_bytes=stdin_bytes)
    if r.returncode != 0:
        raise TrustedGitError(_git_fail(what, r))
    return r


class _IndexEntry(NamedTuple):
    mode: str
    oid: str
    skip_worktree: bool


def _index_entries(root: Path) -> "dict[str, _IndexEntry | None]":
    """Всё, что git считает содержимым дерева, ИЗ ИНДЕКСА: `path -> запись | None`.

    `None` — путь с конфликтом (stage != 0): stage-0 записи для него в индексе нет, а правда
    лежит на диске (`git add -A` застейджил бы содержимое рабочего дерева одной stage-0
    записью). Отдельным списком его возвращать не нужно — `None` и означает «спроси диск».

    `-v` добавляет флаг состояния: `S` = SKIP_WORKTREE (sparse checkout). Именно по нему
    отличается «файла нет, потому что он вне конуса» (git считает запись неизменной) от
    «файла нет, потому что его удалили» (это изменение дерева). До 2026-08-13 оба случая
    трактовались как удаление, и в любом sparse-checkout репозитории `compute_tree` не мог
    совпасть с `index_tree` — лесенка была неисполнима (воспроизведено).

    Регистр буквы — ОТДЕЛЬНЫЙ бит `assume-unchanged`, а не другой вид записи: у записи с
    обоими битами флаг `s`, и трактовать его надо как skip-worktree (иначе sparse-файл с
    `assume-unchanged` снова выпал бы из дерева — находка ревью, раунд 3). Поэтому сравнение
    идёт по `tag.upper()`, а `assume-unchanged` на состав дерева не влияет вовсе: то, что
    лежит на диске, всё равно пере-хэшируется.

    ВАЖНО: `env=None` — читаем РЕАЛЬНЫЙ индекс. С `GIT_INDEX_FILE` от пустого tmp-индекса
    `ls-files` вернул бы пустоту с кодом 0, и дерево молча вышло бы пустым."""
    r = _git_ok(root, None, "не перечислить записи индекса", "ls-files", "-v", "-s", "-z")
    out: dict[str, "_IndexEntry | None"] = {}
    for entry in r.stdout.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) < 4 or not path:
            raise TrustedGitError(f"ls-files -v -s изменил формат: {entry!r}")
        tag, mode, oid, stage = parts[0], parts[1], parts[2], parts[3]
        if stage != "0":
            out[path] = None                  # unmerged: решает рабочее дерево
            continue
        out[path] = _IndexEntry(mode, oid, tag.upper() == "S")
    return out


def _from_index_unseen(rel: str, mode: str, oid: str,
                       head: "dict[str, tuple[str, str]]", why: str) -> "tuple[str, str]":
    """Единственная точка, где запись берётся из ИНДЕКСА, а не с диска.

    Всё, что не пере-хэшировано с диска, — содержимое, которого ревьюер физически не видит,
    а индекс наполняет ПРОВЕРЯЕМАЯ сторона. Отсюда правило, общее для всех таких записей:

        невидимое проходит ревью, только если оно НЕ МЕНЯЛОСЬ с последнего коммита.

    Совпало с HEAD — ревьюить нечего, законный рабочий процесс (sparse checkout,
    неинициализированный подмодуль) не страдает. Разошлось — это правка, которую никто не
    может прочитать, и благословлять её нельзя ни молча, ни вовсе.

    Правило живёт ОДНОЙ функцией сознательно: код-ревью 2026-08-13 три раунда подряд находило
    один и тот же класс в разных ветках (skip-worktree с файлом на диске, skip-worktree без
    файла, деинициализированный подмодуль). Чинить экземпляры по одному значило бы ждать
    четвёртый — общее правило закрывает и те формы, которые ещё не назвали."""
    if head.get(rel) != (mode, oid):
        raise GateRefusal(
            f"{rel!r}: {why}, а индекс разошёлся с HEAD — это изменение, которого ревьюер "
            f"физически не видит. Верни содержимое в рабочее дерево, чтобы его можно было "
            f"прочитать, — tree-хэш не посчитать")
    return mode, oid


def _head_entries(root: Path) -> "dict[str, tuple[str, str]]":
    """path -> (mode, oid) в HEAD. Пустой словарь, если HEAD ещё нет (репо без коммитов)."""
    r = _git_mutate(root, None, "ls-tree", "-r", "-z", "HEAD")
    if r.returncode != 0:
        return {}
    out: dict[str, tuple[str, str]] = {}
    for entry in r.stdout.split("\0"):
        if not entry:
            continue
        meta, _, path = entry.partition("\t")
        parts = meta.split()
        if len(parts) < 3 or not path:
            raise TrustedGitError(f"ls-tree изменил формат: {entry!r}")
        out[path] = (parts[0], parts[2])
    return out


def _untracked_paths(root: Path) -> "list[str]":
    """Неотслеживаемые и неигнорируемые пути — «всё изменённое» включает и их.

    Вложенный git-репозиторий (вендоренный клон) git отдаёт ОДНИМ путём со слэшем на конце и
    внутрь не заходит; `git add -A` делает из него gitlink. Слэш снимаем, иначе
    `update-index --cacheinfo` такой путь не примет, а сам каталог молча выпал бы из дерева
    вместе со всем поддеревом (та же тихая слепота, что у симлинка на файл)."""
    r = _git_ok(root, None, "не перечислить неотслеживаемые файлы",
                "ls-files", "-o", "--exclude-standard", "-z")
    return [p.rstrip("/") for p in r.stdout.split("\0") if p]


def _is_unseen(root: Path, rel: str, entry: "_IndexEntry | None") -> bool:
    """Берётся ли запись из ИНДЕКСА, а не с диска, — то есть невидима ли она ревьюеру.

    Один предикат на все формы (sparse-запись без файла, неинициализированный подмодуль):
    именно разъехавшиеся копии этого условия дали три раунда находок одного класса."""
    if entry is None:
        return False
    full = root / rel
    if entry.skip_worktree and not os.path.lexists(full):
        return True
    return entry.mode == "160000" and full.is_dir() and not (full / ".git").exists()


def _unseen_drift(root: Path) -> "str | None":
    """Путь, чьё НЕВИДИМОЕ содержимое разошлось с HEAD, либо None.

    Отдельно от `compute_tree`, потому что нужен на pre-commit: см. `check_precommit`."""
    tracked = _index_entries(root)
    unseen = [(rel, e) for rel, e in tracked.items() if _is_unseen(root, rel, e)]
    if not unseen:
        return None                       # исключение не применялось — перепроверять нечего
    head = _head_entries(root)
    return next((rel for rel, e in unseen if head.get(rel) != (e.mode, e.oid)), None)


def _tree_entry(root: Path, env: dict, rel: str, entry: "_IndexEntry | None",
                head: "dict[str, tuple[str, str]]") -> "tuple[str, str] | None":
    """(mode, oid) для одной записи дерева. None = записи в дереве быть не должно.

    Реализует таблицу «источник правды» из дизайна СВЕРХУ ВНИЗ и целиком: раньше решение
    «индекс или диск» принималось в трёх местах (цикл `compute_tree`, эта функция,
    `_gitlink_entry`), и одна строка таблицы из-за этого терялась.

    Пере-хэш с диска — смысл функции: он обходит clean-фильтры репозитория, racy-index и
    `assume-unchanged`, которыми проверяемая сторона могла бы подменить представление."""
    full = root / rel
    if _is_unseen(root, rel, entry):
        # Файл на диске ЕСТЬ → сюда не попадаем и хэшируем диск: иначе «застейджить злое,
        # вернуть на диск доброе» прошло бы мимо ревьюера.
        return _from_index_unseen(
            rel, entry.mode, entry.oid, head,
            "запись помечена skip-worktree и файла на диске нет" if entry.skip_worktree
            else "подмодуль не инициализирован")
    if full.is_symlink():
        # hash-object ПО ПУТИ разыменовывает ссылку: на цели-директории git умирает
        # (`fatal: Unable to add (null) to database` — инцидент 0.9.0), а на цели-файле МОЛЧА
        # отдаёт блоб содержимого цели, то есть врёт при режиме 120000. Блоб симлинка в git —
        # ровно текст цели, СЫРЫМИ байтами (цель может быть не-UTF-8), без перевода строки.
        h = _git_ok(root, env, f"не посчитать хэш симлинка {rel!r}",
                    "hash-object", "--no-filters", "-w", "--stdin",
                    stdin_bytes=os.readlink(os.fsencode(full)))
        return "120000", h.stdout.strip()
    if full.is_dir():
        # Признак gitlink даёт ДИСК (каталог с `.git` — в том числе НЕотслеживаемый вендоренный
        # клон, который `git add -A` тоже превращает в gitlink) либо ИНДЕКС с mode 160000
        # (подмодуль, который просто не инициализирован). Каталог, оказавшийся на месте
        # ОБЫЧНОЙ записи, подмодулем не является: раньше он фабриковал `160000` поверх
        # блоб-oid'а, а каталог с содержимым и вовсе ронял update-index
        # (`appears as both a file and as a directory`). Правильное поведение то же, что у
        # `git add -A`: сама запись уходит из дерева, а файлы внутри приезжают своими путями.
        # Сюда доходят только ВИДИМЫЕ каталоги: невидимые (подмодуль без содержимого) уже
        # разобраны выше. Значит либо инициализированный подмодуль, либо вложенный
        # репозиторий, либо обычный каталог на месте не-gitlink записи — у последнего запись
        # уходит из дерева, а файлы внутри приезжают своими путями, как у `git add -A`.
        return _gitlink_entry(root, rel) if (full / ".git").exists() else None
    if full.is_file():
        h = _git_ok(root, env, f"не посчитать хэш {rel!r}",
                    "hash-object", "--no-filters", "-w", "--", rel)
        return ("100755" if os.access(full, os.X_OK) else "100644"), h.stdout.strip()
    # Ни файла, ни симлинка, ни каталога: удалённый путь (запись обязана уйти из дерева) либо
    # спецфайл, который `git add -A` тоже не берёт. Обе ветки совпадают с git — тихо пропустить
    # здесь ЗНАЧИТ то же, что делает git, а не «не поняли и потеряли».
    return None


def _gitlink_entry(root: Path, rel: str) -> "tuple[str, str] | None":
    """Подмодуль: какой коммит попадёт в дерево. None = записи быть не должно.

    Подмодуль не файл и не симлинк, поэтому файловая ветка его пропускала — и дерево не могло
    совпасть с `index_tree` НИКОГДА (в индексе gitlink есть всегда). В репозитории с
    подмодулем лесенка была неисполнима так же, как с симлинком на директорию (находка
    дизайн-ревью 2026-08-13, воспроизведена: честная цепочка давала check_precommit == 2).

    Семантика — «что застейджил бы `git add -A`»: инициализированный отдаёт свой HEAD (сдвиг
    указателя обязан быть виден), неинициализированный — oid индекса (`git add -A` его не
    трогает), удалённый каталог выпадает из дерева, как выпадает удалённый файл."""
    full = root / rel
    h = _git_mutate(full, None, "rev-parse", "HEAD")
    if h.returncode != 0:
        # Инициализирован, но HEAD не читается: состояние рабочего дерева неизвестно.
        # Подставить индекс значило бы объявить совпадение, которого мы не проверяли.
        raise TrustedGitError(_git_fail(f"не прочитать HEAD подмодуля {rel!r}", h))
    return "160000", h.stdout.strip()


def compute_tree(root: Path) -> str:
    """TREE-хэш «всё изменённое»: ПУСТОЙ временный индекс, GIT_INDEX_FILE=<tmp>
    git add -A && git write-tree (полный re-hash). Реальный индекс НЕ мутируется.

    Ladder-бухгалтерия (`.claude/.ladder-*`) исключена: иначе запись pending/маркера
    самим протоколом меняла бы дерево между соседними begin/mark-вызовами
    (self-referential — маркер о состоянии кода не должен зависеть от файла,
    описывающего этот же маркер). Исключение — просто пропуск пути при сборке; ни
    `add -A`, ни pathspec-негаций, на которых это спотыкалось раньше, здесь больше нет."""
    with _timed("compute_tree") as _span, tempfile.TemporaryDirectory() as td:
        # ПУСТОЙ tmp-индекс, НЕ копия реального (ревью Task 3): shutil.copy сбрасывал mtime
        # индекса в «сейчас», отключая git-детекцию racy-правок — same-size правка в ту же
        # секунду (типичный фикс /simplify) могла отдать STALE blob → ложный tree-хэш на
        # chokepoint'е целостности. Пустой индекс = полный re-hash рабочего дерева (репо
        # маленький, цена мизерна), иммунно к racy-index по построению.
        tmp_index = Path(td) / "index"
        env = dict(os.environ)
        env["GIT_INDEX_FILE"] = str(tmp_index)
        # `git add` ЗАПУСКАЕТ clean-фильтры репозитория (`.gitattributes`), поэтому дерево
        # считалось бы от преобразованного содержимого, а не от того, что лежит на диске.
        # Собираем индекс plumbing'ом: hash-object --no-filters + update-index --cacheinfo.
        tracked = _index_entries(root)
        # HEAD нужен для любой невидимой записи. Условной загрузки нет сознательно: она уже
        # один раз протухла, когда к skip-worktree добавились неинициализированные подмодули.
        head = _head_entries(root)
        skip = set(_BOOKKEEPING_PATHS)
        # dict.fromkeys — дедуп с сохранением порядка git'а (он уже отсортирован)
        listing = dict.fromkeys([*tracked, *_untracked_paths(root)])
        _span.scale = len(listing)              # то, что РЕАЛЬНО хэшируется, а не только tracked
        for rel in listing:
            if rel in skip:
                continue
            got = _tree_entry(root, env, rel, tracked.get(rel), head)
            if got is None:
                continue                      # записи в дереве быть не должно (удаление)
            mode, blob = got
            _git_ok(root, env, f"update-index отверг {rel!r}",
                    "update-index", "--add", "--cacheinfo", f"{mode},{blob},{rel}")
        return _git_ok(root, env, "write-tree не удался", "write-tree").stdout.strip()


def index_tree(root: Path) -> str:
    """git write-tree РЕАЛЬНОГО индекса (для pre-commit — ровно то, что закоммитится)."""
    with _timed("index_tree"):
        return _git_ok(root, None, "write-tree реального индекса не удался",
                       "write-tree").stdout.strip()


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
    Для КАЖДОГО прохода кроме первого валидирует chain-start: текущий tree ==
    tree_after ПРЕДЫДУЩЕГО прохода канонического порядка, иначе LadderError (ручная правка
    между проходами / предыдущий проход не запускался). EARS-2 security-спеки."""
    if pass_name not in DEPLOY_REQUIRED_PASSES:
        raise LadderError(f"неизвестный проход {pass_name!r} — ожидается один из "
                          f"{DEPLOY_REQUIRED_PASSES}")
    # `with` на теле, а не декоратор: span обязан начаться ПОСЛЕ валидации имени прохода —
    # иначе опечатка в аргументе порождала бы событие «фаза упала», которого не было.
    with _timed("begin_pass"):
        _begin_pass_inner(root, pass_name)


def _begin_pass_inner(root: Path, pass_name: str) -> None:
    tree = compute_tree(root)
    idx = DEPLOY_REQUIRED_PASSES.index(pass_name)
    if idx > 0:
        prev = DEPLOY_REQUIRED_PASSES[idx - 1]
        prev_marker = read_marker(root, prev)
        if prev_marker is None or prev_marker.get("tree_after") != tree:
            raise LadderError(
                f"{pass_name}: старт цепочки не совпадает с {prev}.tree_after — "
                f"сначала пройди {prev} (или ручная правка сломала цепочку между проходами)")
    _atomic_write(_pending_path(root, pass_name), {
        "tree_before": tree,
        "ts": datetime.now(timezone.utc).isoformat(),
    })
    # Роль печатаем в момент begin — это точка, где путают «агент запустит» и «оператор наберёт».
    # Команда печатается в исполнимой форме (через шим): ladder_gate.py не лежит ни в PATH,
    # ни в рабочем каталоге — скопированная «как есть» строка дала бы command not found.
    print(f"[ladder-gate] begin {pass_name}: tree_before снят. "
          f"Проход выполняется через {_PASS_RUNNER[pass_name]}. "
          f"После РЕАЛЬНО выполненного прохода: {_run_cmd(root)} mark {pass_name}",
          file=sys.stderr)


def mark_pass(root: Path, pass_name: str) -> None:
    """pending обязателен (LadderError если нет). Consume-then-publish (R8):
    удаляет pending ПЕРВЫМ, затем атомарно публикует маркер {tree_before, tree_after,
    session, ts}. Повторный mark без нового begin → LadderError (R7, анти-replay)."""
    if pass_name not in DEPLOY_REQUIRED_PASSES:   # симметрия с begin_pass (ревью Task 1)
        raise LadderError(f"неизвестный проход {pass_name!r} — ожидается один из "
                          f"{DEPLOY_REQUIRED_PASSES}")
    with _timed("mark_pass"):
        _mark_pass_inner(root, pass_name)


def _mark_pass_inner(root: Path, pass_name: str) -> None:
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

def _chain_instructions(root: Path) -> str:
    """Сообщение заблокированного коммита. Функция, а не константа: команду надо печатать
    в форме, исполнимой ИМЕННО В ЭТОМ репо (ревью 2026-07-26: begin печатал проверенный путь,
    а это сообщение — жёстко зашитый, хотя копируют из него чаще)."""
    run = _run_cmd(root)
    return "\n".join([
        f"[ladder-gate] цепочка {' → '.join(DEPLOY_REQUIRED_PASSES)} не подтверждена "
        f"для коммита.",
        "Протокол:",
        *(f"  {i}. {run} begin {p} → проход {p} ({_PASS_RUNNER[p]}) → {run} mark {p}"
          for i, p in enumerate(DEPLOY_REQUIRED_PASSES, 1)),
        "Затем закоммить снова.",
        _RUNNER_RULE,
        "ВАЖНО: mark ставится только после РЕАЛЬНО выполненного прохода. Гейт проверяет\n"
        "порядок и неизменность дерева между begin и mark, но НЕ доказывает, что проход\n"
        "состоялся: пометка непройденного прохода делает гейт зелёным без ревью.",
        "Обход (осознанно, с аудитом): LADDER_SKIP=1 [LADDER_SKIP_REASON=\"...\"] git commit ...",
    ])


def _audit_line(root: Path, msg: str) -> None:
    # Тот же формат, что audit() в codex_review_gate.py (iso-ts + сообщение), но параметризован
    # по root — audit-путь codex_review_gate вычислен от cwd-репо и непригоден в tmp-репо тестов.
    log = root / _AUDIT_LOG_RELPATH
    log.parent.mkdir(parents=True, exist_ok=True)
    with open(log, "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")


def changed_paths_staged(root: Path) -> list[str]:
    """Пути, застейдженные относительно HEAD (`git diff --cached --name-only`)."""
    # Голый git позволял шиму выйти нулём с ПУСТЫМ выводом: код-коммит выглядел
    # не-кодовым, check_precommit возвращал 0 до проверки лесенки и без записи скипа.
    out = _git_out(root, ["diff", "--cached", "--name-only"])
    return [line for line in out.splitlines() if line]


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
    """Chain-валидность по ВСЕМ звеньям канонического порядка (EARS-3 security-спеки):
    для каждой соседней пары `prev.tree_after == next.tree_before`, и `последний.tree_after ==
    expected_tree` (индекс на pre-commit, HEAD^{tree} на post-commit). Разрыв ЛЮБОГО звена или
    отсутствие любого маркера → невалидно (SEC2/SEC3/SEC17/SEC18)."""
    markers = [read_marker(root, name) for name in DEPLOY_REQUIRED_PASSES]
    if any(m is None for m in markers):
        return False
    for prev, nxt in zip(markers, markers[1:]):
        if prev.get("tree_after") != nxt.get("tree_before"):
            return False
    return markers[-1].get("tree_after") == expected_tree


_ENGINE_BROKEN_BANNER = "[ladder-gate] ⛔ ДВИЖОК ГЕЙТА СЛОМАН — лесенка НЕИСПОЛНИМА"
_GATE_REFUSAL_BANNER = "[ladder-gate] ⛔ ГЕЙТ ОТКАЗАЛСЯ РЕШАТЬ — доверие к git нарушено"


def _engine_broken_message(exc: BaseException) -> str:
    """Отказ движка обязан читаться иначе, чем непройденный проход.

    Инцидент 0.9.0: сломанный `compute_tree` давал ровно то же «цепочка не подтверждена», что
    и собственная забывчивость оператора, а подсказка в конце вела прямо к LADDER_SKIP. Отказ
    инфраструктуры выглядел как забытый проход и подталкивал к обходу гейта — поломка прожила
    двое суток незамеченной.

    Ловим ЛЮБОЕ исключение, а не перечень типов: форм отказа нашлось четыре за один день
    (симлинк-на-директорию, битый симлинк, не-UTF-8 цель, sparse checkout), а не-UTF-8 ИМЯ
    файла на APFS даже не воспроизводится. Аллоулист форм тут невозможен. Это не
    проглатывание: печатается и баннер, и полный трейсбек, код возврата ненулевой, ни одна
    ветка не начинает пропускать коммит — меняется только то, что оператор читает первым."""
    refusal = isinstance(exc, GateRefusal)
    head = (_GATE_REFUSAL_BANNER if refusal else _ENGINE_BROKEN_BANNER)
    tail = ([
        "Обходить НЕЛЬЗЯ: обход — ровно то, чего добивается подмена. Разберись, почему git",
        "разрешается не туда, и только потом коммить.",
    ] if refusal else [
        "Если коммит нужен ДО починки — это осознанный обход ОТКАЗА ДВИЖКА, с аудитом:",
        '  LADDER_SKIP=1 LADDER_SKIP_REASON="движок гейта сломан: <причина>" git commit ...',
    ])
    return "\n".join([
        head,
        f"Причина: {exc}",
        *([] if refusal else [
            "Это ОТКАЗ ИНФРАСТРУКТУРЫ, а не непройденный проход: ни begin, ни mark не могут",
            "отработать, пока причина не устранена. Проходы тут ни при чём — чинить движок.",
        ]),
        "Диагностика:",
        # format_exception(exc), а не format_exc(): функция не обязана вызываться внутри
        # except-блока и не должна молча печатать "NoneType: None" вне его.
        "".join(traceback.format_exception(exc)).rstrip(),
        *tail,
    ])


@_timed("check_precommit")
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
    try:
        if _chain_valid_against(root, index_tree(root)):
            # Решение по НЕВИДИМЫМ записям принимал compute_tree в момент mark — против HEAD,
            # каким он был ТОГДА. А HEAD — обычная ссылка: её переставляют на чужую ветку
            # (`git symbolic-ref`) НЕ трогая ни индекс, ни рабочее дерево, проводят лесенку и
            # возвращают обратно. Маркеры честные, ledger полный, деплой-гейт доволен — а в
            # коммит уезжает содержимое, которого никто не видел (находка security-ревью
            # 2026-08-13, воспроизведена end-to-end). Поэтому там, где исключение вообще
            # применялось, оно перепроверяется ЗДЕСЬ — против того HEAD, на который коммит
            # реально ложится. Сверять целиком compute_tree == index_tree нельзя: незастейдженная
            # правка в дереве законна и коммита не касается, а блокировала бы его.
            drift = _unseen_drift(root)
            if drift is not None:
                print(_engine_broken_message(GateRefusal(
                    f"{drift!r}: невидимое содержимое (нет на диске, есть в индексе) разошлось "
                    f"с HEAD, на который ложится коммит. Лесенка могла быть пройдена против "
                    f"ДРУГОГО HEAD. Верни содержимое в рабочее дерево и пройди заново")),
                    file=sys.stderr)
                return 2
            return 0
        # Прежде чем звать «пройди лесенку», убедись, что её физически МОЖНО пройти: маркеров
        # нет ровно тогда, когда begin/mark падали. Проба стоит один tree-хэш и только здесь,
        # на уже-провальной ветке.
        compute_tree(root)
    except Exception as e:                      # noqa: BLE001 — граница хука, см. баннер
        print(_engine_broken_message(e), file=sys.stderr)
        return 2                                # fail-closed: коммит по-прежнему блокирован
    print(_chain_instructions(root), file=sys.stderr)
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


def _git_mutate(root: Path, env: "dict | None", *args: str,
                stdin_bytes: "bytes | None" = None):
    """Мутации индекса/дерева: абсолютный бинарь и санированное окружение, как в слое.
    Отдельно от `_trusted_git`, потому что нуждается в GIT_INDEX_FILE.

    Канал ВСЕГДА двоичный, декодируем сами. `text=True` кодирует stdin и декодирует stdout
    локалью и строго: цель симлинка может содержать любые байты кроме NUL, и surrogate-escape
    от `os.readlink` ронял его `UnicodeEncodeError` — мимо диагностики, потому что это не
    `TrustedGitError` (находка дизайн-ревью 2026-08-13). Путь от git'а тоже не обязан быть
    валидным UTF-8, поэтому stdout — `surrogateescape` (суррогаты возвращаются в исходные
    байты при передаче строки обратно аргументом subprocess), а stderr — `replace`, чтобы
    диагностика печаталась всегда."""
    git = _trusted_git_bin()
    if git is None:
        raise TrustedGitError("доверенный git недоступен — индекс/дерево не построить")
    base = {k: v for k, v in os.environ.items() if k in _GIT_ENV_ALLOW}
    base["HOME"] = str(_trusted_home())
    base["PATH"] = os.pathsep.join(_TRUSTED_PATH_DIRS)
    base.update(_GIT_SAFE_ENV)
    if env and env.get("GIT_INDEX_FILE"):
        base["GIT_INDEX_FILE"] = env["GIT_INDEX_FILE"]
    r = subprocess.run([git, *_GIT_NEUTRALIZE, *args], cwd=root,
                       capture_output=True, input=stdin_bytes, env=base)
    return subprocess.CompletedProcess(r.args, r.returncode,
                                       r.stdout.decode("utf-8", "surrogateescape"),
                                       r.stderr.decode("utf-8", "replace"))


def _git_out(root: Path, args: list[str]) -> str:
    """Все чтения лесенки — через доверенный слой. Голый git позволял шиму вернуть пустой
    `rev-list baseline..HEAD`, и check_range одобрял диапазон без evidence и без аудита."""
    r = _trusted_git(*args, cwd=root)
    if r is None or r.returncode != 0:
        raise TrustedGitError(f"доверенный git недоступен для {args[0]!r} — лесенка не может "
                              "принять решение")
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
        _write_ledger(root, head, {"passes": ["exempt-noncode"], "tree": tree, "ts": ts,
                                   "ladder_schema": LADDER_SCHEMA})
        return
    if os.environ.get("LADDER_SKIP") == "1":
        reason = os.environ.get("LADDER_SKIP_REASON", "")
        _write_ledger(root, head, {"skipped": True, "reason": reason, "tree": tree, "ts": ts,
                                   "ladder_schema": LADDER_SCHEMA})
        return
    if _chain_valid_against(root, tree):
        last = read_marker(root, DEPLOY_REQUIRED_PASSES[-1]) or {}
        _write_ledger(root, head, {
            "passes": list(DEPLOY_REQUIRED_PASSES), "tree": tree,
            "session": last.get("session", ""), "ts": ts,
            "ladder_schema": LADDER_SCHEMA,
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
    """Через доверенный слой и fail-closed. Голый `git` тут был прямым fail-open: при любом
    заданном `ladder.epoch_sha` PATH-шим с exit 0 объявлял ЛЮБОЙ коммит древним, и все
    непокрытые лесенкой коммиты молча получали освобождение (находка ревью 09.08.2026)."""
    # Не через _git_out: exit 1 здесь — легитимный ответ «не предок», а не сбой.
    r = _trusted_git("merge-base", "--is-ancestor", sha, ancestor_of, cwd=root)
    if r is None or r.returncode not in (0, 1):
        raise TrustedGitError("доверенный git недоступен — эпоху не проверить, освобождение "
                              "не выдаётся")
    return r.returncode == 0


def _required_for_record(record: dict) -> tuple:
    """Набор проходов, обязательный для ЭТОЙ записи, по её происхождению (EARS-5):
    есть `ladder_schema >= 2` → текущий канонический набор; поля нет (легаси, писал старый код
    до появления security-прохода) → легаси-набор. Невалидная/меньшая схема → трактуем как
    легаси (fail-safe: не блокируем старые диапазоны из-за мусора в поле)."""
    schema = record.get("ladder_schema")
    if isinstance(schema, int) and not isinstance(schema, bool) and schema >= 2:
        return DEPLOY_REQUIRED_PASSES
    return LEGACY_REQUIRED_PASSES


def _passes_complete(passes: object, record: dict) -> bool:
    """Полнота набора проходов; обязательный набор определяется ПРОИСХОЖДЕНИЕМ записи."""
    return isinstance(passes, list) and all(p in passes for p in _required_for_record(record))


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
                if _passes_complete(passes, record) or passes == ["exempt-noncode"]:
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


def range_skips(root: Path, baseline: str) -> "list[str]":
    """SHA коммитов диапазона baseline..HEAD с ledger-записью skipped=true (спека inframon-
    интерфейса R1-F3): исторические LADDER_SKIP-обходы для вердикта деплой-гейта —
    `covered-with-skips` вместо маскирующего `covered`."""
    out = _git_out(root, ["rev-list", f"{baseline}..HEAD"])
    skipped = []
    for sha in out.splitlines():
        if not sha:
            continue
        rec = read_ledger(root, sha)
        if rec is not None and rec.get("skipped") is True:
            skipped.append(sha)
    return skipped


def main(argv: list[str]) -> int:
    try:
        return _dispatch(argv)
    except LadderError as e:
        print(f"[ladder-gate] {e}", file=sys.stderr)
        return 2
    except Exception as e:                      # noqa: BLE001 — граница CLI, не глотание:
        print(_engine_broken_message(e), file=sys.stderr)   # баннер + трейсбек + код 3
        return 3


def _dispatch(argv: list[str]) -> int:
    if argv and argv[0] == "check-precommit":
        return check_precommit(_repo_root())
    if argv and argv[0] == "record-commit":
        record_commit(_repo_root())
        return 0
    if argv and argv[0] == "check-range":
        if len(argv) < 2:
            print("usage: ladder_gate.py check-range <baseline>", file=sys.stderr)
            return 1
        return check_range(_repo_root(), argv[1])
    if len(argv) < 2 or argv[0] not in ("begin", "mark"):
        print("usage: ladder_gate.py begin|mark <pass> | check-precommit | record-commit | "
              "check-range <baseline>", file=sys.stderr)
        return 1
    cmd, pass_name = argv[0], argv[1]
    root = _repo_root()
    if cmd == "begin":
        begin_pass(root, pass_name)
    else:
        mark_pass(root, pass_name)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
