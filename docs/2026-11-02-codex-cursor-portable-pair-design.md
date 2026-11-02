# Сертифицированная blocking-пара Codex + Cursor

Дата: 2026-11-02. Статус: реализация по явному решению владельца.

## Проблема и решения

Доступный Claude CLI возвращает `oauth_not_allowed_for_organization`. Codex и
Cursor CLI установлены и работают, но portable resolver 0.10.7 допускает только
Codex+Claude, а Cursor — candidate с legacy-only адаптером.

Варианты:

- **Оставить как есть** — без Anthropic deploy заблокирован. Отклонено как нерабочее.
- **Выбирать панель через `REVIEW_PROVIDER`** — отклонено: вызывающий не должен
  менять или уменьшать blocking-панель переменной окружения.
- **Пометить Cursor `verified`** — отклонено: JSON Cursor содержит verdict/usage,
  но не фактическую модель; это была бы ложная аттестация.
- **Деградировать до одного ревьюера** — отклонено: возможно саморевью.
- **Детерминированная пара Codex+Cursor после сертификации** — выбрано.

## Решение

Portable resolver сначала проверяет пару Codex+Cursor; если certified Cursor
записей нет, использует прежнюю пару Codex+Claude. Более одной certified
Cursor blocking-записи — fail-closed без выбора по порядку JSON и без fallback.
Панель по-прежнему состоит ровно из двух обязательных независимых семейств.
Outage выбранного адаптера — fail-closed, автоматического перехода после
частичного раунда нет.

Базовая пара проверяется ДО Gemini: точные `(provider, adapter, family)` —
`codex/codex-companion/openai` + `cursor/cursor-cli/xai` либо прежние
`codex/codex-companion/openai` + `claude/claude-cli/anthropic`. Диспетчер
production adapter также использует точную пару `(provider, adapter)`.
Добавленный verified Gemini не может легализовать неверную базовую пару.

Cursor получает production text-адаптер, общий с certification runner. Модель
берётся из единственной certification record, не из `CURSOR_REVIEW_MODEL`.
Закреплённая модель — `grok-4.7-high`, `attestation=declared`, не `verified`.
Production diff проходит через `_diff_text`. Бинарь разрешается
только из фиксированных каталогов `_trusted_home()`, без `PATH`, с
`resolve(strict=True)` и запретом пути внутри репозитория. Запуск — в стерильном
cwd вне репозитория с `_certified_subprocess_env()`, без proxy/routing/loader
переменных. Repo-local, symlink и PATH-шимы не исполняются.

Владелец явно принял риск двух `declared` локальных CLI. Исключение разрешено
только для пары адаптеров `codex-companion` + `cursor-cli` и семейств
`openai` + `xai`. Для всех остальных панелей по-прежнему требуется хотя бы один
`verified`. Cursor certification record получает report после двух полных
прогонов корпуса; статус повышается отдельной reviewed-правкой.

## Сценарии / BSAC / EARS

| ID | Сценарий | Обязательный результат |
|---|---|---|
| C1 | Cursor candidate или отсутствует; Claude certified | Codex+Claude, прежнее поведение |
| C2 | Cursor и Claude certified | Codex+Cursor, детерминированно и без env-выбора |
| C3 | Cursor certified, Claude отсутствует/недоступен на пути запуска | Codex+Cursor; outage Cursor — блок, не одиночный Codex |
| C4 | Cursor candidate с бинарем на машине | Не входит в portable panel |
| C5 | Повтор семейства или unknown в любой панели | Блок |
| C6 | Codex+Cursor, оба `declared`, точные разрешённые адаптеры | Допустимо по решению владельца |
| C7 | Любая другая панель без `verified` | Блок |
| C8 | `REVIEW_PROVIDER=codex|cursor|both` на deploy-пути | Блок, как прежде |
| C9 | Cursor certification: каждый кейс ×2, одна requested/actual модель | Полный report; только затем отдельная promotion-правка |
| C10 | Cursor certification: падение кейса, malformed JSON/verdict, разные actual models | Report `pass=false`, promotion запрещён |
| C11 | Production Cursor вызван с certification model | Тот же CLI/prompt/timeout/normalization, что в runner |
| C12 | Gemini profile | Добавляет Gemini к выбранной паре, не заменяет её |
| C13 | Hostile `PATH/HOME/TMPDIR/CURSOR_*`/proxy, repo-local или symlink-шим | Шим не исполняется; запуск блокируется или идёт только доверенный binary |
| C14 | Wrong adapter/family; неверная базовая пара + certified Gemini | Блок до добавления Gemini |
| C15 | Две certified Cursor blocking-записи, включая одну модель | Блок без fallback и выбора по порядку JSON |

EARS:

- **EARS-1 (C1,C2):** WHEN portable plan строится, система SHALL выбрать
  Codex+Cursor при certified Cursor, иначе Codex+Claude при certified Claude.
- **EARS-2 (C3,C4,C8):** IF обязательный reviewer не certified или недоступен,
  THEN система SHALL остановить review без понижения панели.
- **EARS-3 (C5):** IF семейства панели повторяются/unknown, THEN система SHALL
  отклонить панель.
- **EARS-4 (C6,C7):** WHERE оба члена `declared`, система SHALL допустить только
  точную пару Codex companion + Cursor CLI с OpenAI+xAI.
- **EARS-5 (C9,C10):** WHEN Cursor сертифицируется, runner SHALL выполнить каждый
  обязательный кейс минимум дважды тем же production-адаптером и записать одну
  actual model во всех строках.
- **EARS-6 (C11):** WHEN production вызывает Cursor, система SHALL использовать
  requested model из выбранной certification record, а не env.
- **EARS-7 (C12):** WHEN выбран Gemini profile, система SHALL добавить Gemini к
  обязательной паре.
- **EARS-8 (C13):** WHERE Cursor работает как blocking reviewer, система SHALL
  использовать trusted binary/cwd/env и `_diff_text`, исключая repo/PATH-шимы.
- **EARS-9 (C14):** WHEN базовая пара имеет неправильные provider/adapter/family,
  THEN система SHALL отклонить её до добавления Gemini.
- **EARS-10 (C15):** IF certified Cursor blocking-записей больше одной, THEN
  система SHALL остановить резолв без fallback.

Покрытие измерений: certified/candidate/ambiguous/missing × Cursor/Claude/Gemini;
declared/verified × разрешённые/чужие адаптеры; уникальные/повторные семейства;
valid/invalid/outage адаптера; trusted/hostile binary, cwd, env и источник diff.
Runtime fallback после частичного раунда намеренно не реализуется.
