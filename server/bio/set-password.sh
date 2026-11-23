#!/bin/bash
# Setzt das Admin-Passwort. Das Passwort wird nur als Hash gespeichert.
set -euo pipefail
cd "$(dirname "$0")"

read -rsp "Neues Admin-Passwort (mind. 12 Zeichen): " first; echo
read -rsp "Nochmal zur Kontrolle: " second; echo

if [ "$first" != "$second" ]; then echo "FEHLER: Die Passwoerter sind nicht gleich."; exit 1; fi
if [ "${#first}" -lt 12 ]; then echo "FEHLER: Mindestens 12 Zeichen."; exit 1; fi

mkdir -p panel-data
umask 077
printf '%s' "$first" | docker run --rm -i --network none -v "$PWD/panel:/app:ro" node:22-alpine node /app/hash.js > panel-data/auth.json.tmp
mv panel-data/auth.json.tmp panel-data/auth.json
unset first second
echo PASSWORT-GESETZT
