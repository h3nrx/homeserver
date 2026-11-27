#!/bin/bash
# Sets the admin password. Only a hash of it is saved.
set -euo pipefail
cd "$(dirname "$0")"

read -rsp "New admin password (at least 12 characters): " first; echo
read -rsp "Once more to check: " second; echo

if [ "$first" != "$second" ]; then echo "ERROR: The passwords don't match."; exit 1; fi
if [ "${#first}" -lt 12 ]; then echo "ERROR: At least 12 characters."; exit 1; fi

mkdir -p panel-data
umask 077
printf '%s' "$first" | docker run --rm -i --network none -v "$PWD/panel:/app:ro" node:22-alpine node /app/hash.js > panel-data/auth.json.tmp
mv panel-data/auth.json.tmp panel-data/auth.json
unset first second
echo PASSWORD-SET
