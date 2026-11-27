#!/bin/bash
# Runs before Minecraft every time the container starts.
# If the admin panel asked for a reset, the world is created anew.
set -u
SEED_FORMAT='^[A-Za-z0-9_-]{1,32}$'
marker=/control/reset-seed
current=/control/seed

if [ -f "$marker" ]; then
  seed="$(head -c 64 "$marker" | tr -d '\r\n')"
  rm -f "$marker"
  if [[ "$seed" =~ $SEED_FORMAT ]]; then
    echo "[boot] Creating a new world with seed $seed"
    rm -rf -- /data/world /data/world_nether /data/world_the_end /data/plugins/Essentials/userdata
    printf '%s\n' "$seed" > "$current.tmp" && mv -f "$current.tmp" "$current"
  else
    echo "[boot] Invalid seed, ignoring the reset"
  fi
fi

if [ -s "$current" ]; then
  seed="$(head -n 1 "$current")"
  if [[ "$seed" =~ $SEED_FORMAT ]]; then
    export SEED="$seed"
  fi
fi

exec /image/scripts/start
