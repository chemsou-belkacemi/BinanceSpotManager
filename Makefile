# BinanceSpotManager — commandes courantes. `make` ou `make help` pour la liste.
# Tout s'execute dans des conteneurs : seuls Docker et make sont requis sur l'hote.

COMPOSE ?= docker compose
SERVICE ?= ui
STAMP := $(shell date +%Y%m%d-%H%M%S)

.DEFAULT_GOAL := help
.PHONY: help init build network up down restart ps logs worker-start worker-stop worker-restart \
        check open-orders demo-tests integration migrate-oco run test shell backup backup-chiffre import-data \
        users user-add user-remove proxy-reload master-key compte

help: ## Affiche cette aide
	@grep -E '^[a-zA-Z_-]+:.*?## ' $(MAKEFILE_LIST) | \
		awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

init: ## Cree .env a partir de .env.example (sans ecraser un .env existant)
	@if [ -f .env ]; then echo ".env existe deja : inchange"; \
	else cp .env.example .env && echo ".env cree : renseigner les cles Demo"; fi

build: ## Construit l'image
	$(COMPOSE) build

network: ## Cree le reseau Docker partage avec CryptoSignalIntelligence (csi-bridge) s'il manque
	@docker network inspect csi-bridge >/dev/null 2>&1 || docker network create csi-bridge

up: network ## Demarre l'interface et le worker en arriere-plan
	$(COMPOSE) up -d --build
	@echo "Interface : http://127.0.0.1:$${BSM_UI_PORT:-8501}"

down: ## Arrete et supprime les conteneurs (les volumes de donnees sont conserves)
	$(COMPOSE) down

restart: network ## Redemarre les services (a faire apres une modification de .env)
	$(COMPOSE) up -d --force-recreate

ps: ## Etat des services et des healthchecks
	$(COMPOSE) ps

logs: ## Suit les journaux de tous les services (ou SERVICE=worker|ui)
	$(COMPOSE) logs -f --tail=200 $(if $(filter command line,$(origin SERVICE)),$(SERVICE),)

worker-start: network ## Demarre le conteneur worker
	$(COMPOSE) start worker

worker-stop: ## Arrete le conteneur worker (SIGTERM, fin de boucle propre)
	$(COMPOSE) stop worker

worker-restart: network ## Redemarre le conteneur worker (remplace l'arret force du Dashboard)
	$(COMPOSE) restart worker

check: network ## Verifie la connexion Binance Demo (aucun ordre cree)
	$(COMPOSE) run --rm --no-deps worker python scripts/check_connection.py

open-orders: network ## Ordres ouverts Binance et rapprochement local, lecture seule (SYMBOL=BTCUSDT)
	$(COMPOSE) run --rm --no-deps worker python scripts/check_open_orders.py $(if $(SYMBOL),--symbol $(SYMBOL),)

demo-tests: network ## Essais Demo en lecture seule (EXECUTE=1 ajoute /order/test, sans execution)
	$(COMPOSE) run --rm --no-deps worker python scripts/demo_tests.py $(if $(EXECUTE),--execute,)

integration: ## Tests d'integration Demo en lecture seule (cles .env, sans les volumes de donnees)
	$(COMPOSE) run --rm --build integration

migrate-oco: network ## Migre une position vers un OCO Demo, ecritures reelles (POSITION=id EXECUTE=1)
	@test -n "$(POSITION)" -a "$(EXECUTE)" = "1" || { \
		echo "Usage : make migrate-oco POSITION=<position_id> EXECUTE=1"; \
		echo "Ecrit sur Binance Demo ; l'apercu se consulte dans le Dashboard."; exit 1; }
	$(COMPOSE) run --rm --no-deps worker python scripts/migrate_oco_demo.py --position-id $(POSITION) --execute

run: network ## Commande libre dans un conteneur relie aux donnees (CMD="python scripts/...")
	@test -n "$(CMD)" || { echo 'Usage : make run CMD="python scripts/<script>.py ..."'; exit 1; }
	$(COMPOSE) run --rm --no-deps worker $(CMD)

test: ## Tests hors ligne dans un conteneur jetable, sans volume ni secret (TESTS="tests/test_x.py")
	$(COMPOSE) run --rm --build test $(if $(TESTS),python -m pytest -p no:cacheprovider $(TESTS),)

shell: ## Shell dans un service en cours d'execution (SERVICE=ui|worker)
	$(COMPOSE) exec $(SERVICE) sh

backup: ## Archive data/ et logs/ dans ./backups (worker arrete ; jamais la cle maitresse)
	@mkdir -p backups
	$(COMPOSE) stop worker
	@$(COMPOSE) run --rm --no-deps -T worker tar czf - -C /app data logs > backups/bsm-$(STAMP).tar.gz; \
	status=$$?; $(COMPOSE) start worker; \
	if [ $$status -eq 0 ]; then echo "Sauvegarde : backups/bsm-$(STAMP).tar.gz (non chiffree)"; \
	else rm -f backups/bsm-$(STAMP).tar.gz; fi; \
	exit $$status

GARDER ?= 14

backup-chiffre: network ## Sauvegarde CHIFFREE (age, cle publique deploy/sauvegarde.age.pub) dans ./backups, 14 gardees (GARDER=<n>)
	@COMPOSE="$(COMPOSE)" bash scripts/sauvegarde_chiffree.sh --garder "$(GARDER)"

master-key: ## Cree la cle maitresse du coffre (volume bsm-keys, hors sauvegardes) si elle manque
	$(COMPOSE) run --rm --no-deps keytool

compte: master-key ## Cree un compte de connexion a l'interface, mot de passe + TOTP (NAME=<identifiant>)
	@echo "$(NAME)" | grep -Eq '^[A-Za-z0-9_.-]{2,32}$$' || { echo "Usage : make compte NAME=<identifiant> [REMPLACER=1]"; exit 1; }
	$(COMPOSE) run --rm --no-deps keytool python scripts/creer_compte.py $(NAME) $(if $(REMPLACER),--remplacer,)

USERS_FILE := deploy/users.caddy

users: ## Liste les utilisateurs de l'acces public
	@if [ -s $(USERS_FILE) ]; then cut -d' ' -f1 $(USERS_FILE); else echo "Aucun utilisateur (make user-add NAME=<nom>)"; fi

user-add: ## Ajoute un utilisateur de l'acces public ou change son mot de passe (NAME=<nom>)
	@echo "$(NAME)" | grep -Eq '^[A-Za-z0-9_.-]{2,32}$$' || { echo "Usage : make user-add NAME=<nom> (lettres, chiffres, . _ -)"; exit 1; }
	@umask 077; printf "Mot de passe pour $(NAME) (12 caracteres minimum) : "; \
	stty -echo; read -r pw; stty echo; echo; \
	[ $${#pw} -ge 12 ] || { echo "Trop court : 12 caracteres minimum"; exit 1; }; \
	hash=$$(printf '%s\n' "$$pw" | docker run --rm -i caddy:2-alpine caddy hash-password) || exit 1; \
	touch $(USERS_FILE); \
	{ grep -v "^$(NAME) " $(USERS_FILE) || true; printf '%s %s\n' "$(NAME)" "$$hash"; } > $(USERS_FILE).tmp; \
	mv $(USERS_FILE).tmp $(USERS_FILE); chmod 600 $(USERS_FILE); \
	echo "Utilisateur $(NAME) enregistre"
	@$(MAKE) --no-print-directory proxy-reload

user-remove: ## Retire un utilisateur de l'acces public (NAME=<nom>)
	@test -n "$(NAME)" || { echo "Usage : make user-remove NAME=<nom>"; exit 1; }
	@grep -q "^$(NAME) " $(USERS_FILE) 2>/dev/null || { echo "Utilisateur $(NAME) inconnu"; exit 1; }
	@[ "$$(grep -vc "^$(NAME) " $(USERS_FILE))" -gt 0 ] || { \
		echo "Refuse : $(NAME) est le dernier utilisateur. En ajouter un autre, ou retirer"; \
		echo "COMPOSE_PROFILES=public de .env puis make down && make up pour fermer l'acces public."; exit 1; }
	@umask 077; grep -v "^$(NAME) " $(USERS_FILE) > $(USERS_FILE).tmp; mv $(USERS_FILE).tmp $(USERS_FILE); \
	echo "Utilisateur $(NAME) retire"
	@$(MAKE) --no-print-directory proxy-reload

proxy-reload: ## Applique la liste des utilisateurs au proxy en cours d'execution
	@if [ -n "$$($(COMPOSE) ps -q --status running proxy 2>/dev/null)" ]; then \
		$(COMPOSE) exec -T proxy caddy reload --config /etc/caddy/deploy/Caddyfile --adapter caddyfile \
		&& echo "Acces public mis a jour" \
		|| { echo "ECHEC du rechargement : l'ancienne liste d'utilisateurs reste active"; exit 1; }; \
	else echo "Proxy arrete : la liste sera appliquee au prochain make up"; fi

import-data: network ## Importe ./data (installation locale) dans un volume Docker encore vierge
	@test -d data || { echo "Aucun dossier ./data a importer"; exit 1; }
	@echo "Le worker local (hors Docker) doit etre arrete avant l'import."
	$(COMPOSE) stop worker ui
	@$(COMPOSE) run --rm --no-deps --user root --cap-add CHOWN --cap-add DAC_OVERRIDE --cap-add FOWNER \
		-v "$(CURDIR)/data:/import:ro" worker sh -c '\
		if [ -n "$$(ls -A /app/data/positions 2>/dev/null)" ] || [ -e /app/data/order_intents.sqlite3 ]; then \
			echo "Volume Docker deja utilise : import refuse (rien n a ete modifie)"; exit 1; fi; \
		cp -a /import/. /app/data/ \
		&& rm -f /app/data/bot_worker.lock /app/data/bot_runtime.json \
		&& chown -R 10001:10001 /app/data \
		&& echo "Import termine"'; \
	status=$$?; $(COMPOSE) up -d; exit $$status
