# claude-gates — переносимые ревью-гейты Claude↔Codex

Claude Code-плагин `gates`: боевая система независимого ревью, портированная из
внутреннего боевого проекта (22.07.2026 — полный цикл: ~35 реальных багов найдено независимым Codex-ревью,
протокол сходимости довёл деплой до самостоятельного схождения). Три слоя:

1. **G1 дизайн-гейт** — правки код-путей блокируются, пока дизайн не прошёл независимое
   Codex-ревью (`/design-review`, маркер пер-сессионный). Fail-open (мышление не стопорится).
2. **Enforced-лесенка** — перед каждым код-коммитом доказанные проходы `/simplify` →
   `/code-review` (begin/mark-протокол с tree-chain, pre/post-commit git-хуки, ledger).
3. **Commit-bound деплой-гейт** — `check-reviewed`: чистое дерево → baseline → range-проверка
   лесенки всего `baseline..HEAD` → Codex adversarial-ревью диффа со строгим парсингом
   вердикта. Fail-closed. Протокол сходимости (finding-ledger, адъюдикации
   `fixed|residual-failsafe|refuted`, переговоры `[DUP:]`/`[DISPUTE:]`, эскалация к человеку,
   carry-over) — деплой сходится сам, без «стены high'ов».

## Установка

```
/plugin marketplace add shagiev/claude-gates
/plugin install gates@lenar-gates
```

(с локального клона: `/plugin marketplace add <путь-к-клону>`)

Требуется Codex-плагин (ревью-движок): `/plugin marketplace add openai/codex-plugin-cc` →
`/plugin install codex@openai-codex` (логин ChatGPT). Для чтения конфига — PyYAML
(`pip3 install pyyaml`; без него гейты работают в строгом режиме «все пути = код»).

## Онбординг проекта

В корне целевого git-репо: **`/gates-init`** — сгенерирует `.codex-gate.yaml` (код-пути,
эпоха), поставит git-хуки-шимы (переживают обновления плагина; fail-closed при удалённом
плагине), создаст `AGENTS.md` из скелета, покажет Makefile-snippet деплой-гейта
(deploy-lock, `check-decision`, baseline), сделает онбординг-коммит.

Установка плагина БЕЗ онбординга ничего не меняет: хуки молчат в проектах без
`.codex-gate.yaml` (признак — файл в worktree или HEAD).

## Цикл разработки в онбордженном проекте

```
/design-review → правки кода
→ bash .githooks/gates-run ladder_gate.py begin simplify → /simplify → … mark simplify
→ … begin code-review → /code-review → … mark code-review
→ git commit                        # pre-commit проверяет цепочку
→ make deploy                       # check-reviewed: ladder-range + Codex-ревью диффа
```

Между раундами деплой-ревью: `findings` / `adjudicate <Fid> <status> "<причина>"`.

## Стоп-политика цикла ревью (кратко)

Критерий остановки — по классу оставшихся находок, не по нулю: **чинить** fail-open
(гейт пропускает опасное) и корректностные баги; **в реестр остатков** (`AGENTS.md`) —
fail-safe/niche/стиль; **архитектурное** — исключить из сходимости → Фаза 2. Стоп при 2
сухих раундах / шумовом раунде / хард-капе. Severity ревьюера калибровать самому.
Полная версия: `docs/methodology/2026-07-21-codex-review-gates-phase1-design.md`,
§«Стоп-политика цикла Codex-ревью (v2)».

## Escape-hatch'и (все аудируются)

`LADDER_SKIP=1` — только лесенка; `CODEX_REVIEW_SKIP=1` — только Codex-часть; полный обход —
оба. При активном инциденте актуатора: сначала kill-switch проекта, не слепой SKIP (ML6).

## Документация

- `docs/2026-07-22-gates-plugin-port-design.md` — спека плагина (4 дизайн-решения,
  Codex-ревьюнута, 4 раунда → approve; реестр остатков).
- `docs/methodology/` — исходные спеки системы: Phase 1 (Codex-гейты + стоп-политика),
  Phase 1.5 (лесенка), Phase 1.6 (протокол сходимости + carry-over).
- Тесты: `python3 -m pytest tests/ -q` (192).
