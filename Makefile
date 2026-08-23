# Деплой плагина gates на флот. Дизайн: docs/2026-08-23-plugin-deploy-design.md
#
# Pre-deploy гейт (чистое дерево + тесты) живёт В САМОМ скрипте, а не здесь: цель `deploy` —
# удобная обёртка, а не точка контроля. Когда гейт стоял в Makefile, документированный прямой
# вызов `python3 scripts/deploy_gates.py` мутировал флот вообще без проверок.
.PHONY: test check deploy gates-restore deploy-status

test:
	python3 -m pytest tests/ -q

check:                       ## то же, что проверит деплой: чистое дерево + тесты
	@python3 -c "import sys; sys.path.insert(0,'plugins/gates/scripts'); \
	 import codex_review_gate as g; sys.exit(0 if g.working_tree_clean() else 1)" \
	 || { echo "✗ дерево грязное — выкатывать нечего воспроизводимого"; exit 1; }
	@$(MAKE) --no-print-directory test

deploy:                      ## выкатить origin/main на весь флот (канарейка → остальные)
	python3 scripts/deploy_gates.py

gates-restore:               ## вернуть хост на прошлую версию: make gates-restore HOST=…
	@test -n "$(HOST)" || { echo "usage: make gates-restore HOST=<id>"; exit 1; }
	python3 scripts/deploy_gates.py --restore $(HOST)

deploy-status:               ## что сейчас на флоте
	@python3 scripts/deploy_gates.py --status
