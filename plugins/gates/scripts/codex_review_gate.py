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
import subprocess
import sys
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
    findings = [(f["severity"].strip().lower(), str(f.get("title", "")).strip())
                for f in result["findings"]]
    return ReviewVerdict(
        verdict=result["verdict"], findings=findings,
        no_findings_marker=(not findings),   # структурно: пусто findings = чисто, не дрейф
    )


def parse_review_output(text: str) -> ReviewVerdict:
    clean = strip_ansi(text)
    js = _verdict_from_json(clean)            # JSON-first (contract adversarial-review --json)
    if js is not None:
        return js
    m = _VERDICT_RE.search(clean)             # текст-фолбэк (рендер Verdict:/[severity])
    verdict = m.group(1).strip() if m else None
    findings = [(mm.group("sev").strip().lower(), mm.group("rest").strip())
                for mm in _FINDING_RE.finditer(clean)]
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
HARD_CODE_PATH_EXACT = {GATE_CONFIG_NAME, "Makefile"}
HARD_CODE_PATH_PREFIXES = (".githooks/",)
_DEFAULT_HARD_CAP = 8


def _detect_repo_root() -> "Path | None":
    """git rev-parse --show-toplevel от cwd (скрипт живёт в кэше плагина — __file__ бесполезен).
    None = не git-репо/сбой git: хуки → exit 0, явные гейты → явная ошибка (fail-closed)."""
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True)
    except OSError:
        return None
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return Path(r.stdout.strip())


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


def resolve_companion_cmd() -> list[str]:
    override = os.environ.get("CODEX_COMPANION_CMD")
    if override:
        return shlex.split(override)
    # CODEX_PLUGIN_ROOT (наш override) и официальный CLAUDE_PLUGIN_ROOT (напр. --plugin-dir install);
    # используем только если companion реально там есть, иначе продолжаем к кэш-глобу (Codex P2).
    for env_var in ("CODEX_PLUGIN_ROOT", "CLAUDE_PLUGIN_ROOT"):
        root = os.environ.get(env_var)
        if not root:
            continue
        cand = Path(root) / "scripts" / "codex-companion.mjs"
        if cand.exists():
            return ["node", str(cand)]
        deep = sorted(glob.glob(str(Path(root) / "**" / "codex-companion.mjs"), recursive=True))
        if deep:
            return ["node", deep[-1]]
    matches = sorted(glob.glob(
        os.path.expanduser("~/.claude/plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs")))
    if not matches:
        raise FileNotFoundError("codex-companion.mjs не найден (установлен ли плагин openai-codex?)")
    return ["node", matches[-1]]


_REVIEW_FOCUS = (
    "Review the committed changes for correctness, safety, and money-loss risks per AGENTS.md. "
    "Return a structured Verdict and findings with severity; critical/high block the deploy.")


def run_companion_review(base: str | None, scope: str) -> str | None:
    # adversarial-review --json даёт СТРУКТУРНЫЙ result{verdict,findings[severity]} (схема),
    # в отличие от нативного `review`, чей вывод — текст P1/P2/P3 без Verdict: (инцидент:
    # нативный формат ломал парсер → make deploy всегда блокировался).
    try:
        cmd = resolve_companion_cmd()   # Codex P2: отсутствие плагина = outage (None), не traceback
    except FileNotFoundError as e:
        print(f"[codex-gate] плагин codex-companion не найден: {e}", file=sys.stderr)
        return None
    cmd += ["adversarial-review", "--wait", "--json", "--scope", scope]
    if base:
        cmd += ["--base", base]
    cmd.append(_REVIEW_FOCUS + _adjudication_prompt_block())   # переговорная память серии
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=_REVIEW_TIMEOUT_S)
    except (subprocess.TimeoutExpired, OSError) as e:
        print(f"[codex-gate] review не удался: {type(e).__name__}: {e}", file=sys.stderr)
        return None
    if r.returncode != 0:
        print(f"[codex-gate] review exit={r.returncode}: {r.stderr.strip()[:400]}", file=sys.stderr)
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


def write_ledger(head_sha: str, diff_sha: str, baseline: str, verdict: ReviewVerdict) -> None:
    LEDGER_DIR.mkdir(parents=True, exist_ok=True)
    ledger_path(head_sha).write_text(json.dumps({
        "head_sha": head_sha, "diff_sha256": diff_sha, "baseline_sha": baseline,
        "verdict": verdict.verdict, "findings": verdict.findings,
        "no_findings_marker": verdict.no_findings_marker,
        "malformed": verdict.malformed,
        "ts": datetime.now(timezone.utc).isoformat(),
    }, ensure_ascii=False, indent=2))


def read_valid_ledger(head_sha: str, diff_sha: str) -> ReviewVerdict | None:
    p = ledger_path(head_sha)
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if rec.get("head_sha") != head_sha or rec.get("diff_sha256") != diff_sha:
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
        inherited = {}
        for k, f in (led.get("findings") or {}).items():
            if f.get("status") == "carried":
                inherited[f"F{len(inherited) + 1}"] = {
                    "severity": f.get("severity"), "title": f.get("title"),
                    "status": "open", "dup_of": None, "disputes": 0, "round": 0,
                    "carried_from": led["baseline"],
                    "carry_count": int(f.get("carry_count") or 0) + 1,
                }
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


def merge_round(led: dict, blocking_findings: "list[tuple[str, str]]",
                review_started_ts: "float | None" = None) -> None:
    """Влить раунд Codex в ledger. [DUP:открытого/fixed] → duplicate-привязка;
    [DUP:residual/refuted] → пере-подъём = dispute (спека R1: DUP ≠ согласие);
    [DISPUTE:Fx] → disputes+1 + re-open; прочее → новый open."""
    led["rounds"] = int(led.get("rounds") or 0) + 1
    # Флаг чистится, только если review СТАРТОВАЛ после последней адъюдикации (спор F3-3:
    # старый review, финишировавший после адъюдикации, очищал флаг, не видев её).
    if review_started_ts is None or review_started_ts >= float(led.get("last_adj_ts") or 0):
        led["needs_review_round"] = False
    fnd = led.setdefault("findings", {})

    def new_fid() -> str:
        return f"F{len(fnd) + 1}"

    for sev, title in blocking_findings:
        kind, fid, rest = parse_finding_prefix(title)
        target = fnd.get(fid) if fid else None
        if kind in ("dup", "dispute") and target is not None:
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
                              "dup_of": fid, "disputes": 0, "round": led["rounds"]}
            continue
        if kind == "dispute" and target is not None:
            if target["status"] == "resolved-by-user":
                target["late_note"] = rest        # финальность человека (см. выше)
                continue
            target["status"] = "open"
            target["disputes"] = int(target.get("disputes") or 0) + 1
            target["dispute_note"] = rest
            continue
        fnd[new_fid()] = {"severity": sev, "title": rest, "status": "open",
                          "dup_of": None, "disputes": 0, "round": led["rounds"]}


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
    f["status"] = status
    f["reason"] = reason.strip()
    import time as _time
    led["last_adj_ts"] = _time.time()
    led["needs_review_round"] = True   # Codex должен УВИДЕТЬ адъюдикацию (спор F3-2: кэш
    audit(f"adjudicate {fid} → {status}: {reason.strip()!r}")   # позволял allow без его раунда)


def apply_carry_over(led: dict) -> "list[str]":
    """Пост-hard-cap НОВЫЕ неоспоренные не-critical находки → carried (реш. юзера 22.07):
    срочный деплой едет, находка стартует следующую серию ОТКРЫТОЙ (бэклог с зубами).
    Critical/unknown-severity и оспоренные — НЕ переносятся (блокируют/эскалируют)."""
    if int(led.get("rounds") or 0) <= HARD_CAP_ROUNDS:
        return []
    carried = []
    for k, f in (led.get("findings") or {}).items():
        if (f.get("status") == "open"
                and int(f.get("round") or 0) == int(led.get("rounds") or 0)
                and f.get("severity") in KNOWN_SEVERITIES and f.get("severity") != "critical"
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


def _run_empirical(cmd: str, timeout_s: int, root: Path) -> "tuple[str, str]":
    """Прогон тест-команды. ('pass'|'fail'|'timeout'|'error', хвост вывода). Любой не-'pass' →
    блок (актуатор-урок: «не запустилось/зависло» ≠ «прошло»).

    argv через `shlex.split` + БЕЗ shell (bounded authority, defense-in-depth): `test_command`
    исполняется как список аргументов, shell-метасимволы не интерпретируются. Покрывает обычные
    команды (`python3 -m pytest -q`, `make test`); для пайплайнов/`&&` — обернуть в скрипт и
    указать его (`test_command: ./run-tests.sh`)."""
    try:
        argv = shlex.split(cmd)
    except ValueError as e:
        return ("error", f"не разобрать test_command: {e}")   # незакрытая кавычка и т.п.
    if not argv:
        return ("error", "пустая test_command")
    try:
        r = subprocess.run(argv, cwd=root, capture_output=True, text=True, timeout=timeout_s)
    except subprocess.TimeoutExpired:
        return ("timeout", "")
    except OSError as e:
        return ("error", f"{type(e).__name__}: {e}")   # команда не найдена и т.п.
    tail = (r.stdout + r.stderr)[-800:]
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
                          ladder_st: str, empirical_st: str, codex_st: str) -> int:
    """Ф2: машиночитаемый вердикт для inframon. Delete-then-write под локом (R1-F2/R2-F2).
    0 = ок/best-effort-warning; 2 = блок (unlink упал, старый вердикт остался бы маскировать)."""
    path = VERDICT_DIR / f"{head}.json"
    import time as _time
    payload = {
        "schema": 1, "run_id": f"{int(_time.time() * 1000)}-{os.getpid()}",
        "head_sha": head, "baseline_sha": baseline or "", "diff_sha256": diff_sha,
        "gates": {"ladder": ladder_st, "empirical": empirical_st, "codex": codex_st},
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    with _verdict_lock():
        try:
            path.unlink(missing_ok=True)          # delete-then-write: старый НЕ должен пережить
        except OSError:
            if path.exists():                     # V5b: не убрать вводящий в заблуждение старый
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
        print(f"[codex-gate] ✗ empirical: test_command изменилась с baseline (« {base_cmd[:40]} » "
              f"→ « {cmd[:40]} ») — смена = потенциальное ослабление, требует EMPIRICAL_SKIP=1 "
              "(аудит, как снятие гейта). Деплой остановлен.", file=sys.stderr)
        return 2
    print(f"[codex-gate] empirical: прогон «{cmd[:80]}» (timeout {timeout}s)…", file=sys.stderr)
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
        audit(f"LADDER_SKIP=1 — ladder-range пропущен (reason={reason!r})")
        print("[codex-gate] ⚠️ LADDER_SKIP=1 — ladder-range пропущен (см. audit).",
              file=sys.stderr)
    elif _ladder_check(baseline) != 0:
        print("[codex-gate] ✗ ladder-range не покрыт — см. вывод выше. Деплой остановлен.",
              file=sys.stderr)
        return 2

    # --- EMPIRICAL часть (тикет #1: механическая проверка ДО Codex; ladder → empirical → Codex) ---
    if empirical_skip:
        reason = os.environ.get("EMPIRICAL_SKIP_REASON", "")
        audit(f"EMPIRICAL_SKIP=1 — эмпирический гейт пропущен (reason={reason!r})")
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
    if read_valid_ledger(head, diff_sha) is not None:
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
            if _write_deploy_verdict(head, baseline, diff_sha, l_st, e_st, c_st):
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
    out = run_companion_review(base=baseline, scope="branch")
    verdict = parse_review_output(out) if out is not None else None
    if verdict is None:          # ML6: реальный outage/timeout → аварийный контур БЕЗ Codex
        print("[codex-gate] ✗ Codex недоступен/невалидный вывод — деплой остановлен. "
              "При АКТИВНОМ инциденте актуатора: СНАЧАЛА kill-switch проекта (freeze — "
              "останавливает актуатор без Codex); ЗАТЕМ при необходимости rollback (пока "
              "заморожено). Rollback БЕЗ freeze актуатор НЕ останавливает. Ремонт — через гейт.",
              file=sys.stderr)
        return 2
    if not verdict.valid:        # дрейф схемы — ledger не кормим мусором
        print("[codex-gate] ✗ невалидный вывод ревью (дрейф схемы) — деплой остановлен",
              file=sys.stderr)
        return 2
    # ─ Протокол сходимости (Фаза 1.6): память между раундами вместо стены high'ов ─
    blocking = [(s, t) for s, t in verdict.findings
                if s in SEVERITY_BLOCKING or s not in KNOWN_SEVERITIES]
    with findings_lock():        # догфуд F3: RE-LOAD под локом — ревью шло минуты, конкурентная
        led = load_findings_ledger(baseline)   # сессия могла изменить серию; merge поверх свежего
        if led is None:
            print("[codex-gate] ✗ findings-ledger повреждён. Деплой остановлен.", file=sys.stderr)
            return 2
        merge_round(led, blocking, review_started_ts=review_started_ts)
        apply_carry_over(led)
        save_findings_ledger(led)
        decision, msg = convergence_decision(led)
    if decision == "allow":
        if not verdict.blocking:
            write_ledger(head, diff_sha, baseline, verdict)   # чистый вердикт кэшируем
        _record_reviewed(head)
        for sev, title in verdict.findings:
            if sev not in SEVERITY_BLOCKING:   # совещательные — показать оператору
                print(f"    [{sev}] {title}", file=sys.stderr)
        l_st, e_st, c_st = _verdict_statuses("allow")
        if _write_deploy_verdict(head, baseline, diff_sha, l_st, e_st, c_st):
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
        try:
            p = str(pp.relative_to(REPO_ROOT))
        except ValueError:
            return False   # абсолютный путь вне репозитория — не наш код-путь
    # схлопнуть ../ (docs/../app/x.py → app/x.py; Codex P2). normpath делает это сам — БЕЗ
    # предварительного .lstrip("./"): lstrip трактует аргумент как МНОЖЕСТВО символов, а не
    # префикс, поэтому ".githooks/pre-commit" терял ведущую точку и переставал матчиться
    # (регресс проекта-источника, покрыт тестом).
    p = os.path.normpath(p)
    # Жёсткие пути — ДО конфига и экземпций (ML-P1: конфиг не может вывести их из-под гейта)
    if p in HARD_CODE_PATH_EXACT or any(p.startswith(pre) for pre in HARD_CODE_PATH_PREFIXES):
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
    _atomic_write_json(_marker_path(session), payload)   # общий атомарный писатель


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
        audit(f"trivial-marker session={session} reason={detail!r}")


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


def _hook_session(data: dict) -> str:
    return data.get("session_id") or _env_session()


def _design_gate(session: str, drift_msg: str, unreviewed_msg: str) -> int:
    """Общая ветка design-гейта по состоянию маркера (тикет #3): drifted → своё сообщение,
    не-valid → generic, valid → 0."""
    state = _marker_state(session)
    if state == "drifted":
        return _deny(drift_msg)
    if state != "valid":
        return _deny(unreviewed_msg)
    return 0


def gate_edit_cli(hook_json: str) -> int:
    if not _hooks_active():   # BS-P1: не-онбордженный проект / не git-репо → плагин молчит
        return 0
    try:
        data = json.loads(hook_json or "{}")
    except json.JSONDecodeError:
        return 0
    ti = data.get("tool_input") or {}
    path = ti.get("file_path") or ti.get("notebook_path", "")   # NotebookEdit шлёт notebook_path
    if not (path and is_code_path(path)):
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
    if not _hooks_active():   # BS-P1
        return 0
    try:
        data = json.loads(hook_json or "{}")
    except json.JSONDecodeError:
        return 0
    command = (data.get("tool_input") or {}).get("command", "")
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
    print(f"codex_review_gate: неизвестная команда {cmd!r}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
