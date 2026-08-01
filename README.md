# claude-gates — переносимые ревью-гейты Claude↔Codex

Плагин `gates` для Claude Code и Codex: боевая система независимого ревью, портированная из
внутреннего боевого проекта (полный цикл 22.07.2026: ~35 реальных багов найдено независимым
Codex-ревью, протокол сходимости довёл деплой до самостоятельного схождения). Слои:

1. **G1 дизайн-гейт** — правки код-путей блокируются, пока дизайн не прошёл независимое
   Codex-ревью (`/design-review`, маркер пер-сессионный). Fail-open (мышление не стопорится).
   - **Дрейф-детектор**: design-маркер биндится к дизайн-файлу (reviewed-hash); правка
     дизайна ПОСЛЕ ревью → следующая правка кода блокируется до ре-ревью.
   - **Структурная валидация BSAC**: стаб без секции сценариев/BSAC/EARS нельзя пометить
     как отревьюенный (escape — `--trivial`).
2. **Enforced-лесенка** — перед каждым код-коммитом проходы `simplify` → `code-review` →
   `security` (begin/mark-протокол с tree-chain, pre/post-commit git-хуки, ledger). Гейт
   доказывает ПОРЯДОК и неизменность дерева между begin и mark, но не факт прохода; часть
   ревью-команд (`/code-review`) помечена платформой `disable-model-invocation` и их набирает
   оператор — `begin` печатает, кто запускает каждый проход.
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
   машиночитаемый вердикт гейта `logs/review_verdicts/<sha>.json` (schema 2; скипы видимы,
   включая исторические). Серверный энфорсмент — за пределами плагина, по ту сторону контракта.

## Установка в Claude Code

```
/plugin marketplace add shagiev/claude-gates
/plugin install gates@lenar-gates
```

(с локального клона: `/plugin marketplace add <путь-к-клону>`; надёжное обновление:
`/plugin uninstall gates@lenar-gates` → `/plugin install gates@lenar-gates`; встроенный
`/plugin update` для этого marketplace ранее молча не обновлял клон)

Требуется Codex-плагин (ревью-движок): `/plugin marketplace add openai/codex-plugin-cc` →
`/plugin install codex@openai-codex` (логин ChatGPT). Для чтения конфига — PyYAML
(`pip3 install pyyaml`; без него гейты работают в строгом режиме «все пути = код»).

## Codex preview

Codex-манифест и marketplace entry опубликованы как устанавливаемый preview
(`policy.installation: AVAILABLE`). G1 разбирает настоящий `apply_patch`
(Add/Update/Delete/Move и multi-file) и fail-closed блокирует неизвестный envelope. Сам факт
установки preview не означает, что portable deploy-review уже готов к использованию: под Codex
его нельзя включать без сертифицированного независимого non-Anthropic reviewer (Codex не может
блокирующе ревьюить Codex).

```
codex plugin marketplace add shagiev/claude-gates
codex plugin add gates@lenar-gates
```

(с локального клона: `codex plugin marketplace add <путь-к-клону>`.) На Codex CLI 0.146.0
проверен полный install/activation smoke: доставленный плагином PreToolUse hook перехватывает
реальный `apply_patch`, возвращает блокирующий `exit 2`, а причина доходит до модели.
Portable runtime уже имеет прямой Gemini HTTPS adapter (`GOOGLE_API_KEY`/`GEMINI_API_KEY`) и
обязательный отдельный Claude supplemental adapter. Gemini и Cursor пока имеют статус `candidate`:
первый ждёт живого corpus-прогона, второй не аттестует actual model в JSON CLI. Поэтому resolver
честно блокируется, а не объявляет непроверенную модель независимой. Cursor остаётся опциональным
legacy-adapter, а не зависимостью установки; portable resolver не включит его даже простой
сменой registry status без нового attesting adapter.
Если `REVIEW_PROVIDER` не задан, universal default — `portable`; legacy
`REVIEW_PROVIDER=codex|cursor|both` доступен только как явный режим совместимости.
Для существующего онбординга это намеренная fail-closed миграция: пока Gemini остаётся
`candidate`, deploy остановится с инструкцией сертификации. Временный
`REVIEW_PROVIDER=cursor`/`both` возвращает legacy-поведение, но **не** даёт гарантию portable
actual-model attestation и не включает обязательный Claude supplemental; это осознанное
понижение, а не рекомендуемый универсальный режим.
Установка preview не требует Cursor или Gemini-ключа. Без сертифицированного независимого
backend portable deploy-review намеренно блокируется с диагностикой; остальные возможности
плагина и явные legacy-режимы остаются доступны в описанных пределах.
Mandatory Claude adapter ищет native/legacy/npm CLI в `~/.local/bin`,
`~/.claude/local`, `~/.volta/bin`, `~/.nvm/versions/node/*/bin`, Homebrew и
`/usr/local/bin`; произвольный PATH не используется, фактический realpath пишется в audit.
Нормативный план adapters/certification:
`docs/2026-07-30-cross-host-reviewer-architecture.md`.

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
→ bash .githooks/gates-run ladder_gate.py begin simplify → проход simplify → … mark simplify
→ … begin code-review → /code-review (набирает ОПЕРАТОР) → … mark code-review
→ … begin security → проход security → … mark security
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
