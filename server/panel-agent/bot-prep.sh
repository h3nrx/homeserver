#!/bin/bash
# Runs before every bot start (as the bot user): makes sure the virtual
# environment exists and installs requirements.txt whenever it changed.
set -euo pipefail

BOT_ID="${1:?missing bot id}"
cd "__HOME__/bots/${BOT_ID}"

if [ ! -x .venv/bin/python ]; then
  echo "[panel] Creating virtual environment"
  rm -rf .venv
  /usr/bin/python3 -m venv .venv
fi

STAMP=.venv/.requirements.sha256
if [ -s requirements.txt ]; then
  want="$(sha256sum requirements.txt | cut -d' ' -f1)"
  have="$(cat "$STAMP" 2>/dev/null || true)"
  if [ "$want" != "$have" ]; then
    echo "[panel] Installing dependencies from requirements.txt"
    .venv/bin/pip install --disable-pip-version-check --no-input --quiet -r requirements.txt
    echo "$want" > "$STAMP"
    echo "[panel] Dependencies ready"
  fi
fi
