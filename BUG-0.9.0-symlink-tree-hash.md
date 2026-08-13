# gates 0.9.0: `compute_tree` падает на tracked-симлинке, лесенка неисполнима

**Severity: critical.** Гейт не «строгий», а фактически выключенный: в затронутом
репозитории ни один code-коммит не может пройти лесенку, единственный путь — `LADDER_SKIP=1`.

## Симптом

Любая команда лесенки в репозитории `wb-ad-ruler`:

```
$ bash .githooks/gates-run ladder_gate.py begin simplify
codex_review_gate.TrustedGitError: не посчитать хэш '.agents/skills/actuator-safety'
                                   — tree-хэш не посчитать
```

Падает `begin`, `mark` и `check-precommit` — то есть и сам проход, и pre-commit хук.

## Версия и датировка

- Сломанная версия: **0.9.0**, `installPath .../cache/lenar-gates/gates/0.9.0`,
  `gitCommitSha f5a81c2` (= `chore: релиз gates 0.9.0 — слой, арбитр, бюджет ревью (#9)`).
- `installedAt 2026-08-11T12:54:31Z`. Последняя успешная запись в `logs/ladder_ledger/`
  того же репозитория — **11 авг 12:55**. После апгрейда лесенка не отработала ни разу.
- 0.8.0 на этом же дереве работал.

## Корень

`scripts/ladder_gate.py`, `compute_tree`, строки ~215-223:

```python
if full.is_symlink():
    mode = "120000"                                    # режим определён верно
elif full.is_file():
    mode = "100755" if os.access(full, os.X_OK) else "100644"
else:
    continue
h = _trusted_git("hash-object", "--no-filters", "-w", "--", rel, cwd=root)   # ← здесь
```

Режим для симлинка выставляется правильно, но хэш всё равно берётся `hash-object` **по пути**.
`hash-object` разыменовывает симлинк и читает цель. Если цель — **директория**, git отвечает:

```
$ git hash-object --no-filters -w -- .agents/skills/actuator-safety
fatal: Unable to add (null) to database          (exit 128)
```

→ `returncode != 0` → `TrustedGitError` → лесенка неисполнима.

**Почему 0.8.0 не падал:** ветки `is_symlink()` там не было, а `Path.is_file()` для
симлинка-на-директорию ложно → `continue`. Симлинки просто не попадали в tree-хэш
(латентная дыра, но fail-safe по доступности). Релиз 0.9.0 честно попытался их учесть
и превратил тихий пропуск в жёсткое падение.

**Кого задевает:** любой репозиторий, где есть tracked-симлинк на директорию. В
`wb-ad-ruler` это не случайность, а требование конституции: `.agents/skills/<name>` —
симлинк на `.claude/skills/<name>/`, чтобы Codex и Claude читали один физический skill
(AGENTS.md, раздел «Общие skills»). Таких симлинков там 8.

## Воспроизведение (30 секунд, чистый репозиторий) — ПРОВЕРЕНО

```bash
mkdir -p /tmp/g && cd /tmp/g && git init -q . && mkdir -p real/sub && echo x > real/sub/f
ln -s real/sub link && git add -A && git commit -qm init
python3.13 ~/.claude/plugins/cache/lenar-gates/gates/0.9.0/scripts/ladder_gate.py begin simplify
# → TrustedGitError: не посчитать хэш 'link' — tree-хэш не посчитать
```

Специфика `wb-ad-ruler` не нужна: хватает одного симлинка на директорию.

## Фикс — проверен на копии движка

Симлинк надо хэшировать по **тексту ссылки**: блоб симлинка в git равен ровно тексту цели
(без завершающего перевода строки). Готового канала для этого в движке нет — `_trusted_git`
не умеет stdin, поэтому патч в двух файлах.

`codex_review_gate.py`:

```diff
-def _trusted_git(*args: str, cwd: "str | Path | None" = None
+def _trusted_git(*args: str, cwd: "str | Path | None" = None,
+                 stdin_text: "str | None" = None
                  ) -> "subprocess.CompletedProcess | None":
@@
         return subprocess.run([git, *_GIT_NEUTRALIZE, *args],
                               cwd=str(cwd) if cwd else REPO_ROOT,
-                              capture_output=True, text=True, env=env)
+                              capture_output=True, text=True, env=env,
+                              input=stdin_text)
```

`ladder_gate.py`, `compute_tree`:

```diff
-            h = _trusted_git("hash-object", "--no-filters", "-w", "--", rel, cwd=root)
+            if mode == "120000":
+                # hash-object по ПУТИ разыменовывает ссылку и умирает на цели-директории
+                # (`fatal: Unable to add (null) to database`). Блоб симлинка в git — это
+                # ровно текст цели, без завершающего перевода строки.
+                h = _trusted_git("hash-object", "--no-filters", "-w", "--stdin",
+                                 cwd=root, stdin_text=os.readlink(full))
+            else:
+                h = _trusted_git("hash-object", "--no-filters", "-w", "--", rel, cwd=root)
```

Проверка не «не падает», а на равенство эталону — пропатченный `compute_tree` на чистом
репро-репозитории даёт ровно git-ов tree-хэш:

```
compute_tree            → fa13ce3a45ba9ca353a43e9e550a72824b4130aa
git rev-parse HEAD^{tree} → fa13ce3a45ba9ca353a43e9e550a72824b4130aa
```

И на реальном симлинке `wb-ad-ruler` блоб совпадает с тем, что хранит git:

```
git rev-parse HEAD:.agents/skills/actuator-safety            → 734c31b9d7673d9c55281af14851370f6a494a8c
printf '%s' "$(readlink ...)" | git hash-object --stdin      → 734c31b9d7673d9c55281af14851370f6a494a8c
```

Патч накладывался на копию `cache/lenar-gates/gates/0.9.0/scripts`; **сам плагин и его
исходник `~/src/claude-gates` не тронуты** — решение и релиз за владельцем движка.

Регресс-тест: фикстура-репозиторий с симлинком **на директорию** (симлинк на файл этот
баг не ловит — `hash-object` по нему проходит и молча хэширует содержимое цели, что даёт
неверный, но не падающий tree-хэш; тест стоит сделать на обоих).

## Отдельная находка: поломка движка маскируется под «ревью не пройдено»

Когда `compute_tree` падает, `check-precommit` печатает штатное:

```
[ladder-gate] цепочка simplify → code-review → security не подтверждена для коммита.
...
Обход (осознанно, с аудитом): LADDER_SKIP=1 ... git commit
```

Оператор видит ровно то же сообщение, что и при честно не пройденной лесенке. Сигнала
«движок сломан, проход физически невозможен» нет — и подсказка в конце ведёт прямо к
`LADDER_SKIP`. То есть отказ инфраструктуры выглядит как собственная забывчивость и
подталкивает к обходу гейта. Так поломка и прожила двое суток незамеченной.

Просьба развести два состояния: `TrustedGitError` (и любой отказ движка) должен давать
отдельный громкий текст «ДВИЖОК ГЕЙТА СЛОМАН, лесенка неисполнима», а не сливаться с
«проходы не отмечены». Это же относится к тексту самой ошибки: `не посчитать хэш X` не
содержит stderr от git (`fatal: Unable to add (null) to database`) — без него причина
не диагностируется, пришлось воспроизводить `hash-object` руками.
