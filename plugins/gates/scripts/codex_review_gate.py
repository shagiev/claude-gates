"""Codex review gate: делегирует ревью плагину codex-companion.mjs, парсит вердикт (СТРОГО),
решает block/allow, ведёт ledger и дизайн-маркер. Порт из боевого проекта-источника (Phase 1 + 1.6) в
плагин gates: repo-root динамический (git rev-parse от cwd), код-пути из `.codex-gate.yaml`
с безопасными строгими дефолтами, opt-in автосрабатывающих хуков по наличию конфига.
Спека: docs/2026-07-22-gates-plugin-port-design.md (+ docs/methodology/)."""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

try:
    import yaml
except ImportError:            # PyYAML может отсутствовать в системном python3 (решение 3):
    yaml = None                # конфиг нечитаем → строгий режим, не traceback

SEVERITY_BLOCKING = {"critical", "high"}
KNOWN_SEVERITIES = {"critical", "high", "medium", "low"}   # R1-1b: всё остальное = блок
RECOGNIZED_VERDICTS = {"approve", "needs-attention"}

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_VERDICT_RE = re.compile(r"^Verdict:\s*(.+?)\s*$", re.MULTILINE)
_FINDING_RE = re.compile(r"^\s*-\s*\[(?P<sev>[^\]]+)\]\s*(?P<rest>.*)$", re.MULTILINE)  # R1-1b: любой ярлык
_NO_FINDINGS_RE = re.compile(r"No material findings\.", re.IGNORECASE)
_MALFORMED_FINDING_RE = re.compile(r"^\s*-\s*\[[^\]]*$", re.MULTILINE)   # R3b: bullet с '[' без ']' до EOL


def strip_ansi(s: str) -> str:
    return _ANSI_RE.sub("", s)


@dataclass
class ReviewVerdict:
    verdict: str | None
    findings: list[tuple[str, str]] = field(default_factory=list)
    malformed: bool = False           # усечённый/битый bullet в выводе
    no_findings_marker: bool = False  # явное "No material findings."

    @property
    def blocking(self) -> bool:
        # R1-1b: critical/high ИЛИ любая НЕизвестная severity (напр. [urgent]) → блок.
        return any(sev.lower() in SEVERITY_BLOCKING or sev.lower() not in KNOWN_SEVERITIES
                   for sev, _ in self.findings)

    @property
    def valid(self) -> bool:
        # R1-1/R3b: любой признак дрейфа/усечения = НЕвалиден (fail-closed на деплое).
        if self.verdict not in RECOGNIZED_VERDICTS:
            return False
        if self.malformed:                                              # усечённый bullet
            return False
        if self.verdict == "needs-attention" and not self.findings:     # attention без находок
            return False
        if self.verdict == "approve" and not self.findings and not self.no_findings_marker:
            return False   # approve без явного "No material findings" и без находок = дрейф
        return True


@dataclass(frozen=True)
class ReviewerCertification:
    provider: str
    adapter: str
    requested_model: str
    actual_models: tuple[str, ...]
    family: str
    roles: tuple[str, ...]
    certification_id: str
    status: str
    # §6: `verified` — провайдер вернул фактическую модель в ответе и она совпала с реестром;
    # `declared` — провайдер лишь эхо-ит запрошенный slug (остаток M8 в AGENTS.md).
    attestation: str = "declared"


@dataclass
class ReviewerRun:
    role: str
    provider: str
    requested_model: str
    actual_models: tuple[str, ...]
    family: str
    certification_id: str
    status: str
    verdict: ReviewVerdict | None = None
    detail: str = ""
    usage: dict = field(default_factory=dict)


def _nonempty_str(v: object) -> bool:
    return isinstance(v, str) and bool(v.strip())


def _result_schema_ok(result: dict) -> bool:
    """Полная проверка result по review-output.schema.json (Codex P1: любая неполнота/дрейф =
    fail-closed, а не «чистый approve»)."""
    if result.get("verdict") not in RECOGNIZED_VERDICTS:
        return False
    if not _nonempty_str(result.get("summary")):
        return False
    ns = result.get("next_steps")
    if not isinstance(ns, list) or any(not _nonempty_str(s) for s in ns):
        return False
    findings = result.get("findings")
    if not isinstance(findings, list):
        return False
    for f in findings:
        if not isinstance(f, dict):
            return False
        if not all(_nonempty_str(f.get(k)) for k in ("severity", "title", "body", "file")):
            return False
        if not isinstance(f.get("recommendation"), str):
            return False
        for k in ("line_start", "line_end"):
            v = f.get(k)
            if not isinstance(v, int) or isinstance(v, bool) or v < 1:
                return False
        c = f.get("confidence")
        if not isinstance(c, (int, float)) or isinstance(c, bool) or not (0 <= c <= 1):
            return False
    return True


def _verdict_from_json(text: str) -> "ReviewVerdict | None":
    """adversarial-review --json → {result:{verdict,findings:[{severity...}]}, parseError,
    codex:{status}}. Структурный контракт (review-output.schema.json) — устойчив к формату
    рендера. None = не JSON/не тот envelope (пусть решает текст-фолбэк)."""
    import json as _json
    try:
        obj = _json.loads(text)
    except (_json.JSONDecodeError, TypeError, ValueError):
        return None
    # Раз это валидный JSON — трактуем как envelope companion и валидируем СТРОГО (Codex:
    # malformed envelope не должен пройти как чистый approve). Любое отклонение → invalid
    # (verdict=None → fail-closed на деплое). Текст-фолбэк только когда это ВООБЩЕ не JSON.
    if not isinstance(obj, dict):
        return ReviewVerdict(verdict=None)
    codex = obj.get("codex")
    status = codex.get("status") if isinstance(codex, dict) else None
    # status должен быть НАСТОЯЩИМ int==0 (в Python False==0 и 0.0==0 — дрейф не должен пройти)
    if not isinstance(status, int) or isinstance(status, bool) or status != 0:
        return ReviewVerdict(verdict=None)
    if obj.get("parseError"):
        return ReviewVerdict(verdict=None)   # модель не вернула валидный структурный вывод
    result = obj.get("result")
    if not isinstance(result, dict) or not _result_schema_ok(result):
        return ReviewVerdict(verdict=None)   # неполная/дрейфнувшая схема → невалидно (fail-closed)
    # CHOKEPOINT редакции (ревью 25.07 R2): заголовки находок — НЕдоверенный текст ревьюера,
    # который может процитировать секрет из ревьюируемого кода. Редактируем ЗДЕСЬ, в единой
    # точке входа, чтобы всё ниже по потоку (ledger-файл, сообщение блока, advisory-вывод,
    # аудит carry-over) наследовало редакцию автоматически — инвариант в одной точке, а не
    # залатанный у каждого потребителя.
    findings = [(f["severity"].strip().lower(),
                 redact_secrets(str(f.get("title", "")).strip()))
                for f in result["findings"]]
    return ReviewVerdict(
        verdict=result["verdict"], findings=findings,
        no_findings_marker=(not findings),   # структурно: пусто findings = чисто, не дрейф
    )


# ═══ Редактирование секретов в operator-facing выводе (ревью 25.07, security-класс
# конституции: «секрет в логе/сообщении об ошибке/аудите» = blocking) ═══
# Вывод зависимостей (companion-stderr/stdout, хвост тест-команды) — НЕдоверенный текст: там
# могут оказаться Authorization-заголовки, API-ключи, signed URL, DSN с паролем, секреты,
# повторённые из ревьюируемого кода. Усечение НЕ защищает — нужна редакция по образцам.
_REDACTED = "«скрыто»"
# (regex, шаблон замены) — шаблон задан РЯДОМ с правилом (ревью R4: выбор замены по числу
# групп путал три правила с разной семантикой и уродовал signed URL в DSN-форму).
_REDACT_RULES = (
    # помеченные пары ключ=значение; кавычки вокруг ключа и значения допускаются
    # Ключ может быть ЧАСТЬЮ большего идентификатора: `AWS_SECRET_ACCESS_KEY=…` (R5/R6-F1 —
    # `\b` не срабатывал, т.к. `_` считается word-символом, и значение с `/` не ловилось
    # правилом длинного токена). Поэтому ключ = целый идентификатор, содержащий ключевое слово.
    (re.compile(r"(?i)((?<![A-Za-z0-9])[A-Za-z0-9_]*"
                r"(?:api[-_]?key|access[-_]?token|refresh[-_]?token|token|secret|password|"
                r"passwd|pwd|authorization|private[-_]?key)[A-Za-z0-9_]*)"
                r"[\"']?(\s*[:=]\s*)[\"']?"
                # СХЕМА аутентификации перед credential (R7: поглощался только `bearer`,
                # поэтому `Authorization: Basic dXNlcjpwYXNz==` теряло схему, но не credential;
                # base64 короче 40 и с `=` не ловился и правилом длинного токена)
                r"(?:(?:bearer|basic|digest|negotiate|ntlm|hoba|mutual|apikey|token)\s+)?"
                r"[^\s,;\"']{4,}[\"']?"),
     r"\1\2" + _REDACTED),
    # схема + credential без метки-ключа (`Bearer <tok>` / `Basic <b64>` сами по себе)
    (re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9_\-\.=+/]{12,}"), _REDACTED),
    (re.compile(r"\b(sk-[A-Za-z0-9_\-]{8,}|ghp_[A-Za-z0-9]{8,}|gho_[A-Za-z0-9]{8,}|"
                r"github_pat_[A-Za-z0-9_]{8,}|xox[baprs]-[A-Za-z0-9\-]{8,}|"
                r"AKIA[0-9A-Z]{8,}|ASIA[0-9A-Z]{8,})"), _REDACTED),
    # поля Digest-auth: значение обязано быть длинным hash-подобным (иначе съедалось бы
    # обычное слово «response=ok») — R7
    (re.compile(r"(?i)\b(response|nonce|cnonce|opaque)(\s*=\s*)[\"']?[A-Za-z0-9+/=_-]{16,}[\"']?"),
     r"\1\2" + _REDACTED),
    (re.compile(r"\beyJ[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}\.[A-Za-z0-9_\-]{6,}"), _REDACTED),
    # подписи/токены в URL — сохраняем имя параметра
    (re.compile(r"(?i)([?&](?:sig|signature|x-amz-signature|x-amz-credential|access_token|"
                r"token|key)=)[^&\s]+"), r"\1" + _REDACTED),
    # DSN с паролем: scheme://user:pass@host — сохраняем пользователя
    # имя пользователя МОЖЕТ отсутствовать: `redis://:secret@host` (R5-F1)
    (re.compile(r"://([^\s:/@]*):[^\s@]{3,}@"), r"://\1:" + _REDACTED + "@"),
    # длинный неразрывный токен (без точек/слэшей — URL и фразы не задеваются)
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), _REDACTED),
)


# ПОЛНЫЙ реестр источников недоверенного текста, попадающего оператору/в файлы (ревью 25.07,
# три раунда — перечислен целиком вместо латания по одному):
#   1. невалидный вывод companion → outage_details();
#   2. stderr companion при non-zero exit → run_companion_review();
#   3. заголовки находок валидного вердикта → chokepoint разбора (_verdict_from_json + текст);
#   4. причина адъюдикации (ввод оператора/автоматики) → adjudicate(): ledger + audit +
#      команда `findings` + промпт следующего раунда;
#   5. *_SKIP_REASON (LADDER/EMPIRICAL) и detail тривиального маркера → audit;
#   6. эхо `empirical.test_command` в сообщении прогона;
#   7. хвост stdout/stderr тест-команды → _run_empirical();
#   8. ТЕКСТ ИСКЛЮЧЕНИЯ (TimeoutExpired/OSError/ValueError) — включает весь argv команды;
#   9. stdout/stderr `cursor-agent` (провайдер cursor) → run_cursor_review();
#  10. тело дизайн-ревью → CLI `companion-review`. ОСОЗНАННОЕ ИСКЛЮЧЕНИЕ: печатается ДОСЛОВНО.
#      Это не диагностика, а предмет чтения; редакция по шаблонам порезала бы находку,
#      цитирующую sha256, `token=`-строку или любой длинный идентификатор, и ревью стало бы
#      нечитаемым. Компенсация: сам вывод не попадает в маркер (detail маркера редактируется),
#      а деградировавший конверт до печати отсекает companion_outage_reason().
#  11. Gemini HTTP/body/transport diagnostics → run_gemini_review_text(); известный API key
#      удаляется по точному значению ДО общей pattern-based редакции.
#  12. Claude CLI stdout/stderr/исключение → run_claude_supplemental().
# Новый источник → редактировать В ЕГО ИСТОЧНИКЕ, а не у потребителей; сознательное
# исключение — записывать сюда же с обоснованием, чтобы реестр оставался полным.
def redact_secrets(text: str) -> str:
    """Замена секрето-образных подстрок на «скрыто». Сохраняет читаемую причину отказа
    (напр. «You have hit your usage limit… try again at 8:06 PM» не задевается)."""
    out = text
    for rx, template in _REDACT_RULES:
        out = rx.sub(template, out)
    return out


def outage_details(text: "str | None") -> str:
    """Диагностический хвост для невалидного вывода ревью (проверка quota-деградации,
    2026-07-25): реальная причина (напр. «You have hit your usage limit … resets at 15:00»)
    лежит в codex.stderr/parseError/rawOutput envelope и раньше ВЫБРАСЫВАЛАСЬ — оператор
    видел вводящий в заблуждение «дрейф схемы». Контракт парсера не трогаем — только
    извлекаем текст причины для сообщения."""
    if not text:
        return ""
    raw = strip_ansi(text).strip()
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return redact_secrets(raw)[:300]         # сырой текст (напр. лимит) — показать как есть
    if not isinstance(obj, dict):
        return redact_secrets(raw)[:300]
    # R4-F1: редакция ПО ПОЛЯМ после парсинга. Раньше редактировался сериализованный JSON, где
    # кавычки экранированы (\"), и labeled-pair правило их не видело → секрет уходил оператору.
    # Разбор снимает экранирование, поэтому правила работают на настоящем тексте.
    parts = []
    codex = obj.get("codex")
    if isinstance(codex, dict):
        if codex.get("status") not in (0, None):
            parts.append(f"codex exit={codex.get('status')}")
        for k in ("stderr", "stdout"):
            v = codex.get(k)
            if isinstance(v, str) and v.strip():
                parts.append(f"{k}: {redact_secrets(v.strip())[:200]}")
                break                            # достаточно первого непустого
    pe = obj.get("parseError")
    if isinstance(pe, str) and pe.strip():
        parts.append(f"parseError: {redact_secrets(pe.strip())[:150]}")
    raw_out = obj.get("rawOutput")
    if isinstance(raw_out, str) and raw_out.strip() and not any(
            "stdout" in p or "stderr" in p for p in parts):
        parts.append(f"raw: {redact_secrets(raw_out.strip())[:200]}")
    return "; ".join(parts)[:400]


def parse_review_output(text: str) -> ReviewVerdict:
    clean = strip_ansi(text)
    js = _verdict_from_json(clean)            # JSON-first (contract adversarial-review --json)
    if js is not None:
        return js
    m = _VERDICT_RE.search(clean)             # текст-фолбэк (рендер Verdict:/[severity])
    verdict = m.group(1).strip() if m else None
    findings = [(mm.group("sev").strip().lower(), redact_secrets(mm.group("rest").strip()))
                for mm in _FINDING_RE.finditer(clean)]   # тот же chokepoint для текст-контракта
    return ReviewVerdict(
        verdict=verdict, findings=findings,
        malformed=bool(_MALFORMED_FINDING_RE.search(clean)),
        no_findings_marker=bool(_NO_FINDINGS_RE.search(clean)),
    )


def decide_exit(verdict: ReviewVerdict | None, fail_closed: bool) -> int:
    # R1-1: None/невалидный/дрейфнувший = «недоступно», НЕ «чисто».
    if verdict is None or not verdict.valid:
        return 2 if fail_closed else 0
    return 2 if verdict.blocking else 0


# ═══════ Динамический repo-root + конфиг .codex-gate.yaml (решения 1–2 спеки плагина) ═══════

GATE_CONFIG_NAME = ".codex-gate.yaml"
# Жёсткие код-пути (НЕ отключаемы конфигом, ML-P1): правка конфига/хуков/деплой-рецепта
# сама гейтится лесенкой и видна Codex-ревью диффа — иначе конфиг мог бы ослабить сам себя.
HARD_CODE_PATH_EXACT = {
    GATE_CONFIG_NAME,
    "Makefile",
    ".codex/hooks.json",
    ".codex/config.toml",
    ".claude/settings.json",
    ".claude/settings.local.json",
}
HARD_CODE_PATH_PREFIXES = (
    ".githooks/",
    ".codex-plugin/",
    ".claude-plugin/",
    ".agents/plugins/",
    ".claude/.design-approved",
    ".claude/.review-disabled-",
    ".claude/.last-reviewed-sha",
    ".claude/.last-deployed-sha",
    ".claude/.deploy-section-pin",
)
HARD_CODE_PATH_COMPONENTS = (
    "/.codex-plugin/",
    "/.claude-plugin/",
    "/.agents/plugins/",
    "/reviewer_corpus/",
)
_DEFAULT_HARD_CAP = 8


def _detect_repo_root(cwd: "Path | None" = None) -> "Path | None":
    """git rev-parse --show-toplevel от cwd (скрипт живёт в кэше плагина — __file__ бесполезен).
    None = не git-репо/сбой git: хуки → exit 0, явные гейты → явная ошибка (fail-closed)."""
    try:
        cmd = ["git"]
        if cwd is not None:
            cmd.extend(["-C", str(cwd)])
        cmd.extend(["rev-parse", "--show-toplevel"])
        r = subprocess.run(cmd,
                           capture_output=True, text=True)
    except OSError:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return Path(os.path.realpath(r.stdout.strip()))


def _onboarded(root: Path) -> bool:
    """Признак «проект онбординат»: конфиг в worktree ИЛИ в HEAD (спека, Codex R1-фикс:
    временное удаление worktree-файла не должно отключать хуки)."""
    if (root / GATE_CONFIG_NAME).exists():
        return True
    r = subprocess.run(["git", "cat-file", "-e", f"HEAD:{GATE_CONFIG_NAME}"],
                       cwd=root, capture_output=True)
    return r.returncode == 0


def _read_gate_config(root: Path) -> "dict | None":
    """Парсит .codex-gate.yaml. None = нет файла / битый YAML / нет PyYAML / не dict —
    вызывающий трактует как строгий режим (безопасные дефолты, решение 1)."""
    p = root / GATE_CONFIG_NAME
    if p.is_symlink():
        # символишен конфиг указывает на untracked-цель вне контроля диффа (Codex code-R1) —
        # трактуем как битый → строгий режим
        print(f"[codex-gate] {GATE_CONFIG_NAME} — симлинк, не принимается: строгий режим",
              file=sys.stderr)
        return None
    if not p.exists():
        return None
    if yaml is None:
        print(f"[codex-gate] PyYAML не установлен — {GATE_CONFIG_NAME} нечитаем, строгий режим "
              "(все пути = код). Почини: pip install pyyaml", file=sys.stderr)
        return None
    try:
        data = yaml.safe_load(p.read_text())
    except (yaml.YAMLError, OSError, UnicodeError):
        # UnicodeError: не-UTF-8 файл не должен ронять импорт (хук упал бы exit 1 вместо
        # строгого гейта) — это «битый конфиг» → строгий режим (Codex code-R1 medium)
        return None
    return data if isinstance(data, dict) else None


def _code_paths_from_config(cfg: "dict | None") -> "tuple[tuple[str, ...] | None, set[str]]":
    """(prefixes, exact) из конфига. prefixes=None — строгий режим «всё код»
    (нет/битый конфиг или невалидная секция code_paths)."""
    if not isinstance(cfg, dict):
        return None, set()
    cp = cfg.get("code_paths")
    if not isinstance(cp, dict):
        return None, set()
    prefixes = cp.get("prefixes", [])
    exact = cp.get("exact", [])
    if (not isinstance(prefixes, list) or not all(isinstance(x, str) for x in prefixes)
            or not isinstance(exact, list) or not all(isinstance(x, str) for x in exact)):
        return None, set()
    return tuple(prefixes), set(exact)


def _valid_positive_int(v: object, default: int) -> int:
    """Положительный int из конфига или дефолт (bool — подкласс int, отсекаем явно).
    Общая валидация для hard_cap и empirical.timeout_s."""
    if not isinstance(v, int) or isinstance(v, bool) or v < 1:
        return default
    return v


def _hard_cap_from_config(cfg: "dict | None") -> int:
    if not isinstance(cfg, dict):
        return _DEFAULT_HARD_CAP
    conv = cfg.get("convergence")
    if not isinstance(conv, dict):
        return _DEFAULT_HARD_CAP
    return _valid_positive_int(conv.get("hard_cap", _DEFAULT_HARD_CAP), _DEFAULT_HARD_CAP)


REPO_ROOT = _detect_repo_root()
if REPO_ROOT is not None:
    ONBOARDED = _onboarded(REPO_ROOT)
    _GATE_CFG = _read_gate_config(REPO_ROOT)
else:
    ONBOARDED = False
    _GATE_CFG = None
CODE_PATH_PREFIXES, CODE_PATH_EXACT = _code_paths_from_config(_GATE_CFG)
HARD_CAP_ROUNDS = _hard_cap_from_config(_GATE_CFG)

AUDIT_LOG = (REPO_ROOT / "logs" / "codex_review_audit.log") if REPO_ROOT else None
_REVIEW_TIMEOUT_S = 900


def _hooks_active() -> bool:
    """Opt-in автосрабатывающих хуков (BS-P1): вне git-репо или в не-онбордженном проекте
    (нет конфига ни в worktree, ни в HEAD) плагин не вмешивается."""
    return REPO_ROOT is not None and ONBOARDED


def _set_hook_repo_context(root: Path) -> None:
    """Переключить одноразовый hook-процесс на repo из payload.cwd.

    Codex может быть запущен с ``--cd`` из другого каталога; import-time cwd тогда не является
    целевым repo. Все repo-derived глобалы меняются вместе, чтобы нельзя было смешать root одного
    репо с config/state/ledger/audit другого.
    """
    global REPO_ROOT, ONBOARDED, _GATE_CFG, CODE_PATH_PREFIXES, CODE_PATH_EXACT
    global HARD_CAP_ROUNDS, AUDIT_LOG, DESIGN_MARKER
    global LEDGER_DIR, LAST_DEPLOYED, LAST_REVIEWED, DEPLOY_PIN, FINDINGS_DIR, VERDICT_DIR
    REPO_ROOT = Path(os.path.realpath(root))
    ONBOARDED = _onboarded(REPO_ROOT)
    _GATE_CFG = _read_gate_config(REPO_ROOT)
    CODE_PATH_PREFIXES, CODE_PATH_EXACT = _code_paths_from_config(_GATE_CFG)
    HARD_CAP_ROUNDS = _hard_cap_from_config(_GATE_CFG)
    AUDIT_LOG = REPO_ROOT / "logs" / "codex_review_audit.log"
    DESIGN_MARKER = REPO_ROOT / ".claude" / ".design-approved"
    ledger_override = os.environ.get("CODEX_LEDGER_DIR")
    LEDGER_DIR = Path(ledger_override) if ledger_override else REPO_ROOT / "logs" / "review_ledger"
    LAST_DEPLOYED = REPO_ROOT / ".claude" / ".last-deployed-sha"
    LAST_REVIEWED = REPO_ROOT / ".claude" / ".last-reviewed-sha"
    DEPLOY_PIN = REPO_ROOT / ".claude" / ".deploy-section-pin"
    findings_override = os.environ.get("CODEX_FINDINGS_DIR")
    FINDINGS_DIR = (Path(findings_override) if findings_override
                    else REPO_ROOT / "logs" / "review_findings")
    verdict_override = os.environ.get("CODEX_VERDICT_DIR")
    VERDICT_DIR = (Path(verdict_override) if verdict_override
                   else REPO_ROOT / "logs" / "review_verdicts")


def _refresh_hook_repo_context(data: dict) -> "Path | None":
    """Payload cwd авторитетен для выбора repo; не-git cwd оставляет контекст для fail-closed
    проверки рассогласования.

    Возвращает non-onboarded event root, когда уже активный контекст нельзя сразу выключать:
    path сначала должен быть доказуемо локальным этому opt-out repo. Escape обратно в активный
    parent тогда остаётся под G1.
    """
    cwd = data.get("cwd")
    if not (isinstance(cwd, str) and cwd and Path(cwd).is_absolute()):
        return None
    detected = _detect_repo_root(Path(cwd))
    if detected is None:
        return None
    detected_onboarded = _onboarded(detected)
    if not detected_onboarded and _hooks_active():
        return detected
    if REPO_ROOT is None or Path(os.path.realpath(REPO_ROOT)) != detected:
        _set_hook_repo_context(detected)
    return detected if not detected_onboarded else None


def _require_repo() -> bool:
    if REPO_ROOT is None:
        print("[codex-gate] ✗ не git-репозиторий (git rev-parse --show-toplevel не удался) — "
              "явный гейт требует запуска из корня целевого репо.", file=sys.stderr)
        return False
    return True


def warn_if_strict() -> None:
    if CODE_PATH_PREFIXES is None:
        print(f"[codex-gate] ⚠️ {GATE_CONFIG_NAME} отсутствует/битый — СТРОГИЙ режим: все пути "
              "считаются кодом. Почини конфиг (/gates-init) для нормальной работы.",
              file=sys.stderr)


def _trusted_home() -> Path:
    """Домашний каталог из БД пользователей, а не из $HOME: переменную окружения выставляет
    тот же вызывающий, что запускает деплой, и подменённый HOME указывает на свой кэш плагина."""
    try:
        import pwd
        return Path(pwd.getpwuid(os.getuid()).pw_dir)
    except (ImportError, KeyError):
        return Path.home()


#: PATH для сертифицированного прогона: системные каталоги + типовые установки node.
#: PATH вызывающего не используется — иначе `node` подменяется шимом из репозитория.
_TRUSTED_PATH_DIRS = ("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin")
#: Окружение сертифицированного прогона строится АЛЛОУЛИСТОМ, а не денилистом. Перечислять
#: враждебные переменные — заведомо проигрышная игра: помимо инъекций кода (NODE_OPTIONS,
#: LD_PRELOAD, DYLD_*) существуют провайдер-селекторы (CLAUDE_CODE_USE_BEDROCK/VERTEX),
#: провайдерские base_url, skip-auth флаги, прокси (HTTPS_PROXY) и кастомные CA
#: (NODE_EXTRA_CA_CERTS, SSL_CERT_FILE) — любой из них уводит обязательный слот на
#: подконтрольный шлюз, а `modelUsage`/`model` при этом отрапортуют сертифицированное имя.
#: Всё, что не перечислено здесь, в прогон НЕ попадает. Отсутствие нужной переменной = отказ
#: ревьюера, то есть fail-closed и видно оператору, а не тихий увод маршрута.
_CERTIFIED_ENV_ALLOW = (
    "USER", "LOGNAME", "SHELL", "TERM", "TMPDIR",
    "LANG", "LC_ALL", "LC_CTYPE",
    # креды: это аутентификация, а не маршрут (base_url/провайдер-селекторы отброшены выше)
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
)


def _sterile_codex_home(requested_model: str) -> "str | None":
    """Одноразовый gate-owned CODEX_HOME с конфигом, где задана ТОЛЬКО модель.

    Аллоулист закрывает наследуемые переменные, но не файлы: `~/.codex/config.toml`
    принадлежит вызывающему, и он может сохранить сертифицированное имя модели, дописав
    `model_provider`/`base_url` — запрос уйдёт на подконтрольный эндпоинт, а реестр увидит
    ожидаемое имя. Поэтому blocking-прогон получает свой конфиг без provider-секций.
    Учётные данные копируются как есть: это аутентификация, а не маршрут."""
    # Имя модели приходит из файла вызывающего: кавычка/перевод строки дописали бы в
    # gate-owned конфиг ровно ту provider-секцию, ради изоляции которой он и создаётся.
    if not re.fullmatch(r"[A-Za-z0-9._-]+", requested_model or ""):
        return None
    try:
        home = Path(tempfile.mkdtemp(prefix="gates-codex-home-"))
        (home / "config.toml").write_text(f'model = "{requested_model}"\n')
        src = _trusted_home() / ".codex"
        for name in ("auth.json", "credentials.json"):
            cand = src / name
            if cand.is_file():
                shutil.copy2(cand, home / name)
        return str(home)
    except OSError:
        return None


def _certified_subprocess_env(codex_home: "str | None" = None) -> dict:
    """Минимальное окружение: аллоулист + доверенные PATH и HOME. Ни одна переменная
    вызывающего не может ни подменить бинарь, ни увести запрос на чужой эндпоинт."""
    env = {k: v for k, v in os.environ.items() if k in _CERTIFIED_ENV_ALLOW}
    env["PATH"] = os.pathsep.join(_TRUSTED_PATH_DIRS)
    env["HOME"] = str(_trusted_home())
    if codex_home:
        env["CODEX_HOME"] = codex_home
    return env


def _trusted_node_bin() -> "str | None":
    """Абсолютный node из доверенных каталогов (включая nvm под доверенным HOME)."""
    home = _trusted_home()
    nvm = sorted((home / ".nvm" / "versions" / "node").glob("*/bin/node"), reverse=True)
    for cand in [Path(d) / "node" for d in _TRUSTED_PATH_DIRS] + nvm:
        try:
            resolved = cand.resolve(strict=True)
        except OSError:
            continue
        if resolved.is_file() and os.access(resolved, os.X_OK) and not _inside_repo(resolved):
            return str(resolved)
    return None


def _inside_repo(path: Path) -> bool:
    try:
        path.resolve().relative_to(Path(REPO_ROOT).resolve())
        return True
    except (ValueError, OSError):
        return False


def resolve_companion_cmd(*, allow_env_override: bool = True) -> list[str]:
    """allow_env_override=False — для СЕРТИФИЦИРОВАННОГО blocking-прогона. Иначе вызывающий
    (тот же агент, что запускает деплой) подставляет через CODEX_COMPANION_CMD/*_PLUGIN_ROOT
    свой approve-шим вместо обязательного Codex, и Anthropic-авторский код схлопывается в
    саморевью. Тот же класс, что снятый env-обход REVIEW_PROVIDER (§4)."""
    override = os.environ.get("CODEX_COMPANION_CMD") if allow_env_override else None
    if override:
        return shlex.split(override)
    # CODEX_PLUGIN_ROOT (наш override) и официальный CLAUDE_PLUGIN_ROOT (напр. --plugin-dir install);
    # используем только если companion реально там есть, иначе продолжаем к кэш-глобу (Codex P2).
    for env_var in ("CODEX_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        root = os.environ.get(env_var) if allow_env_override else None
        if not root:
            continue
        cand = Path(root) / "scripts" / "codex-companion.mjs"
        if cand.exists():
            return ["node", str(cand)]
        deep = sorted(glob.glob(str(Path(root) / "**" / "codex-companion.mjs"), recursive=True))
        if deep:
            return ["node", deep[-1]]
    # Сертифицированный прогон: HOME/PATH вызывающего не участвуют, node — абсолютный,
    # ни он, ни companion не могут лежать внутри ревьюируемого репозитория.
    home = Path(os.path.expanduser("~")) if allow_env_override else _trusted_home()
    matches = sorted(glob.glob(str(
        home / ".claude/plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs")))
    if not allow_env_override:
        matches = [m for m in matches if not _inside_repo(Path(m))]
    if not matches:
        raise FileNotFoundError("codex-companion.mjs не найден (установлен ли плагин openai-codex?)")
    if allow_env_override:
        return ["node", matches[-1]]
    node = _trusted_node_bin()
    if node is None:
        raise FileNotFoundError("доверенный node не найден для сертифицированного прогона")
    return [node, matches[-1]]


_REVIEW_FOCUS = (
    "Review the committed changes for correctness, safety, and money-loss risks per AGENTS.md. "
    "Return a structured Verdict and findings with severity; critical/high block the deploy.")


def _exec_companion(args: list[str], *, allow_env_override: bool = True,
                    codex_home: "str | None" = None) -> subprocess.CompletedProcess | None:
    """Единственное место, где companion запускается: резолв, таймаут и редакция argv в
    диагностике. Вынесено, чтобы у внешних потребителей (скилл design-review) НЕ было повода
    собирать вызов самим — самосборка печатала argv мимо редакции.
    allow_env_override=False обязателен для сертифицированного blocking-прогона (см.
    resolve_companion_cmd): там путь к движку не берётся из окружения.
    None = отказ (плагин не найден / таймаут / OSError); решение по отказу — за вызывающим."""
    try:
        cmd = resolve_companion_cmd(allow_env_override=allow_env_override)   # Codex P2: нет плагина = outage
    except FileNotFoundError as e:
        print(f"[codex-gate] плагин codex-companion не найден: {e}", file=sys.stderr)
        return None
    try:
        return subprocess.run(cmd + args, capture_output=True, text=True,
                              timeout=_REVIEW_TIMEOUT_S,
                              env=(None if allow_env_override
                                   else _certified_subprocess_env(codex_home)))
    except (subprocess.TimeoutExpired, OSError) as e:
        # источник #8 (R5-F2): TimeoutExpired.__str__ включает ВЕСЬ argv — если в команде есть
        # `--api-key=…`, он попал бы оператору целиком; редактируем текст исключения
        print(f"[codex-gate] companion не отработал: {type(e).__name__}: "
              f"{redact_secrets(str(e))}", file=sys.stderr)
        return None


def companion_outage_reason(out: str) -> str | None:
    """Причина, по которой вывод companion НЕ является ревью (пусто либо деградировавший
    конверт: quota, ошибка модели, отсутствующий result), иначе None.

    Нужна отдельно от parse_review_output: дизайн-ревью возвращает ПРОЗУ без `Verdict:`,
    поэтому требовать валидный вердикт здесь нельзя, а пропускать outage — нельзя тем более
    (инцидент с квотой 2026-07-25: companion отдаёт exit 0 и конверт, который без этой
    проверки читается как «замечаний нет»)."""
    if not out.strip():
        return "companion вернул пустой вывод"
    raw = strip_ansi(out).strip()
    try:
        obj = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return None                              # обычный текст ревью — это норма
    if not isinstance(obj, dict):
        return None
    codex = obj.get("codex")
    degraded = (
        (isinstance(codex, dict) and codex.get("status") not in (0, None))
        or bool(str(obj.get("parseError") or "").strip())
        or obj.get("result") is None
    )
    if not degraded:
        return None
    return outage_details(raw) or "деградировавший конверт companion (result отсутствует)"


def run_companion_review(base: str | None, scope: str) -> str | None:
    # adversarial-review --json даёт СТРУКТУРНЫЙ result{verdict,findings[severity]} (схема),
    # в отличие от нативного `review`, чей вывод — текст P1/P2/P3 без Verdict: (инцидент:
    # нативный формат ломал парсер → make deploy всегда блокировался).
    args = ["adversarial-review", "--wait", "--json", "--scope", scope]
    if base:
        args += ["--base", base]
    args.append(_REVIEW_FOCUS + _adjudication_prompt_block())   # переговорная память серии
    r = _exec_companion(args)
    if r is None:
        return None
    if r.returncode != 0:
        print(f"[codex-gate] review exit={r.returncode}: "
              f"{redact_secrets(r.stderr.strip())[:400]}", file=sys.stderr)   # источник #2
        return None
    return r.stdout


def git_head() -> str:
    return subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True, text=True,
                          check=True).stdout.strip()


def diff_sha256(base: str, head: str = "HEAD") -> str:
    # head явно (R2-F2): check_reviewed биндит всё к захваченному head_before, а не к «HEAD»,
    # который мог сдвинуться конкурентным коммитом за время гейта.
    diff = subprocess.run(["git", "diff", f"{base}..{head}"], capture_output=True, text=True,
                          check=True).stdout
    return hashlib.sha256(diff.encode()).hexdigest()


def working_tree_clean() -> bool:
    out = subprocess.run(["git", "status", "--porcelain"], capture_output=True, text=True,
                         check=True).stdout
    return out.strip() == ""


def skip_requested() -> bool:
    return os.environ.get("CODEX_REVIEW_SKIP") == "1"


def audit(msg: str) -> None:
    if AUDIT_LOG is None:   # вне git-репо аудит некуда писать — не терять след молча
        print(f"[codex-gate] audit (нет репо): {msg}", file=sys.stderr)
        return
    AUDIT_LOG.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_LOG, "a") as f:
        f.write(f"{datetime.now(timezone.utc).isoformat()} {msg}\n")


# LEDGER_DIR перекрывается env CODEX_LEDGER_DIR (изоляция subprocess-тестов make check-reviewed)
_ledger_env = os.environ.get("CODEX_LEDGER_DIR")
LEDGER_DIR = Path(_ledger_env) if _ledger_env else (
    (REPO_ROOT / "logs" / "review_ledger") if REPO_ROOT else None)
LAST_DEPLOYED = (REPO_ROOT / ".claude" / ".last-deployed-sha") if REPO_ROOT else None
LAST_REVIEWED = (REPO_ROOT / ".claude" / ".last-reviewed-sha") if REPO_ROOT else None   # SHA, одобренный check-reviewed


def _record_reviewed(head_sha: str) -> None:
    # деплой-рецепт сверит захваченный SHA с этим → задеплоено ровно то, что одобрено
    LAST_REVIEWED.parent.mkdir(parents=True, exist_ok=True)
    LAST_REVIEWED.write_text(head_sha + "\n")


def resolve_baseline() -> str | None:
    # R1-2: неизвестный baseline → None (fail-closed). НЕ HEAD~1 (ревьюило бы лишь последний
    # коммит, а rsync деплоит всё дерево). Явный CODEX_DEPLOY_BASELINE — В ПРИОРИТЕТЕ над
    # локальным .last-deployed-sha (иначе протухший/кросс-машинный файл нельзя перебить —
    # оператор задаёт baseline, а файл выигрывал; Codex P2).
    env = os.environ.get("CODEX_DEPLOY_BASELINE")
    if env and env.strip():
        return env.strip()
    if LAST_DEPLOYED is not None and LAST_DEPLOYED.exists():
        sha = LAST_DEPLOYED.read_text().strip()
        if sha:
            return sha
    return None


def ledger_path(head_sha: str) -> Path:
    return LEDGER_DIR / f"{head_sha}.json"


def write_ledger(head_sha: str, diff_sha: str, baseline: str, verdict: ReviewVerdict,
                 reviewers: "list[dict] | None" = None) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    ledger_path(head_sha).write_text(json.dumps({
        "head_sha": head_sha, "diff_sha256": diff_sha, "baseline_sha": baseline,
        "reviewers": reviewers if reviewers is not None else [],
        "verdict": verdict.verdict, "findings": verdict.findings,
        "no_findings_marker": verdict.no_findings_marker,
        "malformed": verdict.malformed,
        "ts": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2))


def _reviewers_key(reviewers: "list[dict] | None") -> "list[tuple] | None":
    rows = []
    for r in reviewers or []:
        if not isinstance(r, dict):
            return None
        actual = r.get("actual_models", [])
        if not isinstance(actual, list) or not all(isinstance(v, str) for v in actual):
            return None
        rows.append((
            str(r.get("role", "")),
            str(r.get("provider", "")),
            str(r.get("requested_model", "")),
            str(r.get("model", "")),
            tuple(actual),
            str(r.get("family", "")),
            str(r.get("certification_id", "")),
            str(r.get("policy_id", "")),
            str(r.get("attestation", "")),
        ))
    return sorted(rows)


def read_valid_ledger(head_sha: str, diff_sha: str,
                      reviewers: "list[dict] | None" = None) -> ReviewVerdict | None:
    p = ledger_path(head_sha)
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if rec.get("head_sha") != head_sha or rec.get("diff_sha256") != diff_sha:
        return None
    if reviewers is not None:
        # EARS-5b: кэш годен ТОЛЬКО при совпадении набора {провайдер, модель}. Иначе запрос
        # `both` удовлетворялся бы записью от одного (гейт ЗАЯВЛЯЛ бы двухглазое ревью, которого
        # не было), а кэш cursor+GPT — запросом cursor+Grok (подмена разнообразия). Запись без
        # поля `reviewers` (легаси) годна только для запроса «только codex».
        cached = rec.get("reviewers")
        if cached is None or not isinstance(cached, list) or not cached:
            if [str(r.get("provider", "")) for r in (reviewers or [])] != ["codex"]:
                return None            # легаси-запись годна только для запроса «только codex»
        else:
            cached_key = _reviewers_key(cached)
            requested_key = _reviewers_key(reviewers)
            if cached_key is None or requested_key is None or cached_key != requested_key:
                return None
            for r in cached:                       # переоценка по ТЕКУЩЕМУ allow-list
                if r.get("provider") == "cursor" and r.get("model") not in _CURSOR_MODEL_ALLOW:
                    return None
                role = r.get("role")
                cert_id = r.get("certification_id")
                if role in {"blocking", "supplemental"} and cert_id:
                    policy_id, _certs = load_reviewer_certifications()
                    requested_model = r.get("requested_model")
                    if (not policy_id or r.get("policy_id") != policy_id
                            or not _nonempty_str(requested_model)):
                        return None
                    cert = reviewer_certification(
                        str(r.get("provider", "")),
                        str(requested_model),
                        str(role),
                    )
                    if cert is None or cert.certification_id != cert_id:
                        return None
    v = ReviewVerdict(verdict=rec.get("verdict"),
                      findings=[tuple(f) for f in rec.get("findings", [])],
                      no_findings_marker=rec.get("no_findings_marker", False),
                      malformed=rec.get("malformed", False))
    return None if (v.blocking or not v.valid) else v


def _ladder_check(baseline: str) -> int:
    # Lazy import (monkeypatch-able точка `g._ladder_check`); ladder_gate — sibling-модуль в
    # каталоге плагина (не в пакете `scripts` целевого репо, как было в проекте-источнике).
    try:
        from ladder_gate import check_range
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from ladder_gate import check_range  # type: ignore[no-redef]
    return check_range(REPO_ROOT, baseline)


# ═══════ Протокол сходимости Claude↔Codex (Фаза 1.6) ═══════
# Спека: docs/methodology/2026-07-22-review-convergence-protocol-design.md.
# Finding-ledger с памятью между раундами + адъюдикация Claude + машинное правило
# allow/block/escalate — вместо «стены high'ов» и человеческого SKIP (инцидент 22.07:
# 8 раундов, выход только скипом).

# CODEX_FINDINGS_DIR — изоляция make-субпроцесс-тестов (инцидент: block-стаб тест писал
# находку фикстуры в БОЕВУЮ серию и архивировал её — тот же класс, что CODEX_LEDGER_DIR)
_findings_env = os.environ.get("CODEX_FINDINGS_DIR")
FINDINGS_DIR = Path(_findings_env) if _findings_env else (
    (REPO_ROOT / "logs" / "review_findings") if REPO_ROOT else None)
ADJ_STATUSES = {"fixed", "residual-failsafe", "refuted", "resolved-by-user", "open"}
_PREFIX_RE = re.compile(r"^\[(DUP|DISPUTE):(F\d+)\]\s*(.*)$", re.IGNORECASE)


class AdjudicationError(Exception):
    """Невалидная адъюдикация (critical→residual, пустая причина, неизвестный id)."""


import contextlib
import fcntl


@contextlib.contextmanager
def findings_lock():
    """Эксклюзивный лок ledger-серии (протокол-догфуд F3: конкурентный чистый review
    двух сессий стирал blocking-находку через read-modify-write гонку → allow)."""
    FINDINGS_DIR.mkdir(parents=True, exist_ok=True)
    lf = open(FINDINGS_DIR / ".lock", "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


def parse_finding_prefix(title: str) -> "tuple[str | None, str | None, str]":
    """[DUP:Fx]/[DISPUTE:Fx]-префикс → (kind, fid, остальной title)."""
    m = _PREFIX_RE.match(title.strip())
    if not m:
        return None, None, title.strip()
    return m.group(1).lower(), m.group(2).upper(), m.group(3).strip()


def load_findings_ledger(baseline: "str | None") -> "dict | None":
    """Текущая деплой-серия. Битый файл → None (fail-closed у вызывающего, ML-C3).
    baseline сменился (успешный деплой сдвинул) → архив старой серии, свежая новая."""
    p = FINDINGS_DIR / "current.json"
    if not p.exists():
        return {"baseline": baseline or "", "rounds": 0, "findings": {}}
    try:
        led = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    # Структурная валидация (F7: битый-но-JSON ledger с findings-не-dict давал пустой opens
    # → allow на мусоре). Любое отклонение формы → None → fail-closed у вызывающего.
    if not isinstance(led, dict) or not isinstance(led.get("findings", {}), dict) \
            or not isinstance(led.get("rounds", 0), int) or isinstance(led.get("rounds", 0), bool) \
            or not (isinstance(led.get("baseline"), str) and led.get("baseline")):
        return None   # серия без baseline — сирота, применялась бы к любому деплою (спор F7-3)
    _VALID_STATUSES = ADJ_STATUSES | {"duplicate", "carried"}
    _ADJUDICATED = {"fixed", "residual-failsafe", "refuted", "resolved-by-user"}
    for f in led.get("findings", {}).values():
        if not isinstance(f, dict) or not isinstance(f.get("severity"), str) \
                or f.get("status") not in _VALID_STATUSES:
            return None   # неизвестный status (опечатка/мусор) скрывал бы blocking (спор F7-2)
        if f.get("status") in _ADJUDICATED and not (
                isinstance(f.get("reason"), str) and f["reason"].strip()):
            return None   # адъюдикация без причины = обход аудита рукой в файле (спор F7-4)
    if baseline and led.get("baseline") and led["baseline"] != baseline:
        arch = FINDINGS_DIR / "archive"
        arch.mkdir(parents=True, exist_ok=True)
        ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
        p.rename(arch / f"{ts}-{led['baseline'][:12]}.json")
        # Carry-over (реш. юзера 22.07): carried-находки прошлой серии стартуют НОВУЮ серию
        # ОТКРЫТЫМИ — «бэклог с зубами»: следующий деплой блокируется на них первым делом.
        # §5b (ревью ред. 3/4): наследуется НЕ только `carried`. Раньше аварийный skip после
        # частичного раунда терял известную critical: она лежала `open`, деплой сдвигал baseline,
        # серия архивировалась — и находка исчезала. Наследуем всё НЕРАЗРЕШЁННОЕ:
        #   • open с блокирующей severity — решения по ней нет вовсе;
        #   • неподтверждённая адъюдикация (ревьюеры не видели решения — `needs_review_round`):
        #     иначе Claude помечает чужую critical `fixed`, skip сдвигает baseline до раунда
        #     Codex, и решение никто не проверил;
        #   • carried (low/medium) — как и раньше.
        # НЕ наследуются подтверждённые адъюдикации, resolved-by-user и duplicate: по ним есть
        # решение, увиденное независимой стороной либо принятое человеком (иначе вечный блок).
        pending_adj = bool(led.get("needs_review_round"))
        inherited = {}
        for k, f in (led.get("findings") or {}).items():
            status = f.get("status")
            unconfirmed = pending_adj and status in ("fixed", "refuted", "residual-failsafe")
            if not (status == "carried"
                    or (status == "open" and _is_blocking_severity(f.get("severity")))
                    or unconfirmed):
                continue
            rec = {
                "severity": f.get("severity"), "title": f.get("title"),
                "status": "open", "dup_of": None, "disputes": 0, "round": 0,
                "carried_from": led["baseline"],
                "carry_count": int(f.get("carry_count") or 0) + 1,
                "provider": f.get("provider"),   # происхождение блокирующей находки не теряем
            }
            if unconfirmed:
                # причина адъюдикации переезжает вместе с находкой: следующий раунд обязан
                # показать ревьюерам, ЧТО именно им предлагали принять
                rec["unconfirmed_adjudication"] = True
                rec["reason"] = f.get("reason")
                audit(f"inherit-unconfirmed {k} [{f.get('severity')}] статус {status!r} не был "
                      "показан ревьюерам — наследуется открытым (§5b)")
            inherited[f"F{len(inherited) + 1}"] = rec
        return {"baseline": baseline, "rounds": 0, "findings": inherited}
    return led


def _atomic_write_json(path: Path, obj: object, indent: "int | None" = None) -> None:
    """Атомарная запись JSON (tmp + replace). Общая для findings-ledger и маркеров."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(obj, ensure_ascii=False, indent=indent))
    tmp.replace(path)


def save_findings_ledger(led: dict) -> None:
    _atomic_write_json(FINDINGS_DIR / "current.json", led, indent=2)


def _is_blocking_severity(sev: object) -> bool:
    """Блокирующая severity: critical/high ЛИБО неизвестная (R1-1b: всё неизвестное = блок)."""
    return sev in SEVERITY_BLOCKING or sev not in KNOWN_SEVERITIES


def _dup_root(fnd: dict, fid: str) -> "str | None":
    """Корень цепочки `dup_of`. None = невалидная ссылка (висячая, цикл, слишком длинная).

    §6b (ревью ред. 4): правила статуса нельзя применять к НЕПОСРЕДСТВЕННОЙ цели ссылки — в
    ledger есть записи `duplicate`, и `[DUP:F2]`, где F2 дублирует решённую F1, породил бы ещё
    один неблокирующий дубликат, не пере-открыв ничего. Блокирующая находка тонула бы через
    один уровень косвенности (в т.ч. под влиянием текста диффа)."""
    seen: set[str] = set()
    cur = fid
    while True:
        rec = fnd.get(cur)
        if rec is None or cur in seen:      # висячая ссылка либо цикл → fail-closed
            return None
        if len(seen) > len(fnd):            # страховка от длинной цепочки
            return None
        seen.add(cur)
        nxt = rec.get("dup_of")
        if not nxt:
            # `duplicate` без цели — битая запись, а не корень. Иначе критическая находка со
            # ссылкой на неё оседала бы ещё одним неблокирующим дубликатом (fail-open при
            # порче/ручной правке ledger).
            return None if rec.get("status") == "duplicate" else cur
        cur = nxt                           # self-loop (nxt == cur) поймает проверка `cur in seen`


def merge_round(led: dict, blocking_findings: "list[tuple]",
                review_started_ts: "float | None" = None, partial: bool = False) -> None:
    """Влить раунд Codex в ledger. [DUP:открытого/fixed] → duplicate-привязка;
    [DUP:residual/refuted] → пере-подъём = dispute (спека R1: DUP ≠ согласие);
    [DISPUTE:Fx] → disputes+1 + re-open; прочее → новый open."""
    # partial=True (EARS-14c): раунд НЕ состоялся (один из запрошенных провайдеров отказал) —
    # счётчик не инкрементим (outage не должен жечь hard-cap) и needs_review_round не сбрасываем
    # (адъюдикации показаны не ВСЕМ запрошенным ревьюерам), НО находки успешного вливаем, иначе
    # уже найденный blocking потеряется между прогонами (ledger — единственная память серии).
    if not partial:
        led["rounds"] = int(led.get("rounds") or 0) + 1
        # Флаг чистится, только если review СТАРТОВАЛ после последней адъюдикации (спор F3-3:
        # старый review, финишировавший после адъюдикации, очищал флаг, не видев её).
        if review_started_ts is None or review_started_ts >= float(led.get("last_adj_ts") or 0):
            led["needs_review_round"] = False
    fnd = led.setdefault("findings", {})

    def new_fid() -> str:
        return f"F{len(fnd) + 1}"

    for item in blocking_findings:
        sev, title = item[0], item[1]
        provider = item[2] if len(item) > 2 else None      # EARS-15: кто поднял находку
        kind, fid, rest = parse_finding_prefix(title)
        # §6b: ссылка разрешается до КОРНЯ цепочки dup_of; невалидная (висячая/цикл) → None,
        # и запись падает в ветку «новая open-находка» ниже (fail-closed, как для неизвестного id)
        fid = _dup_root(fnd, fid) if (kind in ("dup", "dispute") and fid) else fid
        target = fnd.get(fid) if fid else None
        # §6b: блокирующий спор/DUP по корню, закрытому ЧЕЛОВЕКОМ, не проглатывается в late_note —
        # он несёт новую улику и обязан открыть отдельную находку с собственным разрешением.
        # Вечной эскалации нет: у новой записи свой disputes=0, исходная остаётся терминальной.
        if (kind in ("dup", "dispute") and target is not None
                and target.get("status") == "resolved-by-user"
                and _is_blocking_severity(sev)):
            fnd[new_fid()] = {"severity": sev, "title": rest, "status": "open", "dup_of": None,
                              "disputes": 0, "round": led["rounds"], "provider": provider,
                              "reopened_from": fid}
            audit(f"reopen-after-resolved {fid} → новая open-находка [{sev}] "
                  f"«{redact_secrets(rest)[:80]}» (§6b: новая улика после решения человека)")
            continue
        if kind in ("dup", "dispute") and target is not None:
            # `carried` не блокирует (convergence считает только `open`), поэтому DUP на такой
            # корень оседал дубликатом, эскалировав лишь severity: получалось запрещённое P7
            # состояние severity=critical/status=carried, и новая находка второго члена пары
            # не блокировала. Блокирующая улика обязана вернуть корень в игру.
            if target.get("status") == "carried" and _is_blocking_severity(sev):
                target["status"] = "open"
                audit(f"reopen-carried {fid} — блокирующая улика вернула перенесённую находку "
                      "в открытые (§5/P7)")
            _SEV_RANK = {"critical": 3, "high": 2, "medium": 1, "low": 0}
            if _SEV_RANK.get(sev, 3) > _SEV_RANK.get(target.get("severity"), 3):   # unknown=critical с ОБЕИХ сторон (спор F4-2)
                target["severity"] = sev   # re-raise эскалирует severity ОБЕИМИ ветками (споры
                                           # F1-2/F1-3: critical-DUP и critical-DISPUTE не повышали
                                           # оригинал → обход запрета critical→residual)
        if kind == "dup" and target is not None:
            if target["status"] == "resolved-by-user":
                # финальность человека: Codex не пере-открывает его решение (F3 d=5:
                # спор пере-открывал resolved-by-user → вечная эскалация мимо юзера)
                target["late_note"] = rest
            elif target["status"] in ("residual-failsafe", "refuted", "fixed"):
                # пере-подъём адъюдицированного; для fixed — улика, что фикс НЕ сработал
                # (протокол-догфуд F1: DUP:fixed сворачивался тихо → allow с непочиненным)
                target["status"] = "open"
                target["disputes"] = int(target.get("disputes") or 0) + 1
            fnd[new_fid()] = {"severity": sev, "title": rest, "status": "duplicate",
                              "dup_of": fid, "disputes": 0, "round": led["rounds"],
                              "provider": provider}
            continue
        if kind == "dispute" and target is not None:
            if target["status"] == "resolved-by-user":
                target["late_note"] = rest        # финальность человека (см. выше)
                continue
            target["status"] = "open"
            target["disputes"] = int(target.get("disputes") or 0) + 1
            target["dispute_note"] = rest
            continue
        rec_new = {"severity": sev, "title": rest, "status": "open", "dup_of": None,
                   "disputes": 0, "round": led["rounds"], "provider": provider}
        if partial:
            rec_new["from_partial_round"] = True
        fnd[new_fid()] = rec_new


def adjudicate(led: dict, fid: str, status: str, reason: str) -> None:
    """Классификация Claude по стоп-политике. Guards (ML-C1): critical не residual;
    причина обязательна; resolved-by-user — только решение человека (можно всё)."""
    f = (led.get("findings") or {}).get(fid)
    if f is None:
        raise AdjudicationError(f"неизвестный finding {fid!r}")
    if status not in ADJ_STATUSES:
        raise AdjudicationError(f"статус {status!r} ∉ {sorted(ADJ_STATUSES)}")
    if not reason.strip():
        raise AdjudicationError("причина обязательна (аудит)")
    sev_known = f.get("severity") in KNOWN_SEVERITIES
    if status == "residual-failsafe" and (f.get("severity") == "critical" or not sev_known):
        raise AdjudicationError(
            "critical-находка не адъюдицируется в residual (ML-C1): только "
            "fixed/refuted, спорная → эскалация человеку")
    # §6c (ревью ред. 5): адъюдицирует Claude — тот же агент, что пишет код и входит в
    # blocking-панель. Разрешённый `critical → refuted` позволял закрыть находку ЧУЖОГО
    # ревьюера, после чего один валидный ответ без находок (согласие ИЛИ стохастический
    # промах) делал её терминальной. Молчание не должно закрывать блокирующую находку:
    # для неё остаются `fixed` (+ состоявшийся раунд) и решение человека.
    if status in ("refuted", "residual-failsafe") and _is_blocking_severity(f.get("severity")):
        raise AdjudicationError(
            f"{status!r} запрещён для severity {f.get('severity')!r}: блокирующую находку "
            "закрывает только `fixed` с состоявшимся раундом обоих ревьюеров либо "
            "`resolved-by-user` (§6c — молчание ревьюера не снимает находку)")
    f["status"] = status
    # источник #4: причина от оператора/автоматики может нести секрет-улику; редактируем ДО
    # сохранения — тогда ledger, audit, вывод `findings` и промпт следующего раунда чисты
    f["reason"] = redact_secrets(reason.strip())
    import time as _time
    led["last_adj_ts"] = _time.time()
    led["needs_review_round"] = True   # Codex должен УВИДЕТЬ адъюдикацию (спор F3-2: кэш
    audit(f"adjudicate {fid} → {status}: {f['reason']!r}")   # позволял allow без его раунда)


def apply_carry_over(led: dict) -> "list[str]":
    """Пост-hard-cap НОВЫЕ неоспоренные не-critical находки → carried (реш. юзера 22.07):
    срочный деплой едет, находка стартует следующую серию ОТКРЫТОЙ (бэклог с зубами).
    Critical/unknown-severity и оспоренные — НЕ переносятся (блокируют/эскалируют)."""
    if int(led.get("rounds") or 0) <= HARD_CAP_ROUNDS:
        return []
    carried = []
    for k, f in (led.get("findings") or {}).items():
        # §5 (ревью ред. 2): `high` входит в SEVERITY_BLOCKING, но раньше исключался только
        # `critical` — новая high независимого ревьюера после hard-cap переставала блокировать.
        # Шумом можно было догнать серию до cap и обесценить настоящую находку, не снимая её.
        if (f.get("status") == "open"
                and int(f.get("round") or 0) == int(led.get("rounds") or 0)
                and not _is_blocking_severity(f.get("severity"))
                and int(f.get("disputes") or 0) == 0
                and not f.get("carried_from")):
            f["status"] = "carried"
            carried.append(k)
            audit(f"carry-over {k} [{f.get('severity')}] «{f.get('title', '')[:80]}» — "
                  f"в следующую серию (post-hard-cap, реш. юзера 22.07)")
            print(f"[codex-gate] ↪️ carry-over {k} [{f.get('severity')}] "
                  f"«{f.get('title', '')[:80]}» — НЕ блокирует этот деплой, откроет следующую "
                  f"серию (следующий deploy на ней заблокируется до разрешения)", file=sys.stderr)
    return carried


def convergence_decision(led: dict) -> "tuple[str, str]":
    """('allow'|'block'|'escalate', message) — машинное правило спеки §4."""
    fnd = led.get("findings") or {}
    opens = {k: f for k, f in fnd.items() if f.get("status") == "open"}
    for k, f in fnd.items():
        d = int(f.get("disputes") or 0)
        thr = 1 if f.get("severity") in ("critical", "high") else 2
        # эскалация: НЕРАЗРЕШЁННЫЙ спор (open) ≥ порога, ИЛИ ≥3 споров всего (жёсткое
        # несогласие: Claude принимает и фиксит, Codex продолжает оспаривать → человек;
        # иначе принятый+пофикшенный спор залипал в вечный escalate — deadlock)
        # resolved-by-user — ТЕРМИНАЛЕН: человек уже в петле, его решение закрывает спор
        # (иначе d≥3 эскалировал бы вечно и после решения — deadlock)
        if f.get("status") == "resolved-by-user":
            continue
        if (d >= thr and f.get("status") == "open") or d >= 3:
            return ("escalate",
                    f"[codex-gate] ⚖️ ЭСКАЛАЦИЯ: спор по {k} «{f.get('title', '')[:60]}» "
                    f"(disputes={d}, severity={f.get('severity')}). Нужно решение человека: "
                    f"`adjudicate {k} resolved-by-user \"...\"` | fix | аварийный SKIP.")
    for k, f in opens.items():
        if int(f.get("carry_count") or 0) >= 2:   # анти-гниение: 2 серии подряд → человек
            return ("escalate",
                    f"[codex-gate] ⚖️ ЭСКАЛАЦИЯ: {k} «{f.get('title', '')[:60]}» пережила "
                    f"{f['carry_count']} серии без разрешения — дальше не переносится, нужно "
                    f"решение человека (fix | resolved-by-user).")
    if opens and int(led.get("rounds") or 0) > HARD_CAP_ROUNDS:
        return ("escalate",
                f"[codex-gate] ⚖️ ЭСКАЛАЦИЯ: hard-cap {HARD_CAP_ROUNDS} раундов пройден, "
                f"открыты: {', '.join(sorted(opens))}. Нужно решение человека.")
    if opens:
        lst = "; ".join(f"{k}[{f.get('severity')}] {f.get('title', '')[:70]}"
                        for k, f in sorted(opens.items()))
        return ("block",
                f"[codex-gate] ✗ открытые находки: {lst}. Адъюдицируй по стоп-политике: "
                f"`codex_review_gate.py adjudicate <Fid> fixed|residual-failsafe|refuted "
                f"\"причина\"` и перезапусти.")
    counts: dict[str, int] = {}
    for f in fnd.values():
        counts[f.get("status", "?")] = counts.get(f.get("status", "?"), 0) + 1
    return ("allow",
            f"[codex-gate] ✓ сошлись за {led.get('rounds')} раунд(ов): " +
            (", ".join(f"{s}={n}" for s, n in sorted(counts.items())) or "0 находок"))


def _adjudication_prompt_block() -> str:
    """Блок адъюдикаций для промпта Codex (переговорная часть, спека §3)."""
    led = load_findings_ledger(None)
    if not led or not led.get("findings"):
        return ""
    lines = []
    for k, f in sorted(led["findings"].items()):
        if f.get("status") in ("residual-failsafe", "refuted", "fixed", "resolved-by-user"):
            lines.append(f"{k} [{f.get('severity')}] «{f.get('title', '')[:80]}» → "
                         f"{f['status']}: {f.get('reason', '')[:120]}")
    if not lines:
        return ""
    return (" PREVIOUSLY ADJUDICATED FINDINGS (agreed history of this deploy series): "
            + " | ".join(lines) +
            ". If you AGREE with an adjudication — do NOT report that finding again. "
            "If your finding restates an OPEN or FIXED item Fx, prefix its title with [DUP:Fx]. "
            "If you DISPUTE an adjudication with NEW evidence, prefix the title with "
            "[DISPUTE:Fx] and put the evidence in body.")


# ═══════ Эмпирический гейт (спека docs/2026-07-22-empirical-gate-design.md, тикет #1) ═══════
# Механическая проверка (прогон тест-команды) в порядке ladder → empirical → Codex: тесты
# падают → блок ДО трат на Codex. Tier 2 (actuator-safety): guard над деплой-актуатором.
_DEFAULT_EMPIRICAL_TIMEOUT = 600


def _config_section_at_ref(root: Path, ref: str, section: str) -> "tuple[str, dict | None]":
    """SHA-bound чтение секции конфига (generic, спека inframon-интерфейса):
    (state, секция-dict|None), state ∈ {'absent','enabled','unreadable'}.
    `absent` — ТОЛЬКО при доказанном отсутствии (успешное чтение дерева без пути ЛИБО
    прочитанный+распарсенный конфиг без секции-dict). ЛЮБАЯ git/tree/object/парс-ошибка →
    'unreadable' (git-сбой ≠ «чисто»). Доказательство — `git ls-tree`, НЕ код возврата
    `git show`/`cat-file -e` (те не различают «нет пути» и «объект не читается»).

    NB: парс-слой НЕ переиспользует _read_gate_config: тому нужен ref-bound blob и
    ТРЁХСТАТУСНЫЙ исход, а _read_gate_config читает worktree и коллапсирует в dict|None."""
    ls = subprocess.run(["git", "ls-tree", ref, "--", GATE_CONFIG_NAME],
                        cwd=root, capture_output=True, text=True)
    if ls.returncode != 0:
        return ("unreadable", None)             # дерево/ref не прочитано — доказательства нет
    if not ls.stdout.strip():
        return ("absent", None)                 # дерево прочитано, пути нет — ДОКАЗАНО absent
    blob = subprocess.run(["git", "cat-file", "blob", f"{ref}:{GATE_CONFIG_NAME}"],
                         cwd=root, capture_output=True)
    if blob.returncode != 0:
        return ("unreadable", None)             # путь есть, но объект не читается
    if yaml is None:
        return ("unreadable", None)             # нет PyYAML при наличии файла — не подтвердить
    try:
        data = yaml.safe_load(blob.stdout.decode())
    except (yaml.YAMLError, UnicodeError):
        return ("unreadable", None)
    if not isinstance(data, dict):
        return ("absent", None)                 # валидный YAML, не dict — секции нет
    sec = data.get(section)
    if not isinstance(sec, dict):
        return ("absent", None)
    return ("enabled", sec)


def _empirical_config(root: Path, ref: str) -> "tuple[str, str | None, int]":
    """Состояние эмпирического гейта на ref (EARS-8/9 эмпирики, поверх generic-читателя):
    (state, test_command|None, timeout_s). Секция без валидной команды = absent (не opt-in)."""
    d = _DEFAULT_EMPIRICAL_TIMEOUT
    state, sec = _config_section_at_ref(root, ref, "empirical")
    if state != "enabled":
        return (state, None, d)
    cmd = sec.get("test_command")
    if not (isinstance(cmd, str) and cmd.strip()):
        return ("absent", None, d)              # секция без валидной команды — доказанно не opt-in
    return ("enabled", cmd.strip(), _valid_positive_int(sec.get("timeout_s", d), d))  # S9


def _run_empirical(cmd: str, timeout_s: int, root: Path,
                   cwd: "Path | None" = None, env: "dict | None" = None) -> "tuple[str, str]":
    """Прогон тест-команды. ('pass'|'fail'|'timeout'|'error', хвост вывода). Любой не-'pass' →
    блок (актуатор-урок: «не запустилось/зависло» ≠ «прошло»).

    `cwd`/`env` — для pre-push гейта (прогон в worktree-пробе с отключёнными git-хуками).
    Дефолты сохраняют поведение деплой-пути: cwd=root, окружение процесса. Вторая копия этой
    функции в prepush_gate означала бы две копии актуатор-урока (ревью 2026-07-26).

    argv через `shlex.split` + БЕЗ shell (bounded authority, defense-in-depth): `test_command`
    исполняется как список аргументов, shell-метасимволы не интерпретируются. Покрывает обычные
    команды (`python3 -m pytest -q`, `make test`); для пайплайнов/`&&` — обернуть в скрипт и
    указать его (`test_command: ./run-tests.sh`)."""
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return ("error", redact_secrets(f"не разобрать test_command: {e}"))
    if not argv:
        return ("error", "пустая test_command")
    try:
        r = subprocess.run(argv, cwd=(cwd or root), capture_output=True, text=True,
                           timeout=timeout_s, env=env)
    except subprocess.TimeoutExpired:
        return ("timeout", "")
    except OSError as e:
        # источник #8: текст исключения может содержать argv с секретом (R5-F2)
        return ("error", redact_secrets(f"{type(e).__name__}: {e}"))
    # редакция того же класса: вывод тестов может содержать дамп env/DSN (ревью 25.07)
    tail = redact_secrets(r.stdout + r.stderr)[-800:]
    return ("pass" if r.returncode == 0 else "fail", tail)


# ═══════ Локальная Фаза 2: интерфейс к inframon (спека 2026-07-23-inframon-interface) ═══════
# Ф1: authoritative baseline через deploy.baseline_command (pin одобренной секции — анти-
# self-hiding); Ф2: машиночитаемый вердикт деплой-гейта для внешнего guard'а (inframon).
_DEFAULT_BASELINE_TIMEOUT = 30
_SHA40_RE = re.compile(r"^[0-9a-f]{40}$")
DEPLOY_PIN = (REPO_ROOT / ".claude" / ".deploy-section-pin") if REPO_ROOT else None
_verdict_env = os.environ.get("CODEX_VERDICT_DIR")
VERDICT_DIR = Path(_verdict_env) if _verdict_env else (
    (REPO_ROOT / "logs" / "review_verdicts") if REPO_ROOT else None)


def _deploy_section_hash(sec: dict) -> str:
    return hashlib.sha256(json.dumps(sec, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _read_pin() -> "str | None | object":
    """Значение pin ('<hash>'|'disabled') | None (нет файла) | _PIN_CORRUPT (битый —
    fail-closed как несовпадение, спека B9b)."""
    if not DEPLOY_PIN.exists():
        return None
    try:
        rec = json.loads(DEPLOY_PIN.read_text())
        pin = rec.get("pin")
        return pin if isinstance(pin, str) and pin else _PIN_CORRUPT
    except (OSError, json.JSONDecodeError):
        return _PIN_CORRUPT


_PIN_CORRUPT = object()


def _write_pin(value: str) -> None:
    _atomic_write_json(DEPLOY_PIN, {"pin": value, "ts": datetime.now(timezone.utc).isoformat()})


def _run_baseline_command(cmd: str, timeout_s: int) -> "str | None":
    """Прогон baseline_command (argv, без shell — как test_command). Полный 40-hex SHA после
    strip; всё прочее (fail/timeout/мусор) → None (fail-closed у вызывающего, НЕ фолбэк)."""
    try:
        argv = shlex.split(cmd)
        if not argv:
            return None
        r = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True,
                          timeout=timeout_s)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return None
    if r.returncode != 0:
        return None
    out = r.stdout.strip().lower()
    return out if _SHA40_RE.match(out) else None


def _resolve_baseline_gate(head: str) -> "tuple[str | None, int]":
    """(baseline, rc). rc=2 — блок (сообщение напечатано); rc=0 — baseline (может быть None →
    существующая R1-2 логика решает дальше). Порядок (спека B1–B12): env-переход (аудит EARS-3/3b,
    обновление pin) → pin-сверка секции deploy (head_before) → команда (no-fallback) → легаси."""
    env = os.environ.get("CODEX_DEPLOY_BASELINE", "").strip()
    state, sec = _config_section_at_ref(REPO_ROOT, head, "deploy")
    if state == "unreadable" and not env:
        print("[codex-gate] ✗ секция deploy в .codex-gate.yaml на HEAD нечитаема (git-сбой/битый "
              "YAML/нет PyYAML) — состояние authoritative-baseline не подтвердить. Деплой "
              "остановлен (переход/обход — явный CODEX_DEPLOY_BASELINE).", file=sys.stderr)
        return (None, 2)
    cmd = None
    timeout = _DEFAULT_BASELINE_TIMEOUT
    if state == "enabled":
        c = sec.get("baseline_command")
        if isinstance(c, str) and c.strip():
            cmd = c.strip()
            timeout = _valid_positive_int(sec.get("baseline_timeout_s", timeout), timeout)
        elif "baseline_command" in sec and not env:
            # code-R1 F2: ключ ПРИСУТСТВУЕТ, но невалиден (список/пусто/не-строка) — опечатка
            # НЕ должна тихо откатывать к протухшему локальному файлу (запрещённый фолбэк)
            print("[codex-gate] ✗ deploy.baseline_command присутствует, но невалиден (ожидается "
                  "непустая строка) — тихий откат на локальный файл запрещён. Почини конфиг или "
                  "явный CODEX_DEPLOY_BASELINE. Деплой остановлен.", file=sys.stderr)
            return (None, 2)
    section_pin = _deploy_section_hash(sec) if cmd else "disabled"
    pin = _read_pin()
    if env:
        # env-переход: EARS-3 (перебив authoritative) + EARS-3b (ЛЮБОЕ изменение pin — аудит)
        if cmd:
            audit(f"CODEX_DEPLOY_BASELINE={env[:12]} перебил authoritative baseline_command (B7)")
        if pin is _PIN_CORRUPT or pin != section_pin:
            old = "corrupt" if pin is _PIN_CORRUPT else (pin or "none")
            audit(f"deploy-section pin переход: {old} → {section_pin[:12] if cmd else 'disabled'} "
                  f"(env-переход, EARS-3b)")
            _write_pin(section_pin)
        return (env, 0)
    if cmd:                                       # секция enabled
        if pin is None:
            print("[codex-gate] ✗ deploy.baseline_command активирован впервые — переходный деплой "
                  "только через явный CODEX_DEPLOY_BASELINE (аудируется, запишет pin). Деплой "
                  "остановлен.", file=sys.stderr)
            return (None, 2)
        if pin is _PIN_CORRUPT or pin != section_pin:
            print("[codex-gate] ✗ секция deploy изменилась относительно одобренного pin (или pin "
                  "битый) — самоскрывающаяся смена baseline_command блокируется (спека R1-F1). "
                  "Переход — явный CODEX_DEPLOY_BASELINE. Деплой остановлен.", file=sys.stderr)
            return (None, 2)
        sha = _run_baseline_command(cmd, timeout)
        if sha is None:                           # no-fallback (EARS-2): НЕ откатываемся на файл
            print("[codex-gate] ✗ authoritative baseline_command упал/таймаут/невалидный вывод — "
                  "деплой остановлен. Фолбэк на локальный .last-deployed-sha ЗАПРЕЩЁН (протухший "
                  "файл = не тот диапазон ревью). Почини источник или явный CODEX_DEPLOY_BASELINE.",
                  file=sys.stderr)
            return (None, 2)
        return (sha, 0)
    # секция absent
    if pin is None:                               # bootstrap (R2-F1): absent не доказывает legacy
        legacy = resolve_baseline()
        if legacy is not None:
            b_state, b_sec = _config_section_at_ref(REPO_ROOT, legacy, "deploy")
            b_cmd = b_state == "enabled" and isinstance(b_sec.get("baseline_command"), str) \
                and b_sec["baseline_command"].strip()
            if b_state == "unreadable" or b_cmd:
                print("[codex-gate] ✗ на baseline секция deploy была включена/нечитаема, на HEAD "
                      "отсутствует, pin нет (новая машина?) — удаление authoritative-источника "
                      "требует явного CODEX_DEPLOY_BASELINE (аудируется). Деплой остановлен.",
                      file=sys.stderr)
                return (None, 2)
            _write_pin("disabled")                # честный legacy — bootstrap завершён (B12)
        return (legacy, 0)
    if pin is _PIN_CORRUPT or pin != "disabled":  # была enabled, секцию удалили без перехода
        print("[codex-gate] ✗ секция deploy удалена, но pin помнит authoritative-источник — "
              "удаление без перехода блокируется. Явный CODEX_DEPLOY_BASELINE (аудируется). "
              "Деплой остановлен.", file=sys.stderr)
        return (None, 2)
    return (resolve_baseline(), 0)                # pin=disabled → легаси честно


def _ladder_range_skips(baseline: str) -> "list[str]":
    try:
        from ladder_gate import range_skips
    except ImportError:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        from ladder_gate import range_skips  # type: ignore[no-redef]
    return range_skips(REPO_ROOT, baseline)


@contextlib.contextmanager
def _verdict_lock():
    VERDICT_DIR.mkdir(parents=True, exist_ok=True)
    lf = open(VERDICT_DIR / ".lock", "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


def _write_deploy_verdict(head: str, baseline: "str | None", diff_sha: str,
                          ladder_st: str, empirical_st: str, codex_st: str,
                          reviewers: "list[dict] | None" = None,
                          supplemental_findings: "list[dict] | None" = None) -> int:
    """Ф2: машиночитаемый вердикт для inframon. Delete-then-write под локом (R1-F2/R2-F2).
    0 = ок/best-effort-warning; 2 = блок (unlink упал, старый вердикт остался бы маскировать)."""
    path = VERDICT_DIR / f"{head}.json"
    import time as _time
    payload = {
        "schema": 2, "run_id": f"{int(_time.time() * 1000)}-{os.getpid()}",
        "head_sha": head, "baseline_sha": baseline or "", "diff_sha256": diff_sha,
        "gates": {"ladder": ladder_st, "empirical": empirical_st, "codex": codex_st},
        "providers": reviewers if reviewers is not None else [],   # Ф1: кто судил (схема 2)
        "supplemental_findings": supplemental_findings or [],
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with _verdict_lock():
        try:
            path.unlink(missing_ok=True)          # delete-then-write: старый НЕ должен пережить
        except OSError:
            # V5b (code-R1): ЛЮБОЙ OSError = блок — exists() не проверяем (при stat-сбое он
            # врёт False, а старый вердикт мог остаться и маскировать скипы); отсутствие файла
            # уже покрыто missing_ok=True, до except не доходит.
            print("[codex-gate] ✗ не удалить старый вердикт — он маскировал бы текущие "
                  "скипы. Деплой остановлен.", file=sys.stderr)
            return 2
        try:
            _atomic_write_json(path, payload, indent=2)
        except OSError as e:                      # V5: файла нет → consumer fail-closed честен
            print(f"[codex-gate] ⚠️ вердикт НЕ записан ({type(e).__name__}: {e}) — inframon "
                  "увидит отсутствие вердикта (его fail-closed). Деплой продолжается.",
                  file=sys.stderr)
    return 0


def _empirical_gate(baseline: str, head: str) -> int:
    """Эмпирический гейт (0=дальше, 2=блок). head=head_before для привязки к SHA (R2-F2).
    baseline валидирован выше по потоку (check_reviewed_cli, R3-F2)."""
    state, cmd, timeout = _empirical_config(REPO_ROOT, head)
    if state == "unreadable":
        print("[codex-gate] ✗ empirical: .codex-gate.yaml на HEAD нечитаем (git-сбой/битый YAML/"
              "нет PyYAML при наличии файла) — состояние гейта не подтвердить. Деплой остановлен "
              "(осознанный обход — EMPIRICAL_SKIP=1, аудируется).", file=sys.stderr)
        return 2
    if state == "absent":
        base_state, _, _ = _empirical_config(REPO_ROOT, baseline)
        if base_state == "absent":
            print("[codex-gate] ⚠️ empirical: тест-команда не задана (empirical.test_command) — "
                  "гейт ПРОПУЩЕН (opt-in; задай команду в .codex-gate.yaml для проверки тестов).",
                  file=sys.stderr)
            return 0
        print(f"[codex-gate] ✗ empirical: гейт был включён в baseline ({base_state}), на HEAD "
              "отсутствует/сломан — снятие гейта требует EMPIRICAL_SKIP=1 (аудит, как и скип). "
              "Деплой остановлен.", file=sys.stderr)
        return 2
    # state == enabled: смена test_command с baseline = потенциальное ослабление (Codex code-R1,
    # ML-E2). Силу двух произвольных команд не сравнить (pytest → true эффективно снимает гейт),
    # потому блокируем ЛЮБУЮ смену без аудируемого EMPIRICAL_SKIP — самодостаточно, без опоры на
    # «увидит Codex» (связка CODEX_REVIEW_SKIP+подмена его обходит). base=absent/unreadable →
    # это ВКЛючение/подтверждение гейта (не ослабление) → команда просто бежит.
    base_state, base_cmd, _ = _empirical_config(REPO_ROOT, baseline)
    if base_state == "enabled" and base_cmd != cmd:
        print(f"[codex-gate] ✗ empirical: test_command изменилась с baseline "
              f"(« {redact_secrets(base_cmd)[:40]} » → « {redact_secrets(cmd)[:40]} ») — "
              f"смена = потенциальное ослабление, требует EMPIRICAL_SKIP=1 "
              "(аудит, как снятие гейта). Деплой остановлен.", file=sys.stderr)
        return 2
    print(f"[codex-gate] empirical: прогон «{redact_secrets(cmd)[:80]}» "
          f"(timeout {timeout}s)…", file=sys.stderr)   # источник #6
    result, tail = _run_empirical(cmd, timeout, REPO_ROOT)
    if result != "pass":
        print(f"[codex-gate] ✗ empirical: тест-команда → {result} — деплой остановлен "
              "(тесты должны быть зелёными). Хвост вывода:", file=sys.stderr)
        if tail:
            print(tail, file=sys.stderr)
        return 2
    if git_head() != head or not working_tree_clean():   # R2-F2: тест для задеплоенного состояния
        print("[codex-gate] ✗ empirical: HEAD/дерево изменились за время прогона — тест был не "
              "для задеплоенного состояния. Деплой остановлен, перезапусти.", file=sys.stderr)
        return 2
    print("[codex-gate] ✓ empirical: тест-команда зелёная", file=sys.stderr)
    return 0


# ═══════ Ф1/Ф3/G4: reviewer adapters + portable resolver ═══════
# REVIEW_PROVIDER отсутствует → portable. Legacy codex|cursor|both доступен только явно.
# Неизвестное/пустое значение → БЛОК (тихий фолбэк скрыл бы опечатку и создал ложное
# впечатление «ревьюил тот, кого просили»). Адаптер cursor — В КОДЕ, а не в шаблоне:
# allow-list модели, --trust, --mode ask и таймаут обязаны быть неотключаемыми.
_LEGACY_PROVIDERS = ("codex", "cursor", "both")
_PORTABLE_PROFILES = ("portable", "strong", "gemini")
_PROVIDERS = _LEGACY_PROVIDERS + _PORTABLE_PROFILES
_CURSOR_MODEL = "cursor-grok-4.5-high"     # другое СЕМЕЙСТВО, чем codex (gpt-5.6-sol) → разнообразие
_CURSOR_TIMEOUT_S = 600
_CURSOR_DIFF_LIMIT = 300_000
_GEMINI_MODEL = "gemini-2.5-pro"
_GEMINI_TIMEOUT_S = 600
_GEMINI_DIFF_LIMIT = 600_000
_CODEX_DIFF_LIMIT = 600_000
_GEMINI_THINKING_BUDGET = 16_384
_GEMINI_MAX_OUTPUT_TOKENS = 65_536
_GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_CLAUDE_REQUESTED_MODEL = "opus"
_CLAUDE_TIMEOUT_S = 900
_CERTIFICATION_REGISTRY = Path(__file__).resolve().parent.parent / "reviewer_certifications.json"
_CORPUS_PATH = Path(__file__).resolve().parent.parent / "reviewer_corpus" / "cases.json"
_REPORTS_DIR = Path(__file__).resolve().parent.parent / "reviewer_corpus" / "reports"
_REQUIRED_CORPUS_CATEGORIES = frozenset({
    "fail-open", "config-weakening", "command-security", "reviewer-independence", "benign",
    "secret-handling", "outage", "schema-drift", "large-multifile", "prompt-injection",
})
_MIN_CERT_REPETITIONS = 2
# Разрешены ТОЛЬКО не-Anthropic модели: Claude пишет код в этом контуре, Claude-ревьюер молча
# превращает независимый гейт в самопроверку. `auto` запрещён — скрытая маршрутизация.
_CURSOR_MODEL_ALLOW = frozenset({
    "cursor-grok-4.5-high", "cursor-grok-4.5-high-fast", "cursor-grok-4.5-low",
    "gpt-5.3-codex", "gpt-5.3-codex-high", "gpt-5.3-codex-high-fast", "gpt-5.3-codex-xhigh",
    "gpt-5.2", "gpt-5.4-high", "gpt-5.4-high-fast", "gpt-5.5-high", "gpt-5.5-high-fast",
    "gpt-5.6-sol-high", "gpt-5.6-sol-high-fast", "gpt-5.6-sol-xhigh",
})
_VERDICT_LINE_RE = re.compile(r"^\s*Verdict:\s*(?:approve|needs-attention)\s*$",
                              re.IGNORECASE | re.MULTILINE)


def model_family(model: str) -> str:
    low = (model or "").strip().casefold()
    if low.startswith("claude-") or low == "opus":
        return "anthropic"
    if (low.startswith(("gpt-", "codex-", "o1", "o3"))
            or low in {"codex", "openai"}):
        return "openai"
    if low.startswith(("grok-", "cursor-grok-")):
        return "xai"
    if low.startswith("gemini-"):
        return "google"
    return "unknown"


def _corpus_expectations() -> "tuple[str, frozenset, dict] | None":
    """(sha256 корпуса, множество обязательных case id, {case_id: category}) либо None."""
    try:
        raw_bytes = _CORPUS_PATH.read_bytes()
        data = json.loads(raw_bytes)
    except (OSError, json.JSONDecodeError, UnicodeError):
        return None
    cases = data.get("cases") if isinstance(data, dict) else None
    if not isinstance(cases, list) or not cases:
        return None
    by_id: dict[str, str] = {}
    for case in cases:
        if (not isinstance(case, dict) or not _nonempty_str(case.get("id"))
                or not _nonempty_str(case.get("category"))):
            return None
        by_id[str(case["id"])] = str(case["category"])
    if not _REQUIRED_CORPUS_CATEGORIES <= set(by_id.values()):
        return None
    return (hashlib.sha256(raw_bytes).hexdigest(), frozenset(by_id), by_id)


def _report_binding_ok(item: dict, policy_id: str) -> bool:
    """§7: certified blocking-запись обязана указывать на КОММИТНУТЫЙ отчёт корпусного прогона.

    Без этой проверки правка одного поля `status` в реестре изготавливала бы сертификацию —
    ровно обход, который не закрывала ред. 2 (money-case M12). Проверка НЕ доказывает
    доверенное исполнение: подделка отчёта вместе с реестром в одном коммите остаётся
    остатком R-CERT-PROVENANCE (Фаза 2), прикрытым тем, что реестр — гейтируемый код-путь.
    """
    report = item.get("report")
    if not isinstance(report, dict):
        return False
    rel = report.get("path")
    if not _nonempty_str(rel):
        return False
    # путь строго внутри поставляемого каталога: ни traversal, ни абсолютный, ни симлинк
    if Path(str(rel)).is_absolute() or ".." in Path(str(rel)).parts:
        return False
    try:
        resolved = (_REPORTS_DIR.parent / str(rel)).resolve(strict=True)
        if not resolved.is_file() or resolved.parent != _REPORTS_DIR.resolve():
            return False
        raw_bytes = resolved.read_bytes()
    except (OSError, RuntimeError):
        return False
    if hashlib.sha256(raw_bytes).hexdigest() != str(report.get("sha256") or ""):
        return False
    corpus = _corpus_expectations()
    if corpus is None:
        return False
    corpus_sha, required_cases, _by_id = corpus
    if str(report.get("corpus_sha256") or "") != corpus_sha:
        return False   # корпус изменился → прошлый прогон больше не про него
    reps = report.get("repetitions")
    if (not isinstance(reps, int) or isinstance(reps, bool) or reps < _MIN_CERT_REPETITIONS
            or reps != report.get("repetitions")):
        return False
    try:
        body = json.loads(raw_bytes)
    except (json.JSONDecodeError, UnicodeError):
        return False
    if not isinstance(body, dict) or body.get("pass") is not True:
        return False
    model = item["actual_models"][0]
    for key, expected in (("policy_id", policy_id), ("provider", item["provider"]),
                          ("adapter", item["adapter"]), ("role", "blocking"),
                          ("requested_model", item["requested_model"]),
                          ("certification_id", item["certification_id"]),
                          ("family", item["family"]), ("attestation", item["attestation"])):
        if body.get(key) != expected:
            return False
    if (body.get("actual_models") != [model] or body.get("repetitions") != reps
            or body.get("corpus_sha256") != corpus_sha):
        return False   # digest сверяется и в реестре, и ВНУТРИ отчёта
    rows = body.get("results")
    if not isinstance(rows, list) or not rows:
        return False
    seen: set[tuple] = set()
    for row in rows:
        if not isinstance(row, dict) or row.get("pass") is not True:
            return False     # ни одной проваленной строки
        # §7/ред. 5: КАЖДАЯ строка снята той же единственной моделью. Иначе модель A проходит
        # почти весь корпус, модель B — одну лёгкую строку, агрегат сходится, а в проде B
        # принимается на money-loss кейсах, которых она не проходила.
        if row.get("actual_model") != model:
            return False
        case, rep = row.get("case"), row.get("repetition")
        if not isinstance(rep, int) or isinstance(rep, bool) or not (1 <= rep <= reps):
            return False
        seen.add((case, rep))
    return seen == {(c, r) for c in required_cases for r in range(1, reps + 1)}


def load_reviewer_certifications() -> "tuple[str | None, tuple[ReviewerCertification, ...]]":
    """Shipped plugin registry only; repo config/env cannot add certified models."""
    try:
        raw = json.loads(_CERTIFICATION_REGISTRY.read_text())
    except (OSError, json.JSONDecodeError, UnicodeError):
        return (None, ())
    # schema 2 (§6/§7): добавлены `attestation` и связка `report`. Схема 1 больше не читается —
    # fail-closed громче, чем тихая деградация до реестра без доказательств сертификации.
    if not isinstance(raw, dict) or raw.get("schema") != 2 or not _nonempty_str(
            raw.get("policy_id")):
        return (None, ())
    items = raw.get("certifications")
    if not isinstance(items, list):
        return (None, ())
    parsed: list[ReviewerCertification] = []
    for item in items:
        if not isinstance(item, dict):
            return (None, ())
        scalar = ("provider", "adapter", "requested_model", "family",
                  "certification_id", "status", "attestation")
        if not all(_nonempty_str(item.get(k)) for k in scalar):
            return (None, ())
        if item["attestation"] not in {"declared", "verified"}:
            return (None, ())
        # certification_id участвует в построении пути отчёта (раннер `--write-report`),
        # поэтому формат узкий: разделители пути и `..` не должны туда попадать вовсе.
        if not re.fullmatch(r"[A-Za-z0-9._-]+", str(item["certification_id"])):
            return (None, ())
        actual = item.get("actual_models")
        roles = item.get("roles")
        if (not isinstance(actual, list) or len(actual) != 1
                or not all(_nonempty_str(v) for v in actual)
                or not isinstance(roles, list) or len(roles) != 1
                or not all(v in {"blocking", "supplemental"} for v in roles)):
            return (None, ())
        family = str(item["family"]).casefold()
        if family not in {"anthropic", "openai", "xai", "google", "local"}:
            return (None, ())
        if item["status"] not in {"candidate", "certified"}:
            return (None, ())
        if any(model_family(str(v)) != family for v in actual):
            return (None, ())
        # §7: certified blocking-слот выдаётся только против коммитнутого корпусного отчёта.
        # `candidate` слота не даёт, поэтому отчёта не требует.
        if item["status"] == "certified" and "blocking" in roles and not _report_binding_ok(
                item, str(raw["policy_id"])):
            return (None, ())
        parsed.append(ReviewerCertification(
            provider=str(item["provider"]),
            adapter=str(item["adapter"]),
            requested_model=str(item["requested_model"]),
            actual_models=tuple(str(v) for v in actual),
            family=family,
            roles=tuple(str(v) for v in roles),
            certification_id=str(item["certification_id"]),
            status=str(item["status"]),
            attestation=str(item["attestation"]),
        ))
    return (str(raw["policy_id"]), tuple(parsed))


def reviewer_certification(provider: str, requested_model: str, role: str,
                           *, allow_candidate: bool = False) -> "ReviewerCertification | None":
    _policy, certs = load_reviewer_certifications()
    for cert in certs:
        if (cert.provider == provider and cert.requested_model == requested_model
                and role in cert.roles
                and (cert.status == "certified" or (allow_candidate and cert.status == "candidate"))):
            return cert
    return None


def codex_model(*, allow_env_override: bool = True) -> str:
    """Фактическая модель Codex-ревью — из `~/.codex/config.toml` (companion не принимает
    --model для review, берётся дефолт CLI). F3: раньше в кэш/вердикт писалось «codex», и смена
    модели не инвалидировала кэш, а вердикт не подтверждал, КТО судил. Нечитаемо → 'unknown'."""
    # companion уважает CODEX_HOME — читать надо ЭФФЕКТИВНЫЙ конфиг, иначе ревью шло бы под
    # одной моделью, а кэш/вердикт записывали другую (ревью R5)
    cfg = ((Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex")) / "config.toml")
           if allow_env_override else (_trusted_home() / ".codex" / "config.toml"))
    try:
        for line in cfg.read_text().splitlines():
            t = line.strip()
            if t.startswith("model") and "=" in t and not t.startswith("model_"):
                return t.split("=", 1)[1].strip().strip('"\'') or "unknown"
    except OSError:
        pass
    return "unknown"


def resolve_providers() -> "tuple[tuple[str, ...] | None, str]":
    """Legacy resolver. Portable profiles are resolved only by the certified-plan path."""
    raw = os.environ.get("REVIEW_PROVIDER")
    if raw is None:
        return (None, "[codex-gate] ✗ REVIEW_PROVIDER не задан: universal default `portable` "
                      "должен резолвиться через certified reviewer plan")
    v = raw.strip().lower()
    if v not in _PROVIDERS:
        return (None, f"[codex-gate] \u2717 REVIEW_PROVIDER={raw!r} неизвестен (ожидается "
                      f"{'|'.join(_PROVIDERS)}) — деплой остановлен, тихого фолбэка на codex НЕТ.")
    if v in _PORTABLE_PROFILES:
        return (None, f"[codex-gate] ✗ профиль {v!r} нельзя исполнять через legacy resolver")
    return ((("codex", "cursor") if v == "both" else (v,)), "")


def resolve_cursor_model() -> "tuple[str | None, str]":
    """(модель, ошибка). Оверрайд только через CURSOR_REVIEW_MODEL и только из allow-list."""
    m = (os.environ.get("CURSOR_REVIEW_MODEL") or _CURSOR_MODEL).strip()
    low = m.lower()
    if "claude" in low or low == "auto" or m not in _CURSOR_MODEL_ALLOW:
        return (None, f"[codex-gate] \u2717 модель ревьюера {m!r} недопустима: ревьюер обязан быть "
                      "НЕ-Anthropic (Claude пишет код — Claude-ревьюер это самопроверка), "
                      "`auto` запрещён (скрытая маршрутизация).")
    return (m, "")


# Явный отказ ревьюера в narration ДО вердикта (ревью R4). Полностью запретить префикс нельзя —
# cursor всегда предваряет ответ narration своих tool-call, ради чего нормализация и существует.
# Но «не смог посмотреть дифф» + «Verdict: approve» — машинно-детектируемое противоречие, в
# отличие от просто неверного вердикта. Формулировки узкие: «could not find issues» (легитимный
# чистый результат) НЕ должен ловиться — поэтому объект отказа обязателен.
_REFUSAL_RE = re.compile(
    r"(?i)\b(?:could\s*n[o']?t|cannot|can't|unable\s+to|failed\s+to|was\s*n[o']?t\s+able\s+to)"
    r"\s+(?:\w+\s+){0,3}?(?:inspect|read|access|fetch|retrieve|open|see|review|analyz|examin|"
    r"obtain|load)\w*\s+(?:the\s+)?(?:diff|change|patch|code|file|repo|content|source)")


def normalize_reviewer_text(text: str) -> "str | None":
    """Нормализация ответа cursor: он склеивает narration своих tool-call с ответом БЕЗ перевода
    строки, поэтому строгий `^Verdict:` не матчится (живой прогон 25.07). Правило — РОВНО ОДНО
    вхождение `Verdict: approve|needs-attention`: нет → None; больше одного → None (ambiguous:
    «берём последнее» спуфилось цитатой `Verdict: approve` ниже реальной находки, R2-F2)."""
    if not text:
        return None
    prepared = re.sub(r"(?<!\n)(Verdict:\s*(?:approve|needs-attention))", r"\n\1", text,
                      flags=re.IGNORECASE)
    if len(_VERDICT_LINE_RE.findall(prepared)) != 1:
        return None
    start = _VERDICT_LINE_RE.search(prepared).start()
    if _REFUSAL_RE.search(prepared[:start]):   # R4: отказ в narration ≠ одобрение
        return None
    block = prepared[start:]
    # F1 (ревью реализации): недостаточно «одного Verdict:» — суффикс должен ЦЕЛИКОМ
    # соответствовать контракту. Иначе narration с примером в блоке кода
    # (```\nVerdict: approve\nNo material findings.\n```  «я не смог посмотреть дифф»)
    # давала бы чистый approve, т.е. ОТКАЗ ревьюера засчитывался бы как одобрение.
    for line in block.splitlines():
        t = line.strip()
        # ТОЧНОЕ совпадение строки, не «содержит» (ревью R2): `No material findings. I could
        # not inspect the diff.` проходило по search() и давало валидный чистый вердикт
        if not t or _VERDICT_LINE_RE.match(line) or re.fullmatch(
                r"no material findings\.?", t, re.IGNORECASE):
            continue
        if re.match(r"^-\s*\[[^\]]+\]", t):       # строка находки
            continue
        # ТОЛЬКО литеральный «Findings:». Поблажка для markdown-заголовков (`#+\s`) была дырой
        # (ревью R3): под видом заголовка проходил любой текст, включая отказ ревьюера
        # «# I could not inspect the diff» — и вердикт оставался валидным чистым approve.
        if re.fullmatch(r"findings:", t, re.IGNORECASE):
            continue
        return None                                  # любая прочая строка (проза, ```) → отказ
    return block


def _build_reviewer_prompt(diff_text: str, *, role: str = "blocking") -> str:
    agents = ""
    if REPO_ROOT is not None and (REPO_ROOT / "AGENTS.md").exists():
        try:
            agents = (REPO_ROOT / "AGENTS.md").read_text()[:20000]
        except OSError:
            agents = ""
    role_text = (
        "You are the independent non-Anthropic blocking adversarial reviewer."
        if role == "blocking"
        else "You are the mandatory Claude supplemental advisory reviewer. You are not the "
             "blocking reviewer and cannot replace or overrule the independent non-Anthropic run."
    )
    return (
        f"{role_text} Review the diff below.\n"
        "Reply in EXACTLY this format and NOTHING else — no preamble, no markdown fences, and "
        "do NOT write the word 'Verdict:' anywhere except the very first line:\n"
        "first line 'Verdict: approve' or 'Verdict: needs-attention'; then a blank line; then "
        "either 'No material findings.' or a bullet list where each line is "
        "'- [severity] title (file:line)' with severity one of critical/high/medium/low.\n"
        + (f"\nProject review constitution (AGENTS.md):\n{agents}\n" if agents else "")
        + _REVIEW_FOCUS + _adjudication_prompt_block()
        + f"\n\nDiff to review:\n```\n{diff_text}\n```\n")


def _build_cursor_prompt(diff_text: str) -> str:
    return _build_reviewer_prompt(diff_text, role="blocking")


def _resolve_cursor_bin() -> "str | None":
    """Абсолютный путь к cursor-agent (F2: голое имя отдаёт выбор бинаря PATH — шим мог бы
    игнорировать --mode ask/--model и вернуть чистый вердикт под видом пиньованного ревьюера).
    Сначала известные места установки, затем PATH; фактический путь пишется в аудит."""
    import shutil
    for cand in (Path.home() / ".local" / "bin" / "cursor-agent",
                 Path("/opt/homebrew/bin/cursor-agent"), Path("/usr/local/bin/cursor-agent")):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return shutil.which("cursor-agent")


def run_cursor_review(base: str, head: str) -> "tuple[str | None, str]":
    """(текст в нашем контракте | None, диагностика). None = отказ → fail-closed у вызывающего."""
    model, err = resolve_cursor_model()
    if model is None:
        return (None, err)
    try:
        diff_text = subprocess.run(["git", "diff", f"{base}..{head}"], cwd=REPO_ROOT,
                                   capture_output=True, text=True, check=True).stdout
    except (subprocess.CalledProcessError, OSError) as e:
        return (None, f"не получить дифф: {type(e).__name__}")
    if len(diff_text) > _CURSOR_DIFF_LIMIT:
        return (None, f"дифф {len(diff_text)} символов > лимита {_CURSOR_DIFF_LIMIT} — сузь "
                      "диапазон или REVIEW_PROVIDER=codex (усечённое ревью выглядело бы полным)")
    binary = _resolve_cursor_bin()
    if binary is None:
        return (None, "cursor-agent не найден (установлен ли Cursor CLI?)")
    cmd = [binary, "-p", "--output-format", "json", "--mode", "ask", "--trust",
           "--model", model, _build_cursor_prompt(diff_text)]
    try:
        r = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True,
                           timeout=_CURSOR_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return (None, f"таймаут {_CURSOR_TIMEOUT_S}s")
    except OSError as e:
        return (None, redact_secrets(f"{type(e).__name__}: {e}"))
    if r.returncode != 0:
        return (None, redact_secrets((r.stderr or r.stdout or "").strip())[:300])
    try:
        env = json.loads((r.stdout or "").strip())
    except (json.JSONDecodeError, ValueError):
        return (None, "вывод cursor-agent не JSON")
    usage = env.get("usage") or {}
    audit(f"cursor-review bin={binary} model={model} in={usage.get('inputTokens')} "
          f"out={usage.get('outputTokens')}")           # наблюдаемость затрат и фактического бинаря
    normalized = normalize_reviewer_text(env.get("result") or "")
    if normalized is None:
        return (None, "ответ не в контракте (нет ровно одного 'Verdict:') — narration или "
                      "цитата-пример; ревью не засчитано")
    return (normalized, f"model={model}")


def _gemini_api_key() -> "str | None":
    # Официальный приоритет Google SDK: GOOGLE_API_KEY над GEMINI_API_KEY. Значение никогда
    # не возвращается в diagnostics/audit и не помещается в URL/argv.
    return os.environ.get("GOOGLE_API_KEY") or os.environ.get("GEMINI_API_KEY")


def _redact_known_secret(text: str, secret: "str | None") -> str:
    """Pattern redaction is defense-in-depth; a credential we already hold is exact-match."""
    without_known = text.replace(secret, _REDACTED) if secret else text
    return redact_secrets(without_known)


def resolve_gemini_model(*, allow_candidate: bool = False) -> "tuple[str | None, str]":
    model = (os.environ.get("GEMINI_REVIEW_MODEL") or _GEMINI_MODEL).strip()
    cert = reviewer_certification("gemini", model, "blocking",
                                  allow_candidate=allow_candidate)
    if cert is None:
        return (None, f"[codex-gate] ✗ Gemini model {model!r} отсутствует в shipped "
                      "certification registry для blocking-роли")
    if cert.family == "unknown":     # фиксированный author set снят (§3 дизайна 2026-08-07):
        return (None, f"[codex-gate] ✗ Gemini model {model!r} имеет unknown family")
    return (model, "")


def _cert_cache_record(cert: ReviewerCertification, role: str) -> dict:
    policy_id, _certs = load_reviewer_certifications()
    return {
        "role": role,
        "provider": cert.provider,
        "requested_model": cert.requested_model,
        "model": cert.actual_models[0] if len(cert.actual_models) == 1
        else cert.requested_model,
        "actual_models": list(cert.actual_models),
        "family": cert.family,
        "certification_id": cert.certification_id,
        "policy_id": policy_id or "",
        "attestation": cert.attestation,
    }


def resolve_portable_review_plan(profile: str
                                 ) -> "tuple[tuple[ReviewerCertification, ...] | None, str]":
    """Blocking certs + final Claude supplemental cert. No repo config/provider auto-routing."""
    if profile not in {"portable", "strong", "gemini"}:
        return (None, f"неизвестный portable profile {profile!r}")
    if profile == "strong":
        return (None, "[codex-gate] ✗ профиль `strong` ещё не реализован: нужен второй "
                      "аттестующий actual model independent adapter (direct xAI/local)")
    # §3: обязательная ПАРА строится безусловно, ДО чтения любого окружения. Обе возможные
    # авторские семьи представлены, поэтому каждый кусок диффа судит как минимум один
    # независимый ревьюер — при любом и недоказуемом авторстве. Определения хоста нет нигде:
    # хост сессии не доказывает авторство диапазона коммитов (возражение ревью ред. 1).
    codex = reviewer_certification("codex", codex_model(), "blocking")
    claude = reviewer_certification("claude", _CLAUDE_REQUESTED_MODEL, "blocking")
    if codex is None or claude is None:
        missing = ", ".join(n for n, c in (("codex", codex), ("claude", claude)) if c is None)
        return (None, f"[codex-gate] ✗ обязательная blocking-пара неполна: нет certified "
                      f"записи для {missing}. Панель не понижается до одного ревьюера — это "
                      "было бы саморевью на коде того же семейства. Прогони certification "
                      "suite (`certify_reviewers.py --provider <name>`) и закоммить отчёт.")
    panel = [codex, claude]
    # §4: REVIEW_PROVIDER больше НЕ выбирает панель — только ДОБАВЛЯЕТ сертифицированного
    # ревьюера. Раньше агент, запускающий деплой, отключал обязательность пары той же
    # переменной, которую сам и выставляет (возражение ревью ред. 2).
    if profile == "gemini":
        gemini_model = (os.environ.get("GEMINI_REVIEW_MODEL") or _GEMINI_MODEL).strip()
        gemini = reviewer_certification("gemini", gemini_model, "blocking")
        if gemini is None or not _gemini_api_key():
            hint = ("" if gemini is not None else
                    " Gemini ещё не сертифицирован: прогони certification suite.")
            return (None, "[codex-gate] ✗ профиль `gemini` просит ДОБАВИТЬ Gemini к обязательной "
                          f"паре, но он недоступен: нужен GOOGLE_API_KEY/GEMINI_API_KEY.{hint}")
        panel.append(gemini)
    families = [cert.family for cert in panel]
    if len(set(families)) != len(families) or "unknown" in families:
        return (None, "[codex-gate] ✗ blocking-панель содержит повтор семейства или unknown")
    return (tuple(panel), "")


def _gemini_response_text(payload: dict) -> "tuple[str | None, str]":
    candidates = payload.get("candidates")
    if not isinstance(candidates, list):
        feedback = payload.get("promptFeedback")
        reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
        suffix = f": blockReason={reason}" if isinstance(reason, str) and reason else ""
        return (None, "Gemini response без candidates" + suffix)
    if len(candidates) != 1:
        feedback = payload.get("promptFeedback")
        reason = feedback.get("blockReason") if isinstance(feedback, dict) else None
        if len(candidates) == 0 and isinstance(reason, str) and reason:
            return (None, f"Gemini response candidates=0: blockReason={reason}")
        return (None, f"Gemini response содержит candidates={len(candidates)}, ожидался один")
    candidate = candidates[0]
    if not isinstance(candidate, dict):
        return (None, "Gemini candidate изменил схему")
    finish_reason = candidate.get("finishReason")
    if finish_reason not in (None, "STOP"):
        return (None, f"Gemini finishReason={redact_secrets(str(finish_reason))[:80]}")
    content = candidate.get("content")
    parts = content.get("parts") if isinstance(content, dict) else None
    if not isinstance(parts, list) or not parts:
        return (None, "Gemini candidate не содержит непустой parts")
    texts = [p.get("text") for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]
    if not texts or len(texts) != len(parts):
        return (None, "Gemini parts содержат неполный/нетекстовый ответ")
    return ("".join(texts), "")


def run_gemini_review_text(diff_text: str, *, allow_candidate: bool = False
                           ) -> "tuple[str | None, str, str, dict]":
    """Direct Gemini HTTPS adapter. Возвращает normalized text, actual model, detail, usage."""
    model, err = resolve_gemini_model(allow_candidate=allow_candidate)
    if model is None:
        return (None, "", err, {})
    cert = reviewer_certification("gemini", model, "blocking",
                                  allow_candidate=allow_candidate)
    key = _gemini_api_key()
    if not key:
        return (None, "", "нет GOOGLE_API_KEY/GEMINI_API_KEY", {})
    if len(diff_text) > _GEMINI_DIFF_LIMIT:
        return (None, "", f"дифф {len(diff_text)} символов > лимита {_GEMINI_DIFF_LIMIT}", {})
    body = json.dumps({
        "contents": [{"role": "user", "parts": [
            {"text": _build_reviewer_prompt(diff_text, role="blocking")},
        ]}],
        "generationConfig": {
            "temperature": 0.1,
            # Gemini 2.5 Pro cannot disable thinking. Bound it explicitly and leave enough of
            # the model's 65k output window for the strict verdict after thought tokens.
            "thinkingConfig": {"thinkingBudget": _GEMINI_THINKING_BUDGET},
            "maxOutputTokens": _GEMINI_MAX_OUTPUT_TOKENS,
            "responseMimeType": "text/plain",
        },
    }).encode()
    url = _GEMINI_ENDPOINT.format(model=model)
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json", "x-goog-api-key": key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_GEMINI_TIMEOUT_S) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode(errors="replace")
        except OSError:
            detail = str(exc.reason)
        return (None, "", _redact_known_secret(detail, key)[:300], {})
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        detail = _redact_known_secret(f"{type(exc).__name__}: {exc}", key)
        return (None, "", detail[:300], {})
    try:
        envelope = json.loads(raw)
    except (json.JSONDecodeError, UnicodeError, TypeError):
        return (None, "", "Gemini response не JSON", {})
    actual = envelope.get("modelVersion")
    if not isinstance(actual, str) or cert is None or actual not in cert.actual_models:
        return (None, str(actual or ""), "Gemini modelVersion не совпал с certification registry",
                {})
    text, envelope_detail = _gemini_response_text(envelope)
    if text is None:
        return (None, actual, envelope_detail, {})
    normalized = normalize_reviewer_text(text or "")
    if normalized is None:
        return (None, actual, "Gemini ответ не прошёл строгий verdict-контракт", {})
    usage = envelope.get("usageMetadata")
    return (normalized, actual, "", usage if isinstance(usage, dict) else {})


def run_gemini_review(base: str, head: str) -> "ReviewerRun":
    requested = (os.environ.get("GEMINI_REVIEW_MODEL") or _GEMINI_MODEL).strip()
    cert = reviewer_certification("gemini", requested, "blocking")
    if cert is None:
        return ReviewerRun("blocking", "gemini", requested, (), "google", "",
                           "unavailable", detail="нет certified Gemini model")
    try:
        diff_text = subprocess.run(
            ["git", "diff", f"{base}..{head}"], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        return ReviewerRun("blocking", "gemini", requested, (), cert.family,
                           cert.certification_id, "invalid",
                           detail=f"не получить дифф: {type(exc).__name__}")
    text, actual, detail, usage = run_gemini_review_text(diff_text)
    if text is None:
        return ReviewerRun("blocking", "gemini", requested,
                           (actual,) if actual else (), cert.family, cert.certification_id,
                           "invalid", detail=detail, usage=usage)
    verdict = parse_review_output(text)
    return ReviewerRun("blocking", "gemini", requested, (actual,), cert.family,
                       cert.certification_id, "ok" if verdict.valid else "invalid",
                       verdict=verdict if verdict.valid else None,
                       detail="" if verdict.valid else "невалидный verdict", usage=usage)


def _resolve_claude_bin() -> "str | None":
    # Mandatory artifact must not be satisfiable by an arbitrary PATH shim. These are the
    # supported native/Homebrew/system install locations; absence is fail-closed with setup
    # guidance. The exact server-side model is independently checked in modelUsage below.
    # Claude — обязательный член пары, поэтому его бинарь резолвится так же строго, как
    # companion: $HOME вызывающего подставил бы шим из ~/.local/bin (находка F6).
    home = _trusted_home()
    fixed = (
        home / ".local" / "bin" / "claude",
        home / ".claude" / "local" / "claude",
        home / ".volta" / "bin" / "claude",
        Path("/opt/homebrew/bin/claude"),
        Path("/usr/local/bin/claude"),
    )
    # npm under nvm has no stable version component. Enumerate only the known nvm install root,
    # never arbitrary PATH; newest lexical version wins and the resolved binary is audited.
    nvm = sorted((home / ".nvm" / "versions" / "node").glob("*/bin/claude"), reverse=True)
    for cand in (*fixed, *nvm):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


def run_claude_supplemental(base: str, head: str) -> "ReviewerRun":
    cert = reviewer_certification("claude", _CLAUDE_REQUESTED_MODEL, "supplemental")
    if cert is None:
        return ReviewerRun("supplemental", "claude", _CLAUDE_REQUESTED_MODEL, (),
                           "anthropic", "", "unavailable",
                           detail="нет certified Claude supplemental model")
    binary = _resolve_claude_bin()
    if binary is None:
        return ReviewerRun("supplemental", "claude", _CLAUDE_REQUESTED_MODEL, (),
                           cert.family, cert.certification_id, "unavailable",
                           detail="claude CLI не найден")
    try:
        resolved_binary = str(Path(binary).resolve(strict=True))
    except OSError:
        return ReviewerRun("supplemental", "claude", _CLAUDE_REQUESTED_MODEL, (),
                           cert.family, cert.certification_id, "unavailable",
                           detail="claude CLI path не резолвится")
    audit(f"claude-supplemental bin={resolved_binary} requested={_CLAUDE_REQUESTED_MODEL}")
    try:
        diff_text = subprocess.run(
            ["git", "diff", f"{base}..{head}"], cwd=REPO_ROOT,
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError) as exc:
        return ReviewerRun("supplemental", "claude", _CLAUDE_REQUESTED_MODEL, (),
                           cert.family, cert.certification_id, "invalid",
                           detail=f"не получить дифф: {type(exc).__name__}")
    cmd = [
        binary, "-p", "--output-format", "json", "--tools", "Read,Glob,Grep",
        "--model", _CLAUDE_REQUESTED_MODEL, "--no-session-persistence",
    ]
    try:
        result = subprocess.run(
            cmd, cwd=REPO_ROOT, input=_build_reviewer_prompt(
                diff_text, role="supplemental"),
            capture_output=True, text=True, timeout=_CLAUDE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return ReviewerRun("supplemental", "claude", _CLAUDE_REQUESTED_MODEL, (),
                           cert.family, cert.certification_id, "timeout",
                           detail=f"таймаут {_CLAUDE_TIMEOUT_S}s")
    except OSError as exc:
        return ReviewerRun("supplemental", "claude", _CLAUDE_REQUESTED_MODEL, (),
                           cert.family, cert.certification_id, "unavailable",
                           detail=redact_secrets(f"{type(exc).__name__}: {exc}")[:300])
    if result.returncode != 0:
        return ReviewerRun("supplemental", "claude", _CLAUDE_REQUESTED_MODEL, (),
                           cert.family, cert.certification_id, "invalid",
                           detail=redact_secrets((result.stderr or result.stdout or "").strip())[:300])
    try:
        envelope = json.loads((result.stdout or "").strip())
    except (json.JSONDecodeError, ValueError):
        return ReviewerRun("supplemental", "claude", _CLAUDE_REQUESTED_MODEL, (),
                           cert.family, cert.certification_id, "invalid",
                           detail="Claude output не JSON")
    model_usage = envelope.get("modelUsage")
    actual_models = tuple(sorted(model_usage)) if isinstance(model_usage, dict) else ()
    allowed_actual_sets = {(model,) for model in cert.actual_models}
    if envelope.get("is_error") is not False or actual_models not in allowed_actual_sets:
        return ReviewerRun("supplemental", "claude", _CLAUDE_REQUESTED_MODEL, actual_models,
                           cert.family, cert.certification_id, "invalid",
                           detail="Claude actual model/error не совпал с certification registry")
    normalized = normalize_reviewer_text(envelope.get("result") or "")
    verdict = parse_review_output(normalized or "")
    if normalized is None or not verdict.valid:
        return ReviewerRun("supplemental", "claude", _CLAUDE_REQUESTED_MODEL, actual_models,
                           cert.family, cert.certification_id, "invalid",
                           detail="Claude ответ не прошёл строгий verdict-контракт")
    usage = envelope.get("usage")
    return ReviewerRun("supplemental", "claude", _CLAUDE_REQUESTED_MODEL, actual_models,
                       cert.family, cert.certification_id, "ok", verdict=verdict,
                       usage=usage if isinstance(usage, dict) else {})


def _diff_text(base: str, head: str) -> "tuple[str | None, str]":
    try:
        return (subprocess.run(["git", "diff", f"{base}..{head}"], cwd=REPO_ROOT,
                               capture_output=True, text=True, check=True).stdout, "")
    except (subprocess.CalledProcessError, OSError) as exc:
        return (None, f"не получить дифф: {type(exc).__name__}")


def run_codex_review_text(diff_text: str, *, role: str = "blocking",
                          allow_candidate: bool = False
                          ) -> "tuple[str | None, str, str, dict, str]":
    """(нормализованный текст|None, фактическая модель, диагностика, usage, статус).

    §7: Codex ревьюит ТОТ ЖЕ контракт, что Gemini и Claude — вход текстом диффа, строгий
    reviewer-промпт, `normalize_reviewer_text`. Иначе сертификация проверяла бы адаптер,
    отличный от боевого (`adversarial-review` работает по git-диапазону и своей схеме),
    и evidence ничего не доказывал бы про прод (money-case M7).

    ⚠️ Аттестации фактической модели тут НЕТ: companion эхо-ит `request.model`, а
    `codex_model()` читает локальный конфиг. Это `declared`-запись (остаток M8 в AGENTS.md).
    """
    requested = codex_model(allow_env_override=False)   # конфиг ревьюера — доверенный, не из env
    cert = reviewer_certification("codex", requested, role, allow_candidate=allow_candidate)
    if cert is None:
        return (None, requested, "нет certified Codex model для роли " + role, {}, "unavailable")
    if len(diff_text) > _CODEX_DIFF_LIMIT:
        return (None, requested, f"дифф больше {_CODEX_DIFF_LIMIT} символов: усечённое ревью "
                                 "выглядело бы полным", {}, "invalid")
    # `--json` ОБЯЗАТЕЛЕН. Без него companion печатает `execution.rendered`, который у `task`
    # пуст (`runForegroundCommand`: `outputResult(json ? payload : rendered)`), и ревью
    # выглядело бы «пустым выводом» — при сертификации так молча провалилась половина кейсов.
    # `--sandbox` companion НЕ знает: флаг уезжал в positionals и склеивался с промптом.
    # Без `--write` ход и так read-only; промпт обязан быть ЕДИНСТВЕННЫМ позиционным.
    sterile_home = _sterile_codex_home(requested)
    try:
        r = _exec_companion(["task", "--json", "--model", requested,
                             _build_reviewer_prompt(diff_text, role=role)],
                            allow_env_override=False, codex_home=sterile_home)
    finally:
        if sterile_home:
            shutil.rmtree(sterile_home, ignore_errors=True)
    if r is None:
        return (None, requested, "companion недоступен (плагин не найден/таймаут)", {}, "unavailable")
    if r.returncode != 0:
        detail = outage_details(r.stdout) or redact_secrets((r.stderr or "").strip())[:300]
        return (None, requested, f"companion exit={r.returncode}: {detail}", {}, "unavailable")
    try:
        envelope = json.loads((r.stdout or "").strip())
    except (json.JSONDecodeError, ValueError):
        return (None, requested, "Codex task envelope не JSON", {}, "invalid")
    env_status = envelope.get("status") if isinstance(envelope, dict) else None
    if (not isinstance(env_status, int) or isinstance(env_status, bool)
            or env_status != 0):
        # код 0 ≠ ревью состоялось: деградировавший конверт (квота, ошибка модели) приходит нулём
        return (None, requested,
                f"Codex task status={redact_secrets(str(envelope.get('status')))[:60]}", {},
                "unavailable")
    normalized = normalize_reviewer_text(envelope.get("rawOutput") or "")
    if normalized is None:
        return (None, requested, "Codex ответ не прошёл строгий verdict-контракт", {}, "invalid")
    return (normalized, requested, "", {}, "ok")


def run_claude_review_text(diff_text: str, *, role: str = "blocking",
                           allow_candidate: bool = False
                           ) -> "tuple[str | None, str, str, dict, str]":
    """Тот же контракт для Claude. `modelUsage` конверта — НАСТОЯЩАЯ аттестация (`verified`)."""
    cert = reviewer_certification("claude", _CLAUDE_REQUESTED_MODEL, role,
                                  allow_candidate=allow_candidate)
    if cert is None:
        return (None, "", "нет certified Claude model для роли " + role, {}, "unavailable")
    binary = _resolve_claude_bin()
    if binary is None:
        return (None, "", "claude CLI не найден", {}, "unavailable")
    try:
        resolved_binary = str(Path(binary).resolve(strict=True))
    except OSError:
        return (None, "", "claude CLI path не резолвится", {}, "unavailable")
    audit(f"claude-{role} bin={resolved_binary} requested={_CLAUDE_REQUESTED_MODEL}")
    cmd = [binary, "-p", "--output-format", "json", "--tools", "Read,Glob,Grep",
           "--model", _CLAUDE_REQUESTED_MODEL, "--no-session-persistence"]
    try:
        result = subprocess.run(cmd, cwd=REPO_ROOT,
                                input=_build_reviewer_prompt(diff_text, role=role),
                                capture_output=True, text=True, timeout=_CLAUDE_TIMEOUT_S,
                                env=_certified_subprocess_env())
    except subprocess.TimeoutExpired:
        return (None, "", f"таймаут {_CLAUDE_TIMEOUT_S}s", {}, "timeout")
    except OSError as exc:
        return (None, "", redact_secrets(f"{type(exc).__name__}: {exc}")[:300], {}, "unavailable")
    if result.returncode != 0:
        return (None, "", redact_secrets((result.stderr or result.stdout or "").strip())[:300], {},
                "invalid")
    try:
        envelope = json.loads((result.stdout or "").strip())
    except (json.JSONDecodeError, ValueError):
        return (None, "", "Claude output не JSON", {}, "invalid")
    model_usage = envelope.get("modelUsage")
    # Ключи modelUsage приходят из ответа CLI, то есть недоверенны: при отбраковке они
    # попадают в audit и operator output, и секретоподобное значение утекло бы наружу.
    actual_models = (tuple(sorted(redact_secrets(str(m)) for m in model_usage))
                     if isinstance(model_usage, dict) else ())
    actual = ",".join(actual_models)
    if envelope.get("is_error") is not False or actual_models not in {
            (m,) for m in cert.actual_models}:
        return (None, actual, "Claude actual model/error не совпал с certification registry",
                {}, "invalid")
    normalized = normalize_reviewer_text(envelope.get("result") or "")
    if normalized is None:
        return (None, actual, "Claude ответ не прошёл строгий verdict-контракт", {}, "invalid")
    usage = envelope.get("usage")
    return (normalized, actual, "", usage if isinstance(usage, dict) else {}, "ok")


def _run_text_reviewer(cert: ReviewerCertification, role: str, base: str, head: str,
                       runner) -> ReviewerRun:
    """Общая обвязка текстовых адаптеров: дифф → runner → ReviewerRun (fail-closed на каждом шаге)."""
    diff_text, derr = _diff_text(base, head)
    if diff_text is None:
        return ReviewerRun(role, cert.provider, cert.requested_model, (), cert.family,
                           cert.certification_id, "invalid", detail=derr)
    text, actual, detail, usage, status = runner(diff_text, role=role)
    actuals = tuple(a for a in (actual,) if a)
    if text is None:
        # статус приходит ОТ адаптера. Выводить его обратно из русского текста диагностики
        # значило бы, что переформулировка сообщения молча меняет класс отказа.
        return ReviewerRun(role, cert.provider, cert.requested_model, actuals, cert.family,
                           cert.certification_id, status, detail=detail)
    verdict = parse_review_output(text)
    if not verdict.valid:
        return ReviewerRun(role, cert.provider, cert.requested_model, actuals, cert.family,
                           cert.certification_id, "invalid",
                           detail="ответ не прошёл строгий verdict-контракт")
    return ReviewerRun(role, cert.provider, cert.requested_model, actuals, cert.family,
                       cert.certification_id, "ok", verdict=verdict, usage=usage)


def run_certified_reviewer(cert: ReviewerCertification, base: str, head: str) -> ReviewerRun:
    if cert.roles == ("blocking",):
        role = "blocking"
    elif cert.roles == ("supplemental",):
        role = "supplemental"
    else:
        return ReviewerRun("blocking", cert.provider, cert.requested_model, (), cert.family,
                           cert.certification_id, "invalid",
                           detail="certification должна иметь ровно одну однозначную role")
    if cert.provider == "gemini":
        return run_gemini_review(base, head)
    if cert.provider == "claude":
        return _run_text_reviewer(cert, role, base, head, run_claude_review_text)
    if cert.provider == "codex":
        return _run_text_reviewer(cert, role, base, head, run_codex_review_text)
    if cert.provider == "cursor":
        return ReviewerRun(role, cert.provider, cert.requested_model, (), cert.family,
                           cert.certification_id, "unavailable",
                           detail="Cursor CLI не аттестует actual model; только legacy adapter")
    return ReviewerRun(role, cert.provider, cert.requested_model, (), cert.family,
                       cert.certification_id, "unavailable",
                       detail=f"adapter {cert.adapter!r} не реализован")


def review_with_provider(provider: str, base: str,
                         head: str) -> "tuple[ReviewVerdict | None, str, str]":
    """(вердикт|None, модель, диагностика) для ОДНОГО провайдера."""
    if provider == "codex":
        out = run_companion_review(base=base, scope="branch")
        if out is None:
            return (None, "codex", "companion недоступен")
        v = parse_review_output(out)
        return ((v if v.valid else None), codex_model(),
                "" if v.valid else (outage_details(out) or "невалидный вывод"))
    text, info = run_cursor_review(base, head)
    model = info[len("model="):] if info.startswith("model=") else \
        (os.environ.get("CURSOR_REVIEW_MODEL") or _CURSOR_MODEL)
    if text is None:
        return (None, model, info)
    v = parse_review_output(text)
    return ((v if v.valid else None), model,
            "" if v.valid else "вывод не прошёл валидацию контракта")


def check_reviewed_cli() -> int:
    if not _require_repo():
        return 2
    warn_if_strict()
    if not working_tree_clean():                 # R1-3 — ДО всех SKIP (Codex P1: skip не
        print("[codex-gate] ✗ рабочее дерево грязное — reviewed≡deployed держится только на "  # должен пускать грязь)
              "чистом дереве; закоммить перед деплоем.", file=sys.stderr)
        return 2
    head_before = git_head()   # R2-F2: захват ОДИН раз после clean-tree; всё биндится к нему
    baseline, rc_b = _resolve_baseline_gate(head_before)   # inframon Ф1: pin/authoritative/легаси
    if rc_b:
        return 2
    ladder_skip = os.environ.get("LADDER_SKIP") == "1"
    codex_skip = skip_requested()
    empirical_skip = os.environ.get("EMPIRICAL_SKIP") == "1"
    # baseline+ancestry нужны ladder-, empirical- И Codex-частям — общая проверка ДО всех.
    # Пропускается целиком, только если ВСЕ ТРИ части скипнуты осознанно (R3-F2: иначе
    # HEAD=absent + неизвестный baseline мог бы пройти как absent/absent в эмпирике).
    if not (ladder_skip and codex_skip and empirical_skip):
        if baseline is None:                         # R1-2
            print("[codex-gate] ✗ baseline деплоя неизвестен (нет .last-deployed-sha и "
                  "CODEX_DEPLOY_BASELINE) — задай задеплоенный SHA. Деплой остановлен.", file=sys.stderr)
            return 2
        # baseline должен быть предком HEAD, иначе baseline..HEAD не покрывает реальный дельту
        # (протухший/кросс-машинный/rollback SHA) — Codex P2, fail-closed.
        anc = subprocess.run(["git", "merge-base", "--is-ancestor", baseline, "HEAD"],
                             capture_output=True, text=True)
        if anc.returncode != 0:
            print(f"[codex-gate] ✗ baseline {baseline[:12]} не предок HEAD (протухший/кросс-машинный) "
                  "— задай верный CODEX_DEPLOY_BASELINE. Деплой остановлен.", file=sys.stderr)
            return 2

    # --- LADDER часть (спека §4: /simplify → /code-review покрытие ВСЕГО baseline..HEAD) ---
    if ladder_skip:
        reason = os.environ.get("LADDER_SKIP_REASON", "")
        audit(f"LADDER_SKIP=1 — ladder-range пропущен (reason={redact_secrets(reason)!r})")
        print("[codex-gate] ⚠️ LADDER_SKIP=1 — ladder-range пропущен (см. audit).",
              file=sys.stderr)
    elif _ladder_check(baseline) != 0:
        print("[codex-gate] ✗ ladder-range не покрыт — см. вывод выше. Деплой остановлен.",
              file=sys.stderr)
        return 2

    # --- EMPIRICAL часть (тикет #1: механическая проверка ДО Codex; ladder → empirical → Codex) ---
    if empirical_skip:
        reason = os.environ.get("EMPIRICAL_SKIP_REASON", "")
        audit(f"EMPIRICAL_SKIP=1 — эмпирический гейт пропущен "
              f"(reason={redact_secrets(reason)!r})")
        print("[codex-gate] ⚠️ EMPIRICAL_SKIP=1 — эмпирический гейт пропущен (см. audit).",
              file=sys.stderr)
    elif _empirical_gate(baseline, head_before) != 0:   # тесты падают/нечитаемо/снят → блок ДО Codex
        return 2

    # Статусы для вердикта inframon (Ф2): скипы и исторические обходы диапазона — видимы
    def _verdict_statuses(codex_st: str) -> "tuple[str, str, str]":
        if ladder_skip:
            ladder_st = "skipped"
        else:
            ladder_st = "covered-with-skips" if _ladder_range_skips(baseline) else "covered"
        if empirical_skip:
            empirical_st = "skipped"
        else:
            e_state = _empirical_config(REPO_ROOT, head_before)[0]
            empirical_st = "pass" if e_state == "enabled" else "not-configured"
        return (ladder_st, empirical_st, codex_st)

    # --- Ф2: сессионное выключение ревьюера. Проверка ДО кэш-ветки и до любого раннего allow
    # (EARS-9b): иначе валидный кэш разрешил бы деплой при «выключенном» ревьюере. Исключение —
    # осознанный CODEX_REVIEW_SKIP (аварийный контур сохраняется).
    _disabled = review_disabled_reason(_env_session())
    if _disabled is not None and not codex_skip:
        _disabled_banner(_disabled)
        print("[codex-gate] ✗ ревьюер выключен в этой сессии — деплой остановлен. Два выхода: "
              "`review-enable` (вернуть ревьюера) ИЛИ аварийный CODEX_REVIEW_SKIP=1 (аудируется). "
              "Смена провайдера выключение НЕ обходит.", file=sys.stderr)
        return 2

    # --- CODEX часть (прежняя логика; теперь ПОСЛЕ ladder+empirical — CODEX_REVIEW_SKIP их не пропускает) ---
    if codex_skip:
        _record_reviewed(head_before)   # осознанный skip — задеплоенный SHA тоже фиксируем (R2-F2)
        audit("CODEX_REVIEW_SKIP=1 — деплой-ревью ПРОПУЩЕНО")
        print("[codex-gate] ⚠️ CODEX_REVIEW_SKIP=1 — деплой-ревью ПРОПУЩЕНО (см. audit). "
              "При активном инциденте актуатора: сначала kill-switch проекта, "
              "потом лечи через гейт.", file=sys.stderr)
        l_st, e_st, c_st = _verdict_statuses("skipped")
        if _write_deploy_verdict(head_before, baseline,
                                 diff_sha256(baseline, head_before) if baseline else "",
                                 l_st, e_st, c_st):
            return 2
        return 0
    head = head_before                        # R2-F2: биндим ledger/reviewed к захваченному SHA
    diff_sha = diff_sha256(baseline, head_before)
    raw_provider = os.environ.get("REVIEW_PROVIDER")
    raw_profile = "portable" if raw_provider is None else raw_provider.strip().casefold()
    portable_profile = raw_profile in _PORTABLE_PROFILES
    portable_certs: tuple[ReviewerCertification, ...] = ()
    if portable_profile:
        plan, perr = resolve_portable_review_plan(raw_profile)
        if plan is None:
            print(perr, file=sys.stderr)
            return 2
        portable_certs = plan
        requested_reviewers = [
            _cert_cache_record(cert, "supplemental" if "supplemental" in cert.roles else "blocking")
            for cert in portable_certs
        ]
        providers = tuple(cert.provider for cert in portable_certs)
    else:
        # §4: легаси-значения означали «панель МЕНЬШЕ обязательной пары», а такой панели больше
        # нет. Тихо понижать нельзя — это и был env-обход обязательности (money-case M3).
        print(f"[codex-gate] ✗ REVIEW_PROVIDER={raw_provider!r} больше не поддерживается на "
              "деплой-пути: панель Codex+Claude обязательна, и переменной окружения её понизить "
              "нельзя (это был обход независимости). Допустимы только профили, ДОБАВЛЯЮЩИЕ "
              f"ревьюера: {'|'.join(_PORTABLE_PROFILES)}. Аварийный выход — CODEX_REVIEW_SKIP=1 "
              "(громкий и аудируемый).", file=sys.stderr)
        return 2
    audit("review-providers запрошены: "                   # EARS-5: кто судил — в аудит
          + ", ".join(f"{r['provider']}({r['model']})" for r in requested_reviewers))
    if read_valid_ledger(head, diff_sha, requested_reviewers) is not None:
        # Кэш чистого ревью НЕ обходит протокол сходимости (Codex-спор F3: конкурентная
        # сессия могла записать open-находку в серию — кэш её не видел).
        with findings_lock():
            led_c = load_findings_ledger(baseline)
            if led_c is None:
                print("[codex-gate] ✗ findings-ledger повреждён. Деплой остановлен.",
                      file=sys.stderr)
                return 2
            decision_c, msg_c = convergence_decision(led_c)
            pending_adj = bool(led_c.get("needs_review_round"))
        if pending_adj:
            print("[codex-gate] адъюдикации ещё не показаны Codex — кэш пропущен, гоним "
                  "реальный раунд (переговорный контроль §5, спор F3-2)", file=sys.stderr)
        elif decision_c != "allow":
            print(msg_c, file=sys.stderr)
            return 2
        else:
            _record_reviewed(head)
            l_st, e_st, c_st = _verdict_statuses("cached")
            if _write_deploy_verdict(head, baseline, diff_sha, l_st, e_st, c_st,
                                     requested_reviewers):
                return 2
            print("[codex-gate] ✓ валидная запись ревью для HEAD — деплой разрешён")
            return 0
    # Ledger серии — ДО прогона ревью (протокол-догфуд F2: иначе адъюдикации ПРОШЛОЙ
    # серии утекали в промпт новой до архивации и подавляли реальные находки).
    with findings_lock():
        led = load_findings_ledger(baseline)
        if led is None:          # ML-C3: битый ledger = как отсутствие ревью
            print("[codex-gate] ✗ findings-ledger повреждён (logs/review_findings/current.json) "
                  "— почини/удали файл. Деплой остановлен.", file=sys.stderr)
            return 2
        save_findings_ledger(led)   # свежая/архивированная серия видна промпт-блоку
    import time as _time
    review_started_ts = _time.time()   # для ts-guard needs_review_round (спор F3-3)
    # Ф3: КАЖДЫЙ запрошенный провайдер ревьюит один и тот же дифф в ОДНОМ прогоне; второй проход
    # выполняется ДАЖЕ при blocking у первого (union за один раунд — адъюдикация разом, EARS-14b).
    results, failures, supplemental_advisory = [], [], []
    if portable_profile:
        for cert in portable_certs:
            run = run_certified_reviewer(cert, baseline, head)
            actual = ",".join(run.actual_models) or run.requested_model
            audit(f"review-run role={run.role} provider={run.provider} actual={actual} "
                  f"family={run.family} certification={run.certification_id} status={run.status}")
            if run.status != "ok" or run.verdict is None:
                failures.append((run.provider, actual, run.detail))
                print(f"[codex-gate] ✗ {run.role} ревьюер {run.provider} ({actual}) "
                      f"не дал валидный artifact: {run.detail}", file=sys.stderr)
                continue
            if run.role == "blocking":
                results.append((run.provider, actual, run.verdict))
            else:
                for sev, title in run.verdict.findings:
                    supplemental_advisory.append((sev, title, run.provider))
                    audit(f"supplemental-finding provider={run.provider} severity={sev} "
                          f"title={redact_secrets(title)!r}")
            print(f"[codex-gate] {run.role} ревьюер {run.provider} ({actual}): "
                  f"{run.verdict.verdict}, находок {len(run.verdict.findings)}", file=sys.stderr)
    # union находок с пометкой провайдера (EARS-12/15)
    blocking = [(sev, title, prov) for prov, _m, v in results for sev, title in v.findings
                if sev in SEVERITY_BLOCKING or sev not in KNOWN_SEVERITIES]
    advisory = ([(sev, title, prov) for prov, _m, v in results for sev, title in v.findings
                 if sev in KNOWN_SEVERITIES and sev not in SEVERITY_BLOCKING]
                + supplemental_advisory)
    if failures:
        # EARS-14: отказ ЛЮБОГО → блок без деградации; EARS-14c: находки успешного ВЛИВАЕМ
        # (partial: rounds не инкрементим, needs_review_round не сбрасываем)
        if blocking:
            with findings_lock():
                led_p = load_findings_ledger(baseline)
                if led_p is not None:
                    merge_round(led_p, blocking, partial=True)
                    save_findings_ledger(led_p)
            print("[codex-gate] находки успевшего ревьюера сохранены в серию (частичный раунд: "
                  "счётчик раундов НЕ увеличен)", file=sys.stderr)
        names = ", ".join(f"{p}" for p, _m, _d in failures)
        downgrade = ("Portable review не деградирует до Codex/Claude; настрой certified backend."
                     if portable_profile else
                     "Осознанное legacy-понижение: REVIEW_PROVIDER=codex (или cursor).")
        print(f"[codex-gate] ✗ не все запрошенные ревьюеры отработали ({names}) — деплой "
              f"остановлен без деградации до одного. {downgrade}\n"
              "ML6 (аварийный контур, если СЕЙЧАС идёт инцидент актуатора): СНАЧАЛА kill-switch "
              "проекта (freeze — останавливает актуатор без ревьюера); ЗАТЕМ при необходимости "
              "rollback (пока заморожено). Rollback БЕЗ freeze актуатор НЕ останавливает. "
              "Ремонт — через гейт.", file=sys.stderr)
        return 2
    if not results:
        print("[codex-gate] ✗ review plan не содержит успешного blocking reviewer — деплой "
              "остановлен (fail-closed)", file=sys.stderr)
        return 2
    verdict = results[0][2]      # для кэша достаточно любого валидного (union решает ниже)
    with findings_lock():        # догфуд F3: RE-LOAD под локом — ревью шло минуты, конкурентная
        led = load_findings_ledger(baseline)   # сессия могла изменить серию; merge поверх свежего
        if led is None:
            print("[codex-gate] ✗ findings-ledger повреждён. Деплой остановлен.", file=sys.stderr)
            return 2
        merge_round(led, blocking, review_started_ts=review_started_ts)   # ОДИН раунд (EARS-13)
        apply_carry_over(led)
        save_findings_ledger(led)
        decision, msg = convergence_decision(led)
    if decision == "allow":
        if not blocking and not supplemental_advisory:
            # Кэшируем только полностью чистый run. Иначе следующий deploy обязан заново
            # получить supplemental artifact, чтобы advisory не исчезла из audit/verdict.
            write_ledger(head, diff_sha, baseline, verdict, requested_reviewers)   # кэш
        _record_reviewed(head)
        for sev, title, prov in advisory:      # совещательные — с пометкой провайдера
            print(f"    [{sev}] ({prov}) {title}", file=sys.stderr)
        l_st, e_st, c_st = _verdict_statuses("allow")
        supplemental_records = [
            {"severity": sev, "title": title, "provider": prov}
            for sev, title, prov in supplemental_advisory
        ]
        if _write_deploy_verdict(head, baseline, diff_sha, l_st, e_st, c_st,
                                 requested_reviewers, supplemental_records):
            return 2
        print(msg)
        print("[codex-gate] ✓ деплой разрешён (протокол сходимости)")
        return 0
    print(msg, file=sys.stderr)
    return 2


# --- Маркер (session-bound, R1-6) + gate-edit + gate-bash (R1-5) + main ---
DESIGN_MARKER = (REPO_ROOT / ".claude" / ".design-approved") if REPO_ROOT else None

# R1-5: признаки мутации файла в Bash-команде (best-effort эвристика; полнота — Фаза 2).
# git apply / patch: цель в патч-файле (вне cmdline) → безусловно, НО в КОМАНДНОЙ позиции
# (начало / после ;&| / xargs) — иначе `echo patch`, `git log --grep patch` ложно блочатся (Codex).
_STRONG_MUTATION_RE = re.compile(
    r"(?:^|[;&|\n]|\bxargs\s+)\s*(?:git\s+apply|patch)\b", re.IGNORECASE | re.MULTILINE)
# sed с in-place флагом в любой позиции опций: -i / -Ei / -E -i / --in-place (Codex P2)
_SED_I_RE = re.compile(r"\bsed\b(?=[^;&|\n]*(?:--in-place|-[a-z]*i[a-z]*\b))", re.IGNORECASE)
# цель записи в файл: редирект (> или >>, НЕ fd-дупликация 2>&1/&>&) ИЛИ tee <file>.
# Гейтим, только если ЦЕЛЬ — код-путь (иначе read-only диагностика `… > /tmp/log` ложно блочилась;
# и наоборот нумерованный редирект `2> tests/x.py` в код-файл ловится).
_FILE_TARGET_RE = re.compile(
    r"""(?:>>?(?!&)|(?:^|\s)tee\s+(?:-a\s+)?)\s*['"]?([\w./~-]+)""", re.IGNORECASE)  # кавычки ок


def is_code_path(path: str) -> bool:
    p = path
    pp = Path(p)
    if pp.is_absolute():
        if REPO_ROOT is None:
            return False
        root = Path(os.path.realpath(REPO_ROOT))
        candidate = Path(os.path.realpath(pp))
        try:
            p = str(candidate.relative_to(root))
        except ValueError:
            return False   # абсолютный путь вне репозитория — не наш код-путь
    # схлопнуть ../ (docs/../app/x.py → app/x.py; Codex P2). normpath делает это сам — БЕЗ
    # предварительного .lstrip("./"): lstrip трактует аргумент как МНОЖЕСТВО символов, а не
    # префикс, поэтому ".githooks/pre-commit" терял ведущую точку и переставал матчиться
    # (регресс проекта-источника, покрыт тестом).
    p = os.path.normpath(p)
    # Жёсткие пути — ДО конфига и экземпций (ML-P1: конфиг не может вывести их из-под гейта)
    framed = "/" + p.lstrip("/")
    hard_p = p.casefold()
    hard_framed = framed.casefold()
    if (hard_p in {item.casefold() for item in HARD_CODE_PATH_EXACT}
            or any(hard_p.startswith(pre.casefold()) for pre in HARD_CODE_PATH_PREFIXES)
            or any(component.casefold() in hard_framed
                   for component in HARD_CODE_PATH_COMPONENTS)
            or hard_p == "hooks/hooks.json"
            or hard_p.endswith("/hooks/hooks.json")
            or hard_p == "reviewer_certifications.json"
            or hard_p.endswith("/reviewer_certifications.json")):
        return True
    if CODE_PATH_PREFIXES is None:
        return True   # строгий режим (нет/битый конфиг): ВСЁ код, экземпций нет (решение 1)
    if p in CODE_PATH_EXACT:
        return True
    if p.endswith(".md") or p.startswith("docs/") or p.startswith(".claude/"):
        return False
    if any(p.startswith(pre) for pre in CODE_PATH_PREFIXES):
        return True
    if p.endswith(".py") and "/" not in p:
        return True
    return False


def _env_session() -> str:
    # Claude Code экспонирует id сессии как CLAUDE_CODE_SESSION_ID; CLAUDE_SESSION_ID —
    # легаси/тестовый фолбэк. Раньше write_marker читал только legacy → маркер НИКОГДА не
    # совпадал с session_id хука (пусто) → G1 блокировал все правки без разблокировки.
    return os.environ.get("CLAUDE_CODE_SESSION_ID") or os.environ.get("CLAUDE_SESSION_ID", "")


def _marker_path(session: str) -> Path:
    # Пер-сессионный путь (Codex P2: параллельные сессии на одном checkout не затирают маркеры
    # друг друга). Суффикс от DESIGN_MARKER, чтобы тесты по-прежнему монкипатчили DESIGN_MARKER.
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session or "nosession")
    return DESIGN_MARKER.with_name(DESIGN_MARKER.name + "-" + safe)


def _write_marker_payload(session: str, payload: dict) -> None:
    # R4-F3: `detail` (текст оператора) редактируется ЗДЕСЬ — единая точка записи всех маркеров
    # (design/trivial/file-binding), иначе секрет оставался в persisted JSON, хотя аудит был чист.
    # Редактируем ТОЛЬКО detail: хэши (design_hash/designs[].hash) — 64-hex, их правило длинного
    # токена превратило бы в «скрыто» и сломало бы маркер.
    if isinstance(payload.get("detail"), str):
        payload = {**payload, "detail": redact_secrets(payload["detail"])}
    _atomic_write_json(_marker_path(session), payload)


def _load_marker(session: str) -> "dict | None":
    path = _marker_path(session)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return None


# Распознаваемые маркеры секции сценариев/BSAC (тикет #2, EARS-1). Ловим СЛУЧАЙНЫЙ пропуск
# (стаб без матрицы) — семантику судит Codex-ревью. Регистронезависимо, кроме EARS (акроним:
# lowercase 'ears' ложно совпал бы с 'years'/'appears' → fail-open).
_BSAC_MARKERS_CI = ("bsac", "бизнес-сценар", "сценари", "приёмочны", "приемочны",
                    "acceptance criteria", "scenario")
# EARS — как отдельный ТОКЕН (границы), иначе 'YEARS'/'APPEARS' в верхнем регистре ложно
# проходят → стаб без BSAC разблокировал бы код (Codex code-R1).
_EARS_RE = re.compile(r"(?<![A-Za-z])EARS(?![A-Za-z])")


def _has_bsac(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in _BSAC_MARKERS_CI) or bool(_EARS_RE.search(text))


def _read_design(path: Path) -> "tuple[str, str] | None":
    """(sha256_hex, текст) дизайн-файла; None при OSError/decode (fail-closed → дрейф)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    try:
        return hashlib.sha256(data).hexdigest(), data.decode()
    except UnicodeError:
        return None


@contextlib.contextmanager
def _marker_lock(session: str):
    """Эксклюзивный per-session лок (code-R1 F2: незалоченный read-modify-write набора биндингов
    терял дизайн при конкурентных write-marker одной сессии → дрейф потерянного проходил)."""
    path = _marker_path(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    lf = open(str(path) + ".lock", "w")
    try:
        fcntl.flock(lf, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lf, fcntl.LOCK_UN)
        lf.close()


def write_marker(kind: str, detail: str, design_hash: str | None = None) -> None:
    """inline/trivial маркер (легаси-контракт). File-режим — add_design_file_binding (тикет #3)."""
    session = _env_session()
    _write_marker_payload(session, {
        "kind": kind, "detail": detail, "design_hash": design_hash,
        "session": session, "ts": datetime.now(timezone.utc).isoformat(),
    })
    if kind == "trivial":                        # R1-6b: тривиальный маркер — осознанно, в аудит
        audit(f"trivial-marker session={session} reason={redact_secrets(detail)!r}")


def add_design_file_binding(detail: str, design_file: str, reviewed_hash: str) -> int:
    """File-режим design-маркера (тикет #3): мержит биндинг {file, hash: reviewed_hash} в набор
    `designs` маркера сессии. reviewed_hash — из результата ревью (не «что сейчас в файле»).
    Возвращает 0 при sha256(файл)==reviewed_hash, иначе 2 (записанный несовпадающий биндинг
    делает has_marker drifted — маркер невалиден целиком, R2-F1)."""
    session = _env_session()
    with _marker_lock(session):                           # code-R1 F2: load+merge+write атомарно
        rec = _load_marker(session)
        designs = []
        if rec and rec.get("session") == session and rec.get("kind") == "design" \
                and isinstance(rec.get("designs"), list):
            designs = [b for b in rec["designs"]          # прочие биндинги сохраняем
                       if isinstance(b, dict) and b.get("file") != design_file]
        designs.append({"file": design_file, "hash": reviewed_hash})
        _write_marker_payload(session, {
            "kind": "design", "detail": detail, "designs": designs,
            "session": session, "ts": datetime.now(timezone.utc).isoformat(),
        })
    # exit-код информативный (немедленный совет); авторитетная проверка — в _marker_state (R3).
    read = _read_design(REPO_ROOT / design_file)
    if read is None or read[0] != reviewed_hash:
        audit(f"design-file-binding MISMATCH session={session} file={design_file!r} "
              f"reviewed={reviewed_hash[:12]} current={str(read and read[0])[:12]}")
        print(f"[codex-gate] ⚠️ файл {design_file} НЕ совпал с reviewed_hash — записан как дрейф, "
              "маркер невалиден до совпадения/ре-ревью. Codex ревьюил не этот текст?", file=sys.stderr)
        return 2
    if not _has_bsac(read[1]):                            # тикет #2: стаб без BSAC/сценариев
        audit(f"design-file-binding NO-BSAC session={session} file={design_file!r}")
        print(f"[codex-gate] ⚠️ дизайн-файл {design_file} без секции BSAC/сценариев/EARS — маркер "
              "невалиден до добавления (см. /design-review) или используй --trivial для простой "
              "правки. Отревьюенный дизайн ОБЯЗАН нести сценарную матрицу.", file=sys.stderr)
        return 2
    return 0


def _marker_state(session: str) -> str:
    """'valid'|'absent'|'foreign'|'drifted'|'invalid'. design file-режим (тикет #3): valid только
    если ВСЕ биндинги набора совпали с текущими файлами; любой дрейф/непрочитан → 'drifted'."""
    if not session:
        return "invalid"
    path = _marker_path(session)
    if not path.exists():
        return "absent"
    rec = _load_marker(session)
    if rec is None:
        return "invalid"                          # битый маркер не разблокирует
    if rec.get("session") != session:
        return "foreign"                          # протухший/чужой
    kind = rec.get("kind")
    if kind == "trivial":
        return "valid"
    if kind != "design":
        return "invalid"
    designs = rec.get("designs")
    if isinstance(designs, list):                 # file-режим (тикет #3)
        if not designs:
            return "invalid"
        for b in designs:
            if not isinstance(b, dict) or not b.get("file") or not b.get("hash"):
                return "invalid"
            read = _read_design(REPO_ROOT / b["file"])
            if read is None or read[0] != b["hash"]:
                return "drifted"                  # файл изменён/удалён/непрочитан (fail-closed)
            # тикет #2 (R3): BSAC пере-выводится из hash-валидированного контента (== reviewed) —
            # исключает разъезд версий; стаб без секции → drifted (не разблокирует)
            if not _has_bsac(read[1]):
                return "drifted"
        return "valid"
    return "valid" if rec.get("design_hash") else "invalid"   # легаси inline (без дрейф-проверки)


def has_marker(session: str) -> bool:
    # R1-6b + тикет #3: валиден только при непустой совпадающей сессии И (design без дрейфа | trivial).
    return _marker_state(session) == "valid"


def clear_marker() -> None:
    _marker_path(_env_session()).unlink(missing_ok=True)   # только СВОЙ (пер-сессионный)


def bash_touches_code(command: str) -> bool:
    if _STRONG_MUTATION_RE.search(command):
        return True   # git apply/patch в командной позиции (цель в патч-файле)
    # sed -i: гейтим, если среди аргументов есть КОД-путь (docs/*.md — не код, не блочим; Codex)
    if _SED_I_RE.search(command) and any(
            is_code_path(t) for t in re.findall(r"""['"]?([\w./~-]+)""", command)):
        return True
    # редирект/tee: гейтим, только если ЦЕЛЬ записи — код-путь
    return any(is_code_path(m.group(1)) for m in _FILE_TARGET_RE.finditer(command))


def _deny(msg: str) -> int:
    print(msg, file=sys.stderr)
    return 2


class PatchEnvelopeError(ValueError):
    """Codex apply_patch payload не соответствует измеренному wire-контракту."""


_PATCH_PATH_RE = re.compile(
    r"^\*\*\* (?:Add File|Update File|Delete File|Move to): (?P<path>.+)$")
_PATCH_PATH_PREFIXES = (
    "*** Add File:", "*** Update File:", "*** Delete File:", "*** Move to:")
_HOOK_PATH_KEYS = (
    "file_path", "notebook_path", "path",
    "source_path", "destination_path", "old_path", "new_path", "from_path", "to_path",
    "sourcePath", "destinationPath", "oldPath", "newPath", "fromPath", "toPath",
    "source", "destination", "src", "dst", "dest", "target", "target_path", "targetPath",
)


def _is_file_mutator_tool(tool_name_low: str) -> bool:
    if not tool_name_low:
        return True
    if re.match(r"^(?:(?:multi_?)?edit|write|notebook_?edit|apply_?patch)", tool_name_low):
        return True
    if not tool_name_low.startswith("mcp__") or "__" not in tool_name_low:
        return False
    suffix = tool_name_low.rsplit("__", 1)[-1]
    if any(verb in suffix for verb in ("edit", "write", "patch", "replace", "truncate")):
        return True
    mutates = any(verb in suffix for verb in (
        "create", "update", "save", "delete", "remove", "move", "rename", "append", "copy"))
    resource = any(noun in suffix for noun in ("file", "path", "folder", "directory"))
    return mutates and resource


def _apply_patch_paths(command: object) -> list[str]:
    """Извлечь ВСЕ исходные/целевые пути из Codex apply_patch envelope.

    Известный apply_patch с битым/дрейфнувшим envelope не может означать «не-кодовый патч»:
    вызывающий блокирует его fail-closed. Строки содержимого, включая `+*** ...`, заголовками
    не являются; проверяются только точные control-lines без diff-префикса.
    """
    if not isinstance(command, str):
        raise PatchEnvelopeError("command не строка")
    lines = command.splitlines()
    if len(lines) < 3 or lines[0] != "*** Begin Patch" or lines[-1] != "*** End Patch":
        raise PatchEnvelopeError("нет полного Begin/End envelope")
    paths: list[str] = []
    for line in lines[1:-1]:
        match = _PATCH_PATH_RE.fullmatch(line)
        if match:
            path = match.group("path").strip()
            if not path or "\x00" in path:
                raise PatchEnvelopeError("пустой/невалидный путь")
            paths.append(path)
        elif line.startswith(_PATCH_PATH_PREFIXES):
            # Похожий на control-line заголовок с пустым путём/дрейфом грамматики.
            raise PatchEnvelopeError("невалидный path header")
        elif line.startswith("*** ") and line != "*** End of File":
            # Новый control-header нельзя игнорировать рядом с известным docs-путём: иначе
            # schema drift мог бы спрятать кодовый путь и превратить патч в «не-кодовый».
            raise PatchEnvelopeError("неизвестный control header")
    if not paths:
        raise PatchEnvelopeError("в envelope нет путей")
    return paths


def _hook_path_is_code(path: str, data: dict, *, require_cwd: bool = False) -> bool:
    """Классифицировать hook path относительно фактического cwd события.

    apply_patch передаёт относительные пути. Сначала превращаем их в абсолютные от payload.cwd,
    затем существующий is_code_path безопасно нормализует до REPO_ROOT и отвергает выход наружу.
    """
    root = Path(os.path.realpath(REPO_ROOT)) if REPO_ROOT is not None else None
    if root is None and require_cwd:
        raise PatchEnvelopeError("repo root отсутствует")
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd and Path(cwd).is_absolute():
        normalized_cwd = Path(os.path.realpath(cwd))
        if root is None:
            return is_code_path(path)
        try:
            normalized_cwd.relative_to(root)
        except ValueError:
            if require_cwd:
                raise PatchEnvelopeError("cwd вне repo root")
            return is_code_path(path)
        lexical_candidate = (Path(os.path.abspath(path)) if Path(path).is_absolute()
                             else Path(os.path.abspath(normalized_cwd / path)))
        candidate = Path(os.path.realpath(lexical_candidate))
        try:
            candidate.relative_to(root)
        except ValueError:
            if require_cwd:
                raise PatchEnvelopeError("patch path вне repo root")
            try:
                lexical_candidate.relative_to(root)
            except ValueError:
                return False   # явная внешняя цель: opt-in isolation, не наш проект
            return True        # лексически внутри repo, но symlink ушёл наружу → блок
        return is_code_path(str(candidate))
    if Path(path).is_absolute():
        lexical_candidate = Path(os.path.abspath(path))
        candidate = Path(os.path.realpath(path))
        if root is not None:
            try:
                candidate.relative_to(root)
            except ValueError:
                if require_cwd:
                    raise PatchEnvelopeError("patch path вне repo root")
                try:
                    lexical_candidate.relative_to(root)
                except ValueError:
                    return False   # явная внешняя цель: плагин не вмешивается
                return True        # symlink из repo наружу
        return is_code_path(str(candidate))
    if require_cwd:
        raise PatchEnvelopeError("cwd отсутствует/не абсолютный")
    return is_code_path(path)  # Claude legacy payload: сохраняем прежнее поведение


def _hook_candidate_path(path: str, data: dict) -> "Path | None":
    """Абсолютная realpath-цель hook path; None, если относительный путь не к чему привязать."""
    pp = Path(path)
    if pp.is_absolute():
        return Path(os.path.realpath(pp))
    cwd = data.get("cwd")
    if isinstance(cwd, str) and cwd and Path(cwd).is_absolute():
        return Path(os.path.realpath(Path(cwd) / pp))
    return None


def _hook_paths_stay_inside(paths: "list[str]", data: dict, root: Path) -> bool:
    """True только когда каждую цель можно доказуемо оставить внутри opt-out event repo."""
    resolved_root = Path(os.path.realpath(root))
    for path in paths:
        candidate = _hook_candidate_path(path, data)
        if candidate is None:
            return False
        try:
            candidate.relative_to(resolved_root)
        except ValueError:
            return False
    return True


def _hook_session(data: dict) -> str:
    return data.get("session_id") or _env_session()


def _review_disabled_path(session: str) -> Path:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", session or "nosession")
    return DESIGN_MARKER.with_name(".review-disabled-" + safe)


def review_disabled_reason(session: str) -> "str | None":
    """Причина выключения ревьюера в ЭТОЙ сессии, иначе None. Пер-сессионность = авто-истечение
    (Ф2 спеки): «навсегда выключен» невозможен by construction."""
    if not session or DESIGN_MARKER is None:
        return None
    p = _review_disabled_path(session)
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return "(причина нечитаема)"          # маркер есть → выключено (fail-closed к состоянию)
    r = rec.get("reason")
    return r if isinstance(r, str) and r.strip() else "(без причины)"


def _disabled_banner(reason: str) -> None:
    """ГРОМКО, пока включён (actuator-safety: забытый kill-switch недопустим)."""
    print(f"[codex-gate] ⚠️⚠️ РЕВЬЮЕР ВЫКЛЮЧЕН в этой сессии (причина: {reason}) — G1 пропускает "
          "правки кода, деплой ЗАБЛОКИРОВАН. Вернуть: `review-enable`.", file=sys.stderr)


def _design_gate(session: str, drift_msg: str, unreviewed_msg: str) -> int:
    """Общая ветка design-гейта по состоянию маркера (тикет #3): drifted → своё сообщение,
    не-valid → generic, valid → 0."""
    reason = review_disabled_reason(session)
    if reason is not None:                    # Ф2: дизайн-гейт fail-open by design (BS3)
        _disabled_banner(reason)
        return 0
    state = _marker_state(session)
    if state == "drifted":
        return _deny(drift_msg)
    if state != "valid":
        return _deny(unreviewed_msg)
    return 0


def gate_edit_cli(hook_json: str) -> int:
    try:
        data = json.loads(hook_json or "{}")
    except json.JSONDecodeError:
        return (_deny("[codex-gate] edit-hook payload не JSON — правка заблокирована "
                      "(fail-closed)") if _hooks_active() else 0)
    if not isinstance(data, dict):
        return (_deny("[codex-gate] edit-hook payload изменил схему — правка заблокирована "
                      "(fail-closed)") if _hooks_active() else 0)
    non_onboarded_event_root = _refresh_hook_repo_context(data)
    if not _hooks_active():   # BS-P1: не-онбордженный проект / не git-репо → плагин молчит
        return 0
    ti = data.get("tool_input") or {}
    if not isinstance(ti, dict):
        return _deny("[codex-gate] edit-hook tool_input изменил схему — правка заблокирована "
                     "(fail-closed)")
    tool_name = data.get("tool_name")
    tool_name_low = tool_name.casefold() if isinstance(tool_name, str) else ""
    file_tool = _is_file_mutator_tool(tool_name_low)
    # Claude-native tools use file_path/notebook_path; MCP move/rename tools commonly expose
    # source+destination. Preserve every known path so a docs→code move cannot hide its target.
    paths = []
    known_path_keys = set(_HOOK_PATH_KEYS)
    for key in _HOOK_PATH_KEYS:
        if key not in ti:
            continue
        value = ti.get(key)
        if not isinstance(value, str) or not value:
            return _deny(f"[codex-gate] edit-hook payload path {key!r} изменил схему — "
                         "правка заблокирована (fail-closed)")
        if value not in paths:
            paths.append(value)
    for key in ti:
        if key in known_path_keys or not isinstance(key, str):
            continue
        snake_key = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", key).casefold()
        words = set(filter(None, re.split(r"[^a-z0-9]+", snake_key)))
        compact = re.sub(r"[^a-z0-9]", "", snake_key)
        pathlike = (
            bool(words & {"path", "paths", "file", "files", "folder",
                          "directory", "dir", "uri", "uris", "output",
                          "destination", "destinations", "dest", "dst",
                          "target", "targets"})
            or "path" in compact
            or compact.startswith(("dest", "source", "src", "dst", "target", "output"))
            or (compact.endswith(("file", "files", "folder", "directory", "dir", "uri"))
                and compact != "profile")
        )
        if pathlike:
            return _deny(f"[codex-gate] edit-hook payload содержит неизвестный path-like key "
                         f"{key!r} — правка заблокирована (fail-closed)")
    command = ti.get("command")
    is_patch = (bool(re.search(r"(?:^|__)apply_?patch", tool_name_low))
                or (isinstance(command, str) and bool(command)
                    and not paths))
    if is_patch:
        try:
            paths = _apply_patch_paths(command)
            if (non_onboarded_event_root is not None
                    and _hook_paths_stay_inside(paths, data, non_onboarded_event_root)):
                return 0
            touches_code = any(_hook_path_is_code(path, data, require_cwd=True)
                               for path in paths)
        except PatchEnvelopeError as exc:
            reason = str(exc)
            if "cwd" in reason or "repo root" in reason or "path вне" in reason:
                return _deny("[codex-gate] apply_patch cwd/path нельзя доказуемо привязать к "
                             f"целевому repo ({reason}) — правка заблокирована (fail-closed)")
            return _deny("[codex-gate] apply_patch envelope повреждён или изменил схему "
                         f"({reason}) — правка заблокирована (fail-closed)")
        if not touches_code:
            return 0
    else:
        if not paths:
            if not file_tool:
                return 0
            return _deny("[codex-gate] edit-hook payload не распознан: нет patch command или "
                         "file/notebook path — правка заблокирована (fail-closed)")
        if (non_onboarded_event_root is not None
                and _hook_paths_stay_inside(paths, data, non_onboarded_event_root)):
            return 0
        if not any(_hook_path_is_code(path, data) for path in paths):
            return 0
    session = _hook_session(data)
    if not session:   # сессию не определить → fail-open (design-гейт), не блокируем всю работу
        print("[codex-gate] сессия неизвестна — дизайн-гейт пропускает (fail-open)", file=sys.stderr)
        return 0
    return _design_gate(session,   # тикет #3: drifted → своё сообщение, иначе generic
        "Дизайн изменился с момента ревью (дрейф дизайн-файла) — перепрогони /design-review "
        "и перепомечай `write-marker design <detail> <hash> --file <path>`. Правки кода "
        "заблокированы до совпадения с отревьюенным.",
        "Дизайн-ревью не пройдено. Запусти /design-review до правок кода "
        "(или /design-review --trivial \"причина\").")


def gate_bash_cli(hook_json: str) -> int:
    try:
        data = json.loads(hook_json or "{}")
    except json.JSONDecodeError:
        return 0
    if not isinstance(data, dict):
        return 0
    _refresh_hook_repo_context(data)
    if not _hooks_active():   # BS-P1
        return 0
    ti = data.get("tool_input")
    # G1 Bash — документированный best-effort/fail-open путь: schema drift не должен падать
    # traceback'ом и превращать hook-runtime error в неясный блок.
    command = ti.get("command", "") if isinstance(ti, dict) else ""
    if not (command and bash_touches_code(command)):
        return 0
    session = _hook_session(data)
    if not session:
        print("[codex-gate] сессия неизвестна — bash-гейт пропускает (fail-open)", file=sys.stderr)
        return 0   # fail-open
    return _design_gate(session,   # тикет #3
        "Дизайн изменился с момента ревью (дрейф) — а Bash-команда похоже правит код. "
        "Перепрогони /design-review и перепомечай (--file). Заблокировано.",
        "Дизайн-ревью не пройдено, а Bash-команда похоже правит кодовый путь "
        "(sed -i/git apply/patch/redirect). Запусти /design-review сначала. "
        "NB: эвристика частичная (см. остаток R1-5).")


def main(argv: list[str]) -> int:
    cmd = argv[0] if argv else ""
    if cmd == "check-reviewed":
        return check_reviewed_cli()
    if cmd == "check-decision":                # быстрая ревалидация решения (deploy-lock, F3):
        if not _require_repo():
            return 2
        if skip_requested():                   # F5: аварийный CODEX_REVIEW_SKIP жив и здесь
            audit("CODEX_REVIEW_SKIP=1 — check-decision пропущен (аварийный контур)")
            return 0
        _dis = review_disabled_reason(_env_session())
        if _dis is not None:                   # Ф2: выключенный ревьюер блокирует и здесь
            _disabled_banner(_dis)
            print("[codex-gate] ✗ ревьюер выключен — выкатка остановлена (`review-enable` или "
                  "аварийный CODEX_REVIEW_SKIP=1).", file=sys.stderr)
            return 2
        with findings_lock():                  # перечитать серию ПРЯМО перед rsync — конкурентная
            led = load_findings_ledger(None)   # сессия могла записать open/адъюдикацию после allow
            if led is None:
                print("[codex-gate] ✗ findings-ledger повреждён", file=sys.stderr)
                return 2
            if led.get("needs_review_round"):
                print("[codex-gate] ✗ есть адъюдикации, не показанные Codex — решение устарело",
                      file=sys.stderr)
                return 2
            decision, msg = convergence_decision(led)
        if decision != "allow":
            print(msg, file=sys.stderr)
            return 2
        return 0
    if cmd == "findings":                      # протокол сходимости: показать серию
        if not _require_repo():
            return 2
        led = load_findings_ledger(None)
        if led is None:
            print("findings-ledger повреждён", file=sys.stderr)
            return 2
        for k, f in sorted((led.get("findings") or {}).items()):
            print(f"{k} [{f.get('severity')}] {f.get('status')} "
                  f"disputes={f.get('disputes', 0)} — {f.get('title', '')[:90]}"
                  + (f" | {f.get('reason', '')[:60]}" if f.get("reason") else ""))
        print(f"rounds={led.get('rounds')} baseline={str(led.get('baseline'))[:12]}")
        return 0
    if cmd == "adjudicate":                    # adjudicate <Fid> <status> "<причина>"
        if not _require_repo():
            return 2
        if len(argv) < 4:
            print("usage: adjudicate <Fid> fixed|residual-failsafe|refuted|resolved-by-user"
                  "|open \"причина\"", file=sys.stderr)
            return 1
        with findings_lock():
            led = load_findings_ledger(None)
            if led is None:
                print("findings-ledger повреждён", file=sys.stderr)
                return 2
            try:
                adjudicate(led, argv[1], argv[2], argv[3])
            except AdjudicationError as e:
                print(f"[codex-gate] {e}", file=sys.stderr)
                return 2
            save_findings_ledger(led)
        print(f"{argv[1]} → {argv[2]}")
        return 0
    if cmd in ("review-disable", "review-enable", "review-status"):
        if DESIGN_MARKER is None:
            print("[codex-gate] ✗ не git-репозиторий", file=sys.stderr)
            return 2
        session = _env_session()
        if not session:                        # S10: нечего скоупить → fail-closed
            print("[codex-gate] ✗ сессия неизвестна — выключение нечего скоупить "
                  "(пер-сессионный маркер). Команда отклонена.", file=sys.stderr)
            return 2
        path = _review_disabled_path(session)
        if cmd == "review-status":
            reason = review_disabled_reason(session)
            print(f"ревьюер: {'ВЫКЛЮЧЕН (' + reason + ')' if reason else 'включён'}")
            return 0
        if cmd == "review-enable":
            existed = path.exists()
            path.unlink(missing_ok=True)
            if existed:
                audit(f"review-enable session={session} — ревьюер снова включён")
            print("[codex-gate] ✓ ревьюер включён" + ("" if existed else " (и не был выключен)"))
            return 0
        reason = argv[1].strip() if len(argv) > 1 else ""
        if not reason:                         # S2: причина обязательна (как у адъюдикаций)
            print("usage: review-disable \"<причина>\"  — причина обязательна (аудит)",
                  file=sys.stderr)
            return 1
        _atomic_write_json(path, {"reason": redact_secrets(reason), "session": session,
                                  "ts": datetime.now(timezone.utc).isoformat()})
        audit(f"review-disable session={session} reason={redact_secrets(reason)!r} — "
              "ревьюер ВЫКЛЮЧЕН в сессии (деплой заблокирован, G1 пропускает)")
        _disabled_banner(redact_secrets(reason))
        return 0
    if cmd == "gate-edit":
        return gate_edit_cli(sys.stdin.read())
    if cmd == "gate-bash":
        return gate_bash_cli(sys.stdin.read())
    if cmd == "write-marker":
        if DESIGN_MARKER is None:
            print("[codex-gate] ✗ не git-репозиторий — маркер писать некуда", file=sys.stderr)
            return 2
        args = argv[1:]
        kind = args[0] if args else "design"
        detail = args[1] if len(args) > 1 else ""
        rest = args[2:]
        design_file = None
        file_flag = "--file" in rest               # тикет #3: file-режим design-маркера
        if file_flag:
            i = rest.index("--file")
            design_file = rest[i + 1] if i + 1 < len(rest) else None
            rest = rest[:i] + rest[i + 2:]
        reviewed_hash = rest[0] if rest else None
        if file_flag:      # code-R1 F1: --file задан → СТРОГО file-режим, НЕ проваливаться в inline
            if kind != "design" or not design_file or not reviewed_hash:
                print("usage: write-marker design <detail> <reviewed_hash> --file <path>",
                      file=sys.stderr)
                return 1                            # ошибка аргументов НЕ пишет маркер (fail-closed)
            return add_design_file_binding(detail, design_file, reviewed_hash)
        if kind == "design":       # F2 (altitude): inline design без --file = БЕЗ дрейф-защиты —
            print("[codex-gate] ⚠️ inline design-маркер (без --file) НЕ защищён от дрейфа: "  # громко, не молча
                  "пост-ревью правка дизайна не будет поймана. Для нетривиального/actuator/"
                  "data-loss дизайна используй `--file <path>` (см. /design-review).", file=sys.stderr)
        write_marker(kind, detail, reviewed_hash)
        return 0
    if cmd == "clear-marker":
        if not _hooks_active():   # SessionStart в любом проекте: молча no-op вне онбординга
            return 0
        clear_marker()
        return 0
    if cmd == "companion-review":
        # Дизайн-ревью для скилла: подкоманда ВЫПОЛНЯЕТ ревью, а не печатает argv.
        # Печать argv (прошлая companion-path) обходила редакцию — в argv может лежать
        # `--api-key=…` (тот же класс, что R5-F2), и вдобавок вынуждала скилл пересобирать
        # команду шеллом. Здесь остаётся тестируемый путь: таймаут, редакция, fail-closed.
        passthrough = argv[1:]
        if not passthrough:
            print("usage: companion-review [--base <ref>] [--scope <scope>] \"<фокус-текст>\"",
                  file=sys.stderr)
            return 1
        r = _exec_companion(["adversarial-review", "--wait", *passthrough])
        if r is None:
            return 2                     # отказ уже объяснён в stderr, дальше — fail-closed
        if r.returncode != 0:
            # Причина отказа рендерится в stdout (конверт), а не в stderr (шум прогресса) —
            # выбрасывать stdout здесь значило бы вернуть регрессию «причина outage невидима».
            reason = outage_details(r.stdout) or redact_secrets(r.stderr.strip())[:400]
            print(f"[codex-gate] companion exit={r.returncode}: {reason}", file=sys.stderr)
            return 2
        outage = companion_outage_reason(r.stdout)
        if outage is not None:
            # Код 0 ≠ ревью состоялось: при исчерпанной квоте companion выходит нулём и отдаёт
            # деградировавший конверт. Без этой ветки он читался бы как «замечаний нет».
            print(f"[codex-gate] ревью НЕ выполнено: {outage}", file=sys.stderr)
            return 2
        # Тело ревью печатается дословно: это вход для читателя-агента, а не диагностика,
        # и порча текста редакцией исказила бы находки (источник #10 реестра ниже).
        print(r.stdout, end="")
        return 0
    print(f"codex_review_gate: неизвестная команда {cmd!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
