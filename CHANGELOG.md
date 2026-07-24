# Changelog

## 0.2.0 — 2026-07-24

Все фичи прошли цикл: спека → Codex adversarial-review до approve → TDD → лесенка →
Codex-ревью кода до approve.

- **Эмпирический гейт** (#1): `empirical.test_command` как условие деплоя, порядок
  ladder → empirical → Codex; трёхстатусная модель конфига (absent доказывается `git
  ls-tree`, git/parse-сбой = unreadable = блок); снятие/подмена команды после включения —
  блок без аудируемого `EMPIRICAL_SKIP`; биндинг всего прогона к `head_before`; argv без
  shell.
- **Дрейф-детектор design-маркера** (#3): маркер биндится к дизайн-файлу (reviewed-hash из
  результата ревью, set-модель для нескольких дизайнов, per-session lock); правка дизайна
  после пометки → G1 блокирует правки кода до ре-ревью. Методология: пост-ревью правки =
  ре-ревью; destructive-операции — blocking-категория конституции.
- **Структурная валидация BSAC** (#2): file-маркер требует секцию сценариев/BSAC/EARS
  (пере-выводится из hash-валидированного контента — анти-разъезд версий; EARS — токен,
  YEARS/APPEARS не ловятся); стаб рушит coarse-маркер.
- **Интерфейс к внешнему guard'у (inframon)**: `deploy.baseline_command` — authoritative
  deployed-SHA (no-fallback при отказе, pin секции против самоскрывающихся изменений,
  аудируемые env-переходы); машиночитаемый вердикт `logs/review_verdicts/<sha>.json`
  (schema 1, run_id, скипы видимы включая исторические `covered-with-skips`,
  delete-then-write под локом).
- Рефакторы: generic SHA-bound читатель секций конфига, общий `_atomic_write_json`,
  `_design_gate`. Тесты: 192 → 278.

## 0.1.0 — 2026-07-22

Порт боевой системы гейтов в плагин: G1 дизайн-гейт (PreToolUse-хуки, opt-in по
`.codex-gate.yaml` worktree-OR-HEAD), enforced-лесенка `/simplify`→`/code-review`
(begin/mark, tree-chain, git-хуки-шимы с авторитетным резолвом версии), commit-bound
деплой-гейт `check-reviewed` с протоколом сходимости (finding-ledger, адъюдикации,
carry-over, эскалация). Конфиг `.codex-gate.yaml` со строгими дефолтами и жёсткими
код-путями; анти-лаундеринг незастейдженного конфига; скиллы `design-review`/`gates-init`;
шаблоны (AGENTS-скелет, Makefile-snippet, githooks). 192 теста.
