#!/bin/sh
# Refuse de demarrer sans hote ni utilisateur : jamais d'interface publique sans authentification.
set -eu

USERS=/etc/caddy/deploy/users.caddy

if [ -z "${BSM_PUBLIC_HOST:-}" ]; then
	echo "Acces public desactive : renseigner BSM_PUBLIC_HOST dans .env" >&2
	exit 1
fi
if ! grep -Eq '^[A-Za-z0-9_.-]+ \$2[aby]\$' "$USERS" 2>/dev/null; then
	echo "Acces public desactive : aucun utilisateur (make user-add NAME=<nom>)" >&2
	exit 1
fi

exec caddy run --config /etc/caddy/deploy/Caddyfile --adapter caddyfile
