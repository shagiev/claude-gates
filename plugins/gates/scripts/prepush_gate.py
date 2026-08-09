"""Pre-push интеграционный гейт.

Спека: docs/2026-07-26-prepush-integration-gate-design.md (ред. 5, цикл ревью закрыт на
хард-капе после 5 раундов). Обещание гейта ровно одно:

    слияние пушимого коммита с базой НЕ конфликтует.

Тесты на смерженной пробе запускаются, красные блокируют, но ЗЕЛЁНЫЕ НЕ ДОКАЗЫВАЮТ НИЧЕГО:
исполняемый код определяется не деревом (editable install/venv/NODE_PATH разрешают импорт в
НАСТОЯЩЕЕ дерево), поэтому проба может отработать, не выполнив слияния. Пометка об этом
печатается всегда, когда тесты запускались (И5) — иначе `merge_test_command: /bin/true`
выглядел бы покрытием.
"""
from __future__ import annotations

import contextlib
import hashlib
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

#: Транспорты, запрещённые для сетевой операции. `ext::` исполняет ПРОИЗВОЛЬНУЮ команду —
#: это RCE, а не выбор источника. Значение, начинающееся с `-`, инжектит опцию в argv.
#: Локальные пути и `file://` НЕ запрещаем: они не исполняют код, а «remote указывает на
#: подконтрольный локальный репозиторий» — та же граница, что R-ARTIFACT-PROVENANCE
#: (владелец машины), и запрет ломал бы легитимные локальные bare-репозитории.
_FORBIDDEN_REMOTE_PREFIXES = ("ext::", "-")


def _fetch_remote_allowed(url: str) -> bool:
    low = (url or "").strip().lower()
    return bool(low) and not low.startswith(_FORBIDDEN_REMOTE_PREFIXES)


def _prepush_fetch(root: "Path", remote: str, branch: str, timeout: int):
    """Сетевая операция — ОТДЕЛЬНЫЙ адаптер: `protocol.*.allow=never` несовместим с fetch по
    построению, поэтому транспорт разрешается явно и URL валидируется. Всё остальное
    (абсолютный бинарь, аллоулист окружения, нейтрализация исполняемых конфигов) — как в слое."""
    git = _trusted_git_bin()
    if git is None:
        raise TrustedGitError("доверенный git недоступен — fetch не выполнить безопасно")
    url = _prepush_git("remote", "get-url", remote, cwd=root)
    if url.returncode != 0:
        # remote неизвестен/не читается — это ДОСТУПНОСТЬ, а не безопасность: пусть решает
        # существующая политика on_fetch_failure (по умолчанию блок, скип аудируется).
        return subprocess.CompletedProcess([], 128, "", f"remote {remote!r} не читается")
    if not _fetch_remote_allowed(url.stdout.strip()):
        # А это уже безопасность: `ext::` исполняет произвольную команду.
        raise TrustedGitError(
            f"remote {remote!r} использует запрещённый транспорт (`ext::` исполняет "
            "произвольную команду) — сетевая операция отклонена")
    env = {k: v for k, v in os.environ.items() if k in _GIT_ENV_ALLOW}
    env["HOME"] = str(_trusted_home())
    env["PATH"] = os.pathsep.join(_TRUSTED_PATH_DIRS)
    env.update({k: v for k, v in _GIT_SAFE_ENV.items()})
    # убираем ПАРУ `-c protocol.allow=never` целиком: одиночный висячий `-c` съедал
    # следующий аргумент как значение конфига, и `fetch` переставал быть подкомандой
    pairs = list(zip(_GIT_NEUTRALIZE[::2], _GIT_NEUTRALIZE[1::2]))
    neutralize = tuple(x for flag, val in pairs if val != "protocol.allow=never"
                       for x in (flag, val))
    # Проверки URL НЕДОСТАТОЧНО: git исполняет `remote.<name>.uploadpack` как КОМАНДУ на
    # локальном транспорте, а `remote.<name>.vcs` и `core.gitProxy` дают то же самое другими
    # путями. Воспроизведено ревью 09.08.2026 на системном git: `remote.probe.uploadpack`
    # исполнилась при обычном pre-push fetch правами хука. Гасим все три и жёстко закрепляем
    # upload-pack: неотразимая часть — именно КОМАНДА, а не адрес.
    # `--upload-pack` из командной строки перебивает `remote.<name>.uploadpack` (замерено
    # 09.08.2026), поэтому дублировать его через `-c` не нужно — иначе git ругается на два
    # значения. А вот `remote.<name>.vcs` гасить ПУСТЫМ значением нельзя: git тогда ищет
    # helper `git-remote-` и fetch ломается. Поэтому vcs — ОТКЛОНЯЕМ, как filter-драйверы.
    vcs = _prepush_git("config", "--get", f"remote.{remote}.vcs", cwd=root)
    if vcs.returncode == 0 and vcs.stdout.strip():
        raise TrustedGitError(
            f"remote {remote!r} задаёт vcs={vcs.stdout.strip()!r} — git исполнит внешний "
            "transport-helper; сетевая операция отклонена")
    exec_conf = ("-c", "core.gitProxy=")
    return subprocess.run([git, *neutralize, *exec_conf, "fetch", "--quiet",
                           "--upload-pack", "git-upload-pack", "--end-of-options",
                           remote, branch],
                          cwd=root, capture_output=True, text=True, timeout=timeout, env=env)


def _prepush_git(*args: str, cwd: "Path | None" = None):
    """Локальные чтения pre-push — через доверенный слой. Голый git позволял шиму отдать
    `rev-list --count` = "0" и пропустить интеграционные проверки без аудита."""
    r = _trusted_git(*args, cwd=cwd)
    if r is None:
        raise TrustedGitError("доверенный git недоступен — pre-push не может принять решение")
    return r



try:
    from codex_review_gate import (GATE_CONFIG_NAME, _config_section_at_ref,
                                   _trusted_git, _trusted_git_bin, _GIT_ENV_ALLOW,
                                   _GIT_SAFE_ENV, _GIT_NEUTRALIZE, _TRUSTED_PATH_DIRS,
                                   _trusted_home, TrustedGitError,
                                   _deploy_section_hash, _run_empirical, redact_secrets)
    from ladder_gate import _audit_line
except ImportError:                                    # запуск как голый скрипт
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from codex_review_gate import (GATE_CONFIG_NAME, _config_section_at_ref,  # type: ignore
                                   _trusted_git, _trusted_git_bin, _GIT_ENV_ALLOW,
                                   _GIT_SAFE_ENV, _GIT_NEUTRALIZE, _TRUSTED_PATH_DIRS,
                                   _trusted_home, TrustedGitError,
                                   _deploy_section_hash, _run_empirical, redact_secrets)
    from ladder_gate import _audit_line                                       # type: ignore

import re

_FETCH_POLICIES = ("block", "skip-audited")
# Компоненты ref'а: только безопасный набор и БЕЗ ведущего дефиса. base_ref приходит из
# .codex-gate.yaml ПУШИМОГО коммита, то есть может быть написан другим человеком, и уходит
# позиционным argv в git fetch/rev-parse/merge-tree. Значение вида
# `--upload-pack=touch /tmp/pwned/.` git трактует как опцию и ИСПОЛНЯЕТ команду (проверено
# эксплойтом на security-проходе 2026-07-26). Валидации «строка и есть слэш» было мало.
_REF_COMPONENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")
_DEFAULT_BASE_REF = "origin/main"
_DEFAULT_TEST_TIMEOUT = 900
_DEFAULT_FETCH_TIMEOUT = 120


class ConfigError(Exception):
    """Битая/неизвестная настройка `integration.*` — блок, без тихого фолбэка."""


@dataclass(frozen=True)
class PushRef:
    local_ref: str
    local_sha: str
    remote_ref: str
    remote_sha: str

    @property
    def is_delete(self) -> bool:
        return set(self.local_sha) == {"0"}

    @property
    def branch(self) -> str:
        """ПОЛНЫЙ путь ветки без `refs/heads/`.

        Ревью 2026-07-26 (critical): `rsplit("/", 1)` превращал `refs/heads/release/main`
        в `main`, ветка считалась базовой, и предсказание слияния ПРОПУСКАЛОСЬ — обычное
        вложенное имя снимало единственное обещание гейта.

        Второе (то же ревью): при `git push origin HEAD:refs/heads/x` git передаёт local_ref
        = `HEAD`, и branch становился «HEAD» — а `refs/remotes/origin/HEAD` есть в любом клоне,
        поэтому детектор пересоздания давал ПОСТОЯННЫЙ ложный блок, из которого нет выхода
        кроме INTEGRATION_SKIP. Имя ветки берём из целевого ref'а, когда источник — не ветка."""
        if self.local_ref.startswith("refs/heads/"):
            return self.local_ref.removeprefix("refs/heads/")
        return self.remote_ref.removeprefix("refs/heads/")


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reason: str = ""


@dataclass(frozen=True)
class MergeResult:
    status: str                      # clean | conflict | unknown
    tree: str = ""
    paths: list[str] = field(default_factory=list)
    detail: str = ""


@dataclass(frozen=True)
class IntegrationConfig:
    base_ref: str = _DEFAULT_BASE_REF
    on_fetch_failure: str = "block"
    merge_test_command: str = ""
    test_timeout: int = _DEFAULT_TEST_TIMEOUT
    fetch_timeout: int = _DEFAULT_FETCH_TIMEOUT


# ═══ Вход pre-push ═══

def parse_lines(text: str) -> list[PushRef]:
    """stdin pre-push: `<local ref> <local sha> <remote ref> <remote sha>` построчно."""
    return [PushRef(*p) for p in (raw.split() for raw in text.splitlines()) if len(p) == 4]


def _parse_base_ref(base_ref: str) -> tuple[str, str]:
    remote, _, branch = base_ref.partition("/")
    return remote, branch


def classify(line: PushRef, remote: str, base_ref: str) -> str:
    """delete | tag | base | branch. Пропуск — ПО ПРОВЕРКАМ, а не по строке целиком:
    для базовой ветки предсказание слияния неприменимо, а анти-самоослабление — вполне."""
    if line.is_delete:
        return "delete"
    if line.local_ref.startswith("refs/tags/"):
        return "tag"
    base_remote, base_branch = _parse_base_ref(base_ref)
    # Сравнение по ПАРЕ (remote, branch): у форка (пуш в origin, база upstream/main) совпадение
    # по одному имени ветки пропустило бы предсказание слияния — fail-open обещания (E2а).
    if remote == base_remote and line.branch == base_branch:
        return "base"
    return "branch"


# ═══ Конфиг ═══

def integration_section(root: Path, ref: str) -> tuple[str, "dict | None"]:
    """Трёхстатусное чтение секции `integration` на ref: absent | enabled | unreadable.
    Отсутствие ДОКАЗЫВАЕТСЯ (`ls-tree` прочитан, пути нет); сбой чтения — «unreadable»,
    а не «absent» (И3, тот же контракт, что у остальных секций конфига)."""
    return _config_section_at_ref(root, ref, "integration")


def _positive_int(sec: dict, key: str, default: int) -> int:
    val = sec.get(key, default)
    if not isinstance(val, int) or isinstance(val, bool) or val <= 0:
        raise ConfigError(f"{key}={val!r} — ожидается положительное целое")
    return val


def default_base_ref(root: "Path | None" = None) -> str:
    """`origin/main` — не универсальный дефолт: на репозиториях с `master` (и любой другой
    основной веткой) он давал бы ложный блок на каждом пуше (ревью 2026-07-26). Спрашиваем
    git о фактической основной ветке remote'а, и только если он не знает — падаем на литерал."""
    if root is None:
        return _DEFAULT_BASE_REF
    r = _prepush_git("symbolic-ref", "--short", "refs/remotes/origin/HEAD",
                       cwd=root)
    ref = r.stdout.strip()
    return ref if r.returncode == 0 and "/" in ref else _DEFAULT_BASE_REF


def _operator_merge_test_command(root: "Path | None") -> str:
    """`merge_test_command` из конфига ОПЕРАТОРА (рабочее дерево), не из пушимого коммита.
    Пушимый коммит может быть написан кем угодно, а эта строка исполняется — читать её оттуда
    значит отдать произвольное исполнение автору ветки. Обратное направление (оператор ослабил
    свою же команду) риска не несёт: это его собственная машина."""
    if root is None:
        return ""
    try:
        import yaml
        data = yaml.safe_load((root / GATE_CONFIG_NAME).read_text())
    except (OSError, UnicodeError, ImportError):
        return ""
    except Exception:                       # битый YAML — команду не запускаем (fail-safe)
        return ""
    sec = (data or {}).get("integration") if isinstance(data, dict) else None
    cmd = (sec or {}).get("merge_test_command", "") if isinstance(sec, dict) else ""
    return cmd if isinstance(cmd, str) else ""


def integration_config(section: "dict | None", root: "Path | None" = None) -> IntegrationConfig:
    """Валидация с блоком на неизвестном значении: тихий фолбэк скрыл бы опечатку и создал
    ложное впечатление «политика та, которую просили» (как REVIEW_PROVIDER)."""
    sec = section or {}
    policy = sec.get("on_fetch_failure", "block")
    if policy not in _FETCH_POLICIES:
        raise ConfigError(
            f"on_fetch_failure={policy!r} — ожидается одно из {_FETCH_POLICIES} "
            "(значение «warn» переименовано в «skip-audited»: это не «предупредить и всё равно "
            "проверить», а полный пропуск единственного обещания гейта)")
    # Команда берётся ТОЛЬКО из конфига оператора в рабочем дереве, а не из пушимого коммита:
    # исполняемое значение из чужой ветки = RCE на машине пушащего при рядовом `git push`
    # (security-проход 2026-07-26, эксплойт воспроизведён). Политика при этом по-прежнему
    # читается из коммита — там она защищена от незастейдженного ослабления.
    cmd = _operator_merge_test_command(root)
    base_ref = sec.get("base_ref", default_base_ref(root))
    if not isinstance(base_ref, str) or "/" not in base_ref:
        raise ConfigError(f"base_ref={base_ref!r} — ожидается «<remote>/<branch>»")
    remote_part, branch_part = _parse_base_ref(base_ref)
    for label, part in (("remote", remote_part), ("branch", branch_part)):
        if not _REF_COMPONENT_RE.match(part):
            raise ConfigError(
                f"base_ref: {label}-часть {part!r} недопустима. Разрешены [A-Za-z0-9._/-] "
                "без ведущего дефиса: значение вроде «--upload-pack=…» git принял бы за опцию "
                "и ИСПОЛНИЛ бы как команду (RCE, найдено security-проходом 2026-07-26)")
    return IntegrationConfig(base_ref=base_ref, on_fetch_failure=policy, merge_test_command=cmd,
                             test_timeout=_positive_int(sec, "test_timeout",
                                                        _DEFAULT_TEST_TIMEOUT),
                             fetch_timeout=_positive_int(sec, "fetch_timeout",
                                                         _DEFAULT_FETCH_TIMEOUT))


# ═══ Обходы: флаг + обязательная причина + ЗАПИСЬ В АУДИТ ═══

def _ack(env_name: str, root: "Path | None" = None) -> "str | None":
    """Аудируемое подтверждение (как LADDER_SKIP/EMPIRICAL_SKIP): флаг + НЕПУСТАЯ причина.

    Запись в аудит делается ЗДЕСЬ, а не у вызывающих: ревью 2026-07-26 показало, что политика
    называлась `skip-audited`, а не аудировалось ничего — все пути только печатали в stderr,
    который у git-хука не сохраняется нигде. Аудит внутри `_ack` делает пропуск записи
    структурно невозможным для будущих вызывающих."""
    if os.environ.get(env_name) != "1":
        return None
    reason = (os.environ.get(f"{env_name}_REASON") or "").strip()
    if not reason:
        return None
    if root is not None:
        _audit_line(root, f"{env_name}=1 reason={redact_secrets(reason)!r} — "
                          "pre-push проверка ПРОПУЩЕНА (осознанный обход)")
    return reason


def gate_skipped(root: "Path | None" = None) -> "str | None":
    return _ack("INTEGRATION_SKIP", root)


def tests_skipped(root: "Path | None" = None) -> "str | None":
    return _ack("INTEGRATION_TESTS_SKIP", root)


# ═══ Анти-самоослабление (И4) ═══

def _resolve_remote_name(root: Path, remote: str) -> "str | None":
    """argv[0] у pre-push — имя remote ЛИБО URL (`git push https://… feature`). Для URL
    пространство `refs/remotes/<remote>/…` не существует, и детектор пересоздания молча
    не срабатывал (ревью 2026-07-26). Возвращает имя настроенного remote либо None."""
    names = _prepush_git("remote", cwd=root)
    configured = [n for n in names.stdout.split() if n]
    if remote in configured:
        return remote
    for name in configured:
        url = _prepush_git("remote", "get-url", name, cwd=root).stdout.strip()
        if url and url == remote:
            return name
    return None


def _tracking_ref_exists(root: Path, remote: str, branch: str) -> bool:
    r = _prepush_git("rev-parse", "--verify", "--quiet", "--end-of-options",
                        f"refs/remotes/{remote}/{branch}", cwd=root)
    return r.returncode == 0


def self_weaken_verdict(root: Path, remote_sha: str, remote: str, branch: str,
                        local_sha: str, pushed: "tuple[str, dict | None] | None" = None
                        ) -> Verdict:
    """Референс — предыдущий ПРИНЯТЫЙ tip этого ref'а (`<remote sha>` из stdin), а НЕ база:
    `origin/main` не двигается до мержа, поэтому сравнение с ней не срабатывало ровно на
    коммите, прописывающем себе ослабление (ревью ред. 3).

    `pushed` — уже прочитанная секция пушимого коммита (вызывающий её всё равно читает;
    повторное чтение было лишним git-вызовом и делало ветку unreadable недостижимой)."""
    pushed_state, pushed_sec = pushed if pushed is not None \
        else integration_section(root, local_sha)
    if pushed_state == "unreadable":
        return Verdict(False, "конфиг в пушимом коммите нечитаем — состояние гейта "
                              "не подтвердить (нечитаемо ≠ отсутствует)")
    ack = _ack("INTEGRATION_CONFIG_CHANGE", root)

    if set(remote_sha) == {"0"}:
        resolved = _resolve_remote_name(root, remote)
        if resolved is None:
            # Пуш по URL: пространства refs/remotes/<remote>/… нет, детектор пересоздания
            # слеп — и раньше это молча читалось как «новая ветка → введение гейта».
            if ack:
                return Verdict(True)
            return Verdict(False, f"пуш по адресу {remote!r}, не совпадающему ни с одним "
                                  "настроенным remote: проверить, новая это ветка или "
                                  "пересозданная, нельзя. Пушь по имени remote либо "
                                  "INTEGRATION_CONFIG_CHANGE=1 с причиной")
        if _tracking_ref_exists(root, resolved, branch):
            if ack:
                return Verdict(True)
            return Verdict(False, f"ветка {branch!r} выглядит пересозданной (remote_sha нулевой, "
                                  f"но локальный refs/remotes/{remote}/{branch} существует) — "
                                  "это тихий сброс референса анти-самоослабления. Нужен "
                                  "INTEGRATION_CONFIG_CHANGE=1 с причиной либо git fetch --prune")
        return Verdict(True)          # новая ветка: введение гейта, не ослабление

    prev_state, prev_sec = integration_section(root, remote_sha)
    if prev_state == "unreadable":
        if ack:
            return Verdict(True)
        return Verdict(False, f"секцию integration в предыдущем tip'е {remote_sha[:12]} "
                              "не удалось прочитать (объекта нет локально?) — это НЕ "
                              "доказательство её отсутствия. Сделай git fetch нужного tip'а "
                              "либо INTEGRATION_CONFIG_CHANGE=1 с причиной")
    if prev_state == "absent":
        if not (pushed_sec or {}):
            return Verdict(True)      # ни там, ни тут секции нет — менять нечего
        if ack:
            return Verdict(True)
        # Ревью 2026-07-26 (fail-open): «absent → проход» было неверно. Отсутствие секции НЕ
        # означает выключенный гейт — он работает на дефолтах (base_ref origin/main,
        # on_fetch_failure=block). Поэтому появление секции на УЖЕ запушенной ветке — смена
        # политики: так проходило `base_ref: origin/<эта ветка>`, после чего проверка слияния
        # не выполнялась НИКОГДА, а в аудите оставалась лишь строка base-skip.
        return Verdict(False, "секция integration ПОЯВИЛАСЬ относительно предыдущего "
                              "запушенного состояния ветки: до неё гейт работал на дефолтах, "
                              "поэтому это смена политики, а не включение. Нужен "
                              "INTEGRATION_CONFIG_CHANGE=1 с причиной")
    if _deploy_section_hash(prev_sec or {}) != _deploy_section_hash(pushed_sec or {}):
        if ack:
            return Verdict(True)
        return Verdict(False, "секция integration изменена относительно предыдущего "
                              "запушенного состояния — силу двух политик сравнить нельзя, "
                              "поэтому любая смена требует INTEGRATION_CONFIG_CHANGE=1 с причиной")
    return Verdict(True)


# ═══ Предсказание слияния ═══

def predict_merge(root: Path, base: str, sha: str) -> MergeResult:
    """`merge-tree --write-tree` — предсказание БЕЗ касания рабочего дерева.
    «Неизвестно» ≠ «чисто» (И3)."""
    r = _prepush_git("merge-tree", "--write-tree", "--end-of-options", base, sha,
                       cwd=root)
    if r.returncode == 0:
        first = r.stdout.strip().splitlines()
        oid = first[0].strip() if first else ""
        if len(oid) < 40:
            # Пустой/битый вывод при нулевом коде — «неизвестно» (раньше IndexError убивал
            # процесс трейсбеком вместо диагностики E8).
            return MergeResult("unknown", detail="merge-tree вернул 0 без OID дерева")
        return MergeResult("clean", tree=oid)
    if r.returncode == 1:
        # ИЗМЕРЕНО 2026-07-26: битый OID и мусорная ссылка тоже дают rc=1, но с ПУСТЫМ stdout
        # («not something we can merge» уходит в stderr), тогда как настоящий конфликт печатает
        # OID дерева первой строкой. Различаем по выводу, а не по коду: иначе отсутствие объекта
        # выдавалось бы за конфликт с пустым списком файлов (спека ред. 5 §3 утверждала «код
        # ≠0,1» — неверно, поймано тестом C13).
        if not r.stdout.strip():
            return MergeResult("unknown", detail=redact_secrets((r.stderr or "").strip())[:400])
        paths: list[str] = []
        for raw in r.stdout.splitlines()[1:]:
            if "\t" in raw:                      # `mode oid stage\tpath`
                p = raw.split("\t", 1)[1].strip()
                if p not in paths:
                    paths.append(p)
            elif not raw.strip():
                break
        return MergeResult("conflict", paths=paths)
    return MergeResult("unknown", detail=redact_secrets((r.stderr or r.stdout).strip())[:400])


# ═══ Проба ═══

def _git_common_dir(root: Path) -> Path:
    """`.git` — НЕ всегда каталог: в связанном worktree и в submodule это файл, поэтому путь
    пробы нельзя строить как `root/.git/...` (ревью 2026-07-26: `worktree add` там падает,
    оператор получает блок с непонятным сообщением)."""
    r = _prepush_git("rev-parse", "--git-common-dir", cwd=root)
    if r.returncode != 0 or not r.stdout.strip():
        return root / ".git"
    p = Path(r.stdout.strip())
    return p if p.is_absolute() else (root / p)


@contextlib.contextmanager
def hookless_env(root: "Path | None" = None):
    """Хуки в пробе НЕ изолированы сами по себе: `core.hooksPath` наследуется из общего конфига
    (измерено). Без этого вложенный `git commit` в тест-команде запустил бы наши pre/post-commit
    против дерева-пробы и записал бы ledger для висячего SHA (И6).

    Указываем на РЕАЛЬНО пустой каталог, а не на «магически несуществующий» путь: так
    «хуков здесь нет» — факт, а не допущение о названии (ревью 2026-07-26)."""
    with tempfile.TemporaryDirectory(prefix="gates-probe-hooks-") as empty:
        # ⛔ Унаследованные GIT_CONFIG_* НЕ переносятся. Раньше они дополнялись, и это давало
        # ровно ту дыру, ради которой probe и обвешан проверками: `GIT_CONFIG_COUNT=1` +
        # `filter.evil.smudge` вызывающего доезжали до `worktree add`, тогда как перечисление
        # драйверов идёт через `_trusted_git`, который их снимает. Проверка видела пустоту, а
        # checkout исполнял команду (воспроизведено ревью 09.08.2026). Конфигурация checkout
        # обязана состоять ТОЛЬКО из наших записей — иначе проверять нечего.
        env = dict(os.environ)
        env.pop("GIT_CONFIG_PARAMETERS", None)
        for k in [k for k in env if k.startswith("GIT_CONFIG_")]:
            env.pop(k, None)
        env["GIT_CONFIG_KEY_0"] = "core.hooksPath"
        env["GIT_CONFIG_VALUE_0"] = empty
        n = 1
        if root is not None:
            # `safe.directory` раньше приезжал из окружения вызывающего вместе со всем
            # остальным. Он нужен по делу (иначе git откажется работать с каталогом), поэтому
            # выставляется НАМИ и только для того корня, который мы же и проверяем.
            env[f"GIT_CONFIG_KEY_{n}"] = "safe.directory"
            env[f"GIT_CONFIG_VALUE_{n}"] = str(root)
            n += 1
        env["GIT_CONFIG_COUNT"] = str(n)
        yield env


#: `worktree add` делает checkout и исполняет smudge/process-фильтры, ВЫБРАННЫЕ атрибутами
#: ревьюируемого репозитория. Гасить их поимённо нельзя: драйвер может называться как угодно,
#: а имя приходит из `.gitattributes` проверяемой стороны. Поэтому перед checkout мы
#: ПЕРЕЧИСЛЯЕМ активные драйверы из конфига и падаем, если хоть один задан.
def _active_filter_drivers(root: Path) -> list[str]:
    """Только `smudge`/`process`: ИМЕННО их исполняет checkout. `clean`-конфигурации
    checkout не запускает, и блокировать по ним — отказ в функции без выигрыша в
    безопасности (находка ревью 09.08.2026, G26)."""
    r = _prepush_git("config", "--get-regexp", r"^filter\..*\.(process|smudge)$",
                     cwd=root)
    if r.returncode not in (0, 1):        # 1 = совпадений нет, это норма
        raise TrustedGitError("не перечислить filter-драйверы — probe небезопасен")
    names = []
    for line in r.stdout.splitlines():
        key = line.split(None, 1)[0] if line.strip() else ""
        parts = key.split(".")
        if len(parts) >= 3:
            names.append(".".join(parts[1:-1]))
    return sorted(set(names))


def _probe_git(root: Path, env: dict, *args: str):
    """Мутирующие операции probe — абсолютный бинарь, аллоулист окружения, нейтрализация.
    Отдельно от `_trusted_git`, потому что нуждается в GIT_CONFIG_* из hookless_env."""
    git = _trusted_git_bin()
    if git is None:
        raise TrustedGitError("доверенный git недоступен — probe не построить")
    base_env = {k: v for k, v in os.environ.items() if k in _GIT_ENV_ALLOW}
    base_env["HOME"] = str(_trusted_home())
    base_env["PATH"] = os.pathsep.join(_TRUSTED_PATH_DIRS)
    base_env.update(_GIT_SAFE_ENV)
    # GIT_CONFIG_* из hookless_env — это НАШИ значения (пустой hooksPath), не вызывающего
    for k, v in env.items():
        if k.startswith("GIT_CONFIG_"):
            base_env[k] = v
    return subprocess.run([git, *_GIT_NEUTRALIZE, *args], cwd=root,
                          capture_output=True, text=True, env=base_env)


@contextlib.contextmanager
def probe_worktree(root: Path, base: str, sha: str, tree: str):
    """Материализует смерженное дерево в отдельный worktree. Рабочее дерево пользователя
    не трогается (И1). Уборка best-effort + prune на старте (И7: SIGKILL `finally` не выполнит)."""
    # `worktree add` делает CHECKOUT, то есть запускает smudge/process-фильтры, выбранные
    # атрибутами и локальным конфигом РЕВЬЮИРУЕМОГО репозитория — код проверяемой стороны
    # исполнялся бы до интеграционных тестов. Плюс раньше сюда уезжало полное окружение
    # вызывающего (PATH/loader). Оба закрыты: доверенный бинарь, аллоулист, нейтрализация,
    # и фильтры отключены явно на время checkout.
    with hookless_env(root) as env:
        # Фильтры — не единственный путь исполнения при checkout: `worktree add` запускает
        # хук post-checkout, а partial clone тянет недостающие объекты через promisor-remote
        # с ВНЕШНИМ transport-helper'ом. Хуки гасит core.hooksPath=/dev/null в _GIT_NEUTRALIZE;
        # promisor гасить нечем — на нём падаем закрыто (G27).
        partial = _prepush_git("config", "--get-regexp",
                               r"^(extensions\.partialclone|remote\..*\.promisor)$", cwd=root)
        if partial.returncode not in (0, 1):
            raise TrustedGitError("не проверить partial-clone конфигурацию — probe небезопасен")
        if partial.returncode == 0 and partial.stdout.strip():
            raise RuntimeError(
                "репозиторий — partial clone (promisor): checkout probe дотянул бы недостающие "
                "объекты внешним transport-helper'ом правами гейта. Интеграционная проверка "
                "остановлена.")
        drivers = _active_filter_drivers(root)
        if drivers:
            raise RuntimeError(
                "в репозитории заданы исполняемые при checkout filter-драйверы ("
                + ", ".join(drivers) + "): checkout "
                "probe исполнил бы их команды правами гейта. Интеграционная проверка "
                "остановлена — уберите драйверы или отключите integration-гейт осознанно.")
        _probe_git(root, env, "worktree", "prune")
        ct = _probe_git(root, env, "commit-tree", tree, "-p", base, "-p", sha,
                        "-m", "integration probe")
        if ct.returncode != 0 or not ct.stdout.strip():
            raise RuntimeError("commit-tree не удался: "
                               f"{redact_secrets(ct.stderr.strip())[:300]}")
        commit = ct.stdout.strip()
        path = _git_common_dir(root) / "gates-probe" / f"{os.getpid()}-{sha[:8]}"
        r = _probe_git(root, env, "worktree", "add", "-q", "--detach", str(path), commit)
        if r.returncode != 0:
            raise RuntimeError("worktree add не удался: "
                               f"{redact_secrets(r.stderr.strip())[:300]}")
        try:
            yield path, env
        finally:
            _probe_git(root, env, "worktree", "remove", "--force", str(path))


def worktree_snapshot(root: Path) -> str:
    """Снимок ОТСЛЕЖИВАЕМЫХ изменений рабочего дерева — контроль побочного эффекта (И1).
    Записи в gitignored пути (кэши) намеренно не видны: остаток §9.2."""
    r = _prepush_git("status", "--porcelain", cwd=root)
    return hashlib.sha256(r.stdout.encode()).hexdigest()


def run_merge_tests(cwd: Path, cmd: str, timeout_s: int,
                    env: "dict | None" = None) -> tuple[str, str]:
    """Прогон в пробе. Переиспользует `_run_empirical` деплой-гейта: держать вторую копию
    актуатор-урока «зависло ≠ прошло» значило бы чинить его дважды."""
    if env is None:
        with hookless_env() as e:
            return _run_empirical(cmd, timeout_s, cwd, cwd=cwd, env=e)
    return _run_empirical(cmd, timeout_s, cwd, cwd=cwd, env=env)


# ═══ Сообщения (формулировки — часть приёмки) ═══

def report_tests_green() -> str:
    """И5/E11: печатается ВСЕГДА при зелёных тестах. Без пометки no-op команда выглядела бы
    покрытием — в ред. 2 это было хуже, чем отсутствие команды."""
    return ("[prepush] тесты на пробе зелёные, но это НЕ доказывает работоспособность слияния: "
            "команда могла исполнить код из настоящего дерева (editable install/venv/NODE_PATH). "
            "Гейт гарантирует только отсутствие конфликтов.")


def report_tests_absent() -> str:
    return ("[prepush] конфликтов нет; тесты на слиянии НЕ запускались "
            "(integration.merge_test_command не задана).")


def report_probe_incomplete() -> str:
    """С20: проба не подтягивает submodules и не прогоняет LFS-smudge."""
    return ("[prepush] NB: окружение пробы могло не содержать submodules/LFS/sparse-checkout — "
            "результат тестов по этим данным неполон.")


def report_fetch_failed(policy: str, reason: str) -> str:
    """С8/И3: при неосвежённой базе нельзя заключить «интегрировать нечего» — ред. 2 именно так
    и делала, и это был тихий пропуск."""
    return (f"[prepush] база не обновлена ({reason[:200]}), политика {policy}: слияние "
            "НЕ проверялось. Это конфигурируемый аудируемый обход единственного обещания "
            "гейта, а не «предупредили и проверили».")


# ═══ CLI ═══

def _onboarded(root: Path) -> bool:
    """Своя, параметризованная по root, версия: `codex_review_gate.ONBOARDED` вычисляется от cwd
    на импорте и из хука/tmp-репо непригодна (та же причина, что у `_audit_line`)."""
    if (root / GATE_CONFIG_NAME).exists():
        return True
    return _prepush_git("cat-file", "-e", f"HEAD:{GATE_CONFIG_NAME}",
                          cwd=root).returncode == 0


def main(argv: list[str], stdin_text: "str | None" = None, root: "Path | None" = None) -> int:
    root = Path(root) if root else Path.cwd()
    if not _onboarded(root):
        return 0                                   # И8: молчим вне онбординга
    text = stdin_text if stdin_text is not None else sys.stdin.read()
    remote = argv[0] if argv else "origin"
    skip = gate_skipped(root)
    if skip:
        print(f"[prepush] INTEGRATION_SKIP=1 — гейт пропущен целиком. Причина: "
              f"{redact_secrets(skip)}", file=sys.stderr)
        return 0
    fetched: dict[tuple[str, str], "str | None"] = {}     # fetch — один раз на (remote, branch)
    rc = 0
    for line in parse_lines(text):
        if _process_line(root, line, remote, fetched) != 0:
            rc = 2
    return rc


def _early_skip(line: PushRef) -> "str | None":
    """delete/тег определяются ДО чтения конфига. У удаления `local_sha` нулевой, поэтому
    `ls-tree` по нему падает → «unreadable» → блок: `git push :branch` и пуш аннотированного
    тега ломались на онбордженном репо (ревью 2026-07-26, нарушение E2)."""
    if line.is_delete:
        return "delete"
    if line.local_ref.startswith("refs/tags/"):
        return "tag"
    return None


def _process_line(root: Path, line: PushRef, remote: str, fetched: dict) -> int:
    if _early_skip(line) is not None:
        return 0
    pushed = integration_section(root, line.local_sha)
    if pushed[0] == "unreadable":
        print(f"[prepush] ✗ {line.local_ref}: конфиг в пушимом коммите нечитаем — блок",
              file=sys.stderr)
        return 2
    try:
        cfg = integration_config(pushed[1], root)
    except ConfigError as e:
        print(f"[prepush] ✗ {line.local_ref}: {e}", file=sys.stderr)
        return 2
    kind = classify(line, remote=remote, base_ref=cfg.base_ref)
    if kind in ("delete", "tag"):
        return 0
    verdict = self_weaken_verdict(root, line.remote_sha, remote, line.branch, line.local_sha,
                                  pushed=pushed)
    if not verdict.ok:
        print(f"[prepush] ✗ {line.local_ref}: {verdict.reason}", file=sys.stderr)
        return 2
    if kind == "base":
        # Аудит обязателен: конфиг, объявивший базой саму пушимую ветку (`base_ref: origin/<эта
        # ветка>`), иначе отключал бы предсказание слияния БЕССЛЕДНО — в отличие от
        # skip-audited и INTEGRATION_SKIP (ревью 2026-07-26).
        _audit_line(root, f"base-skip ref={line.local_ref} base_ref={cfg.base_ref} — "
                          "предсказание слияния неприменимо (пушится сама база)")
        return 0                        # И4 уже проверен выше
    return _check_integration(root, line, cfg, fetched)


def _fetch_base(root: Path, cfg: IntegrationConfig, fetched: dict) -> "str | None":
    """None = успех, иначе причина отказа. Результат кэшируется на (remote, branch): fetch —
    единственная сетевая операция гейта, и повторять её на каждый ref было бы расточительно."""
    remote, branch = _parse_base_ref(cfg.base_ref)
    key = (remote, branch, cfg.fetch_timeout)   # таймаут в ключе: строка с коротким таймаутом
                                                # не должна travить кэш для строки с длинным
    if key in fetched:
        return fetched[key]
    try:
        r = _prepush_fetch(root, remote, branch, cfg.fetch_timeout)
        reason = None if r.returncode == 0 else (
            redact_secrets(r.stderr.strip())[:300] or "git fetch не удался")
    except subprocess.TimeoutExpired:
        # Раньше исключение вылетало трейсбеком МИМО политики on_fetch_failure, которую оно
        # обязано было запустить (ревью 2026-07-26).
        reason = f"git fetch не уложился в {cfg.fetch_timeout}s"
    except OSError as e:
        reason = redact_secrets(f"{type(e).__name__}: {e}")[:300]
    fetched[key] = reason
    return reason


def _check_integration(root: Path, line: PushRef, cfg: IntegrationConfig, fetched: dict) -> int:
    reason = _fetch_base(root, cfg, fetched)
    if reason is not None:
        if cfg.on_fetch_failure == "block":
            _audit_line(root, f"on_fetch_failure=block ref={line.local_ref} reason={reason!r} "
                              "— пуш заблокирован")
            print(f"[prepush] ✗ {line.local_ref}: база не обновлена ({reason}) — блок "
                  "(политика on_fetch_failure=block)", file=sys.stderr)
            return 2
        _audit_line(root, f"on_fetch_failure={cfg.on_fetch_failure} ref={line.local_ref} "
                          f"reason={reason!r} — слияние НЕ проверялось (конфигурируемый обход)")
        print(report_fetch_failed(cfg.on_fetch_failure, reason), file=sys.stderr)
        return 0                        # шаги отставания/слияния/тестов НЕ выполняются (И3)
    behind = _prepush_git("rev-list", "--count", f"{line.local_sha}..{cfg.base_ref}",
                            cwd=root)
    if behind.returncode != 0:
        # И3: сбой сравнения — «неизвестно», а не «есть что интегрировать, разберёмся ниже».
        print(f"[prepush] ✗ {line.local_ref}: не удалось сравнить с {cfg.base_ref} "
              f"({redact_secrets(behind.stderr.strip())[:200]}) — «неизвестно» не значит «чисто»",
              file=sys.stderr)
        return 2
    if behind.stdout.strip() == "0":
        return 0                        # интегрировать нечего

    merged = predict_merge(root, base=cfg.base_ref, sha=line.local_sha)
    if merged.status == "conflict":
        print(f"[prepush] ✗ {line.local_ref}: слияние с {cfg.base_ref} конфликтует. Файлы:",
              file=sys.stderr)
        for p in merged.paths:
            print(f"    {p}", file=sys.stderr)
        print("[prepush] разреши конфликт локально; гейт рабочее дерево не трогает.",
              file=sys.stderr)
        return 2
    if merged.status != "clean":
        print(f"[prepush] ✗ {line.local_ref}: предсказание слияния не удалось "
              f"({merged.detail}) — «неизвестно» не значит «чисто»", file=sys.stderr)
        return 2
    base_sha = _prepush_git("rev-parse", "--end-of-options", cfg.base_ref, cwd=root).stdout.strip()
    _audit_line(root, f"integration ok ref={line.local_ref} sha={line.local_sha[:12]} "
                      f"base={cfg.base_ref} base_sha={base_sha[:12]} — конфликтов нет")
    if not cfg.merge_test_command:
        print(report_tests_absent(), file=sys.stderr)
        return 0
    tskip = tests_skipped(root)
    if tskip:
        print(f"[prepush] конфликтов нет; тесты пропущены (INTEGRATION_TESTS_SKIP). Причина: "
              f"{redact_secrets(tskip)}", file=sys.stderr)
        return 0
    before = worktree_snapshot(root)
    try:
        with probe_worktree(root, base=cfg.base_ref, sha=line.local_sha,
                            tree=merged.tree) as (w, env):
            result, tail = run_merge_tests(w, cfg.merge_test_command, cfg.test_timeout, env=env)
    except RuntimeError as e:
        print(f"[prepush] ✗ {line.local_ref}: {e}", file=sys.stderr)
        return 2
    if worktree_snapshot(root) != before:
        print(f"[prepush] ✗ {line.local_ref}: merge_test_command изменила рабочее дерево — "
              "команда негерметична, результату верить нельзя", file=sys.stderr)
        return 2
    if result != "pass":
        print(f"[prepush] ✗ {line.local_ref}: тесты на пробе → {result}. Причиной может быть и "
              "семантический конфликт, и негерметичное окружение пробы — гейт их не различает. "
              "Хвост вывода:", file=sys.stderr)
        if tail:
            print(tail, file=sys.stderr)
        return 2
    print(report_tests_green(), file=sys.stderr)
    print(report_probe_incomplete(), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
