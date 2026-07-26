# Codex CLI как хост гейтов: что измерено, что нет (тикет C)

Дата: 2026-07-26. Версия Codex: **0.145.0** (`/opt/homebrew/Caskroom/codex/0.145.0`).
Статус: исследование. **Реализацию блокирует один незакрытый замер (§5).**

Задача тикета: те же гейты, но хост — Codex, а зеркальное ревью делает Claude.

## 1. Главный вывод

Codex CLI имеет систему хуков и плагинов, **wire-совместимую с Claude Code**. Это меняет оценку
работы с «портировать заново» на «добавить второй манифест и хост-адаптер».

## 2. Что совпадает (измерено: строки бинаря + официальные доки)

| Что | Claude Code | Codex 0.145.0 |
|---|---|---|
| События | PreToolUse, PostToolUse, SessionStart, UserPromptSubmit, Stop, PreCompact, SubagentStart/Stop | **те же** + `PermissionRequest`, `SessionEnd`, `PostCompact` |
| Вход хука | stdin JSON | **те же поля**: `session_id`, `cwd`, `hook_event_name`, `transcript_path`, `permission_mode`, `tool_name`, `tool_input`, `turn_id` |
| Блокировка | exit 2 + stderr, либо `hookSpecificOutput.permissionDecision: "deny"` | **идентично**, включая `permissionDecisionReason` |
| Файл хуков в плагине | `hooks/hooks.json` | **то же имя** |
| Скиллы | `skills/<name>/SKILL.md` + frontmatter | **тот же формат** |
| Корень плагина в env | `CLAUDE_PLUGIN_ROOT` | `PLUGIN_ROOT`/`PLUGIN_DATA` **плюс алиасы `CLAUDE_PLUGIN_ROOT`/`CLAUDE_PLUGIN_DATA`** «для совместимости с существующими plugin hooks» |
| Matcher | regex по имени инструмента | regex; для правок файлов принимает `apply_patch`, а также **`Edit`/`Write`** как алиасы |
| Имена инструментов | `Bash`, `Edit`, `Write` | `Bash`; правки — `apply_patch` (в hook input `tool_name` всегда `apply_patch`); MCP — `mcp__<server>__<tool>` |

Практическое следствие: наш `hooks/hooks.json` с `${CLAUDE_PLUGIN_ROOT}` в командах уезжает
в Codex **почти дословно**.

## 3. Что различается

- **Манифест:** `.codex-plugin/plugin.json` (у нас `.claude-plugin/plugin.json`). Поля близки:
  `name`, `version`, `description`, `skills`, `hooks`, `mcpServers`, `apps`, `interface{…}`.
  Внутри `.codex-plugin/` лежит ТОЛЬКО манифест; `skills/`, `hooks/`, `assets/` — в корне плагина.
- **Маркетплейс:** `$REPO_ROOT/.agents/plugins/marketplace.json` либо `~/.agents/plugins/…`.
  Источники: `local` (path), `git-subdir` (url+path+ref), `npm`. Схема отличается от нашей
  `.claude-plugin/marketplace.json`, но файлы могут лежать рядом.
- **Конфиг хуков вне плагина:** `~/.codex/hooks.json`, `~/.codex/config.toml` (`[[hooks.PreToolUse]]`),
  `<repo>/.codex/hooks.json`, `<repo>/.codex/config.toml`. Проектный слой работает только если
  `.codex/` доверен.
- **Доверие к хукам:** перед исполнением не-managed командных хуков Codex требует ревью
  (`/hooks`), доверие пиньуется по ХЭШУ — изменённый хук требует повторного подтверждения.
  Для автоматизации есть `--dangerously-bypass-hook-trust`. **Это дополнительный шаг онбординга,
  которого у Claude Code нет.**
- **Таймауты:** дефолт 600 с; у `SessionEnd` дефолт 1 с (максимум 3).
- **Своё событие `PermissionRequest`** с полем `permissionDecision` — потенциальная точка для
  G1, более естественная, чем PreToolUse.

## 4. Что придётся написать

1. **Адаптер ревьюера claude** рядом с `run_cursor_review`: `claude -p --output-format json`
   (headless-режим есть, проверен `--help`). ~80 строк; абстракция провайдеров из 0.4.0 готова.
2. **Инверсия правила независимости — единственное опасное место.** Сейчас allow-list намертво
   банит `claude-*`, потому что автор — Claude. Настоящий инвариант: *семейство ревьюера ≠
   семейство автора*. Нужна явная `HOST_FAMILY` и allow-list, выводимый из неё
   (host=codex → бан `gpt-*`/`codex-*`). При «универсализации» этот инвариант теряется молча —
   получится Codex, ревьюящий Codex, и главная гарантия испарится без красных проверок.
3. **Хост-адаптер:** `session_id` из stdin (сейчас из `CLAUDE_CODE_SESSION_ID`), нормализация
   `tool_input` для `apply_patch` (форма отличается от `Edit`/`Write`), маппинг «проход → команда
   хоста» для лесенки.
4. **Второй манифест** `.codex-plugin/plugin.json` + `.agents/plugins/marketplace.json` рядом
   с существующими; общие `skills/` и `hooks/`.
5. **Онбординг:** шаг подтверждения доверия хукам (`/hooks`), которого нет в Claude Code.

**Что НЕ требует работы:** оба Python-скрипта, `prepush_gate.py`, шимы `.githooks/` (самый
сильный слой — он на уровне git и хосту безразличен), `.codex-gate.yaml`, ledger, протокол
сходимости, эмпирический гейт, интерфейс к inframon. `AGENTS.md` — вообще нативный формат
конституции Codex.

## 5. ЧТО НЕ ИЗМЕРЕНО (блокирует реализацию)

Контракт подтверждён **строками бинаря и доками, но живьём хук в Codex не запускался.** После
пяти редакций спеки A, где четыре дефекта из пяти были «перенёс механику, потеряв различение»,
проектировать на непроверенном контракте нельзя.

**Блокирующий эксперимент:** положить в `<repo>/.codex/hooks.json` логгер stdin на `PreToolUse`,
подтвердить доверие, выполнить `codex exec` с задачей, требующей `apply_patch` и `Bash`, и
сверить фактический JSON с таблицей §2 — особенно:

- реальные значения `tool_name` и форму `tool_input` для `apply_patch`;
- работает ли exit 2 как deny (и попадает ли stderr к модели);
- выставляется ли `CLAUDE_PLUGIN_ROOT` для хуков **из плагина** (не из локального конфига);
- присутствует ли `session_id` и совпадает ли он между событиями одного прогона.

**Замер заблокирован квотой Codex** (исчерпана, восстановление ~28.07.2026): `codex exec`
требует модельного вызова, без него до PreToolUse дело не доходит.

## 6. Рекомендация по упаковке

**Один плагин с двумя манифестами**, не два плагина и не форк. Причина не в удобстве: свод
правил (лесенка, сходимость, ledger) обязан быть одним кодом — расхождение семантики между
хостами было бы хуже любой дубликации. Каталоги `skills/` и `hooks/` общие; различаются
манифесты и ~100 строк хост-адаптера.
