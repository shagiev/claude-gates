# Плагин `gates` — порт ревью-гейтов Claude↔Codex из боевого проекта-источника

**Дата:** 2026-07-22. **Статус:** design (на Codex-ревью).
**Источники семантики:** спеки проекта-источника Phase 1 / 1.5 / 1.6 (копии в `docs/methodology/`),
боевой код `scripts/codex_review_gate.py` (~900 строк, 124 теста) и `scripts/ladder_gate.py`
(~430 строк, 41 тест). Система прожила полный боевой цикл 22.07.2026 (~35 багов найдено
независимым ревью, протокол сходимости довёл деплой до самостоятельного схождения).

## Миссия

Упаковать проверенную систему гейтов в Claude Code-плагин, устанавливаемый из
маркетплейс-репо, чтобы включать её в любом проекте одной командой (`/gates-init`), без
копипаста скриптов. **Порт, не переписывание**: логика парсинга вердиктов, chain-семантика
лесенки, протокол сходимости переносятся как есть; меняется только привязка к конкретному
репозиторию (пути, константы код-путей, эпоха).

## Схема плагина

```
.claude-plugin/marketplace.json     # маркетплейс lenar-gates, плагин gates → ./plugins/gates
plugins/gates/
├── .claude-plugin/plugin.json      # {"name":"gates","version":"0.1.0",...}
├── skills/design-review/SKILL.md   # порт (пути → резолв плагина глобом)
├── skills/gates-init/SKILL.md      # НОВЫЙ: онбординг проекта (решение 4)
├── hooks/hooks.json                # PreToolUse gate-edit/gate-bash, SessionStart clear-marker
├── scripts/codex_review_gate.py    # порт + генерализация (решения 1–2)
├── scripts/ladder_gate.py          # порт + генерализация
└── templates/
    ├── AGENTS.skeleton.md          # каркас конституции (наполнение — пер-проект)
    ├── codex-gate.example.yaml     # пример конфига
    ├── Makefile.snippet            # check-reviewed, deploy-lock, check-decision, baseline
    └── githooks/pre-commit, post-commit   # глоб-шимы (решение 3)
docs/                               # эта спека + docs/methodology/ (копии 3 спек-источников)
tests/                              # порт обоих тест-файлов + fixtures + conftest.py
```

Pytest — из корня репо (`python3 -m pytest tests/ -q`); `tests/conftest.py` добавляет
`plugins/gates/scripts` в `sys.path`, тесты импортируют `codex_review_gate` /
`ladder_gate` напрямую (как раньше `scripts.*`).

## Решение 1 — конфиг-экстернализация: `.codex-gate.yaml` в корне целевого репо

Всё, что в проекте-источнике было прибито константами, становится пер-проектным конфигом:

```yaml
# .codex-gate.yaml (корень целевого репо, коммитится)
code_paths:
  prefixes: ["app/", "tests/", "scripts/"]      # какие пути — «код»
  exact: ["Dockerfile", "docker-compose.yml", "config.yaml",
          "requirements.txt", "pyproject.toml"]
ladder:
  enabled: true          # гасит ТОЛЬКО pre-commit (деплой-гейт не читает, как раньше)
  epoch_sha: <sha>       # grandfathering до-ladder истории (ставит gates-init)
convergence:
  hard_cap: 8            # раундов до carry-over/эскалации
```

**Жёсткая (не отключаемая конфигом) часть код-путей** — аргумент «ослабление через конфиг»
(Codex ловил в проекте-источнике R3/ML-L7): `.codex-gate.yaml`, `.githooks/`, `Makefile` ВСЕГДА
код-пути — добавляются в коде, конфиг их убрать не может. Следствие: правка самого конфига
(смена эпохи, урезание prefixes, hard_cap) гейтится лесенкой и видна Codex-ревью диффа —
именно поэтому эпоху и hard_cap допустимо держать в конфиге. `DEPLOY_REQUIRED_PASSES`
остаётся константой кода (НЕ конфиг) — как в спеке Фазы 1.5.

**Безопасные дефолты (строже) при отсутствии/битости:**
- Файл есть, но YAML битый / PyYAML не установлен / секция не dict → как «нет конфига».
- «Нет конфига» для **явно вызываемых** гейтов (check-reviewed, check-range,
  pre-commit-шим, check-decision): `is_code_path()` возвращает True для ЛЮБОГО пути внутри
  репо («всё код», исчезают и docs/.md-экземпции), лесенка включена, epoch=None (вся
  история проверяется), hard_cap=8. Плюс громкий stderr-warning «нет .codex-gate.yaml —
  строгий режим». Явный вызов = проект опты-ин, отсутствие конфига = мисконфигурация,
  строгость должна заставить её починить.
- `hard_cap` из конфига валидируется: не-int / <1 → дефолт 8. (Занижение hard_cap ослабляет
  гейт — carry-over наступает раньше; защита — та же: правка конфига гейтится и ревьюится.)

**Opt-in для автосрабатывающих хуков (новое относительно HANDOFF, вскрыто при
проектировании):** плагин-хуки (PreToolUse gate-edit/gate-bash, SessionStart clear-marker)
после установки плагина срабатывают в КАЖДОМ проекте пользователя. Без признака опты-ина G1
заблокировал бы правки кода во всех репо, включая не-онбордженные. Правило:

| Канал вызова | Нет git-репо | git-репо БЕЗ конфига (worktree И HEAD) | Конфиг есть, но битый |
|---|---|---|---|
| PreToolUse/SessionStart хуки | exit 0 (тихо) | exit 0 (не онбордился) | **строгий режим** (гейтит; битый конфиг в онбордженном репо не должен тихо снять G1) |
| Явные гейты (check-reviewed и пр.) | явная ошибка | строгий режим | строгий режим |

Признак «проект онбординат» (Codex R1-фикс, спор «временное удаление конфига»): конфиг
существует **в worktree ИЛИ в HEAD** (`git cat-file -e HEAD:.codex-gate.yaml`). Есть в HEAD,
нет в worktree (удалили/переименовали не закоммитив) → репо онбординат, файл нечитаем →
**строгий режим**, не exit 0. Тем самым «удалить → править → вернуть» не отключает хуки.
Закоммиченное удаление — код-коммит (конфиг жёстко код-путь): требует лесенку и виден
Codex-ревью диффа. Остаток (niche, в реестр): checkout до-онбординговой ревизии, где конфига
нет и в HEAD, — G1-слой отключён; деплой-цепочка не страдает (baseline/ledger fail-closed).

## Решение 2 — динамический repo-root

`REPO_ROOT = Path(__file__).parent.parent` больше не работает (скрипты живут в кэше
плагина). Замена: `repo_root()` = `git rev-parse --show-toplevel` от **cwd** (Claude Code
запускает хуки в каталоге проекта; git-хуки и make — в корне репо), с `lru_cache`.

- Module-level пути (`AUDIT_LOG`, `LEDGER_DIR`, `FINDINGS_DIR`, `DESIGN_MARKER`,
  `LAST_DEPLOYED`, `LAST_REVIEWED`) **остаются атрибутами модуля**, вычисляются при импорте
  от `repo_root()` — это сохраняет monkeypatch-точки всех 124+41 портируемых тестов.
- cwd вне git-репо: атрибуты становятся None; хук-энтрипоинты (gate-edit/gate-bash/
  clear-marker) при None → exit 0 (см. таблицу выше); явные CLI-команды → явная ошибка
  «не git-репозиторий» (fail-closed).
- Env-оверрайды `CODEX_LEDGER_DIR` / `CODEX_FINDINGS_DIR` сохраняются (их используют тесты
  и make-субпроцессы).
- `ladder_gate` уже принимает `root: Path` параметром во всех функциях — образец; его CLI
  по-прежнему `Path.cwd()`. Чтение конфига (`ladder.enabled`, `epoch_sha`) — от переданного
  `root` (не от import-time cwd); `LADDER_EPOCH_SHA` остаётся module-атрибутом-оверрайдом
  для тестов: `None` → читать конфиг root'а, иначе — использовать значение атрибута.

## Решение 3 — git-хуки: глоб-шимы, переживающие обновления плагина

Git-хуки бегут вне Claude Code — `${CLAUDE_PLUGIN_ROOT}` недоступен, а путь к кэшу содержит
версию (`~/.claude/plugins/cache/lenar-gates/gates/<version>/`), меняющуюся при обновлении.
Шим в целевом репо (`.githooks/pre-commit`, ставит gates-init):

```bash
#!/usr/bin/env bash
# gates shim: авторитетный резолв установленной версии плагина (переживает обновления)
P=$(python3 - <<'EOF'
import json, os, sys
meta = os.path.expanduser("~/.claude/plugins/installed_plugins.json")
try:
    d = json.load(open(meta))
except Exception:
    print("FALLBACK"); sys.exit(0)   # метаданные нечитаемы → глоб-фолбэк
entries = (d.get("plugins") or {}).get("gates@lenar-gates") or []
for e in entries:                     # несколько scope'ов → первый с реально существующим путём
    ip = e.get("installPath") or ""
    if not os.path.isabs(ip):         # пустой/относительный путь (дрейф схемы) НЕ должен
        continue                      # резолвиться в repo-local scripts/ от cwd хука (R3)
    p = os.path.join(ip, "scripts")
    if os.path.isfile(os.path.join(p, "ladder_gate.py")):
        print(p); sys.exit(0)
print("")                             # метаданные читаемы, записи/пути нет → плагин НЕ установлен
EOF
)
if [ "$P" = "FALLBACK" ]; then
  P=$(ls -d "$HOME"/.claude/plugins/cache/lenar-gates/gates/*/scripts 2>/dev/null | sort -V | tail -1)
fi
if [ -z "$P" ] || [ ! -f "$P/ladder_gate.py" ]; then
  echo "[gates] плагин gates@lenar-gates не установлен (installed_plugins.json) —" >&2
  echo "[gates] коммит ПРЕРВАН (fail-closed). Установи: /plugin install gates@lenar-gates" >&2
  echo "[gates] Осознанный обход: LADDER_SKIP=1 git commit ... (аудит на деплой-гейте)" >&2
  exit 1
fi
exec python3 "$P/ladder_gate.py" check-precommit
```

- **Авторитетный резолв first** (Codex R1/R2-фиксы): версия берётся из
  `~/.claude/plugins/installed_plugins.json` (`installPath` — то, что реально активно у
  Claude Code); из нескольких scope-записей выбирается первая с реально существующим
  скриптом. **Читаемые метаданные БЕЗ записи плагина = «удалён» → fail-closed абортом**,
  протухший кэш глобом НЕ подхватывается; глоб `sort -V` — фолбэк только при нечитаемых
  метаданных. Перед exec проверяется существование самого скрипта.
- Пустой резолв → **громкая ошибка + abort коммита** (fail-closed), с инструкцией установки
  и явным escape (`LADDER_SKIP=1`). Тот же паттерн, что `resolve_companion_cmd`.
- `post-commit`-шим аналогичен, но никогда не абортит (git не умеет): пустой глоб → громкий
  stderr-warning, `exit 0`; отсутствие ledger-записи безопасно — деплой-гейт заблокирует.
- `sort -V` выбирает максимальную версию (кэш может держать несколько).
- Интерпретатор — `python3` (не `python3.13`): целевые машины разные; скрипты совместимы
  с 3.9+ (`from __future__ import annotations`).
- PyYAML может отсутствовать в системном python3: `import yaml` в скриптах становится
  опциональным (`try/except ImportError → yaml=None`), отсутствие = «конфиг нечитаем» =
  строгий режим (решение 1) + громкий warning с подсказкой `pip install pyyaml`.

## Решение 4 — скилл `/gates-init`: онбординг проекта одной командой

Шаги скилла (каждый идемпотентен, существующее не перезаписывается молча):

1. **Проверить Codex-плагин**: глоб `~/.claude/plugins/cache/openai-codex/codex/*/scripts/codex-companion.mjs`
   пуст → СТОП с инструкцией (`/plugin marketplace add openai/codex-plugin-cc` →
   `/plugin install codex@openai-codex`, логин ChatGPT).
2. **Сгенерировать `.codex-gate.yaml`**: код-пути — спросить пользователя или вывести из
   структуры репо (предложить кандидатов: каталоги с исходниками, манифесты зависимостей,
   Dockerfile/compose, конфиги приложения); `ladder.epoch_sha = git rev-parse HEAD`
   (вся прошлая история grandfathered, гейты начинаются «с этого момента»).
3. **`AGENTS.md` из скелета, ЕСЛИ отсутствует** — наполнение инвариантами вместе с
   пользователем (образец структуры: blocking-инварианты / medium-low / реестр остатков).
   Существующий AGENTS.md не трогается.
4. **Git-хуки**: скопировать шимы в `.githooks/` + `git config core.hooksPath .githooks`.
5. **Онбординг-коммит сразу** (Codex R2-фикс «окно до первого коммита»): закоммитить
   `.codex-gate.yaml` + `.githooks/` + AGENTS.md одной командой
   `LADDER_SKIP=1 LADDER_SKIP_REASON="gates-init onboarding" git commit ...` — pre-commit
   скипается с аудитом, post-commit пишет skipped-запись → деплой-диапазон покрыт штатной
   механикой (громкая пометка на деплое). Конфиг попадает в HEAD немедленно →
   worktree-OR-HEAD признак онбординга активен с первого момента, окна «конфиг только в
   worktree» нет. Это единственный штатно-рекомендованный LADDER_SKIP.
6. **Makefile-snippet показать/вставить**: `check-reviewed` в deploy-цепочку, deploy-lock
   (atomic `mkdir` + `trap 'rmdir' EXIT` + отдельный `trap 'exit 130' INT TERM` — на macOS
   нет `flock`), `check-decision` прямо перед выкаткой, запись `.claude/.last-deployed-sha`
   после успешной верификации. Деплой у каждого проекта свой → snippet + инструкция,
   молчаливой правки чужого deploy-рецепта нет. В сниппете `$$` (make ест `$`).
7. **Первый baseline**: подсказать `CODEX_DEPLOY_BASELINE=<sha>` для первого деплоя
   (пока нет `.claude/.last-deployed-sha`).
8. **Итоговая сводка**: что включено, какие пути считаются кодом, как выглядит цикл
   (begin/mark → commit → check-reviewed), где escape-hatch'и и что они аудируются.

`.gitignore` целевого репо: gates-init проверяет, что `.claude/.ladder-*`,
`.claude/.design-approved*`, `.claude/.last-*-sha`, `logs/` игнорируются (иначе служебные
маркеры засоряют диффы), и предлагает добавить строки.

## Генерализация при порте (конкретика)

`codex_review_gate.py`:
- `REPO_ROOT` → `repo_root()` (решение 2); все производные пути — от него.
- `CODE_PATH_PREFIXES`/`CODE_PATH_EXACT` → из конфига; жёсткие добавки `.codex-gate.yaml`,
  `.githooks/`, `Makefile` — в коде (решение 1). «Нет конфига» → режим «всё код».
- `HARD_CAP_ROUNDS` → из конфига (дефолт 8, валидация).
- `_REVIEW_FOCUS` — без специфики проекта-источника («money-loss risks per AGENTS.md» остаётся:
  AGENTS.md — конституция целевого репо).
- Фолбэк-импорт `ladder_gate` → `sys.path.insert(0, str(Path(__file__).parent))`
  (плагин-каталог, не `REPO_ROOT/scripts`).

`ladder_gate.py`:
- `ladder_enabled(root)`: читает `.codex-gate.yaml` (было `config.yaml`), та же семантика
  «битый/нет → True».
- `LADDER_EPOCH_SHA` константа → оверрайд-атрибут + чтение `ladder.epoch_sha` из конфига
  root'а (решение 2).
- `import yaml` → опциональный (решение 3).

`hooks/hooks.json` (образец написания путей — hooks.json codex-плагина):
```json
{ "hooks": { "PreToolUse": [
    { "matcher": "Edit|Write|NotebookEdit", "hooks": [{ "type": "command",
      "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/codex_review_gate.py\" gate-edit" }] },
    { "matcher": "Bash", "hooks": [{ "type": "command",
      "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/codex_review_gate.py\" gate-bash" }] }],
  "SessionStart": [ { "hooks": [{ "type": "command",
      "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/codex_review_gate.py\" clear-marker" }] }] } }
```

Тесты (порт ОБОИХ файлов, 124+41):
- Правки только: (а) импорт `scripts.codex_review_gate` → `codex_review_gate` через
  conftest sys.path; (б) фикстуры конфига — tmp-репо, которым нужен не-строгий режим,
  получают свой `.codex-gate.yaml`; `config.yaml`-фикстуры ladder-тестов → `.codex-gate.yaml`.
- conftest.py: autouse-фикстура пинит `g.CODE_PATH_PREFIXES`/`CODE_PATH_EXACT` на
  канонические тестовые значения (`app/`, `tests/`, `scripts/`, `lib/`, …) — тесты
  детерминированы и не зависят от конфига репо-носителя.
- Изоляция-инварианты сохраняются: autouse `CODEX_COMPANION_CMD` инертный,
  `CLAUDE_CODE_SESSION_ID` delenv, `CODEX_FINDINGS_DIR`/`CODEX_LEDGER_DIR` в tmp,
  `LADDER_SKIP`/`CODEX_REVIEW_SKIP` delenv. Тесты не трогают реальные ledger'ы/маркеры.
- Сам плагин-репо дог-фудится: получает свой `.codex-gate.yaml`
  (`plugins/gates/scripts/`, `tests/` — код).

## BSAC — бизнес-сценарии и краевые случаи

Наследуемые сценарии (BS1–BS4 Фазы 1, BS-L1–L4 Фазы 1.5, BS-C1–C3 Фазы 1.6) переносятся
вместе с их тестами. Новая поверхность — только упаковка:

- **BS-P1 (изоляция):** установка плагина НЕ меняет поведение не-онбордженных проектов —
  хуки exit 0 без `.codex-gate.yaml` (ни в worktree, ни в HEAD) и вне git-репо. (тесты:
  gate-edit/gate-bash/clear-marker в не-git cwd и в git-репо без конфига; конфиг в HEAD,
  но удалён из worktree → строгий режим, НЕ exit 0)
- **BS-P2 (не ослабить конфигом):** правка/удаление `.codex-gate.yaml` сама проходит гейты
  (жёсткий код-путь); `DEPLOY_REQUIRED_PASSES` конфигом недостижим. (тесты: is_code_path
  для `.codex-gate.yaml` == True всегда; урезанный конфиг не снимает жёсткие пути)
- **BS-P3 (обновления плагина):** шим находит свежую версию глобом; плагин удалён →
  pre-commit громко абортит (fail-closed), post-commit громко предупреждает, деплой-гейт
  блокирует. (смоук: шим с пустым глобом → exit 1 с инструкцией)
- **BS-P4 (строже при мисконфиге):** битый YAML / нет PyYAML / нет конфига при явном
  вызове → строгий режим «всё код», громкий warning, никакой тихой деградации. (тесты:
  is_code_path в строгом режиме; ladder_enabled на битом конфиге)

Денежные/критичные краевые случаи новой поверхности:
- **ML-P1 ослабление через конфиг** → закрыто жёсткими код-путями + деплой-гейт видит
  правку конфига (выше).
- **ML-P2 протухший шим после обновления плагина** → глоб `*/scripts` + `sort -V` всегда
  берёт свежую версию; шим не содержит версии.
- **ML-P3 хуки терроризируют чужие проекты** (не денежный, но доверие-разрушающий) →
  opt-in по наличию конфига (BS-P1).
- **ML-P4 нет PyYAML на целевой машине** → строгий режим + громкая подсказка, не тихий
  пропуск и не traceback.
- **ML-P5 плагин удалён между коммитом и деплоем** → ladder-ledger уже записан или
  отсутствует; отсутствие → деплой-гейт fail-closed (наследованная механика BS-L3).
- **ML-P6 временное удаление конфига (delete → edit → restore)** → закрыто worktree-OR-HEAD
  признаком онбординга (решение 1): пропажа конфига в онбордженном репо = строгий режим.

## Реестр остатков (Codex-ревью спеки R1; стоп-политика v2 — не пере-флагать)

- **AGENTS.md вне жёстких код-путей** — наследованный by-design остаток источника (Фаза 1.5
  R6): конституция ревьюера — .md, гейтить её G1/лесенкой = циклическая зависимость
  дизайн-гейта. Компенсация: ослабление AGENTS.md видно Codex-ревью деплой-диффа (файл в
  `baseline..HEAD`); авторитетный pin конституции — Фаза 2 источника. gates-init существующий
  AGENTS.md не валидирует (наполнение — с пользователем).
- **G1-слой — session-scoped best-effort, cwd-rooted** — наследованное ограничение Фазы 1
  («обходим, не заявляет негеймибельность»): правка вложенного/соседнего онбордженного репо
  из сессии с другим cwd, checkout до-онбординговой ревизии, произвольный shell — вне охвата
  G1. Дизайн-гейт fail-open by design (BS3-асимметрия: мышление не стопорится); денежная
  защита — деплой-цепочка, она явная и fail-closed от корня целевого репо.
- **Version-skew pre/post-commit при обновлении плагина посреди коммита** — niche: шим
  резолвит авторитетно (installed_plugins.json), post-commit и так не абортит, ledger-tree
  валидируется на деплое независимо от версии, писавшей запись.

## Известные грабли (кровью 22.07, обязательны к сохранению при порте)

- Схема review-output Codex-плагина: `additionalProperties:false` — сигналы только
  title-префиксами `[DUP:]`/`[DISPUTE:]`; парсер JSON-first + строгий текст-фолбэк —
  **логику не трогать**.
- macOS без `flock` CLI → deploy-lock через atomic `mkdir` + два отдельных `trap`.
- `git diff HEAD` ≠ снимок коммита → только `git write-tree`; tmp-индекс строить С НУЛЯ
  (racy-index: copy сбрасывает mtime → stale blob).
- `resolved-by-user` терминален; carry-over только пост-hard-cap не-critical неоспоренных.
- Тесты не трогают боевые ledger'ы (инцидент: тест заархивировал реальную findings-серию).

## Что НЕ делаем (осознанно)

- Не переписываем логику гейтов/протокола — только порт с генерализацией привязки.
- Не решаем кросс-машинный ledger/baseline и негеймибельность — остатки Фазы 2 источника,
  наследуются как задокументированные.
- Не публикуем на GitHub в этой итерации (установка с локального пути; пуш — следующий шаг).
- Не энфорсим наличие Makefile-гейта в целевом проекте: gates-init показывает snippet,
  решение о встраивании — за пользователем (деплой у всех свой).

## Тестирование

- Портированные 124+41 тестов зелёные из корня репо — регресс порта.
- Новые юнит-тесты: opt-in-матрица хуков (BS-P1), строгий режим is_code_path без конфига
  (BS-P4), чтение code_paths/hard_cap/epoch из `.codex-gate.yaml`, жёсткие код-пути
  (BS-P2), hard_cap-валидация.
- Живой смоук онбординга (Definition of done HANDOFF): scratch-репо в /tmp → шаги
  gates-init руками → код-коммит без лесенки блокируется шимом; докс-коммит проходит;
  `check-reviewed` гоняет Codex (стаб/реальный) и пишет ledger.
