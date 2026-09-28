# BinanceSpotManager — commandes courantes. `make` ou `make help` pour la liste.
# Tout s'execute dans des conteneurs : seuls Docker et make sont requis sur l'hote.

COMPOSE ?= docker compose
SERVICE ?= ui
STAMP := $(shell date +%Y%m%d-%H%M%S)

.DEFAULT_GOAL := help
.PHONY: help init build up down restart ps logs worker-start worker-stop worker-restart \
        check open-orders demo-tests integration migrate-oco run test shell backup import-data

help: ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

init: ## Cree .env a partir de .env.example (sans ecraser un .env existant)
	@if [ -f .env ]; then echo ".env existe deja : inchange"; \
	else cp .env.example .env && echo ".env cree : renseigner les cles Demo"; fi

build: ## Construit l'image
	$(COMPOSE) build

up: ## Demarre l'interface et le worker en arriere-plan
	$(COMPOSE) up -d --build
	@echo "Interface : http://127.0.0.1:$${BSM_UI_PORT:-8501}"

down: ## Arrete et supprime les conteneurs (les volumes de donnees sont conserves)
	$(COMPOSE) down

restart: ## Redemarre les services (a faire apres une modification de .env)
	$(COMPOSE) up -d --force-recreate

ps: ## Etat des services et des healthchecks
	$(COMPOSE) ps

logs: ## Suit les journaux de tous les services (ou SERVICE=worker|ui)
	$(COMPOSE) logs -f --tail=200 $(if $(filter command line,$(origin SERVICE)),$(SERVICE),)

worker-start: ## Demarre le conteneur worker
	$(COMPOSE) start worker

worker-stop: ## Arrete le conteneur worker (SIGTERM, fin de boucle propre)
	$(COMPOSE) stop worker

worker-restart: ## Redemarre le conteneur worker (remplace l'arret force du Dashboard)
	$(COMPOSE) restart worker

check: ## Verifie la connexion Binance Demo (aucun ordre cree)
	$(COMPOSE) run --rm --no-deps worker python scripts/check_connection.py

open-orders: ## Ordres ouverts Binance et rapprochement local, lecture seule (SYMBOL=BTCUSDT)
	$(COMPOSE) run --rm --no-deps worker python scripts/check_open_orders.py $(if $(SYMBOL),--symbol $(SYMBOL),)

demo-tests: ## Essais Demo en lecture seule (EXECUTE=1 ajoute /order/test, sans execution)
	$(COMPOSE) run --rm --no-deps worker python scripts/demo_tests.py $(if $(EXECUTE),--execute,)

integration: ## Tests d'integration Demo en lecture seule (cles .env, sans les volumes de donnees)
	$(COMPOSE) run --rm --build integration

migrate-oco: ## Migre une position vers un OCO Demo, ecritures reelles (POSITION=id EXECUTE=1)
	@test -n "$(POSITION)" -a "$(EXECUTE)" = "1" || { \
		echo "Usage : make migrate-oco POSITION=<position_id> EXECUTE=1"; \
		echo "Ecrit sur Binance Demo ; l'apercu se consulte dans le Dashboard."; exit 1; }
	$(COMPOSE) run --rm --no-deps worker python scripts/migrate_oco_demo.py --position-id $(POSITION) --execute

run: ## Commande libre dans un conteneur relie aux donnees (CMD="python scripts/...")
	@test -n "$(CMD)" || { echo 'Usage : make run CMD="python scripts/<script>.py ..."'; exit 1; }
	$(COMPOSE) run --rm --no-deps worker $(CMD)

test: ## Tests hors ligne dans un conteneur jetable, sans volume ni secret (TESTS="tests/test_x.py")
	$(COMPOSE) run --rm --build test $(if $(TESTS),python -m pytest -p no:cacheprovider $(TESTS),)

shell: ## Shell dans un service en cours d'execution (SERVICE=ui|worker)
	$(COMPOSE) exec $(SERVICE) sh

backup: ## Archive data/ et logs/ dans ./backups (worker arrete le temps de la copie)
	@mkdir -p backups
	$(COMPOSE) stop worker
	@$(COMPOSE) run --rm --no-deps -T worker tar czf - -C /app data logs > backups/bsm-$(STAMP).tar.gz; \
	status=$$?; $(COMPOSE) start worker; \
	if [ $$status -eq 0 ]; then echo "Sauvegarde : backups/bsm-$(STAMP).tar.gz (non chiffree)"; \
	else rm -f backups/bsm-$(STAMP).tar.gz; fi; \
	exit $$status

import-data: ## Importe ./data (installation locale) dans un volume Docker encore vierge
	@test -d data || { echo "Aucun dossier ./data a importer"; exit 1; }
	@echo "Le worker local (hors Docker) doit etre arrete avant l'import."
	$(COMPOSE) stop worker ui
	@$(COMPOSE) run --rm --no-deps --user root -v "$(CURDIR)/data:/import:ro" worker sh -c '\
		if [ -n "$$(ls -A /app/data/positions 2>/dev/null)" ] || [ -e /app/data/order_intents.sqlite3 ]; then \
			echo "Volume Docker deja utilise : import refuse (rien n a ete modifie)"; exit 1; fi; \
		cp -a /import/. /app/data/ \
		&& rm -f /app/data/bot_worker.lock /app/data/bot_runtime.json \
		&& chown -R 10001:10001 /app/data \
		&& echo "Import termine"'; \
	status=$$?; $(COMPOSE) up -d; exit $$status
