# Versioned system Python в trusted launch-chain

Дата: 2026-11-05, редакция 2026-11-07. Tier: 2 — resolver выбирает интерпретатор blocking-гейтов.

## Проблема и воспроизведение

`make deploy` версии 0.11.2 остановил выкатку на `smartape-vps-1`: local canary прошёл, удалённые
Claude/Codex smoke не нашли trusted system Python; автоматический rollback обоих хостов проверен.
На хосте `/usr/bin/python3` — root-owned symlink на `/usr/bin/python3.14`, target — regular
`root:root` 0755, `/`, `/usr` и `/usr/bin` — regular root-owned 0755 directories. Resolver в
`gates-run`, добавленный в 0.11.0, принимает только regular non-symlink по трём неверсионированным
путям, поэтому штатная Linux-раскладка неработоспособна. На Mac используется regular
`/usr/bin/python3` 3.9.6 с root-owned ancestor chain; `/opt/homebrew/bin/python3` — пользовательский
symlink и уже отклоняется. Это регрессия поддержки Kimi host, а не повод обойти smoke.

## Решения и альтернативы

- **Glob `python3.N`/`python3.NN`.** Отвергнуто после первого ревью: writable parent допускал
  подмену directory entry, а несколько versioned кандидатов не имели политики системного default.
- **Descriptor-bound/native launcher или artifact-bound managed Python.** Не выбрано. Это отдельная
  multi-platform supply-chain: root-owned provisioning, подписанные runtime artifacts, digest до
  первого запуска, совместимость stdlib/extensions, update/rollback. Для узкой Linux-совместимости
  такой контур несоразмерен. Он может рассматриваться в Фазе 2 отдельно.
- **Проверять ancestors только у нового symlink.** Отвергнуто: более приоритетные regular-кандидаты
  сохраняли бы заменяемость через writable parent. Инвариант единый для всех путей.
- **Только owner/mode без ACL.** Отвергнуто: macOS ACL может дать write/add_file/delete_child при
  внешнем mode 0755, невидимом для mode bits. Linux effective ACL-mask покрывается group mode bits.
- **Разрешить произвольный symlink или только `/usr/bin/python3.14`.** Отвергнуто: absolute targets,
  alternatives и `..` расширяют trust boundary; жёсткая версия сломается при следующем upgrade.
- **Только minor N/NN.** Отвергнуто после последнего ревью: slashless numeric target с любым числом
  numeric components сохраняет ту же trust boundary, но поддерживает `python3.100` и patch layouts.
  Принимается `python3.` + digit, затем только цифры/точки без ведущей/замыкающей или повторной
  точки. `-config`, буквы и malformed dot-формы отклоняются.
- **`PATH`, `/usr/bin/env python3` или ручная установка Python на сервер.** Отвергнуто: caller PATH
  подставляет интерпретатор, а ручная host-specific мутация не воспроизводима из репозитория.
- **Second stage, sibling, heredoc и shell environment scrub.** Не выбрано после ревью альтернатив:
  публичные sentinel допускают обход, sibling разрывает artifact binding, heredoc зависит от temp
  storage и special-builtin stdin semantics, а shell-only scrub не отменяет startup controls
  (`SHELLOPTS`, `PS4`, loader constructors) и может раскрыть credentials. Полноценная pre-shell
  стерилизация требует нативного launcher из предыдущей альтернативы и выходит за рамки релиза.
- **Единый trusted-candidate validator + узкий same-dir default symlink. Выбрано.** Кандидаты и их
  приоритет прежние: `/opt/homebrew/bin/python3`, `/usr/local/bin/python3`, `/usr/bin/python3`.
  Для КАЖДОГО regular candidate проверяются:

  1. файл существует, regular, executable, не symlink, root-owned, не group/world writable;
  2. каждый ancestor от parent до `/` — existing non-symlink directory, root-owned и не
     group/world writable;
  3. на Darwin файл и каждый ancestor не имеют никакого ACL через абсолютный `/usr/bin/find -acl`;
     на Linux effective named-user/group ACL write требует mask с write, отражаемую в group class
     mode bits и ловимую `\( -perm -020 -o -perm -002 \)`; другие ОС отклоняются;
  4. каждый ownership/mode/ACL/OS probe завершился rc=0; ошибка probe отклоняет candidate и не
     эквивалентна «нарушений нет».

  Если `/usr/bin/python3` — symlink, он принимается только при root-owned link, trusted ancestor
  chain, отсутствии Darwin ACL и slashless numeric target `python3.<digits/dots>` от абсолютного
  `/usr/bin/readlink`. Полученный target проходит общий regular validator, и в `PY` попадает target,
  не symlink. Для двух других путей symlink запрещён. При нескольких установленных версиях
  выбирается только target системного default; сортировки, locale и glob нет.

  **Fail-safe цена:** regular Python в user/group-writable или ACL-writable Homebrew tree не trusted
  даже при root-owned файле; любой Darwin ACL отклоняется. Homebrew-only машина обязана предоставить
  system Python в безопасной цепочке. Текущие хосты флота такие пути имеют.

## Согласованная граница threat model

По решению владельца 2026-11-07 сохраняется существующая граница `R-ARTIFACT-PROVENANCE` и ML-E5:
OS toolchain и окружение оператора — trusted. `/bin/sh`, `find`, `uname`, `readlink`, начальный
loader и shell startup controls не стерилизуются и не заявляются защищёнными от намеренно
враждебного оператора. Такой оператор уже имеет документированные `LADDER_SKIP`/`CODEX_REVIEW_SKIP`
и права на подмену caller-writable артефактов. Поэтому second stage и shell-scrub из рассмотренных
альтернатив убраны, а недоказуемые обещания очистки НЕ делаются.

Выбор Python игнорирует caller PATH и `PYTHON*` благодаря фиксированным кандидатам и `-I -B`; HOME
вычисляется через OS account. Существующие allowlists сертифицированных reviewer-вызовов и trusted
git/ssh слоёв НЕ ослабляются. Они — отдельная защита от случайной контаминации и продолжают работать.

## Сценарии и приёмочные критерии (BSAC)

Размерности: regular/symlink × три пути × minor 1–2 цифры × несколько Python × ownership/mode/ACL/
probe файла × ownership/mode/type/ACL/probe каждого ancestor × Darwin/Linux/другая ОС × target ×
отсутствие кандидата. Комбинации:

- **S1. Существующий macOS/Linux regular.** Неверсионированный regular и вся ancestor chain валидны
  → выбирается он; приоритет трёх прежних путей сохраняется.
- **S2. Инцидент Linux.** `/usr/bin/python3` — root symlink на same-dir `python3.14`, вся parent
  chain root 0755, target regular root 0755 → выбирается regular `/usr/bin/python3.14`.
- **S3. Однозначная minor и несколько версий.** Target `python3.9` валиден; при соседних
  `python3.9`, `python3.10`, `python3.14` default на `python3.14` выбирает именно его независимо от
  порядка создания и locale.
- **S4. Недопустимый target.** Absolute path, `../`, nested relative path, `python3.14-config`,
  `python3.`, `python3..14`, `python3.14.`, `python3.a`, несуществующий target, versioned symlink
  или сбой `readlink` → fail-closed. Валидные `python3.100` и `python3.14.1` выбираются.
- **S5. Недоверенный файл.** Regular/default symlink/target не root-owned, target non-executable,
  group/world-writable, не regular либо имеет Darwin ACL → candidate отклоняется. Mode 0777 самого
  symlink не дефект: entry защищает trusted parent, ACL проверяется отдельно.
- **S6. Недоверенный ancestor любого кандидата.** Для каждого из трёх путей любой ancestor до `/`
  отсутствует, является symlink, не root-owned, group/world-writable либо имеет Darwin ACL →
  candidate отклоняется. Валидный менее приоритетный candidate может быть выбран; без него — блок.
- **S7. Probe failure / unsupported OS.** Любой ownership/mode/ACL `find` или `uname` завершился
  ненулевым rc → candidate отклоняется. Не-Darwin/non-Linux не получает fallback. Unsafe ancestors
  отклоняются до instrumented `readlink`/Python.
- **S8. Check/exec substitution.** В writable или Darwin ACL-writable parent замена directory entry
  отклоняется проверкой; в валидной root-owned 0555/0755 chain без ACL не-root `os.replace` запрещён
  OS. Покрываются каждый приоритетный путь и оба Python exec внутри принятой OS trust boundary.
- **S9. Нет кандидатов.** Все пути отсутствуют/небезопасны → прежние сообщение и exit 1 до чтения
  или исполнения gate-owned bootstrap; synthetic target не исполняется.
- **S10. Полный и боевой маршрут.** В ordinary pytest локальный Docker без сети запускает полный
  НЕИЗМЕНЁННЫЙ `gates-run` с Linux `/usr/bin/python3 → python3.13`, pinned bootstrap и fake managed
  installation. Прямые вызовы доходят до финального gate с `sys.executable=/usr/bin/python3.13` и
  сохраняют `pre-push-ref`; `/bin/sh -s` исполняет те же проверенные байты. Невалидное имя script
  и настоящий shim 0.11.2 на symlink-only Linux отклоняются, а отдельный deploy-test доказывает
  отсутствие мутации при broken rollback target. Полный regression зелёный; fleet smoke на
  `smartape-vps-1` блокирует непроверенную правку и пропускает документационную, дерево установки
  совпадает с закреплённым коммитом.

Покрытие: S1–S9 исполняют реальный resolver-block shim в temp-сценарии для каждого candidate path.
Тестовая копия заменяет абсолютные probes на stubs: текущий uid моделирует root, отдельный точный
path принудительно возвращает owner mismatch, ACL или nonzero probe rc; mode и реальный Darwin ACL
делегируются настоящему `find`, включая отдельные 0775/0757. Отдельные прогоны входят в
owner/ACL/probe-failure ветки regular, default symlink, target и каждого ancestor всех трёх путей;
instrumented `readlink` доказывает ранний отказ, пустой `PY` — отказ до Python. Mode/type негативы
используют настоящие FS-объекты, включая symlink ancestor и запрещённый `os.replace` в non-writable
parent. Поставляемый shim не параметризуется; S10 доказывает home resolution → digest bootstrap →
installation selection → финальный `exec` полным неизменённым shim.

## EARS

- **EARS-1.** WHEN любой regular candidate и его полная ancestor chain проходят
  type/ownership/mode/ACL probes с rc=0, THE resolver MAY выбрать его согласно прежнему приоритету.
- **EARS-2.** WHEN `/usr/bin/python3` — root-owned symlink без Darwin ACL на slashless numeric
  `python3.<digits/dots>`, вся ancestor chain trusted, а target проходит общий regular validator,
  THE resolver SHALL выбрать regular target; WHEN доступно несколько versioned файлов, THE resolver
  SHALL использовать только target системного default.
- **EARS-3.** IF файл, default symlink, target или любой ancestor нарушает type/ownership/mode/ACL
  инвариант либо его probe завершился ошибкой, THEN resolver SHALL отклонить соответствующий
  candidate и не исполнять его.
- **EARS-4.** IF `readlink` завершился ошибкой либо target содержит slash, suffix, нечисловые
  символы, ведущую/замыкающую или повторную точку, THEN resolver SHALL отклонить его.
- **EARS-5.** IF ОС не Darwin/Linux либо ни один candidate не проходит проверки, THEN shim SHALL
  завершиться fail-closed до чтения/исполнения gate-owned bootstrap.

## Откат и границы

Изменение ограничено одним `gates-run`, его repo-копией, тестами и документацией. Deploy сохраняет
canary → fleet и автоматический restore по проверенному snapshot; rollback targets совместимы только
если сами проходят smoke. На symlink-only Linux 0.11.2 такой целью НЕ является: deploy обязан
остановиться до мутации, а сначала восстановить проверенную pre-0.11 версию штатным reviewed-каналом.
Подменять старый shim или обходить `check_rollback_target` запрещено. Текущий `smartape-vps-1` имеет
0.10.7 (подтверждено 2026-11-08), Mac с 0.11.2 использует regular `/usr/bin/python3`, поэтому рабочие
пути не блокируются. При новом отказе повторный rollback обязателен; `LADDER_SKIP` и dirty tree нет.

Не добавляется fleet-канал Kimi: experimental Server API и его auth/lifecycle остаются отдельным
дизайном. Не меняются native Kimi hooks, trusted resolver установок, модели ревьюеров и portable
certification. Произвольные absolute/alternatives symlink и нечисловые/malformed targets сознательно
не добавляются; numeric versions и patch layouts допускаются по одной grammar. Другие ОС до
отдельного анализа effective ACL завершаются fail-closed. Вопросы
pre-shell стерилизации и multi-platform managed runtime остаются в Фазе 2 и не входят в релиз.
