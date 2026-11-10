# Kimi Code как третий host плагина gates

Дата: 2026-11-01. Ярус: `decision`.

## Проблема

Плагин должен давать один и тот же AI SDLC на Claude Code, Codex и Kimi Code:
дизайн с бизнес-сценариями и альтернативами → независимое дизайн-ревью → код →
simplify → security → независимое code-review → commit/CI с верхнеуровневыми проверками.

Сейчас установка под Kimi невозможна, а четырёх существующих механизмов недостаточно:

1. Нет Kimi-манифеста; `hooks/hooks.json` сам по себе Kimi не загружает.
2. `gates-run` резолвит только `~/.claude/plugins/...`, поэтому на Kimi-only машине
   pre-commit/pre-push остаются fail-closed даже после корректной установки.
3. `write-marker` получает сессию только из `CLAUDE_*`. Kimi передаёт `session_id` в stdin
   hook, но не экспонирует его Bash-команде. Маркер попадёт в `nosession`, а hook будет
   искать маркер реальной сессии: G1 невозможно разблокировать.
4. Лесенка ссылается на `/simplify`, `/code-review`, `/security-review`, которых плагин
   для Kimi не поставляет. Просто напечатать эти имена — значит предложить несуществующий
   маршрут; заменить независимое code-review самочтением Kimi — потерять основной инвариант.

Цена бездействия: Kimi либо не работает с плагином вообще, либо получает видимость
workflow без обязательной независимой проверки и без работающих git-гейтов.

## Измеренные факты и ограничения

Замеры и документация на 2026-11-01, Kimi Code CLI 2.4.1:

- Официальный формат: `.kimi-plugin/plugin.json` (или `kimi.plugin.json`), поля `skills`,
  `commands`, `hooks`; plugin hooks получают `KIMI_CODE_HOME` и `KIMI_PLUGIN_ROOT`.
  Источник: https://www.kimi.com/code/docs/en/kimi-code-cli/customization/plugins.html
- `PreToolUse` принимает stdin JSON со `session_id`, `cwd`, `tool_name`, `tool_input`;
  exit 2 + stderr блокирует. `SessionStart` доступен. Матчеры — regex по имени инструмента.
  Источник: https://www.kimi.com/code/docs/en/kimi-code-cli/customization/hooks.html
- Kimi поддерживает plugin slash commands `/gates:<name>` и Skills. Значит проходы можно
  поставить самим плагином, а не требовать несуществующие builtin-команды.
- Установленные плагины копируются в `$KIMI_CODE_HOME/plugins/managed/<id>/`; записи
  `installed.json` хранят `id`, `root`, `enabled`. Схема подтверждена чтением установленного
  CLI 2.4.1 (`plugin/store.ts`, `plugin/manager.ts` в собранном `main.mjs`): активный hook
  берётся только при `enabled && state == ok`.
- В текущей Kimi-сессии `KIMI_CODE_SESSION_ID`, `KIMI_SESSION_ID`,
  `CLAUDE_CODE_SESSION_ID` не выставлены. Полагаться на environment нельзя.
- Отрицательный результат probe: `kimi -p "/plugins install …"` с изолированным
  `KIMI_CODE_HOME` и фиктивным provider не выполнил control-команду, а ушёл в model run.
  Поэтому автоматический установщик/деплой на этом недокументированном поведении не строим.
- Kimi hooks fail-open при собственной ошибке/таймауте процесса. Наши обработчики должны
  возвращать 2 при распознанном schema drift; это не меняет общий by-design статус G1
  best-effort. Денежная граница остаётся fail-closed git/deploy-цепочкой.

## Решения и альтернативы

### Р1. Один дистрибутив и один gates core

- **Оставить как есть.** Не решает задачу ни одного из четырёх блокеров.
- **Отдельный форк для Kimi.** Позволил бы быстро поменять UX, но дизайн-гейт, лесенка,
  convergence и security-правила немедленно получили бы две версии. Отвергнуто: расхождение
  семантики хуже небольшой дубликации JSON.
- **Один плагин с `.kimi-plugin/plugin.json`, общими scripts/skills/templates. Выбрано.**
  Kimi-манифест объявляет те же события через собственную схему и вызывает существующие
  `gate-edit`, `gate-bash`, `clear-marker`. Общий core не ветвится по host после чтения
  стандартного payload.

Критерии: одна политика гейтов; отсутствие изменений Claude/Codex wire-поведения; минимум
host-specific кода. Пересмотреть, если Kimi изменит wire-контракт так, что общая
нормализация перестанет быть возможной.

### Р2. Регистрация hooks и защита control-plane

- **Только глобальная запись в `~/.kimi-code/config.toml`.** Не выбрано: мутирует пользовательский
  конфиг, не переносится вместе с плагином и требует отдельного lifecycle.
- **Только native managed manifest.** Отвергнуто после trust-root review: manifest и launcher
  проверяли сами себя; повреждение/удаление manifest могло молча снять hook.
- **Одинаковые native+global gate-owned команды. Выбрано.** В manifest попадают две PreToolUse
  записи (file-mutator matcher и `Bash`) и SessionStart; `/gates-init` добавляет те же строки в
  `~/.kimi-code/config.toml`. Kimi запускает одинаковый `command` только один раз, поэтому
  double-handoff нет. Обе регистрации вызывают `/usr/bin/python3` с OS-account trampoline:
  home вычисляется через `getpwuid`, digest `native_hook.py` проверяется перед exec того же
  buffer; `${HOME}`/`${KIMI_PLUGIN_ROOT}` не участвуют. Dispatcher сначала проверяет opt-in по
  `cwd`/worktree-or-HEAD и возвращает 0 вне scope, затем использует pinned resolver `kimi`;
  disabled/absent/broken installation завершается exit 2 только внутри онбордженного repo.
  Повреждение managed manifest не отключает глобальный trust root.
  Matcher'ы должны быть семантически равны существующим; структурный тест сравнивает оба
  описания, чтобы следующая правка matcher'а не оставила Kimi без защиты.
- `.kimi-plugin/` добавляется в `HARD_CODE_PATH_COMPONENTS`, а `kimi.plugin.json` — в exact
  hard code paths (включая вложенные пути). Оба Kimi manifest-пути включаются в
  `.codex-gate.yaml` репозитория-носителя. Kimi отдаёт приоритет корневому
  `kimi.plugin.json`; resolver отклоняет одновременное наличие обоих вместо проверки только
  неактивного `.kimi-plugin/plugin.json`. Конфиг проекта не может вывести их из-под гейта.

Критерии: отключение plugin запрещает исполнение managed script и не открывает код;
независимый global trust root не исчезает при повреждении manifest; изменение control-plane
видно ревью. Пересмотреть, если Kimi начнёт нативно принимать
`hooks/hooks.json`.

### Р3. Резолв активной установки в git-шиме

- **Только фиксированный `plugins/managed/gates`.** Прост, но после `/plugins disable` или
  удаления записи продолжил бы исполнять отключённый код. Отвергнуто как fail-open.
- **`KIMI_PLUGIN_ROOT` как универсальный override.** В git hook этой переменной нет; разрешить
  её — значит дать вызывающему подменить исполняемый gated-код. Отвергнуто.
- **Требовать одновременно Claude-установку.** Противоречит Kimi-only сценарию.
- **Коммитить canonical home в `.githooks/gates-run`.** Устраняет env override, но абсолютный
  machine-local путь первой машины становился бы авторитетным во всех клонах. Отвергнуто.
- **Произвольный relocated `KIMI_CODE_HOME` с machine-local pin.** Даже 0700 root с valid
  registry/manifest/basename может быть полностью синтезирован вызывающим; ownership и realpath
  не доказывают происхождение executable. Отвергнуто в этой фазе: нет независимого trusted
  provenance, который вызывающий не может выпустить сам.
- **Единый OS-account-rooted resolver для Claude и Kimi. Выбрано.** Сам bootstrap живёт в
  gate-owned `<home>/.local/lib/claude-gates/kimi_install.py` вне worktree и ставится через
  `/gates-init`; `.githooks` содержит только launch shim. Worktree-копия resolver не исполняется:
  прежняя схема позволяла подменить проверяющий код до любой проверки. Bootstrap path и каждый
  его компонент проходят ownership/mode/no-symlink checks до запуска. SHA-256 bootstrap
  закреплён в reviewed `gates-run`; old/new-plugin skew и tampered digest блокируются. Upgrade
  через `/gates-init` атомарен через temp+rename. Digest и исполнение происходят в одном
  Python-процессе над одним прочитанным buffer, поэтому replace между проверкой и exec не может
  подменить исполняемую версию.
  Дом ОБЕИХ установок вычисляется только через `pwd.getpwuid(os.getuid()).pw_dir`; `$HOME`,
  `$KIMI_CODE_HOME`,
  `$KIMI_PLUGIN_ROOT` per-invocation игнорируются. Claude registry/cache принимается только из
  canonical `<home>/.claude/plugins/...`, installPath обязан остаться внутри canonical cache;
  Kimi registry — только `<home>/.kimi-code/plugins/installed.json`, managed root — ровно
  `<home>/.kimi-code/plugins/managed/gates`. Relocated home не поддерживается. К registry,
  install roots, manifests и script ОБОИХ hosts применяются одни ownership/mode/no-symlink/
  confinement/bytecode checks. Fake `$HOME` не может победить ни при active, ни при disabled,
  absent или broken Kimi. Отсутствие обеих установок — прежний fail-closed отказ. Отсутствующий
  Claude registry НЕ активирует stale cache: cache без актуальной registry-записи не исполняется.
  Из массива Kimi `plugins` выбирается ровно одна запись `id=gates`, `enabled is true`;
  disabled, absent, duplicate, malformed, relative/out-of-managed root не исполняются.

**Важное уточнение схемы 2.4.1:** persisted `installed.json` НЕ содержит `state`; Kimi
вычисляет его заново из manifest diagnostics. Поэтому shim не требует несуществующее поле,
а обязан сам получить эквивалент `state=ok`: строго разобрать наш ограниченный manifest,
проверить `name=gates`, все используемые `skills/commands/hooks`, их типы и существование
объявленных regular files/dirs внутри root без symlink. Принимаются только точные `./skills/`
  и `./commands/` и ожидаемые пять commands/три hooks, чтобы Kimi не загрузил непроверенный
  alternate target. Корневой `kimi.plugin.json` приоритетен
у Kimi, поэтому одновременное наличие обоих manifests — отказ. Если необязательное `state`
присутствует, принимается только строка `ok`; failed/installing/quarantined/unknown/nontype —
отказ. Отсутствие state допустимо только после полной локальной валидации.

Registry, managed dirs, manifests и script проверяются на current uid и отсутствие
group/world-write. Script name — ровно один из `codex_review_gate.py`, `ladder_gate.py`,
`prepush_gate.py`, без separators; каждый component проверяется `lstat`, script/manifest/
registry — regular files без symlink, realpath обязан остаться в ожидаемом managed root.
Path traversal и symlink escape блокируются. Проверка → exec имеет прежнюю point-in-time
границу R-DEPLOY-PROOF-IS-POINT-IN-TIME: локальный процесс того же uid может подменить файл
после проверки; постоянный контроль установки здесь не обещается.

При одновременной АКТИВНОЙ установке Claude и Kimi resolver вычисляет delivery digest обеих
копий: отсортированные relative path + owner exec mode + sha256 содержимого для всей поставки
(scripts, manifests, hooks, templates, skills, commands), исключая только НЕисполняемые
installer metadata. `__pycache__`, `*.pyc` и `*.pyo` НЕ исключаются молча, а делают установку
невалидной до запуска: `python3 -B` запрещает запись, но не загрузку bytecode. Расхождение
digest — fail-closed независимо от того, какая копия новее;
Claude-old/Kimi-new и Claude-new/Kimi-old одинаково блокируются. Совпадение digest сохраняет
Claude-приоритет. Enabled, но битая Kimi запись не игнорируется ради Claude fallback; disabled
Kimi в сравнение не входит. Host знать не требуется: выбор делается по identity поставки.

Негативные тесты: полностью синтетический 0700 relocated home не исполняется через env/pin;
fake `$HOME` с поддельной Claude-установкой отклоняется при disabled/absent/broken Kimi;
state drift, manifest precedence, traversal, symlink, writable component и оба направления
version/digest skew блокируются.

Критерии: Kimi-only работает; disabled/removed не исполняется; repo-local подмена невозможна;
coexistence не исполняет устаревший gate; одинаковая поставка сохраняет прежний Claude-путь.

### Р4. Session bridge для write-marker и ladder-аудита

- **Общий файл «текущая Kimi-сессия».** Отвергнуто: подделываем, протухает и смешивает
  параллельные сессии.
- **Читать `KIMI_CODE_SESSION_ID`.** Невозможно: переменной нет в Bash environment.
- **Передавать session id вручную.** Ненадёжный UX; агент не видит значение из hook payload.
- **Одноразовый nonce-handoff через PreToolUse. Выбрано.** Kimi-инструкция вызывает gate CLI
  с `GATES_SESSION_NONCE=<128-bit random>` в environment. Перед Bash `gate-bash` распознаёт
  строго ограниченную форму вызова `gates-run` и nonce, публикует `{session_id, created_at}`
  в per-repo gate-state вне репозитория через create-only `O_CREAT|O_EXCL|O_NOFOLLOW`, 0600.
  Существующий nonce — отказ, а не перезапись: параллельный или повторный publisher не может
  изменить первую session-привязку. Частично записанный после crash файл не валиден для
  потребителя. Порождённый CLI в `_env_session()` атомарно потребляет handoff и использует
  session id из payload. Nonce случайный, одноразовый, короткоживущий (60 секунд); replay,
  неизвестный/expired/malformed nonce не даёт сессии и не разблокирует маркер. Потребление
  consume-first: crash оставляет fail-safe.
- Handoff не является доказательством ревью и не входит в deploy/reviewer resolver. Он только
  связывает CLI-запись с уже существующей hook-сессией. Маркер по-прежнему требует reviewed
  hash, BSAC/альтернативы и квитанцию.
- Общая `_env_session` остаётся chokepoint и используется `codex_review_gate` и
  `ladder_gate`; Claude-путь без nonce не меняется. Nonce-форма документируется в Kimi
  design-review command; существующий Claude UX не переводится на новый механизм.

Критерии: отсутствие общего mutable current-session; параллельные сессии не смешиваются;
невозможность записать маркер в `nosession` по Kimi-инструкции; минимальная поверхность.
Пересмотреть, если Kimi предоставит официальный session env или безопасный `updatedInput`.

### Р5. Настоящие проходы лесенки в Kimi

- **Оставить ссылки на Claude builtin-команды.** Отвергнуто: маршрут не существует.
- **Попросить Kimi самому проверить собственный код и назвать это независимым ревью.**
  Отвергнуто как саморевью.
- **Поставить plugin commands. Выбрано:** `/gates:design-review`, `/gates:simplify`,
  `/gates:security-review`, `/gates:code-review`, `/gates:gates-init`.
  - design-review использует общий Skill и nonce-форму маркера;
  - simplify требует поведенчески нейтральной чистки текущего diff и релевантных тестов;
  - security-review идёт по `AGENTS.md`, безопасным стокам, обходам гейтов и новым веткам;
  - code-review обязан запустить `companion-review --scope working-tree` (Codex) и обработать
    findings по существующей стоп-политике; собственный вердикт Kimi не засчитывается.
- `_PASS_RUNNER` и документация получают явные Kimi-имена наряду со старыми Claude-именами.
  Текст остаётся подсказкой, а не новым evidence: текущая лесенка доказывает порядок и tree-chain,
  не факт прохода. Усиливать evidence в этой задаче не требуется.

Критерии: каждая печатаемая команда существует после установки; code-review действительно
другая модель; нет скрытого изменения ladder-семантики. Пересмотреть, если появится
сертифицированный Kimi-independent reviewer adapter.

### Р6. Blocking reviewer panel

- **Заменить Codex+Claude на Kimi для Kimi-host.** Отвергнуто: это саморевью автора.
- **Добавить Kimi третьим обязательным reviewer.** Требует actual-model attestation, нового
  adapter и живой сертификации; не нужно для работы host и увеличивает стоимость каждого
  деплоя. Отвергнуто в этой задаче.
- **Сохранить сертифицированную пару Codex+Claude. Выбрано.** Для Kimi-authored кода обе
  модели независимы от Moonshot; существующий union и fail-closed outage не меняются.
  Дизайн-ревью также остаётся Codex. Отсутствие reviewer не разрешает fallback на Kimi.

Критерии: независимость и отсутствие новой необязательной credentials-зависимости.
Пересмотреть только через отдельную reviewer-certification спеку.

### Р7. Проверка без новой release/certification-подсистемы

- **Только JSON schema/unit-тесты.** Не доказывают реальный hook, session bridge и git route.
- **Живой model call в каждом pytest.** Отвергнуто: тесты не должны зависеть от внешних
  production-сервисов или тратить квоту.
- **Отдельный release certification runner.** Отвергнуто по стоп-политике после восьми раундов:
  persisted receipt был бы self-attestation; production install нарушал бы изоляцию; container
  потребовал бы новых Kimi+Codex credentials рядом с ещё не сертифицированным candidate.
  Разделение credential plane через несколько identities и broker несоразмерно задаче переноса
  host-adapter. Существующий `deploy_gates.py` не меняется.
- **Isolated `KIMI_CODE_HOME` для live hooks + отдельный resolver E2E.** Отвергнуто после
  сопоставления с production snapshot/restore. Временный home не наследует OAuth/provider state:
  копирование credentials создало бы новый credential plane, а повторный login — ручную и
  неповторяемую приёмку. Кроме того, canonical git resolver намеренно читает pwd-home, а не
  `KIMI_CODE_HOME`, поэтому split acceptance не доказывает единый маршрут managed install →
  hook → git shim. Disposable OS identity дала бы canonical temporary home, но требует
  административного создания отдельной учётной записи и собственного credential plane —
  несоразмерно одноразовой проверке. Поэтому остаётся реальная установка с атомарным journal,
  fail-closed conflict guard и обязательным restore.
- **Официальный `kimi web` Server API для acceptance.** Выбрано для live lifecycle:
  `POST /api/v1/plugins` устанавливает local path, `GET /api/v1/plugins` проверяет state,
  enable/disable/remove и session reload доступны без model prompt. API experimental, поэтому
  runner сначала сверяет `/openapi.json` фактической CLI 2.4.1; несоответствие схемы — отказ.
  `kimi web` запускается под supervisor с lifeline pipe: EOF от runner обнаруживается даже при
  SIGKILL до первой инструкции supervisor и завершает server child; фактический порт разбирается
  из startup banner. До создания Kimi-сессии scratch repo получает `.githooks` ровно из reviewed
  artifact, коммитит их без `LADDER_SKIP` и доказывает runner через `check-precommit`; точный
  digest-pinned bootstrap обязан уже существовать, иначе приёмка останавливается до install.
  После install managed digest и manifest version сверяются с распакованным reviewed artifact;
  та же exact identity повторно проверяется после последнего smoke до отчёта и удаления journal.
  Источник: https://www.kimi.com/code/docs/en/kimi-code-cli/reference/server-api.html
- **Слои проверки. Выбрано:**
  1. детерминированные pytest от сценариев S1–S18, включая subprocess hook и реальные temp git
     repos;
  2. верхнеуровневый E2E: managed Kimi installation fixture → реальный plugin script через
     Kimi-shaped payload → write-marker с handoff → Edit разрешён/заблокирован → ladder chain
     через `gates-run` → commit, включая Claude+Kimi digest parity/skew;
  3. **одноразовая обязательная живая приёмка ЭТОГО изменения** после simplify/security,
     commit и успешного commit-bound `check-reviewed`. Существующий `verify-deployable` собирает
     tar НЕ через `git archive`, а из raw `ls-tree`/`cat-file` отревьюенного HEAD. Перед install
     обязательны `check-decision --head <reviewed-head>` и `check-artifact --sha256 <digest>`;
     post-review drift и hostile `.git/info/attributes` (`export-ignore`/`export-subst`) блокируются.
     Через Server API устанавливается только распакованный digest-bound артефакт, затем runner
     сам создаёт новую сессию и отправляет prompts с `permission_mode=auto`; ручного Enter нет.
     Очередь prompt и `last_turn_reason` должны завершиться успешно, а message transcript обязан
     содержать конкретные `tool_use` и соответствующие `tool_result`: отклонённый code Edit,
     разрешённые docs/post-marker Edit, nonce Bash, все ladder-записи и commit. SessionStart
     подтверждается audit-записью с тем же session id. Отсутствие файла само по себе evidence
     не считается. Используются уже существующие credentials после ревью, без нового credential
     plane, skip-receipt и без постоянного release gate. Версия CLI, reviewed HEAD, artifact digest,
     session id и transcript каждого шага фиксируются в итоговом отчёте; невыполненный шаг не
     позволяет объявить эту адаптацию готовой.
     **Crash-safe rollback:** до первой мутации runner атомарно сохраняет `installed.json`,
     managed/gates и absent/present state в gate-owned snapshot/journal с fsync; невозможность
     снимка = отказ до install. Фазы `snapshot → installing → candidate` фиксируются до/после
     мутаций. Candidate identity включает точную registry-запись и managed digest; если SIGKILL
     произошёл до их fsync и текущая запись не совпадает с исходной, recovery останавливается
     конфликтом, а не восстанавливает без guard. Restore идёт ТОЛЬКО через авторитетный Kimi
     Server API по durable фазам `candidate → removing-candidate → installing-original →
     restoring-enabled`: исходное дерево со всемi modes/empty dirs материализуется в staged path,
     затем remove/install/enable/disable вызываются через API с digest-проверкой после каждого
     шага. Raw read-modify-write общего `installed.json` отсутствует, поэтому parallel updates
     других плагинов не затираются и не требуется несуществующий filesystem CAS. API reinstall
     формирует provenance/source заново; гарантируются исходные code digest и enabled-state, а не
     текстовая идентичность `originalSource`. После сбоя прежняя версия восстанавливается; после
     успеха candidate 0.11.0 намеренно остаётся активным как запрошенное локальное обновление.
     Повторный recovery идемпотентен и завершает частичный restore. После SIGKILL следующий запуск
     сначала завершает restore по journal. Failure-point тесты на synthetic home используют
     настоящий SIGKILL и новый процесс, включая окно до candidate identity и mid-smoke отказ.
- `make check` остаётся детерминированным на чистом CI без Kimi/Docker/network/local state.
  Автоматический fleet channel `kimi` в `deploy_gates.py` не добавляется в этой задаче:
  Server API experimental и требует отдельного дизайна fleet auth/lifecycle. Существующие
  Claude/Codex fleet channels не меняются.

Критерии: воспроизводимая CI-проверка без сети + доказанный реальный пользовательский маршрут.
Пересмотреть fleet channel после появления поддерживаемого non-interactive plugin API.

### Р8. Доверенный launch-chain

- **Голый `python3 -B`.** Отвергнуто: repo-controlled `PATH` подставляет интерпретатор, а
  `PYTHONPATH`/`PYTHONHOME`/startup imports подставляют код ещё до resolver.
- **Absolute shell/Python + isolated interpreter + allowlisted environment. Выбрано.**
  Kimi hooks вызывают `/usr/bin/env -i … /bin/sh` и plugin launcher; launcher принимает только
  allowlisted script basename и выбирает Python из фиксированных системных кандидатов
  (`/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, `/usr/bin/python3`) с regular-file,
  ownership и no-group/world-write проверками. После исправления Linux-раскладки 05.11.2026
  каждый candidate дополнительно требует полную ancestor chain от parent до `/`: non-symlink
  directories, root-owned, non-group/world-writable, успешные probes и отсутствие Darwin ACL
  (Linux effective ACL-mask покрывается mode-проверкой). `/usr/bin/python3` может быть root-owned
  symlink ровно на slashless numeric `python3.<digits/dots>`; target проходит общий regular
  validator и исполняется вместо symlink. `/bin/sh`, `find`, `uname`, `readlink` и окружение
  оператора — pretrusted по R-ARTIFACT-PROVENANCE и ML-E5: их hostile подмена/startup controls вне
  гарантий resolver и не требуют second stage. Запуск — `-I -B`:
  игнорируются `PYTHON*`, cwd и user site. `gates-run` использует тот же резолвер Python, а не PATH;
  shell-вызовы `ls/sort/tail` убираются в Python.
  Git hook shims и `gates-run` используют только POSIX `/bin/sh`, включая абсолютный `/bin/sh`
  при вложенном вызове: bare `bash` из PATH и startup-файлы `BASH_ENV`/`ENV` не участвуют.
  Изолированный системный Python не видит user-site PyYAML. Поэтому dependency-free конфиг
  может быть JSON-совместимым YAML и разбираться стандартным `json`; прочий YAML без системного
  PyYAML по-прежнему делает конфиг нечитаемым и включает строгий режим, а не собственный парсер.
- Sentinel-тесты: `PATH`, `PYTHONPATH`, `PYTHONHOME` и startup imports на Kimi hooks и
  `gates-run`, отдельно Kimi-only, coexistence и digest skew. Подставной интерпретатор не
  запускается; sentinel исполняется только из валидной managed установки.

## Сценарии и приёмочные критерии (BSAC)

Топ-уровневые сценарии и проверка:

- **S1. Kimi-only установка.** Manifest и skills/commands видны; plugin diagnostics без ошибок.
  Проверка: структурный тест manifest + live `/plugins info gates`.
- **S2. Opt-in изоляция.** Вне git или без `.codex-gate.yaml` в worktree-OR-HEAD Kimi-shaped
  Edit/Bash/SessionStart завершаются 0 и не пишут маркеры/audit. Проверка: pytest temp repos.
- **S3. G1 до дизайна.** Kimi `Edit`/`Write` код-пути без маркера → exit 2, причина в stderr;
  docs-only → 0. Проверка: реальный subprocess CLI, не только вызов функции.
- **S4. Bash mutation.** `sed -i`, redirect и `git apply` к код-пути без маркера блокируются;
  read-only Bash проходит. Проверка: существующая матрица + Kimi manifest dispatch.
- **S5. Дизайн-ревью и маркер.** Codex receipt на hash дизайна + nonce-handoff → маркер
  реальной Kimi-сессии → та же правка разрешена; изменение дизайна снова блокирует.
  Проверка: E2E с подставным локальным reviewer, затем живой Codex в этой задаче.
- **S6. Параллельные сессии.** Два session id и два nonce не видят маркеры друг друга;
  SessionStart очищает только собственный слот. Проверка: pytest.
- **S7. Nonce boundaries.** Unknown, malformed, replay, expired, wrong-repo nonce не дают
  валидной сессии; consume-first при сбое не разблокирует. Два параллельных PreToolUse с
  одинаковым nonce и разными session id дают ровно один create-only publish; второй отклонён
  и не меняет binding. Проверка: pytest веток публикации и потребления, включая барьерную гонку.
- **S8. Kimi-only git hooks.** Активная managed запись позволяет `gates-run` исполнить
  `ladder_gate.py`; pre-commit без chain блокирует, после simplify→code-review→security проходит.
  Проверка: реальный temp git repo и subprocess.
- **S9. Disabled/removed/failed Kimi plugin.** `enabled=false`, отсутствие записи, удалённый
  managed root и failed/installing/quarantined/unknown/nontype state не исполняются; без
  валидной Claude-установки — fail-closed. Fake `$HOME` с Claude registry не меняет trusted
  resolver ни в одном из этих состояний. Проверка: pytest + live disable smoke.
- **S10. Registry/manifest/path drift.** Битый JSON, не-array `plugins`, duplicate `gates`,
  relative root, root вне managed, root внутри repo, отсутствующий/невалидный manifest или
  script, оба manifest одновременно, traversal, symlink escape, group/world-writable component
  и `__pycache__`/`*.pyc`/`*.pyo` → отказ с диагностикой, не выполнение чужого кода. Полностью
  синтетический 0700 relocated
  home не поддерживается и не исполняется; per-invocation fake `KIMI_CODE_HOME` не влияет на
  выбор. Проверка: параметризованные pytest, включая попытку отключить hooks приоритетным
  `kimi.plugin.json`.
- **S11. Claude/Kimi coexistence.** Равный delivery digest сохраняет Claude-приоритет;
  Claude-old/Kimi-new и Claude-new/Kimi-old блокируются, enabled+broken Kimi не игнорируется.
  При canonical active Kimi repo-controlled `$HOME` с fake Claude registry/cache/script не
  читается и не исполняется ни при отсутствии canonical Claude, ни при равной active Claude;
  оба skew проверяются с sentinel execution. Те же ветки повторяются с вредоносными `PATH`,
  `PYTHONPATH`, `PYTHONHOME` и startup imports: подмена не исполняется. Проверка: subprocess
  fixtures и baseline tests.
- **S12. Настоящие проходы.** После установки существуют `/gates:simplify`,
  `/gates:security-review`, `/gates:code-review`; code-review вызывает Codex working-tree,
  а не Kimi self-review. Проверка: manifest/content tests + live command listing.
- **S13. Независимость reviewer.** Kimi-authored diff по-прежнему требует Codex+Claude;
  outage/timeout/invalid одного reviewer блокирует, fallback на Kimi отсутствует.
  Проверка: существующие portable/deploy tests без ослабления.
- **S14. Control-plane.** Правка `.kimi-plugin/plugin.json`, matcher'ов, commands и shim —
  code-path даже при пустом пользовательском `code_paths`. Проверка: hard-path tests.
- **S15. Manifest drift.** Kimi file/Bash/SessionStart hooks вызывают те же CLI и имеют
  эквивалентные matchers с Claude/Codex config. Проверка: структурный тест.
- **S16. Реальный Kimi route этой адаптации.** После независимого code-review и commit-bound
  `check-reviewed`: `verify-deployable` → `check-decision` + `check-artifact` → install из
  digest-bound raw-tree tar → reload/new session → code Edit без маркера блокируется, docs Edit
  проходит → design-review/marker → code Edit проходит → ladder/commit. Post-review mutation и
  hostile attributes блокируются. Reviewed HEAD, artifact digest, версия CLI и артефакты шагов
  фиксируются в отчёте; невыполненный шаг блокирует объявление готовности ЭТОГО изменения,
  но не создаёт постоянного release gate для последующих версий. Snapshot/journal и restore
  возвращают прежнюю версию/enabled/source или absent после success/failure/SIGKILL recovery;
  restore не затирает другие плагины и сверяется byte-for-byte. `make check` на fresh CI
  проходит без Kimi/Docker/network/state, потому что вызывает только deterministic gates.
- **S17. Полный regression.** `python3 -m pytest tests/ -q` зелёный; существующие Claude/Codex
  subprocess и deployment tests не изменены по ожиданиям, кроме явно добавленных Kimi-путей.
- **S18. Качественный результат.** Для самой адаптации выполнены design-review → RED →
  implementation → simplify → security → independent code-review → full/E2E tests.
  Проверка: marker, ladder ledger, review artifacts и итоговые команды.

Размерная самопроверка матрицы:

- host: Kimi-only (S1/S8/S16), Claude+Kimi (S11), старые hosts (S13/S17);
- lifecycle: installed/enabled/disabled/removed/malformed (S1/S9/S10);
- repo: onboarded/opt-out (S2–S4);
- session: valid/parallel/replay/expired/drift (S5–S7);
- operation: file/Bash/git/deploy-review (S3/S4/S8/S13);
- evidence: unit/subprocess/E2E/live (S3/S8/S16/S17);
- fail-open комбинации: disabled + stale managed copy (S9), schema drift + repo-local root
  (S10), parallel + replay (S6/S7), reviewer outage + Kimi author (S13).

Краевые случаи, которые могут стоить денег или пропустить непроверенное:

- **E1. Саморевью Kimi.** Предотвращается Р5/Р6 и S12/S13.
- **E2. Маркер другой сессии.** Предотвращается nonce + per-session marker, S6/S7.
- **E3. Исполнение repo-local или отключённого plugin.** Предотвращается managed-root и
  enabled проверками, S9/S10.
- **E4. Пустой/неполный review.** Не меняется существующий fail-closed parser; Kimi не
  объявляется reviewer и не создаёт receipt.
- **E5. Ложный «пройденный» проход.** Поставленные commands содержат конкретные действия;
  ladder по-прежнему честно сообщает границу evidence, не выдаёт mark за доказательство.
- **E6. Скрытая сетевая операция тестов.** Все pytest reviewer transports локально подменены;
  live smoke отделён и не входит в обычный test command.

### EARS → тесты

- **EARS-1.** WHEN Kimi emits an enabled PreToolUse edit event in an onboarded repo without a
  valid design marker, THE plugin SHALL return exit 2 before the edit. → S3, `test_kimi_*`.
- **EARS-2.** WHERE the repo is not onboarded, THE Kimi hooks SHALL exit 0 without changing
  gate state. → S2.
- **EARS-3.** WHEN a Kimi CLI call carries a fresh unique handoff nonce, THE gate SHALL bind its
  marker to the hook payload session exactly once. → S5/S6/S7.
- **EARS-4.** IF a handoff is malformed, expired, replayed or belongs to another repo, THEN it
  SHALL NOT authorize a session or design marker. → S7.
- **EARS-5.** WHEN only Kimi installation is enabled, THE git shim SHALL execute the requested
  script from the validated managed root. → S8.
- **EARS-6.** IF the Kimi record is disabled, absent, duplicated, malformed, not locally
  state-ok, outside managed root, path-unsafe or contains Python bytecode caches, THEN the shim
  SHALL NOT execute that plugin. → S9/S10.
- **EARS-7.** WHEN both Claude and Kimi installations are active, THE shim SHALL require equal
  delivery digests and SHALL fail closed on either direction of version/content skew; a fake
  `$HOME` SHALL NOT alter that choice with or without canonical Claude. → S11/S17.
- **EARS-8.** THE Kimi package SHALL expose simplify, security and independent code-review
  commands; code-review SHALL invoke the existing non-Kimi reviewer. → S12/S13.
- **EARS-9.** THE Kimi manifest, commands and hook registration SHALL be hard code paths.
  → S14/S15.
- **EARS-10.** IF no certified independent reviewer completes, THEN deploy SHALL remain blocked
  and SHALL NOT fall back to Kimi. → S13.
- **EARS-11.** THE standard test suite SHALL exercise the real Kimi-shaped hook and git-shim
  routes without live external services. → S3/S8/S17.
- **EARS-12.** THE acceptance of THIS adaptation SHALL require a real Kimi install/activation
  smoke after independent code review; any unexecuted step SHALL block the readiness claim,
  while deterministic check SHALL remain independent of live Kimi and local state. → S16.

## Файлы изменения

- `plugins/gates/.kimi-plugin/plugin.json` — новый manifest.
- `plugins/gates/commands/*.md` — Kimi workflow commands.
- `plugins/gates/scripts/{codex_review_gate.py,native_hook.py}` — handoff, общий session chokepoint,
  hard paths и изолированный trusted launcher.
- `plugins/gates/scripts/ladder_gate.py` — использование общего session chokepoint и Kimi UX.
- `plugins/gates/templates/githooks/{gates-run,kimi_install.py}` — Kimi managed resolver из
  OS-default home, delivery digest и общий manifest detector без env-override.
- `plugins/gates/skills/{design-review,gates-init}/SKILL.md` — host-qualified инструкции.
- `scripts/kimi_acceptance.py`, `Makefile` — одноразовый artifact-bound live acceptance через
  Server API; не вызывается из `make check`, `make deploy` или обычного pytest.
- `tests/` — RED сначала, затем реализация; новые E2E входят в ветки nonce/resolver/hooks/
  certification; live CLI в обычном pytest не вызывается.
- `.codex-gate.yaml`, `README.md`, `CHANGELOG.md`, версии трёх manifests/marketplace — поставка
  и документация. `AGENTS.md` — только если по итогам ревью появятся принятые остатки.

## Вне охвата

- Kimi как blocking reviewer: отдельная adapter/certification спека.
- Автоматический fleet deploy канала Kimi до поддерживаемого non-interactive API.
- Relocated `KIMI_CODE_HOME` для git-шима: только OS-default home; arbitrary root требует
  отдельной trusted-provenance спеки, потому что structural checks не доказывают источник кода.
- Усиление ladder evidence или исправление известных Claude/Codex session/version-skew
  остатков вне явно описанного nonce-маршрута.
- Переработка reviewer panel, convergence, deployment protocol и pre-push transport.
