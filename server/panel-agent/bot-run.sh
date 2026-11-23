#!/bin/bash
# Starts the bot's configured main file inside its virtual environment.
set -euo pipefail

BOT_ID="${1:?missing bot id}"
cd "__HOME__/bots/${BOT_ID}"

MAIN="$(/usr/bin/python3 - <<'PY'
import json, re
try:
    with open("meta.json") as f:
        main = json.load(f).get("main") or "bot.py"
except Exception:
    main = "bot.py"
seg = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}"
print(main if re.fullmatch(rf"({seg}/){{0,5}}{seg}\.py", main) else "bot.py")
PY
)"

if [ ! -f "$MAIN" ]; then
  echo "[panel] Main file not found: $MAIN" >&2
  exit 1
fi

echo "[panel] Running $MAIN"
exec .venv/bin/python -u "$MAIN"
