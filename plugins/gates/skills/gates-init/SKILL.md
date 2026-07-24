---
name: gates-init
description: Онбординг проекта в систему ревью-гейтов Claude↔Codex одной командой — генерирует .codex-gate.yaml, ставит git-хуки (лесенка), AGENTS.md из скелета, показывает Makefile-snippet деплой-гейта. Использовать в корне git-репозитория, который нужно подключить к гейтам.
---

# gates-init — онбординг проекта в ревью-гейты

Подключает текущий репозиторий к системе гейтов (спека:
`docs/2026-07-22-gates-plugin-port-design.md` в репо плагина). Шаблоны лежат в каталоге
плагина: `<база этого скилла>/../../templates/` (далее `$T`). Все шаги идемпотентны,
существующие файлы пользователя молча НЕ перезаписываются.

Выполни шаги по порядку; при провале шага — остановись и скажи пользователю, что не так.

## 1. Проверить зависимости (стоп при провале)

```bash
ls ~/.claude/plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs 2>/dev/null | tail -1
python3 -c "import yaml" 2>&1
```
- Нет codex-companion → СТОП: «Установи Codex-плагин: `/plugin marketplace add openai/codex-plugin-cc`
  → `/plugin install codex@openai-codex`, залогинься в ChatGPT» — без него деплой-гейт всегда
  fail-closed.
- Нет PyYAML → предупреди: гейты будут работать в СТРОГОМ режиме (все пути = код);
  предложи `pip3 install pyyaml`.
- Не git-репозиторий (`git rev-parse --show-toplevel` падает) → СТОП.

## 2. Сгенерировать `.codex-gate.yaml` (если нет)

Возьми `$T/codex-gate.example.yaml` за основу. Код-пути: осмотри структуру репо (каталоги
исходников, манифесты зависимостей, Dockerfile/compose, конфиги приложения), предложи
пользователю список prefixes/exact через AskUserQuestion и дай поправить.
`ladder.epoch_sha` = `git rev-parse HEAD` (вся прошлая история grandfathered — гейты
начинаются «с этого момента»). `convergence.hard_cap: 8`.
Опциональные секции (предложи, если у проекта есть тесты/внешний деплой-стейт):
- `empirical.test_command` — тест-команда как условие деплоя (напр. `python3 -m pytest -q`;
  argv без shell, для пайплайнов — обёртка-скрипт). ВАЖНО: включённую секцию потом нельзя
  тихо снять/ослабить — только аудируемый `EMPIRICAL_SKIP`.
- `deploy.baseline_command` — authoritative-источник задеплоенного SHA (inframon/ssh);
  активация пинуется: первый деплой после включения — через явный `CODEX_DEPLOY_BASELINE`.
NB: `.codex-gate.yaml`, `Makefile`, `.githooks/` — всегда код-пути (жёстко в коде гейта),
в конфиг их писать не нужно.

## 3. `AGENTS.md` из скелета (если нет)

Скопируй `$T/AGENTS.skeleton.md` → `AGENTS.md`. Секцию «Blocking» наполни доменными
инвариантами ВМЕСТЕ с пользователем (что стоит денег, что необратимо). Существующий
AGENTS.md не трогай — предложи только сверить структуру (blocking / medium-low / реестр
остатков).

## 4. Git-хуки (лесенка) — БЕЗ молчаливой перезаписи чужих хуков (Codex code-R1)

Сначала проверь существующую хук-инфраструктуру:
```bash
git config core.hooksPath          # задан и ≠ .githooks → у проекта СВОЙ hooksPath
ls .githooks/ .git/hooks/ 2>/dev/null | grep -v sample
```
- `core.hooksPath` уже задан на другой каталог, ИЛИ есть существующие активные хуки
  (в `.git/hooks/` не-sample, или свои файлы в `.githooks/`) → **СТОП, спроси пользователя**:
  перезаписать / встроить цепочкой (наш шим вызывается из его хука) / отменить.
  Существующие хуки могут держать безопасность или деплой-автоматику — молча ломать нельзя.
- Конфликтов нет:
```bash
mkdir -p .githooks
cp "$T/githooks/gates-run" "$T/githooks/pre-commit" "$T/githooks/post-commit" .githooks/
chmod +x .githooks/gates-run .githooks/pre-commit .githooks/post-commit
git config core.hooksPath .githooks
```

## 5. `.gitignore` — служебные файлы гейтов

Проверь и предложи добавить (если не игнорируются):
```
.claude/
logs/
```
(маркеры `.claude/.ladder-*`, `.design-approved*`, `.last-*-sha` и ledger'ы `logs/` не должны
засорять диффы).

## 6. Онбординг-коммит СРАЗУ (закрывает окно opt-in — Codex R2-фикс спеки)

**Сначала — чистый индекс** (Codex code-R1: обычный commit включил бы ранее застейдженный
чужой код в санкционированный skip-коммит):
```bash
git diff --cached --quiet || echo "СТОП: в индексе посторонние staged-изменения"
```
Посторонние staged-изменения есть → СТОП: попроси пользователя закоммитить/убрать их, потом
продолжай. Индекс чист →
```bash
git add .codex-gate.yaml .githooks/ AGENTS.md .gitignore
LADDER_SKIP=1 LADDER_SKIP_REASON="gates-init onboarding" \
  git commit -m "chore: gates-init — онбординг ревью-гейтов" -- .codex-gate.yaml .githooks AGENTS.md .gitignore
```
(pathspec в commit — второй пояс: коммитятся ТОЛЬКО файлы онбординга.) Pre-commit скипается
с аудитом, post-commit пишет skipped-запись → деплой-диапазон покрыт штатно. Конфиг попадает
в HEAD немедленно → признак онбординга (worktree-OR-HEAD) активен, окна «конфиг только в
worktree» нет. Это единственный штатно-рекомендованный LADDER_SKIP.

## 7. Makefile-snippet деплой-гейта — показать, НЕ вставлять молча

Покажи пользователю `$T/Makefile.snippet` и объясни обязательные элементы:
`deploy: check-reviewed` (prerequisite), deploy-lock (atomic mkdir + trap — на macOS нет
flock), `check-decision` прямо перед выкаткой, запись `.claude/.last-deployed-sha` только
после успешной пост-деплой верификации. Деплой у каждого проекта свой — предложи встроить
и сделай это только с согласия пользователя.

## 8. Первый baseline

Пока нет `.claude/.last-deployed-sha`, первый `make check-reviewed` заблокируется на
«baseline неизвестен». Подскажи: `CODEX_DEPLOY_BASELINE=<sha задеплоенного состояния> make deploy`
(обычно SHA онбординг-коммита или последнего фактически задеплоенного).

## 9. Итоговая сводка

Покажи: какие пути считаются кодом; цикл разработки
(`/design-review` → правки → `begin/mark simplify` → `/simplify` → … → commit →
`check-reviewed` на деплое); протокол сходимости (`findings` / `adjudicate`); escape-hatch'и
(`LADDER_SKIP=1`, `CODEX_REVIEW_SKIP=1` — оба аудируются) и что G1-маркер сбрасывается
каждую сессию.
