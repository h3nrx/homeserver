#!/bin/bash
# Laeuft bei jedem Containerstart vor Minecraft.
# Liegt eine Reset-Anforderung vom Admin-Panel vor, wird die Welt neu erstellt.
set -u
SEED_FORMAT='^[A-Za-z0-9_-]{1,32}$'
marker=/control/reset-seed
current=/control/seed

if [ -f "$marker" ]; then
  seed="$(head -c 64 "$marker" | tr -d '\r\n')"
  rm -f "$marker"
  if [[ "$seed" =~ $SEED_FORMAT ]]; then
    echo "[boot] Erstelle neue Welt mit Seed $seed"
    rm -rf -- /data/world /data/world_nether /data/world_the_end /data/plugins/Essentials/userdata
    printf '%s\n' "$seed" > "$current.tmp" && mv -f "$current.tmp" "$current"
  else
    echo "[boot] Ungueltiger Seed, Reset wird ignoriert"
  fi
fi

if [ -s "$current" ]; then
  seed="$(head -n 1 "$current")"
  if [[ "$seed" =~ $SEED_FORMAT ]]; then
    export SEED="$seed"
  fi
fi

exec /image/scripts/start
