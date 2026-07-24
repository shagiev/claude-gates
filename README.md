# claude-gates — переносимые ревью-гейты Claude↔Codex

Claude Code-плагин `gates`: боевая система независимого ревью, портированная из
внутреннего боевого проекта (полный цикл 22.07.2026: ~35 реальных багов найдено независимым
Codex-ревью, протокол сходимости довёл деплой до самостоятельного схождения). Слои:

1. **G1 дизайн-гейт** — правки код-путей блокируются, пока дизайн не прошёл независимое
   Codex-ревью (`/design-review`, маркер пер-сессионный). Fail-open (мышление не стопорится).
   - **Дрейф-детектор**: design-маркер биндится к дизайн-файлу (reviewed-hash); правка
     дизайна ПОСЛЕ ревью → следующая правка кода блокируется до ре-ревью.
   - **Структурная валидация BSAC**: стаб без секции сценариев/BSAC/EARS нельзя пометить
     как отревьюенный (escape — `--trivial`).
2. **Enforced-лесенка** — перед каждым код-коммитом доказанные проходы `/simplify` →
   `/code-review` (begin/mark-протокол с tree-chain, pre/post-commit git-хуки, ledger).
3. **Эмпирический гейт** — тест-команда проекта (`empirical.test_command`) как условие
   деплоя, ДО трат на Codex; «не запустилось/зависло» ≠ «прошло» (fail-closed); снятие/
   подмена команды после включения — только через аудируемый `EMPIRICAL_SKIP`.
4. **Commit-bound деплой-гейт** — `check-reviewed`: чистое дерево → baseline →
   range-проверка лесенки всего `baseline..HEAD` → эмпирика → Codex adversarial-ревью диффа
   со строгим парсингом вердикта. Fail-closed. Протокол сходимости (finding-ledger,
   адъюдикации `fixed|residual-failsafe|refuted`, переговоры `[DUP:]`/`[DISPUTE:]`,
   эскалация к человеку, carry-over) — деплой сходится сам, без «стены high'ов».
5. **Интерфейс к внешнему guard'у** (напр. inframon) — authoritative задеплоенный SHA через
   `deploy.baseline_command` (no-fallback, pin секции против самоскрывающихся изменений) и
   машиночитаемый вердикт гейта `logs/review_verdicts/<sha>.json` (schema 1; скипы видимы,
   включая исторические). Серверный энфорсмент — за пределами плагина, по ту сторону контракта.

## Установка

```
/plugin marketplace add shagiev/claude-gates
/plugin install gates@lenar-gates
```

(с локального клона: `/plugin marketplace add <путь-к-клону>`; обновление: `/plugin update gates@lenar-gates`)

Требуется Codex-плагин (ревью-движок): `/plugin marketplace add openai/codex-plugin-cc` →
`/plugin install codex@openai-codex` (логин ChatGPT). Для чтения конфига — PyYAML
(`pip3 install pyyaml`; без него гейты работают в строгом режиме «все пути = код»).

## Онбординг проекта

В корне целевого git-репо: **`/gates-init`** — сгенерирует `.codex-gate.yaml` (код-пути,
эпоха; опционально `empirical`/`deploy`-секции), поставит git-хуки-шимы (переживают
обновления плагина; fail-closed при удалённом плагине), создаст `AGENTS.md` из скелета,
покажет Makefile-snippet деплой-гейта (deploy-lock, `check-decision`, baseline), сделает
онбординг-коммит.

Установка плагина БЕЗ онбординга ничего не меняет: хуки молчат в проектах без
`.codex-gate.yaml` (признак — файл в worktree или HEAD).

## Цикл разработки в онбордженном проекте

```
/design-review (маркер --file c reviewed-hash) → правки кода
→ bash .githooks/gates-run ladder_gate.py begin simplify → /simplify → … mark simplify
→ … begin code-review → /code-review → … mark code-review
→ git commit                        # pre-commit проверяет цепочку
→ make deploy                       # check-reviewed: ladder → empirical → Codex
```

Между раундами деплой-ревью: `findings` / `adjudicate <Fid> <status> "<причина>"`.

## Стоп-политика цикла ревью (кратко)

Критерий остановки — по классу оставшихся находок, не по нулю: **чинить** fail-open
(гейт пропускает опасное) и корректностные баги; **в реестр остатков** (`AGENTS.md`) —
fail-safe/niche/стиль; **архитектурное** — исключить из сходимости → серверная сторона.
Стоп при 2 сухих раундах / шумовом раунде / хард-капе. Severity ревьюера калибровать самому.
Полная версия: `docs/methodology/2026-07-21-codex-review-gates-phase1-design.md`,
§«Стоп-политика цикла Codex-ревью (v2)».

## Escape-hatch'и (все аудируются)

`LADDER_SKIP=1` — лесенка; `CODEX_REVIEW_SKIP=1` — Codex-часть; `EMPIRICAL_SKIP=1` —
тест-команда; полный обход — все три. `CODEX_DEPLOY_BASELINE=<sha>` — явный baseline
(переходы pin аудируются). При активном инциденте актуатора: сначала kill-switch проекта,
не слепой SKIP (ML6). Все скипы видимы во внешнем вердикте гейта.

## Документация

- `docs/2026-07-22-gates-plugin-port-design.md` — спека порта (4 дизайн-решения, реестр остатков).
- `docs/2026-07-22-empirical-gate-design.md` — эмпирический гейт (S1–S16).
- `docs/2026-07-23-design-drift-gate-design.md` — дрейф-детектор design-маркера.
- `docs/2026-07-23-bsac-structural-gate-design.md` — структурная валидация BSAC.
- `docs/2026-07-23-inframon-interface-design.md` — интерфейс к внешнему guard'у (обе стороны границы).
- `docs/methodology/` — исходные спеки системы: Phase 1 (Codex-гейты + стоп-политика),
  Phase 1.5 (лесенка), Phase 1.6 (протокол сходимости + carry-over).
- Каждая фича прошла цикл: спека → Codex adversarial-review до approve → TDD → лесенка →
  Codex-ревью кода до approve. Тесты: `python3 -m pytest tests/ -q` (278).
- История изменений: `CHANGELOG.md`.
