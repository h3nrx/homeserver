#!/bin/bash
# Saves the playit key safely and starts the agent.
set -euo pipefail
cd "$(dirname "$0")"

read -rsp "Paste the SECRET_KEY from playit (right-click), then Enter: " input; echo
if [ -z "$input" ]; then echo "ERROR: Nothing arrived. Please paste with a right-click."; exit 1; fi
key="$(printf '%s' "$input" | grep -Eo '[a-f0-9]{32,128}' | head -n 1 || true)"
if [ -z "$key" ]; then echo "ERROR: No key found (${#input} characters arrived)."; exit 1; fi
unset input

umask 077
printf 'SECRET_KEY=%s\n' "$key" > .env
unset key
docker compose up -d
echo PLAYIT-STARTED
