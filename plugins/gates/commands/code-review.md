---
description: Независимое Codex code-review текущего working tree по стоп-политике
---

Запусти независимое ревью, а не саморевью Kimi:

```bash
/bin/sh .githooks/gates-run codex_review_gate.py companion-review \
  --scope working-tree "Проверь корректность, fail-open, безопасность, регрессии и тесты новых веток по AGENTS.md"
```

Код 0 — единственный признак состоявшегося ревью. Разбери находки по стоп-политике: fail-open и корректностные баги исправь и перепроверь; осознанные fail-safe/niche остатки зафиксируй. Не заменяй Codex собственным вердиктом Kimi и не помечай ladder при недоступном или невалидном ревью.
