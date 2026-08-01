# Универсальная архитектура gates: host-adapters × reviewer-adapters

Дата: 2026-07-30. Статус: рабочая спека для реализации.

## 1. Цель и граница

Один дистрибутив `gates` должен одинаково обеспечивать G1, лесенку и deploy gate в Claude Code
и Codex. Наличие Cursor не является зависимостью установки. При этом универсальность не может
означать тихое понижение до саморевью: если независимого ревьюера нет, deploy блокируется с
конкретной инструкцией настройки.

Две оси независимы:

- **host-adapter** знает wire-формат Claude Code или Codex (`Edit`/`Write` против
  `apply_patch`, session payload, trust/onboarding);
- **reviewer-adapter** запускает конкретный ревью-движок и приводит ответ к одному строгому
  контракту (`codex`, `gemini`, опциональные `cursor`/`xai`, в будущем сертифицированный local).

Общий core не должен ветвиться по хосту после нормализации события или результата ревью.

## 2. Неподвижные инварианты

1. Claude-модель не может быть blocking-ревьюером этого плагина. `auto` со скрытой
   маршрутизацией запрещён.
2. Семейство blocking-ревьюера не входит в консервативное множество возможных авторов
   `{anthropic, openai}`, пока происхождение каждого изменения не доказано доверенным способом.
   Следовательно, универсальный portable-режим допускает Google/xAI/сертифицированное третье
   семейство, но не Codex и не Claude.
3. Наблюдение `model` в hook payload может **ужесточить** выбор, но repo-local файл с этим
   наблюдением не может разрешить модель. `.claude/.host-session` не является trust boundary.
4. Никакой reviewer command не берётся строкой из `.codex-gate.yaml`. Встроенный adapter
   строит фиксированный argv; путь к бинарю резолвится кодом и пишется в аудит после редакции.
5. Неизвестный provider/model/family, несовпадение фактической модели с запросом, outage,
   timeout или schema drift означают блок.
6. Выбор backend, фактическая модель, семейство и версия certification policy входят в cache key
   и deploy verdict. Смена любого поля инвалидирует чистый кэш.
7. Отсутствующий или битый repo config сохраняет строгие дефолты. Регистрация hooks,
   manifests и marketplace — жёсткие код-пути.
8. Любой skip остаётся аудируемым независимо от рабочей копии.
9. Каждый deploy-review имеет две разные обязательные роли:
   - `blocking` — только сертифицированная non-Anthropic модель независимого семейства;
   - `supplemental` — Claude обязан вернуть валидный review artifact, но его verdict/findings
     никогда не являются blocking-решением, не заменяют blocking reviewer и не могут снять его
     findings. Отсутствие supplemental artifact означает незавершённый review-run, а не
     разрешение от Claude.

## 3. Компоненты

```text
Claude Code event ─ Claude host-adapter ─┐
                                        ├─ normalized event ─ gates core
Codex event ────── Codex host-adapter ──┘

gates core ─ reviewer resolver ─ codex adapter
                             ├─ gemini adapter
                             ├─ cursor adapter (legacy only until actual-model attestation)
                             ├─ xai adapter (optional)
                             └─ local adapter (future, certified models only)
```

### 3.1 Нормализованное hook-событие

Внутренний тип:

```text
HostEvent {
  host: claude|codex
  event: PreToolUse|SessionStart|...
  session_id: non-empty|string-unknown
  model: raw model|string-unknown
  tool: edit|shell|other
  paths: [repo-relative normalized paths]
  command: present only for shell policy
}
```

Codex adapter разбирает полный `apply_patch` envelope: `Add File`, `Update File`,
`Delete File`, `Move to`. Несколько путей сохраняются все. Для известного `apply_patch`
неполный envelope, отсутствующий абсолютный `cwd` или отсутствие path headers — fail-closed.
Не-онбордженный проект остаётся no-op.

### 3.2 Reviewer protocol

Каждый adapter возвращает:

```text
ReviewerRun {
  role: blocking|supplemental
  provider
  requested_model
  actual_models[]
  family
  certification_id
  status: ok|unavailable|timeout|invalid
  verdict
  findings[]
  usage
}
```

Core принимает только `status=ok`, полную схему verdict и непустой набор фактических моделей.
Все `actual_models` должны принадлежать одному разрешённому семейству и находиться в shipped
certification registry. Текст ошибки, findings и provider output проходят существующие
chokepoint'ы редакции секретов.

Blocking union и convergence строятся только из `role=blocking`. Claude supplemental findings
пишутся в audit/deploy verdict и показываются оператору как advisory независимо от присланной
Claude severity. Кэш валиден только при совпадении обеих ролей, моделей и certification ids:
старый blocking verdict без обязательного Claude artifact не удовлетворяет новый run.

### 3.3 Capability resolver

Resolver проверяет adapters в детерминированном порядке, но не использует provider-side
`auto`:

1. бинарь/endpoint доступен;
2. авторизация действительно работает через безопасный probe;
3. фактическая модель входит в certification registry;
4. семейство независимо от консервативного author set и от других blocking-ревьюеров.

Профили:

- `portable` — один сертифицированный reviewer третьего семейства плюс все детерминированные
  gates; дефолт;
- `gemini` — явный alias portable-панели, требующий именно direct Gemini;
- `strong` — зарезервирован для двух сертифицированных reviewer разных третьих семейств и
  сейчас явно блокируется как не реализованный. Offline/local adapter — будущий профиль, а не
  принимаемое runtime-значение.

Cursor — legacy adapter, не зависимость и не portable candidate, пока его CLI не аттестует
actual model; одна лишь смена registry status не может включить его в portable resolver.
Первый кандидат без Cursor — `gemini` в headless
режиме через прямой Gemini `generateContent` HTTPS API: ключ берётся только из
`GOOGLE_API_KEY`/`GEMINI_API_KEY`, передаётся заголовком `x-goog-api-key` и никогда не попадает
в URL, argv, audit или operator output. Ответ обязан содержать точный сертифицированный
`modelVersion`. Для thinking-only Gemini 2.5 Pro adapter ограничивает thinking budget
16 384 токенами и оставляет поддерживаемый output window 65 536, чтобы reasoning не съедал
место строгого verdict. `xai` может работать напрямую через API adapter, не требуя Cursor. Codex adapter
остаётся полезен в legacy Claude-only контуре и для отдельных advisory-прогонов, но в portable
deploy gate не считается независимым, пока доверенный provenance не спроектирован.

Если подходящего reviewer нет, сообщение перечисляет только реально поддерживаемые варианты
настройки и завершает deploy с кодом 2. Автоматического перехода к Codex/Claude нет.
Claude supplemental запускается отдельным фиксированным read-only adapter после выбора blocking
панели; его наличие не делает Claude кандидатом resolver'а.

**Живой wire-smoke 31.07.2026.** `claude -p --output-format json --tools Read,Glob,Grep
--model opus --no-session-persistence` отработал через тот же adapter: envelope содержал
`is_error=false`, валидный строгий verdict и единственный ключ `modelUsage=claude-opus-5`.
Certification `claude-opus-5-supplemental-20260731` относится только к supplemental-роли и
ничего не утверждает о независимости Claude как blocking reviewer.

## 4. Certification вместо веры в бренд

Модель допускается в blocking allow-list только записью в shipped registry:

```text
(adapter, exact model or exact actual-model set, family, certification_id)
```

Certification suite содержит:

- реальные critical/high находки из истории `claude-gates`;
- мутации fail-open путей, config weakening и shim/plugin drift;
- shell/path/deserialization/security случаи;
- verdict schema drift, refusal, quota, timeout и пустой ответ;
- секреты во входе и проверку отсутствия секрета в audit/operator output;
- benign-диффы для контроля систематического пере-блока;
- большие и многокомпонентные диффы;
- повторные прогоны для оценки нестабильности.

Evidence разделено на два слоя. Живой model-corpus обязан содержать все категории
`fail-open/config-weakening/command-security/reviewer-independence/benign/secret-handling/
outage/schema-drift/large-multifile/prompt-injection`, а runner не выдаёт certification-pass
при отсутствии любой категории и требует минимум два повтора. Детерминированные отказы
adapter'а (HTTP error, timeout, malformed envelope, actual-model mismatch и редакция
диагностик) проверяются fault-injection pytest-матрицей; corpus-кейс `outage` проверяет,
заметит ли модель внесённую в код fail-open трактовку outage, а не пытается искусственно
вызвать quota у живого API.

Критические обязательные фикстуры должны находиться во всех certification-прогонах.
Порог advisory/benign-находок задаётся версией policy. Результат suite и ручная проверка
ложных утверждений публикуются рядом с registry. Новая модель, новый slug, новый
provider-side routing или обновление policy требуют новой certification_id.

Runtime не запускает certification suite на deploy: он только сверяет фактическую модель с
уже поставленным registry. Canary job периодически перепроверяет модели и готовит изменение
registry обычным review-путём.

## 5. Session bridge

Общий repo-wide файл «текущая сессия» запрещён: он подделываем, протухает и создаёт гонку.

Порядок исследования/реализации:

1. измерить, поддерживает ли Codex безопасный `updatedInput`/добавление аргумента из
   `PreToolUse`;
2. если да — host-adapter передаёт session id в точную shell-команду после строгой валидации;
3. если нет — одноразовый nonce, присутствующий и в shell argv, и в hook payload; hook создаёт
   per-nonce handoff, CLI атомарно потребляет его;
4. handoff используется только для сессионного G1/audit UX и никогда не разрешает reviewer
   или deploy.

Параллельные сессии не читают общий mutable «current» state.

## 6. BSAC / приёмка

**S1 — Claude Code, обычный Edit.** Host-adapter извлекает `file_path`; код без маркера
блокируется, документация проходит.

**S2 — Codex, apply_patch.** Add/Update/Delete/Move и смешанный multi-file patch блокируются,
если хотя бы один нормализованный путь кодовый. Полностью не-кодовый patch проходит.

**S3 — schema drift apply_patch.** Известный tool с битым envelope блокируется, а не считается
не-кодовым.

**S4 — проект не онборджен.** Оба host-adapter выходят 0 вне git или без
`.codex-gate.yaml` в worktree-OR-HEAD.

**S5 — пользователь без Cursor, Gemini доступен и сертифицирован.** Portable resolver выбирает
Gemini, пишет provider/model/certification в verdict, обязательный Claude supplemental даёт
отдельный artifact, deploy работает.

**S6 — Cursor установлен.** Cursor `auto` блокируется. Его JSON CLI не аттестует actual model,
поэтому requested slug недостаточен: adapter остаётся legacy и код resolver'а не включает его
в portable blocking panel даже при ошибочной смене registry status на `certified`.

**S7 — нет независимого reviewer.** Deploy блокируется с инструкцией настройки Gemini/xAI/local;
Codex не ревьюит Codex молча.

**S8 — mixed/unknown authoring.** Консервативный author set `{anthropic, openai}`; допустимо
только третье семейство.

**S9 — repo config просит Claude, Codex или произвольную команду.** Ослабление игнорируется либо
блокируется; конфиг не расширяет shipped registry.

**S10 — фактическая модель отличается от requested или provider использовал дополнительную
неизвестную модель.** Ответ невалиден, deploy блокируется.

**S11 — outage/timeout/malformed result одного из обязательных reviewers.** Блок без деградации
до меньшей панели.

**S11a — Claude supplemental вернул high/critical.** Находка видна в audit/verdict и operator
output как supplemental advisory, но не становится blocking verdict и не может заменить/снять
решение Gemini/xAI.

**S11b — Claude supplemental отсутствует/сломался.** Review-run незавершён и deploy
fail-closed; сообщение явно говорит об отсутствии обязательного artifact, а не утверждает,
что Claude является независимым blocking reviewer.

**S12 — кэш от другой модели/certification/panel.** Кэш не принимается, выполняется новое
ревью.

**S13 — две параллельные Codex-сессии.** Marker/mark/audit не получают session id соседа.

**S14 — hooks config/manifests удалены или ослаблены.** Это код-пути; commit/deploy chain
видит изменение. Отсутствующий установленный plugin у шима даёт громкий fail-closed.

**S15 — LADDER_SKIP после clone/rebase.** Переносимое evidence сохраняет тип skip и его
аудируемость; обычный pass trailer не маскирует skip.

**S16 — Codex plugin установлен, но hooks не обнаружены или не trusted.** Онбординг не считается
успешным только по наличию manifest: обязательный smoke test запускает настоящий `apply_patch`
код-пути без маркера и наблюдает exit 2 с причиной. Если hook не вызван, plugin root alias
не выставлен, trust не подтверждён или headless-процесс завис, onboarding/CI завершается
ошибкой (headless обёрнут внешним timeout), а не сообщает «gates активен».

## 7. EARS

- **E1.** THE plugin SHALL use one gates core with separate host and reviewer adapters.
- **E2.** WHEN Codex sends `apply_patch`, THE Codex host-adapter SHALL extract every source and
  destination path and SHALL block on malformed known envelopes.
- **E3.** THE reviewer resolver SHALL select only shipped, certified, independent exact models.
- **E4.** THE resolver SHALL NOT require Cursor and SHALL NOT use provider-side `auto`.
- **E5.** IF no independent certified reviewer is available, THEN deploy SHALL fail closed with
  setup guidance.
- **E6.** Repo config SHALL NOT add commands, models or families to the blocking allow-list.
- **E7.** Actual provider/model set and certification id SHALL be audited, cached and emitted in
  the deploy verdict.
- **E8.** A hook-observed model or session stored in repo-local state SHALL NOT relax deploy or
  reviewer policy.
- **E9.** Concurrent sessions SHALL NOT share a mutable current-session record.
- **E10.** Every skip SHALL remain visible after clone/rebase.
- **E11.** Codex onboarding SHALL prove hook attachment and trust with a real blocking
  `apply_patch` smoke test; manifest presence alone SHALL NOT count as activation.
- **E12.** Every deploy review SHALL contain at least one certified independent non-Anthropic
  `blocking` run and one certified Claude `supplemental` run.
- **E13.** Claude supplemental verdict/findings SHALL NOT participate in the blocking union,
  SHALL NOT replace a blocking run and SHALL NOT weaken blocking findings.
- **E14.** Gemini credentials SHALL be sent only in the `x-goog-api-key` header and SHALL NOT
  appear in URL, argv, audit, verdict or diagnostics.

## 8. Порядок реализации

1. Codex `apply_patch` parser + matcher + subprocess smoke test.
2. Добавить только валидируемый packaging scaffold и жёсткие control-plane пути; marketplace
   держать `NOT_AVAILABLE`, пока пункты 3–6 не завершены.
3. Выделить существующие Codex/Cursor вызовы за единый `ReviewerRun` protocol без смены
   поведения.
4. Добавить Gemini adapter и capability probe; Cursor оставить optional.
5. Создать certification corpus/runner/registry и перевести resolver на certified models.
6. Перевести universal default в `portable`; отсутствие третьего семейства блокирует.
7. **Выполнено 01.08.2026 на Codex CLI 0.146.0:** plugin-delivered PreToolUse hook установлен
   из `lenar-gates`, реальный `apply_patch` code-path заблокирован с `exit 2`, причина дошла до
   модели, sentinel остался неизменным. После smoke marketplace переведён в `AVAILABLE`,
   команды установки опубликованы. Для автоматизированного прогона использован
   `--dangerously-bypass-hook-trust` только после проверки источника установленного hook.
8. Исследовать и реализовать session bridge без `.host-session`.
9. Отдельной спекой реализовать переносимые typed ladder evidence.
