# Дизайн: GitHub Release deploy для Kimi, Claude Code и Codex

Дата: 2026-11-10. Tier 2: деплой мутирует GitHub Release и установки гейтов, поэтому нужны
fail-closed поведение, доказанный rollback и end-to-end приёмка.

## 1. Проблема и результат

Старый `make deploy` обновляет Claude Code и Codex через Git marketplace, но не Kimi. Kimi
ставится из локального `plugins/gates`, поэтому другая машина обязана иметь синхронизированный
клон, а активная сессия может продолжать держать старую managed copy. Отдельно fleet deploy
упирается в SSH до того, как новая версия опубликована как общий артефакт.

Новый смысл `make deploy`:

1. доказать чистое дерево, regression и `HEAD == origin/main`;
2. построить versioned ZIP ровно из закреплённого коммита, опубликовать GitHub Release;
3. на каждом объявленном хосте обновить Kimi из release ZIP URL, а Claude Code и Codex через их
   GitHub marketplace на той же версии;
4. проверить managed tree и поведение гейта, выполнить доступные host reload hooks;
5. явно отличить `installed` от `active session reloaded`: если интерактивную сессию нельзя
   перезагрузить документированным API, выдать обязательную команду оператору и НЕ записывать
   полностью активный/стабильный результат.

GitHub становится источником доставки для всех трёх хостов. Локальная директория, managed copy,
SSH stdin и рабочее дерево не являются источником новой версии. SSH может остаться транспортом
запуска verifier/installer на удалённом хосте, но не транспортом кода плагина.

## 2. Измеренные ограничения (2026-11-10)

- Kimi документирует `/plugins install <path-or-url>` для local directory, zip URL и GitHub URL;
  managed copy живёт в `$KIMI_CODE_HOME/plugins/managed/<id>/`. Изменения активируются через
  `/reload` или новую сессию; `/plugins reload` перечитывает registry/manifests и запрашивает
  перестроение prompt у live agents.
- Live-замер Kimi 2.4.1 (11.11.2026): `/openapi.json` не envelope-wrapped; plugins возвращаются
  как `{"plugins":[…]}` без `root`, sessions — как `{"items":[…],"has_more":false}`. Plugin
  actions идут через `/api/v1/plugins/{tail}`; same-session `/api/v1/sessions/{id}:reload`
  существует и вызывается, но session state не содержит `gates_version`/`gates_smoke`, поэтому
  успешный reload всё равно не является behavioural proof.
- Запуск `kimi web` требует `--log-level info`, иначе адрес/token не печатаются; чтение startup
  output обязано быть non-blocking, иначе внешний таймаут не действует.
- `git archive <sha>:plugins/gates` получает tree без commit timestamp и проставляет текущее
  время. Воспроизводимость обеспечивается явным `--mtime=@<commit-time>` из того же `<sha>`.
- Live-замер GitHub CLI (11.11.2026): `gh api … --jq .sha` возвращает полный SHA plain text,
  а не JSON string. Keyring-аутентификация работает с allowlist `HOME/PATH/...` без `GH_CONFIG_DIR`.
  Идемпотентный publish `v0.11.3` не перезаписал assets и подтвердил ZIP SHA-256
  `9eecd94697538529324028ad46a7f64ea4698e7406f9e9e5e4be41faa359239f`, tree `deb4fc061d5854ee`.
- Наш манифест лежит в `plugins/gates/.kimi-plugin/plugin.json`, не в корне монорепозитория.
  Поэтому ZIP обязан содержать содержимое `plugins/gates` в КОРНЕ архива.
- Проверенный ранее `kimi -p "/plugins install …"` не выполняет control command, а уходит в model
  run. Автоматизация использует Server API `/api/v1/plugins`, а не `kimi -p` со slash-командой.
- В использованном Server API OpenAPI есть plugin install/remove/enable/disable и sessions/prompts,
  но нет отдельного гарантированного «reload existing interactive TUI session». Поэтому нельзя
  обещать внешний reload чужой активной сессии.
- Live OpenAPI 20.11.2026: install принимает только `source`, без digest или atomic `enabled=false`.
  Поэтому добавлены pre-install publisher digest/exact tree check и post-install verification/rollback;
  собственный второй fetch Kimi между ними остаётся остатком. Владелец явно принял этот residual risk
  20.11.2026; устранение требует digest-aware/install-disabled API Kimi.
- `claude plugin update` сообщает `Restart to apply changes`; Codex также имеет session snapshot.
  Их CLI не предоставляет доказанный нами API перезагрузки уже работающей интерактивной сессии.
- Claude/Codex marketplace CLI обновляет Git source, но не даёт одного общего проверенного способа
  атомарно переключить уже configured marketplace на release tag. Поэтому release ZIP — обязательная
  иммутабельная поставка Kimi; Claude/Codex используют GitHub source и сверяются по версии и полному
  delivery tree. Подвижный `main` — сохраняемый ограниченный остаток R-DEPLOY-INSTALL-FROM-MUTABLE-MAIN:
  гонка не предотвращается, но обязана быть обнаружена.

## 3. Альтернативы

### A. Только локальный путь для Kimi (оставить)

Отвергнуто: требует клон на каждой машине, не даёт единого артефакта и провоцирует расхождение
локальной версии с репозиторием.

### B. Отдельный репозиторий для плагина

Позволил бы `/plugins install <GitHub-repo>`, но раздвоил бы историю и release process общего core.
Отвергнуто на текущем этапе; release ZIP решает ту же задачу без второго источника истины.

### C. Публиковать ZIP как GitHub Actions artifact

Отвергнуто: artifacts эфемерны и не дают стабильного человекочитаемого release URL. Нужен Release
asset с версией и immutable identity.

### D. Единый ZIP installer для Claude/Codex через самодельную распаковку

Отвергнуто: обходит штатные registry/update/remove CLI, создаёт второй канал установки и усложняет
rollback. Оба хоста уже имеют GitHub marketplace adapters; их результат проверяется по дереву.

### E. Убрать fleet verification и считать publish релиза деплоем

Отвергнуто: выпущенный релиз не доказывает, что хосты обновились и активировали его. Недоступный
хост по-прежнему блокирует статус `stable`; можно завершить `release-published` и отдельно сообщить
`fleet-pending`, но нельзя выдать одно за другое.

### F. Выбранная модель: один release + host adapters + доказанная активация

Один production path публикует артефакт, затем обновляет канарейку и остальные хосты, откатывает
мутированную когорту при структурном сбое. Для активных сессий различаются три исхода:
`reloaded` (доказано), `reload-required` (нужна команда оператора), `unknown` (fail-closed).

## 4. Идентичность и публикация релиза

Версия `X` из Claude/Kimi manifest обязана иметь форму `major.minor.patch` с ASCII-цифрами; Codex
version имеет отдельный согласованный suffix. Для `X` строятся ровно:

- tag/release `vX`;
- asset `gates-X.zip`;
- канонический URL `https://github.com/shagiev/claude-gates/releases/download/vX/gates-X.zip`;
- `gates-X.zip.sha256` с digest ZIP и именем файла.

Любой другой source, включая local path, `file://`, fork, userinfo, query, fragment или caller-defined
URL, отклоняется до API/CLI. Deploy не принимает source параметром: канонический URL вычисляется только
из уже провалидированной версии. Старый local source в Kimi snapshot не является допустимым новым или
rollback source. Redirect не является доверием к содержимому: окончательный критерий — независимые
publisher digests ниже, а не имя хоста в цепочке перенаправлений.

ZIP строится доверенным `git archive --format=zip --mtime=@<commit-time> <sha>:plugins/gates`,
не рабочим деревом. В архиве
обязаны быть `.kimi-plugin/plugin.json`, `.claude-plugin/plugin.json`, scripts/templates и exec bits.
Никаких `__pycache__`, `.pyc`, верхнего `plugins/gates/`, абсолютных путей, `..` или symlink escape.

До публикации проверяются:

1. чистое дерево, полный regression, свежий fetch, `HEAD == origin/main == <sha>`;
2. действующее independent-review evidence ровно на `<sha>` (`check-decision --head <sha>` и
   обязательная panel/ladder evidence), иначе ни release, ни host mutation;
3. согласованность версий Claude/Kimi/Codex/marketplace;
4. повторная сборка из того же tree-ish даёт тот же SHA-256 ZIP;
5. тестовое извлечение и сравнение полного tree digest с `expected_tree(sha)`.

Два разных результата фиксируются и передаются host verifier: **publisher ZIP SHA-256** доказывает
идентичность байтов asset, а **publisher expected-tree digest** независимо доказывает соответствие
содержимого reviewed commit. Managed tree никогда не сверяется только с ZIP, скачанным на том же хосте.

Публикация идемпотентна и fail-closed:

- отсутствие release → создать tag/release на `<sha>` как `prerelease` и НЕ `latest`, загрузить assets;
- существующий release → tag target, asset names, publisher ZIP digest и expected-tree digest обязаны
  совпасть; идентичный release засчитывается без перезаписи. `isDraft/isPrerelease` обязаны быть
  boolean. После digest verification существующий draft/latest нормализуется в
  `draft=false, prerelease=true, latest=false` до host mutation, чтобы retry мог скачать asset;
- другой target/digest/конфликтующий asset → блок; `--clobber`, удаление и перемещение tag запрещены;
- после upload выполняется независимый download по каноническому URL и повторная проверка ZIP.
  Public/authenticated download outage выделяется в `ReleaseTransportError` и НЕ меняет release state;
- до зелёной канарейки release остаётся `prerelease` и не `latest`. При artifact/smoke отказе он
  переводится в `draft`, чтобы отвергнутый URL не оставался публичным источником установки. При чисто
  транспортном fleet outage он остаётся `prerelease`, не `latest`;
- после полного fleet install release снимается с `prerelease` и становится `latest`; сам release и его
  история не удаляются автоматически. `release-published` не равен `stable`.

## 5. Обновление хостов

### 5.1 Канарейка и когорта

Сохраняются текущие порядок, lock, snapshots, rollback всей мутированной когорты и запрет `stable`
при пропущенном/недоступном хосте. Inventory расширяется каналом `kimi`; отсутствие объявленного
канала — структурный отказ, а не silently absent. Старый `stable` удаляется ДО первой host mutation;
pending/partial исход не создаёт его заново.

До мутации отдельно фиксируется session inventory: host, host type, session id и наблюдаемость каждой
живой сессии. `active` оценивается ТОЛЬКО против этого immutable списка. Пустой результат, отказ или
неполнота enumeration помечается `sessions=unknown`, даёт `activation=pending` и запрещает exit 0;
новая API-сессия не заменяет неучтённый TUI. Для каждого зафиксированного id позже требуется
same-session proof новой версии/поведения. Доказанное закрытие сессии не засчитывается: оно не
показывает, какая версия исполнялась до закрытия.

До первой мутации для каждого канала должна существовать доказанная цель rollback:

- Claude/Codex: snapshot содержит прежний install pointer, version, expected tree и digest; прежняя
  установка проходит tree/smoke. Rollback возвращает pointer на эту же проверенную установку, а НЕ
  повторяет marketplace update из подвижного `main`;
- Kimi: snapshot содержит прежний canonical release URL, version, expected tree/digest и enabled state.
  До mutation прежний release скачивается и его извлечённое дерево обязано побайтовому snapshot digest;
  локальная managed copy также проходит tree/smoke. После rollback критерий тот же exact snapshot digest,
  а не просто успешный API install или прохождение smoke;
- если прежняя Kimi ставилась локально и проверенного rollback release ещё нет, миграция блокируется
  до публикации такого release. Восстановление из произвольной локальной директории запрещено.

### 5.2 Kimi

На хосте запускается gate-owned Server API runner по существующему `_start_server` и OpenAPI
validation. Вызов install получает ТОЛЬКО вычисленный канонический release asset URL. Затем:

1. API сообщает `enabled && state == ok`, ожидаемые commands/hooks зарегистрированы;
2. managed manifest имеет версию `X`;
3. полный managed tree digest равен **publisher expected-tree digest из `<sha>`**, а скачанный ZIP
   отдельно равен publisher ZIP SHA-256. Равенство managed tree только скачанному ZIP недостаточно;
4. обход managed tree не следует по symlink, отклоняет не-regular entries и выход за installation root;
5. disposable repo smoke доказывает block/pass гейта, после чего version/tree digest повторяется;
6. Server API выполняет поддерживаемые reload/enable actions, если OpenAPI их объявляет. Их ответ
   не считается доказательством reload интерактивной сессии;
7. новая API-owned сессия обязана увидеть новую версию и пройти behavioural smoke. Для уже
   интерактивной сессии без наблюдаемого same-session proof статус `reload-required: /reload` или `/new`.

### 5.3 Claude Code и Codex

Используются штатные marketplace update/install CLI из GitHub source. Локальный путь запрещён.
После update проверяются manifest version и полный tree digest относительно `<sha>`; затем выполняется
текущий disposable smoke и повторный digest. `gitCommitSha` остаётся диагностикой, tree — identity.

CLI вывод «updated» сам по себе ничего не доказывает. Если configured source оказался local, deploy
останавливается и требует перевести marketplace на `shagiev/claude-gates`; молча не переписывает
пользовательскую конфигурацию.

Для активных сессий выводится обязательный reload/restart шаг. `reloaded` требует наблюдаемого
same-session version/behaviour proof; сам факт возврата CLI и подтверждение оператора текстом этого
не доказывают.

### 5.4 Статусы завершения

- `released`: asset опубликован и скачанный digest сошёлся;
- `installed`: ВСЕ объявленные хосты/каналы имеют правильные publisher version/tree и прошли smoke.
  Подмножество получает только `partial-installed`, никогда `installed`;
- `active`: enumeration сессий до мутации завершена успешно, и КАЖДЫЙ id из её immutable inventory
  имеет наблюдаемый same-session proof новой версии/поведения. Доказанное закрытие не заменяет proof.
  Неизвестная, пустая или частичная enumeration = `sessions=unknown` и `activation=pending`;
  подтверждение оператора без наблюдения недостаточно;
- `stable`: записывается только для `active` с `activation=complete`. Для `installed` с
  `activation=pending` сохраняется отдельный non-stable journal и возвращается специальный exit 4;
  для `partial-installed` — exit 3. Старый `stable` уже снят до mutation. Так exit 0 и `stable` не
  сертифицируют флот со старой или неучтённой живой сессией.

## 6. Rollback и аварийные исходы

- Ошибка до первой host mutation: установки не меняются; release остаётся prerelease/draft по правилам §4.
- Ошибка install/tree/smoke на канарейке: rollback канарейки по всем трём каналам; остальные не
  трогаются, artifact release переводится в draft.
- Ошибка на следующем хосте: rollback всего уже мутированного cohort.
- Kimi rollback выполняется через API remove/install прежнего проверенного release URL. Preflight
  сохраняет фактический ZIP SHA-256 в snapshot на диске; тот же digest проверяется при скачивании
  непосредственно перед install. После него заново читаются и обязаны совпасть со snapshot: ZIP digest,
  exact tree digest, enabled state и block/pass smoke. Успешный API, совпавший digest при disabled
  плагине или digest без smoke НЕ засчитываются. Raw `installed.json` write и локальный source
  запрещены. Kimi сам повторно скачивает canonical URL между pre/post-check: убрать это окно без
  digest-aware installer или local source нельзя; подмена в окне обнаруживается post-check и rollback
  не сертифицируется.
- Claude/Codex rollback возвращает snapshot install pointer, не marketplace update; после него snapshot
  version/tree digest, enabled state и block/pass smoke обязаны совпасть.
- Невозможность rollback печатает snapshot/journal и ручную команду; процесс возвращает ненулевой код.
- Отсутствие сети/GitHub/SSH/API, timeout, schema drift, unknown plugin state → отказ, не «канал
  отсутствует» и не «уже обновлено».
- Kill-switch дальнейших мутаций: снять deploy lock только после остановки процесса; при неясном
  состоянии не запускать новый deploy, а сначала `--status`/verify. Повторный deploy идемпотентен.

## 7. BSAC / EARS и сетка сценариев

| # | Сценарий | Обязательный результат / проверка |
|---|---|---|
| R1 | Грязное дерево, красный regression, HEAD != origin/main или нет independent-review evidence на `<sha>` | Ни release, ни host mutation |
| R2 | Версии manifests разошлись | Блок до `gh release` |
| R3 | Повторная сборка одного `<sha>:plugins/gates` | Одинаковый SHA-256 и tree digest |
| R4 | ZIP содержит `plugins/gates/...`, `..`, absolute, symlink escape, pycache | Блок до публикации |
| R5 | Новый release и два assets | `prerelease`, не `latest`; повторный download и publisher digests совпали |
| R6 | Повтор на полностью идентичном release | Без перезаписи; успешная верификация |
| R7 | Существующий tag указывает не на `<sha>` | Блок, tag не перемещается |
| R8 | Asset с тем же именем имеет другой digest | Блок, никакого `--clobber` |
| R9 | Upload прервался / asset отсутствует | Сначала независимо проверяются ВСЕ уже существующие assets; только затем upload отсутствующих; host не мутируется |
| R10 | Скачанный ZIP отличается от publisher ZIP SHA-256 | Блок до host mutation |
| R11 | Managed tree равен скачанному ZIP, но НЕ publisher expected-tree из `<sha>` | Rollback cohort, `installed` запрещён |
| R12 | Kimi API outage/schema drift/unknown state | Fail-closed, никакого absent/success по умолчанию |
| R13 | Managed copy содержит лишний файл/pycache/symlink | Структурный отказ без автоудаления/traversal |
| R14 | Kimi install прошёл, но block/pass smoke не прошёл | Rollback, release в `draft`, stable нет |
| R15 | Claude marketplace local вместо GitHub | Блок до mutation этого канала, нет silent rewrite |
| R16 | Codex marketplace local/unknown ref | Блок по той же политике |
| R17 | Claude/Codex CLI сказал updated, но tree старый | Rollback cohort |
| R18 | `main` сдвинулся между pin и update | Tree/sha drift обнаружен, stable не пишется |
| R19 | SSH/хост недоступен до snapshot | Другие хосты не мутируются; fleet pending |
| R20 | Хост недоступен после mutation канарейки | Доступные rollback targets восстановлены; сбой громкий |
| R21 | Падение Kimi на втором хосте | Rollback всех каналов; tree digest, enabled и block/pass равны snapshot |
| R22 | Rollback tree совпал, но enabled отличается или block/pass не проходит | Rollback НЕ засчитан; journal/ручная команда, нет stable |
| R23 | Повтор после частичного deploy | Идемпотентная верификация/догонка, без recreate release |
| R24 | Два concurrent deploy | Второй блокируется lock до любой mutation |
| R25 | Канал Kimi не объявлен/CLI отсутствует | Структурный отказ, не silently skipped |
| R26 | Подмножество GATES_HOSTS | Только `partial-installed`, exit 3; installed/stable/active не пишется |
| R27 | Новая Kimi API сессия при неучтённом TUI | Её успех НЕ закрывает pre-mutation session inventory |
| R28 | Интерактивная сессия без same-session proof | `reload-required`, non-stable journal, exit 4; active/stable нет |
| R29 | Существующая Claude/Codex сессия | `reload-required restart`; не выдаётся за reloaded |
| R30 | Оператор лишь подтвердил `/reload`/restart текстом | Без same-session version/behaviour proof activation НЕ complete |
| R31 | Старая локальная Kimi без rollback release | Host mutation блокируется до проверки rollback asset по snapshot digest |
| R32 | Секрет/tokens в output | Не печатаются; URL публичный, bearer только внутри API runner |
| R33 | Тесты со снятыми guards | Не мутируют реальные releases, registries, managed copies или fleet |
| R34 | Полный real route: commit/push → release → canary → fleet → reload | Три host channels на X, publisher digest/smoke зелёные, статусы честные |
| R35 | Local/file/fork/userinfo/query/fragment/caller URL или невалидная версия | Hard-fail до install/rollback; принимается только вычисленный canonical URL |
| R36 | Rollback release URL скачивается, но его tree != snapshot | До mutation блок; после mutation rollback не засчитан |
| R37 | Smoke изменил managed tree или добавил symlink | Пост-smoke digest/обход ловит drift, rollback |
| R38 | Artifact smoke отклонён после prerelease | Release переводится в draft и не latest; retry повторно проверяет digest |
| R39 | На `<sha>` нет действующего independent-review evidence | Ни release, ни host mutation |
| R40 | Session enumeration пустая/частичная/failed | `sessions=unknown`, activation pending, exit 4; stable запрещён |
| R41 | Старый stable существует до mutation | Он удаляется до первой mutation и не восстанавливается при pending/partial |
| R42 | Pre-mutation inventory содержит несколько TUI/API ids | Каждый id требует same-session proof; закрытие не засчитывается |
| R43 | ZIP превышает declared или фактический 16 MiB entry / 100 MiB total | Оба extractor’а останавливают streamed extraction; artifact rejection |
| R44 | Downloaded tree отличается от publisher tree либо download падает | `source-tree` = artifact/draft; download/local I/O `source` = transport/prerelease |
| R45 | Любая version/tree/source проблема уже записана у канала | SMOKES этого канала НЕ запускаются: нельзя исполнять заведомо отклонённую установку |
| R46 | Session payload подменяет host либо содержит id `..`/path separator/не-string | Collector сохраняет только channel/id/kind и назначает host сам; activate повторно отклоняет invalid id до API path |
| R47 | Kimi snapshot `kimi_home` отличается от process home | Restore absent/installed отклоняется до API и download; valid home продолжает digest/install |
| R48 | Kimi update получил canonical URL | До API install host скачивает ZIP, проверяет publisher SHA-256 и exact commit tree; failure = ноль API install |
| R49 | Kimi update отклоняет digest/archive/tree до API install | Сохраняются `zip-sha256`/`source-archive`/`tree`; driver возбуждает `DeployAbort(artifact=True)`, rollback + draft. Download/API `kimi-install` остаётся transport/prerelease |
| R50 | Host объявляет claude,codex,kimi; Kimi enumeration успешен | Kimi ids сохраняются и reload выполняется; `pending_channels` для неподдерживаемых enumeration/proof каналов запрещает complete/stable и даёт exit 4. Игнорировать их нельзя |
| R51 | `kimi web` пишет info-логи после token/base до health и во время install/remove/rollback | Daemon drain запускается ДО health-poll и читает outer pipe весь lifetime, отбрасывая байты без RAM/JSON stdout; EOF/terminate завершает thread, silent/partial timeout и lifeline-cleanup сохраняются |
| R52 | Caller задаёт `KIMI_CODE_HOME`/другой env либо live plugin envelope не содержит root | Любой установленный `KIMI_CODE_HOME` = отказ до Popen/API. Server получает getpwuid HOME/cwd и минимальный env без KIMI_*/TMPDIR/secrets. Присутствующий API root обязан совпасть; отсутствующий root — live 2.4.1 contract, допустим только после закрытия env ambiguity |

R51 отвергает отключение info-логов и forwarding в stdout агента: первое скрывает диагностику и не
гарантирует отсутствие stdout, второе ломает машиночитаемый JSON. Pipe нельзя оставлять без reader
после startup: supervisor уже пересылает child logs в outer pipe, поэтому заполнение останавливает и
supervisor, и `kimi web`, включая rollback API.

Покрытие измерений: clean/dirty × release absent/identical/conflicting/partial × network ok/outage ×
host canary/second × channel Kimi/Claude/Codex × install/tree/smoke failure × active/new session ×
rollback available/missing × full/subset/concurrent. Комбинации с безопасностью и rollback имеют
отдельные сценарии, а не выводятся из happy path.

Вне scope этого изменения: auto-deploy по push, GitHub CI signing/attestation, отдельный репозиторий,
semantic reviewer panel, автоматическое обновление неизвестных нам интерактивных сессий и удаление
старых releases/managed caches.

## 8. Тестирование и реальный маршрут

Сначала RED-тесты по R1–R42 в новом `tests/test_github_release_deploy.py`, затем адаптация существующих
`test_deploy.py` и Kimi isolation tests. Все внешние вызовы по умолчанию мокируются на уровне trusted
runner; тестовые payload — несуществующие repo/host/version, безопасные при снятии guards.

Отдельные disposable tests строят ZIP из реального временного Git commit и проверяют имена/mode/content.
Mutation tests подтверждают, что guards при снятии не создают release и не меняют реальные установки.

Live acceptance выполняется только после approve/security и clean commit/push:

1. `make deploy PYTHON=python3.13` публикует реальный versioned prerelease, не latest;
2. Kimi Server API получает именно canonical release URL; managed digest сравнивается с publisher
   expected-tree из reviewed `<sha>`, а не только со скачанным ZIP;
3. Claude/Codex обновляются через GitHub, disposable block/pass и пост-smoke digest повторяются;
4. canary проходит до fleet; затем fleet либо полностью зелёный, либо cohort rollback с exact snapshot
   digests; release становится latest только после полного install;
5. в активной Kimi сессии оператор выполняет `/reload`, Claude/Codex sessions перезапускаются;
6. deploy получает наблюдаемый same-session version/behaviour proof. Без него остаются exit 4 и
   non-stable journal; `/plugins info gates` сам по себе не доказывает reload всей живой сессии.

## 9. Файлы и миграция

Основные: `scripts/deploy_gates.py`, `scripts/deploy_verify.py`, возможный небольшой общий
`scripts/kimi_release.py` вместо размножения API helpers, `deploy-hosts.yaml`, `Makefile`,
`tests/test_github_release_deploy.py`, существующие deploy/Kimi tests, README/CHANGELOG/AGENTS.md.
Старый fleet SSH design получает ссылку на новый источник доставки; его полезные cohort/smoke
инварианты не переписываются заново.

Версия плагина повышается отдельным релизом после реализации. Первый прогон нового deploy обязан
сам опубликовать свой ZIP и обновить все три канала из GitHub — это и есть конечная приёмка.

## 10. Smoke HOME при групповом umask

Замер на `smartape-vps-1` 23.11.2026: при umask `002` disposable smoke создаёт домашние каталоги
`0775`, resolver и Claude registry `0664`. Trusted launcher отклоняет их до запуска лесенки, поэтому
rollback target ошибочно выглядит нерабочим. Тот же smoke при umask `022` проходит; реальная
установка Codex и pinned resolver template на хосте при этом присутствуют и совпадают по digest.

**Выбрано владельцем после сравнения альтернатив и остановки цикла:** `_run_gate` создаёт только
harness-owned каталоги synthetic HOME с `0700` и проверяет `lstat` на тип/владельца до использования.
Registry и resolver открываются через `os.open` с `O_NOFOLLOW|O_NONBLOCK`, без `O_TRUNC`: сначала
`fstat` подтверждает regular file, текущего владельца и `st_nlink == 1`, затем `fchmod(0600)`,
`ftruncate` и запись. Поэтому symlink, hardlink, FIFO и чужой файл отклоняются до изменения sentinel;
special file не может заблокировать open. Изменение process-wide umask отсутствует полностью, поэтому
production subcommands и real-HOME потомки Kimi не наследуют новый режим. `copytree/copystat` сохраняет
режимы rollback target; chmod исходной или скопированной установки не выполняется.

**Отклонённые альтернативы:** main-wide, verify-wide и save/set/restore umask оставляют process-wide
влияние на реальные потомки; `/bin/sh -c` добавляет ненужную shell-границу; рекурсивный chmod disposable
HOME маскирует небезопасный rollback target. После раундов alternatives выбран узкий secure-creation
вариант, не требующий изменения argv или нового worker.

Сценарии приёмки:

- **U1. Групповой umask caller.** Настоящий `smoke_gate_blocks` при исходном umask `002` выполняет обе
  ветки лесенки. До исправления сценарий краснеет с отказом trusted resolver; `os.umask` не вызывается.
- **U2. Обычный umask.** Существующий smoke при umask `022` остаётся зелёным без изменения контракта.
- **U3. Mode parity rollback target.** Group-writable каталог или проверяемый resolver'ом файл внутри
  `install_root` сохраняет режим в synthetic copy и даёт smoke failure; режимы source не меняются.
  Нормализация копии в приватные права запрещена.
- **U4. Fail-closed сохраняется.** Сломанный shim и неблокирующая лесенка по-прежнему дают
  `smoke_gate_blocks` failure. Исправление прав не должно превращать отказ resolver в успешный smoke.
- **U5. Production side effects отсутствуют.** Spy подтверждает отсутствие `os.umask`; argv всех
  subcommands и режимы real-HOME registry/managed/runtime файлов не меняются.
- **U6. Небезопасный entry в scaffold.** Symlink synthetic HOME, registry или resolver на sentinel вне
  disposable HOME отклоняется до chmod/записи. Hardlink, FIFO и чужой владелец файла/каталога также
  отклоняются после descriptor/lstat проверки, но до truncate/write; содержимое и режимы sentinel
  сохраняются, гейт не запускается.
- **U7. Ошибка secure creation.** Ошибка `mkdir`, `lstat`, `chmod`, secure open, `fstat`, `fchmod` или
  `ftruncate` завершает smoke до запуска гейта; исключение не трактуется как успешная проверка.

**Явный остаток, принятый владельцем отдельно от этого fix:** рекурсивный source-side lstat-аудит всех
delivery-файлов и проверка resolver против реального HOME не добавляются. Synthetic smoke не расширяет
прежнее обещание до полной сертификации real-HOME trust chain. Для текущего fleet отсутствующий
gate-owned bootstrap на VPS восстанавливается операционно из pinned template с безопасными режимами;
общая модификация source/real-HOME verification требует отдельного дизайна.

## 11. Rollback только после возможной мутации хоста

Независимый commit review обнаружил, что хост добавляется в rollback cohort до `update`. Поэтому
pre-install отказ Kimi (download/digest/archive/tree) приводит к API remove/install неизменённого
работающего плагина; ошибка этого ненужного восстановления может оставить канал отсутствующим.

**Выбранный минимальный вариант — явный mutation evidence, без второго live-state resolver.** Host
`cmd_update` возвращает `mutated=false` только на путях, где `_install_kimi_release` ещё не вызван.
Перед входом в него и при любом последующем отказе возвращается `mutated=true`. Driver считает хост
мутированным при успешном update до verify, при ошибке любого Claude/Codex CLI, transport/agent error,
а также при отсутствии или невалидном признаке от старого agent. Литеральное `false` от нового agent
освобождает ТЕКУЩИЙ хост от rollback только если ни один предыдущий канал этого же `update` не мог
мутировать. Успешный Kimi update также устанавливает host-level признак до следующего канала.
Driver строго проверяет top-level dict, `ok`, непустой `problems` list и обязательные строки
`code`/`text`; любая schema drift становится `DeployAbort(mutated=true)`, а не необработанным
исключением мимо cohort rollback. Предыдущие хосты cohort не сбрасываются.

Сценарии:

- **M1. Только Kimi, отказ до install.** Текущий хост не rollback'ится; artifact release уходит в draft,
  transport остаётся prerelease, API remove/install не вызывается.
- **M2. Второй хост отказался до install.** Откатывается только ранее обновлённая канарейка.
- **M3. Ошибка после входа в Kimi install.** Текущий хост обязательно rollback'ится, включая restore
  в `absent`, потому что remove/install мог частично выполниться.
- **M4. Предыдущий канал уже мог мутировать.** Это включает Claude/Codex CLI и успешный Kimi перед
  повторным Kimi: даже последующий `mutated=false` не исключает хост из rollback.
- **M5. Старый agent, missing/malformed flag, transport error.** Conservative rollback. Non-dict
  response, невалидный `ok`, null/non-list/пустой `problems` и problem без `code`/`text` — та же
  fail-closed ветка с `mutated=true`.
- **M6. Update успешен, verify падает.** Хост добавлен в cohort до verify и rollback'ится как раньше.
- **M7. Duplicate channel в inventory.** Запись отклоняется до любой мутации; накопление mutation
  evidence в `update` остаётся вторым независимым защитным слоем.
- **M8. Malformed verify после успешной мутации.** `verify` нормализует non-dict, невалидный
  `ok`/`problems`/`channels` и problem без `channel`/`code`/`text` в отрицательный `agent-schema`
  report. Main откатывает текущий и предыдущие хосты; исключение не уходит мимо cohort.
- **M9. Malformed restore или rollback verify.** Rollback добавляет явную schema-ошибку, продолжает
  остальные доступные действия и никогда не сообщает подтверждённый откат по невалидному ответу.

Альтернатива «сравнить live state со snapshot» отклонена для этого fix: она создаёт второй resolver
и риск ложного `unchanged` при неполном ответе API. Install-before-remove — отдельный дизайн Kimi API,
а не необходимое условие устранения ненужного rollback pre-install отказов.
