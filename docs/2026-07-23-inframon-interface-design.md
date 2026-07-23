# Локальная Фаза 2: интерфейс к внешнему guard'у (inframon) — дизайн

**Дата:** 2026-07-23. **Статус:** design (на Codex-ревью). **Tier:** 2 (baseline имеет власть
над деплой-актуатором: неверный baseline → ревьюится не тот диапазон).

## Скоуп-граница (решение пользователя, 2026-07-23)

Плагин gates решает ТОЛЬКО локальные задачи. Негеймибельность, branch protection, immutable-
артефакт/GHCR, серверный деплой/автооткат — проект **inframon** (вместе с мониторингом). Здесь
— только ЛОКАЛЬНАЯ сторона интерфейса между ними. Две фичи:

### Ф1. Authoritative baseline: `deploy.baseline_command`

Остаток R1-2/ML2 источника: `.claude/.last-deployed-sha` локален и врёт при кросс-машинном
деплое → ревьюится не тот диапазон (fail-open: часть диффа не ревьюится). Фикс: конфиг
```yaml
deploy:
  baseline_command: "ssh host cat /app/.deployed-sha"   # или inframon CLI
  baseline_timeout_s: 30                                 # опц., дефолт 30
```
`resolve_baseline` приоритет: (1) env `CODEX_DEPLOY_BASELINE` (явный оператор, аварийный) →
(2) `baseline_command` (authoritative внешний) → (3) локальный `.last-deployed-sha` (легаси,
только если command НЕ настроен). **Ключ (анти-fail-open): настроенный command упал/мусор/
таймаут → БЛОК, НЕ фолбэк на локальный файл** — иначе отказ authoritative-источника молча
деградирует ровно в ту протухшую локальность, которую чиним. Вывод команды валидируется:
strip → полный 40-hex SHA, иначе невалиден. Исполнение как argv (shlex, без shell) — та же
граница доверия, что `empirical.test_command` (конфиг закоммичен, clean-tree первым шагом,
жёсткий код-путь). Конфиг читается SHA-bound от `head_before` (generic-refactor читателя
`_empirical_config` → `_config_section_at_ref(ref, section)` — та же трёхстатусная семантика:
`unreadable` → блок; `absent` → легаси-поведение (файл)).

Env-override при настроенном command — аудит-строка (оператор перебил authoritative источник:
видимо, аварийно; след обязателен).

**Base-vs-head для секции `deploy` (R1-F1).** Изменённая/активированная/удалённая
`baseline_command` может вернуть свежий SHA (вплоть до HEAD) → диапазон `baseline..HEAD`
сузится/опустеет → сама правка команды НЕ ревьюится (self-hiding, без злого умысла — напр.
staging вместо prod). Правило: секция `deploy` на head_before сравнивается с baseline (тот же
SHA-bound механизм); ЛЮБОЕ отличие (absent→enabled, смена command, enabled→absent) → **БЛОК**
с инструкцией «переходный деплой — задай `CODEX_DEPLOY_BASELINE` явно» (env-путь уже
аудируется, B7). После перехода (command в baseline == head) — обычная работа.

### Ф2. Экспорт вердикта деплой-гейта (контракт для inframon)

При КАЖДОМ allow-выходе `check_reviewed_cli` пишется машиночитаемый вердикт
`logs/review_verdicts/<head_sha>.json` (env-override `CODEX_VERDICT_DIR` — тесты):
```json
{"schema": 1, "head_sha": "...", "baseline_sha": "...", "diff_sha256": "...",
 "gates": {"ladder": "covered|skipped", "empirical": "pass|skipped|not-configured",
           "codex": "allow|cached|skipped"},
 "ts": "..."}
```
Скипы НЕ прячутся — вердикт прямо говорит и о ТЕКУЩИХ скипах, и об ИСТОРИЧЕСКИХ обходах
диапазона (R1-F3): `gates.ladder ∈ {covered, covered-with-skips, skipped}` — `covered-with-skips`,
когда в `baseline..HEAD` есть ledger-записи `skipped=true` (новая `range_skips()` в
ladder_gate). Inframon сам решает, доверять ли (его fail-closed — его сторона).

**Delete-then-write (R1-F2):** перед записью нового вердикта старый файл этого head СНАЧАЛА
удаляется, потом пишется новый (`_atomic_write_json`). Провал ЗАПИСИ после успешного unlink →
вердикта НЕТ → consumer fail-closed сработает честно (громкий warning, allow стоит). Провал
UNLINK при существующем старом файле → **БЛОК** (нельзя ни опубликовать честный вердикт, ни
убрать вводящий в заблуждение — старый «чистый» вердикт того же head замаскировал бы скипы
текущего прогона). Иначе — producer best-effort + loud, consumer fail-closed на отсутствие:
правильное распределение по границе. Задокументировано как контрактное решение.

Контракт для inframon: сверять `head_sha`/`diff_sha256` с фактически деплоящимся состоянием,
трактовать отсутствие вердикта/скипы по своей политике.

## Scenario-first (ТЕКСТ до кода)

Измерения:
- **D1 источник baseline:** {env, command, локальный файл, ничего}
- **D2 результат command:** {валидный 40-hex, мусор/пусто, exit≠0, не запустилась, таймаут}
- **D3 env+command одновременно:** {только env, только command, оба}
- **D4 конфиг-секция `deploy` на head_before:** {absent, enabled, unreadable}
- **D5 allow-путь для вердикта:** {fresh-ревью, кэш, скипы}
- **D6 запись вердикта:** {ок, OSError}

Сценарии:

| # | Условие | Ожидание |
|---|---|---|
| B1 | env задан (command не настроен) | env используется (существующее) |
| B2 | command настроен, вернул валидный SHA | этот SHA = baseline (лок. файл игнорируется) |
| B3 | command настроен, exit≠0 / не запустилась / таймаут | **БЛОК** — НЕ фолбэк на файл |
| B4 | command настроен, вывод мусор/пусто/не-40-hex | **БЛОК** |
| B5 | command НЕ настроен (absent) | легаси: локальный файл (существующее) |
| B6 | ничего нет | None → блок (существующий R1-2) |
| B7 | env И command оба | env выигрывает + АУДИТ-строка (аварийный перебив authoritative) |
| B8 | секция deploy unreadable (битый конфиг на head_before) | **БЛОК** (unreadable ≠ absent, как эмпирика) |
| B9 | секция deploy отличается base↔head (активация/смена/удаление command) | **БЛОК** — переходный деплой только через явный `CODEX_DEPLOY_BASELINE` (R1-F1) |
| V1 | allow (fresh-ревью) | вердикт записан: gates.codex=allow, ladder/empirical по факту |
| V2 | allow (кэш чистого ревью) | вердикт записан: gates.codex=cached |
| V3 | allow (три скипа) | вердикт записан: все gates=skipped — скипы видимы |
| V4 | блок (любой) | вердикт НЕ пишется для этого прогона |
| V5 | ЗАПИСЬ вердикта OSError (после unlink) | ГРОМКИЙ warning, allow стоит; старого файла НЕТ (consumer fail-closed честен) |
| V5b | UNLINK старого вердикта упал, файл существует | **БЛОК** — старый чистый вердикт маскировал бы скипы (R1-F2) |
| V6 | `CODEX_VERDICT_DIR` задан | вердикт в него (изоляция тестов) |
| V7 | в диапазоне есть исторические skipped-ledger-записи | `gates.ladder = covered-with-skips` (R1-F3) |

Полнота: D1–D6 покрыты; ключевые — no-fallback (B3/B4), unreadable≠absent (B8), base-vs-head
секции deploy (B9), env-аудит (B7), скипы видимы включая исторические (V3/V7), delete-then-write
(V5/V5b).

Вне scope (граница): проверка вердикта, политика доверия к скипам, серверное состояние,
негеймибельность — inframon. Подпись/подделка вердикта — локальный файл, тот же класс, что
ручная правка маркера (осознанный обход); криптоподпись — сторона inframon при необходимости.

## EARS

- **EARS-1:** WHEN `deploy.baseline_command` настроен И вернул полный 40-hex SHA (после strip)
  — THEN `resolve_baseline` SHALL вернуть его, игнорируя локальный файл. (B2)
- **EARS-2:** IF command настроен, но упал/таймаут/невалидный вывод — THEN гейт SHALL
  заблокировать деплой; фолбэк на локальный файл ЗАПРЕЩЁН. (B3, B4)
- **EARS-3:** WHERE env `CODEX_DEPLOY_BASELINE` задан — он SHALL выигрывать; IF при этом
  command настроен — THEN SHALL писаться аудит-строка. (B1, B7)
- **EARS-4:** Секция `deploy` SHALL читаться SHA-bound (head_before) трёхстатусно; `unreadable`
  → блок; `absent` на ОБОИХ SHA → легаси-поведение. (B5, B8)
- **EARS-4b:** IF секция `deploy` на head_before отличается от baseline (активация/смена/
  удаление) — THEN гейт SHALL заблокировать деплой; переход — только через явный
  `CODEX_DEPLOY_BASELINE` (аудируемый). (B9)
- **EARS-5:** WHEN `check_reviewed_cli` возвращает allow — THEN SHALL быть записан вердикт
  (schema 1) с фактическими статусами гейтов, включая текущие скипы И исторические
  (`covered-with-skips` при skipped-записях диапазона, через `range_skips()`). (V1–V3, V7)
- **EARS-6:** Перед записью SHALL удаляться старый вердикт этого head (delete-then-write);
  IF unlink упал при существующем файле — THEN БЛОК; IF запись упала после unlink — THEN
  громкий warning, allow стоит (файла нет → consumer fail-closed честен). (V5, V5b)
- **EARS-7:** Команда SHALL исполняться как argv (shlex, без shell), с таймаутом
  (`baseline_timeout_s`, дефолт 30, валидация как `timeout_s` эмпирики).

## BSAC

- **BS-I1:** кросс-машинный деплой с настроенным authoritative-источником ревьюит верный
  диапазон; отказ источника не деградирует молча. (B2, B3)
- **BS-I2:** inframon получает проверяемый контракт (вердикт с head/diff/статусами/скипами). (V1–V3)
- **ML-I1** отказ command → фолбэк на протухший файл → не тот диапазон ревьюится. → EARS-2
  no-fallback (блок).
- **ML-I2 [R1-F1]** смена/активация command сузила диапазон и спрятала саму себя (изменённая
  команда возвращает HEAD → пустой дифф → правка не ревьюится). → base-vs-head секции deploy:
  любое отличие → блок; переход только через явный аудируемый `CODEX_DEPLOY_BASELINE` (EARS-4b).
  Прежний аргумент «ancestry-проверка достаточна» ОПРОВЕРГНУТ Codex-ревью (negative result).
  Остаток: злонамеренный оператор — вне охвата.
- **ML-I3** вердикт со скипами выглядит как «проверено». → скипы — явные поля; политика
  доверия — сторона inframon.

## Отвергнутые альтернативы

- **Блокировать деплой при провале записи вердикта:** брикует деплой ради артефакта без
  потребителя; consumer-side fail-closed правильнее по границе.
- **Криптоподпись вердикта:** локальный секрет у того же актёра = театр; реальная
  верификация — пересборка/сверка на стороне inframon.
- ~~«base-vs-head не нужен: вывод валидируется ancestry»~~ — ОТВЕРГНУТО Codex R1 (negative
  result): изменённая команда возвращает свежий SHA → диапазон пуст → правка себя прячет;
  ancestry этого не ловит. base-vs-head ПРИНЯТ (EARS-4b).
- **Фолбэк на локальный файл при отказе command с warning:** ровно тот fail-open, ради
  которого фича существует.

## Тестирование

Юнит: B1–B9 через `resolve_baseline`/`check_reviewed_cli` (command-стабы: echo sha / exit 1 /
sleep / мусор); V1–V7 через `check_reviewed_cli` (fresh/кэш/скипы; monkeypatch OSError записи И unlink;
исторические skipped-записи диапазона → covered-with-skips).
Generic-refactor читателя секций: существующие empirical-тесты остаются зелёными (регресс).
Реальный маршрут: вердикт-файл существует и валиден как JSON со schema=1 после allow.
