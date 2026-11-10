# Предотвращение создания bytecode cache и fail-closed detection

Дата: 2026-11-08, редакция 14 после тринадцати независимых design-review и решений владельца.
Tier: 1 — first-party cache prevention и удаление опасного auto-delete; НОВЫХ launch/security
boundaries и delete actuator'ов НЕТ.

## Исходная задача и факты

Нужно прекратить повторяющиеся ручные удаления `__pycache__` между review/install operations.
Resolver должен по-прежнему отклонять bytecode, а не игнорировать его.

Измерено 2026-11-08 через `PYTHONPYCACHEPREFIX`:

- `python3.13 -m pytest tests/test_kimi_install.py -q` → 36 passed и четыре project pyc:
  `tests/conftest.py`, `tests/test_kimi_install.py`, `codex_review_gate.py`, `kimi_install.py`;
- `python3.13 scripts/deploy_gates.py --status` → четыре project pyc: `deploy_verify.py`,
  `ladder_gate.py`, `prepush_gate.py`, `codex_review_gate.py`.

Trusted `gates-run` уже `-I -B`, но внешний direct Python 3.15 producer тоже наблюдался. Кроме того,
настоящий Linux same-device bind mount доказал дефект существующего
`deploy_verify._drop_stale_bytecode`: `check_tree()` вернул `problems=[]`, а sentinel вне installation
был удалён через bind-mounted `__pycache__`. Этот auto-delete обязан исчезнуть независимо от prevention.

## Решения и альтернативы

Двенадцать раундов отклонили permanent repo/root-owned cleaner, recursive/mount-bound delete,
quarantine и rollback: последний требовал бы OS-specific atomic no-replace syscall и отдельного
сложного протокола. Владелец дважды подтвердил уменьшение scope: cleaner НЕ входит в релиз, текущие
кэши удаляются один раз вручную. Sudo, root-owned install и saved permission не создаются; candidate
Kimi rule не применён и удалён.

Расширенный isolated launcher и pre-import hermetic barrier также НЕ входят: trusted `/usr/bin/python3`
не имеет pytest/PyYAML, а безопасный versioned runtime provisioning — отдельная задача. Подмена caller/
remote Python, ambient site startup и исполнение УЖЕ существующего poisoned `.pyc` остаются существующей
границей R-ARTIFACT-PROVENANCE/ML-E5 и R-DEPLOY-PROOF-IS-POINT-IN-TIME. Этот релиз НЕ заявляет их
исправление и не добавляет новый доверенный маршрут вместо текущего. `-B` запрещает запись, не чтение;
это честно фиксируется, а не называется hermetic isolation.

**Выбрано:** запретить создание cache во всех first-party entrypoints/children, а deploy verification
сделать read-only fail-closed detector. Это устраняет повторяющиеся prompts без нового delete или
interpreter trust boundary. Если внешние producers после релиза останутся частыми, runtime/cleaner —
отдельный дизайн по измерениям.

## A. Prevention во всех first-party entrypoints

1. Root Makefile получает `PYTHON ?= python3`, `override PYTHONDONTWRITEBYTECODE := 1` и отдельный
   `export PYTHONDONTWRITEBYTECODE`; `override` обязателен, потому что command-line
   `make PYTHONDONTWRITEBYTECODE=` иначе отключает защиту. ВСЕ Python recipes используют
   `$(PYTHON) -B`, включая test/check/deploy/restore/status/recertify/accept-kimi. Export наследуют
   pytest subprocesses/host agents. Стандартно: `make test PYTHON=python3.13`,
   `make deploy PYTHON=python3.13`.
2. Прямой `python3 scripts/deploy_gates.py` ДО local imports выставляет
   `sys.dont_write_bytecode=True` и ПРИНУДИТЕЛЬНО `os.environ["PYTHONDONTWRITEBYTECODE"]="1"`;
   `setdefault` запрещён: уже существующее пустое значение CPython трактует как выключенную защиту.
   Pytest child получает `-B`, remote stdin agent и smoke probes — `python3 -B` (прежний interpreter
   resolver/transport не меняется).
3. `recertify.py` получает prelude до local imports; `kimi_acceptance.py` — тот же контракт.
   `deploy_verify.py` идёт по stdin с `-B`; plugin runtime уже `-I -B`.
4. README/комментарии используют только защищённые команды. Resolver НЕ игнорирует cache.

После одноразовой ручной очистки `make test PYTHON=python3.13`, direct deploy `--status` и review через
`gates-run` не создают project/managed `__pycache__`. Static tests перечисляют Makefile recipes/direct
entrypoints; representative child без `-B` доказывает inherited env. Проверка — именно отсутствие новых
файлов, а не новое обещание защиты от подмены interpreter/startup.

## B. Deploy detection вместо удаления

`_drop_stale_bytecode` удаляется. `check_tree` получает read-only detector: entry `__pycache__` любого
типа добавляет structural problem с путём и инструкцией проверить и удалить cache вручную. Detector НЕ
открывает target, НЕ удаляет/переименовывает и НЕ traversing entry. Автоматический
`rmtree`/`ignore_errors` исчезает. Cache даёт nonzero, sentinel сохраняется. Legacy `*.pyc`/`*.pyo` вне
cache остаются лишними файлами и блокируются. Ошибки traversal не трактуются как отсутствие cache.

Старые тесты auto-delete заменяются на отказ/сохранность. Autouse delete guard для `check_tree`
удаляется за ненадобностью; остальные production-address guards сохраняются. Deploy/resolver не
предлагает `LADDER_SKIP` или игнорирование cache. Текущие exact caches на Mac удаляются один раз
вручную; на хостах оператор удаляет их после диагностики deploy.

## BSAC / приёмочные критерии

- **S1. Prevention.** После одноразовой очистки `make test`, direct deploy `--status`, representative
  child и review не создают project/managed bytecode. Static tests не дают добавить Python recipe/direct
  local import без prevention. Матрица окружения обязательна: переменная отсутствует, пустая в env,
  пустая/переопределённая command-line `make PYTHONDONTWRITEBYTECODE=…`; direct child без `-B` во всех
  случаях получает принудительное `1`, Make — `override`.
- **S2. Deploy fail-closed.** Cache directory, symlink или regular file `__pycache__` в installation даёт
  structural problem/nonzero без unlink/rmdir/rename. Detector сообщает path; source/poison/sentinel
  сохраняются. FS error не эквивалентен отсутствию cache.
- **S3. Real Linux bind acceptance.** Disposable `--privileged --network none` Docker без host volumes и
  только temp trees создаёт same-device bind `__pycache__` и intermediate directory. Настоящий
  `check_tree` возвращает structural problem/nonzero, все sentinel сохраняются, частичного удаления нет.
  Проверка read-only, mount-id/delete machinery не требуется. Privileged probe уже доказал same `st_dev`,
  разные `stx_mnt_id`; обычные CAP_SYS_ADMIN/seccomp/user-namespace probes mount не разрешили.
- **S4. Реальный маршрут.** После regression текущие exact caches удаляются один раз вручную; затем
  `make test PYTHON=python3.13`, direct `--status` и `gates-run` smoke не создают новых. Полный regression
  обязателен. Если ручная очистка/acceptance не выполнены, это явный остаток.

Unit matrix входит во все prevention/detect/refusal ветки, включая FS errors. S3 проверяет настоящий bind
и read-only safety, а не mocks. Подмена interpreter и pre-existing poison consumption намеренно не
заявлены acceptance этого изменения.

## EARS

- **EARS-1.** ALL first-party Python/pytest entrypoints and children SHALL отключать создание bytecode до
  local imports через `-B`/`sys.dont_write_bytecode` и ПРИНУДИТЕЛЬНО установленное inherited
  `PYTHONDONTWRITEBYTECODE=1`; пустое env/command-line значение SHALL NOT отключать защиту.
- **EARS-2.** ON cache detection THE deploy verifier SHALL добавить structural problem и SHALL NOT
  удалять/rename/traverse cache или suppress FS errors.
- **EARS-3.** THE change SHALL NOT добавлять cleaner, saved permission, sudo/root-owned installation,
  новый interpreter trust boundary или автоматический bytecode delete; residual cleanup SHALL быть явной
  отдельной операторской процедурой.
- **EARS-4.** IF real privileged bind acceptance не выполнен или sentinel удалён, THEN release SHALL
  считаться непроверенным.

## Проверка, откат и границы

RED-тесты prevention и deploy suites; Makefile/preludes/docs и read-only `check_tree`. Затем
targeted/full regression через `make test PYTHON=python3.13`, disposable Linux S3, simplify,
независимое code review, security и реальный S4. После commit/push — fleet deploy 0.11.3 с fail-closed
detection.

Откат — reviewed revert; permission/sudo/root-owned/quarantine состояния не создаются. Изменение не
удаляет sources, registry, ledger или audit. Отсутствие prompts в штатном SDLC достигается prevention,
а не постоянным delete authority.
