---
description: Дизайн с альтернативами и BSAC, независимое Codex-ревью и session-bound маркер
---

Выполни общий Skill `design-review` для текущей задачи. До первой правки кода запиши дизайн в `docs/`: проблему, сильные альтернативы, бизнес-сценарии, приёмочные критерии и краевые случаи. Прогони независимое Codex-ревью через `companion-review --design-file` и устрани blocking-находки.

На Kimi для записи маркера используй одноразовый session handoff. Сгенерируй 32 hex-символа
и подставь их в команду ЛИТЕРАЛОМ (не `$NONCE` и не `$(python3 …)`: hook должен видеть nonce):

```bash
F=docs/<design>.md
H=$(shasum -a 256 "$F" | cut -d' ' -f1)
GATES_SESSION_NONCE=<32-hex-literal> /bin/sh .githooks/gates-run codex_review_gate.py \
  write-marker design "codex approved: <кратко>" "$H" --file "$F"
```

Nonce используется один раз. Не записывай маркер в `nosession`, не помечай непройденное ревью и не заменяй Codex самопроверкой Kimi.
