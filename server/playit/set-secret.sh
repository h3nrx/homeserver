#!/bin/bash
# Speichert den playit-Key geschuetzt und startet den Agent.
set -euo pipefail
cd "$(dirname "$0")"

read -rsp "SECRET_KEY von playit einfuegen (Rechtsklick), dann Enter: " input; echo
if [ -z "$input" ]; then echo "FEHLER: Es ist nichts angekommen. Bitte mit Rechtsklick einfuegen."; exit 1; fi
key="$(printf '%s' "$input" | grep -Eo '[a-f0-9]{32,128}' | head -n 1 || true)"
if [ -z "$key" ]; then echo "FEHLER: Kein Key erkannt (${#input} Zeichen angekommen)."; exit 1; fi
unset input

umask 077
printf 'SECRET_KEY=%s\n' "$key" > .env
unset key
docker compose up -d
echo PLAYIT-GESTARTET
