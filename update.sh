#!/bin/bash
# =====================================================================
#  Homeserver - Updater
#
#    sudo bash update.sh v1.2.3              Version von GitHub installieren
#    sudo bash update.sh --local <ordner>    aus einer entpackten Kopie installieren
#    sudo bash update.sh --files-only <ordner>
#                                            nur Dateien kopieren (fuer install.sh)
#
#  Normalerweise startet das Admin-Panel dieses Skript (Updates -> Install).
#  Ablauf: herunterladen, pruefen, Sicherung der ersetzten Dateien,
#  installieren, betroffene Dienste neu starten, Gesundheits-Check.
#  Antwortet das Panel danach nicht, wird automatisch zurueckgesetzt.
#
#  Nie angefasst werden: .env-Dateien, Passwoerter (panel-data), hochgeladene
#  Dateien, Bots, Musik, die Minecraft-Welt und eigene Logos.
# =====================================================================
set -uo pipefail
umask 022

# (HS_* only for tests; normally unset)
CONF=${HS_CONF:-/etc/homeserver/config.env}
ETC=$(dirname "$CONF")
LIB=${HS_LIB:-/usr/local/lib/homeserver}
VAR=${HS_VAR:-/var/lib/homeserver}
WORK=$VAR/work
BACK=$VAR/backup
LOG=$VAR/update.log
STATE=${HS_STATE:-/var/lib/panel}
STATUS=$STATE/update.json
SYSD=${HS_SYSTEMD:-/etc/systemd/system}
PANEL_URL=${HS_PANEL_URL:-http://127.0.0.1:8080}
KEYS=(HOME USER BRAND SLUG ICON PUBLIC_URL DISCORD_ID MC_MEMORY TITLE_SIZE OS_TITLE_SIZE REPO)

die() { echo "FEHLER: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || [ -n "${HS_CONF:-}" ] || die "Bitte mit sudo starten."
HEALTH_TRIES=${HS_HEALTH_TRIES:-45}
[ -r "$CONF" ] || die "$CONF fehlt. Erst install.sh oder bootstrap.sh ausfuehren."
# shellcheck disable=SC1090
. "$CONF"
: "${HOMEDIR:?HOMEDIR fehlt in $CONF}" "${OWNER:?OWNER fehlt in $CONF}"
REPO="${REPO:-}"

MODE=release TAG="" SRC=""
case "${1:-}" in
  --local)      MODE=local; SRC="${2:-}"; [ -d "$SRC" ] || die "Ordner fehlt: $SRC" ;;
  --files-only) MODE=files; SRC="${2:-}"; [ -d "$SRC" ] || die "Ordner fehlt: $SRC" ;;
  v*)           TAG="$1" ;;
  *)            die "Aufruf: sudo bash update.sh v1.2.3" ;;
esac
if [ "$MODE" = release ]; then
  [[ $TAG =~ ^v[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || die "Ungueltige Version: $TAG"
  [[ $REPO =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "REPO ist in $CONF nicht gesetzt."
fi

mkdir -p "$VAR"
exec 9>"$VAR/update.lock"
flock -n 9 || die "Es laeuft schon ein Update."
if [ "$MODE" != files ]; then
  exec > >(tee -a "$LOG") 2>&1
  echo; echo "===== $(date '+%F %T') update $MODE ${TAG:-$SRC} ====="
fi

FROM="$(cat "$HOMEDIR/bio/panel/VERSION" 2>/dev/null | tr -d ' \r\n' || true)"
TO="${TAG:-}"
SINCE="$(date +%s%3N)"
STEP=start

status() { # <state> [error]
  [ "$MODE" = files ] && return 0
  [ -d "$STATE" ] || return 0
  python3 - "$STATUS" "$1" "$STEP" "${2:-}" "$TO" "$FROM" "$SINCE" <<'PY'
import json, os, sys, time
p, state, step, err, to, frm, since = sys.argv[1:8]
d = {"state": state, "step": step, "to": to or None, "from": frm or None, "since": int(since), "at": int(time.time() * 1000)}
if err:
    d["error"] = err[:300]
t = p + ".tmp"
with open(t, "w") as f:
    f.write(json.dumps(d))
os.chmod(t, 0o644)
os.replace(t, p)
PY
}
step() { STEP="$1"; echo "--> $1"; status running; }
fail_early() { echo "ABBRUCH: $1"; status failed "$1"; [ "$MODE" = release ] && rm -rf "$WORK"; exit 1; }

# ---------- 1. Quelle holen ----------
step download
if [ "$MODE" = release ]; then
  rm -rf "$WORK"; mkdir -p "$WORK/src"
  curl -fsSL --retry 3 --max-time 600 "https://codeload.github.com/$REPO/tar.gz/refs/tags/$TAG" -o "$WORK/src.tgz" \
    || fail_early "Download von GitHub hat nicht geklappt."
  tar -xzf "$WORK/src.tgz" -C "$WORK/src" --strip-components=1 --no-same-owner --no-same-permissions \
    || fail_early "Das Paket ist kaputt."
  SRC="$WORK/src"
else
  SRC="$(cd "$SRC" && pwd)"
  mkdir -p "$WORK"
fi

# ---------- 2. Pruefen + vorbereiten ----------
step check
[ -f "$SRC/VERSION" ] && [ -f "$SRC/server/bio/panel/server.js" ] && [ -f "$SRC/update.sh" ] \
  || fail_early "Das Paket ist unvollstaendig."
NEWVER="$(tr -d ' \r\n' < "$SRC/VERSION")"
[[ $NEWVER =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || fail_early "VERSION im Paket ist ungueltig."
[ "$MODE" != release ] || [ "v$NEWVER" = "$TAG" ] || fail_early "Paket-Version ($NEWVER) passt nicht zu $TAG."
TO="v$NEWVER"

STAGE="$WORK/stage"
rm -rf "$STAGE"; mkdir -p "$STAGE"
cp -a "$SRC/server/." "$STAGE/"
cp "$SRC/VERSION" "$STAGE/bio/panel/VERSION"
find "$STAGE" -type f \( -name '*.sh' -o -name '*.py' -o -name '*.js' -o -name '*.yaml' -o -name '*.conf' \
  -o -name '*.service' -o -name '*.html' -o -name '*.css' -o -name '*.json' -o -name '*.webmanifest' -o -name 'Dockerfile' \) \
  -exec sed -i 's/\r$//' {} +

sedesc() { printf '%s' "$1" | sed -e 's/[\\|&]/\\&/g'; }
SEDARGS=()
for k in "${KEYS[@]}"; do
  case $k in HOME) v="$HOMEDIR" ;; USER) v="$OWNER" ;; *) v="${!k-}" ;; esac
  SEDARGS+=(-e "s|__${k}__|$(sedesc "$v")|g")
done
grep -rlIZ '__[A-Z_]*__' "$STAGE" | xargs -0 -r sed -i "${SEDARGS[@]}"
left="$(grep -rlI -e '__HOME__' -e '__USER__' -e '__BRAND__' -e '__SLUG__' -e '__ICON__' "$STAGE" || true)"
[ -z "$left" ] || fail_early "Platzhalter nicht ersetzt in: ${left//$STAGE\//}"

while IFS= read -r -d '' f; do bash -n "$f" || fail_early "Syntaxfehler in ${f#"$STAGE"/}"; done \
  < <(find "$STAGE" -name '*.sh' -print0)
python3 - "$STAGE" <<'PY' || fail_early "Python-Syntaxfehler im Paket."
import ast, pathlib, sys
for f in pathlib.Path(sys.argv[1]).rglob("*.py"):
    ast.parse(f.read_text(encoding="utf-8"), str(f))
PY
if command -v docker >/dev/null && docker image inspect node:22-alpine >/dev/null 2>&1; then
  docker run --rm --network none -v "$STAGE/bio/panel:/c:ro" node:22-alpine node --check /c/server.js >/dev/null \
    || fail_early "JavaScript-Syntaxfehler in server.js."
fi

dest() { case $1 in systemd/*) echo "$SYSD/${1#systemd/}" ;; *) echo "$HOMEDIR/$1" ;; esac; }
mapfile -t FILES < <(cd "$STAGE" && find . -type f -printf '%P\n' | sort)
CHANGED=()
for r in "${FILES[@]}"; do cmp -s "$STAGE/$r" "$(dest "$r")" || CHANGED+=("$r"); done
echo "    $NEWVER: ${#CHANGED[@]} von ${#FILES[@]} Dateien sind neu oder geaendert"
need() { printf '%s\n' "${CHANGED[@]}" | grep -qE "$1"; }

# ---------- 3. Sicherung ----------
step backup
rm -rf "$BACK"; mkdir -p "$BACK/files"; : > "$BACK/new.txt"
printf '%s\n' "${CHANGED[@]}" > "$BACK/changed.txt"
echo "${FROM:-unknown}" > "$BACK/FROM"
for r in "${CHANGED[@]}"; do
  d="$(dest "$r")"
  if [ -e "$d" ]; then
    mkdir -p "$BACK/files/$(dirname "$r")"
    cp -a "$d" "$BACK/files/$r" || fail_early "Sicherung von $r hat nicht geklappt."
  else
    echo "$r" >> "$BACK/new.txt"
  fi
done

# ---------- 4. Installieren ----------
put() { # <rel> <from-dir>: overwrite in place (same inode, so bind-mounted single files stay live)
  local r="$1" from="$2" d; d="$(dest "$r")"
  if [ -e "$d" ]; then cp "$from/$r" "$d"; return; fi
  case $r in
    systemd/*) install -D -m 644 "$from/$r" "$d" ;;
    *) if [ "$(id -un)" = "$OWNER" ]; then mkdir -p "$(dirname "$d")"; else runuser -u "$OWNER" -- mkdir -p "$(dirname "$d")"; fi && cp "$from/$r" "$d" && chown "$OWNER:$OWNER" "$d" || return 1
       case $r in *.sh|panel-agent/agent.py|wallet/new-wallet.py) chmod 755 "$d" ;; *) chmod 644 "$d" ;; esac ;;
  esac
}

restore() {
  echo "--> Sicherung wird zurueckgespielt"
  local r
  while IFS= read -r r; do [ -n "$r" ] && cp "$BACK/files/$r" "$(dest "$r")"; done \
    < <(cd "$BACK/files" && find . -type f -printf '%P\n')
  while IFS= read -r r; do [ -n "$r" ] && rm -f "$(dest "$r")"; done < "$BACK/new.txt"
}

apply() { # restart only what changed
  systemctl daemon-reload
  if need '^bio/nginx\.conf$'; then docker exec bio nginx -t || return 1; fi
  if need '^bio/(compose\.yaml|media/Dockerfile)$'; then
    (cd "$HOMEDIR/bio" && docker compose up -d --build) || return 1
  fi
  if need '^bio/panel/'; then docker restart panel >/dev/null || return 1; fi
  if need '^bio/media/'; then docker restart media >/dev/null || return 1; fi
  if need '^bio/(nginx\.conf|compose\.yaml)$'; then docker exec bio nginx -s reload || return 1; fi
  if need '^playit/compose\.yaml$' && [ "$(docker inspect -f '{{.State.Running}}' playit 2>/dev/null)" = true ]; then
    (cd "$HOMEDIR/playit" && docker compose up -d) || return 1
  fi
  if need '^minecraft/'; then echo "    Minecraft-Aenderungen gelten ab dem naechsten Minecraft-Neustart."; fi
  return 0
}

healthy() { # [version]
  local i code
  for i in $(seq 1 "$HEALTH_TRIES"); do
    code="$(curl -s -o "$WORK/health" -m 5 -w "%{http_code}" "$PANEL_URL/api/update/state" || true)"
    if [ "$code" = 200 ] && { [ -z "${1:-}" ] || grep -q "\"version\":\"$1\"" "$WORK/health"; }; then return 0; fi
    if [ -z "${1:-}" ] && [ "$(curl -s -o /dev/null -m 5 -w "%{http_code}" "$PANEL_URL/api/session" || true)" = 200 ]; then return 0; fi
    sleep 2
  done
  return 1
}

rollback() {
  local why="$1"
  echo "!!! $why"
  STEP=rollback; status running "$why"
  restore
  apply || echo "    WARNUNG: Neustart nach dem Zuruecksetzen hatte Fehler"
  healthy "" || echo "    WARNUNG: Panel antwortet auch mit der alten Version nicht"
  status rolledback "$why"
  rm -rf "$WORK"
  exit 1
}

step install
for r in "${CHANGED[@]}"; do
  put "$r" "$STAGE" || { [ "$MODE" = files ] && die "Kopieren von $r hat nicht geklappt."; rollback "Kopieren von $r hat nicht geklappt."; }
done

if [ "$MODE" = files ]; then
  echo "    Dateien liegen bereit ($NEWVER)."
  install -d -m 755 "$LIB"
  install -m 755 "$SRC/update.sh" "$LIB/update.sh.new" && mv -f "$LIB/update.sh.new" "$LIB/update.sh"
  echo "$NEWVER" > "$ETC/VERSION"
  rm -rf "$STAGE"
  exit 0
fi

# ---------- 5. Dienste neu starten ----------
step restart
apply || rollback "Ein Dienst wollte mit der neuen Version nicht starten."

# ---------- 6. Gesundheits-Check ----------
step health
healthy "$NEWVER" || rollback "Das Panel hat nach dem Update nicht geantwortet."

# ---------- Fertig ----------
STEP=done
install -d -m 755 "$LIB"
install -m 755 "$SRC/update.sh" "$LIB/update.sh.new" && mv -f "$LIB/update.sh.new" "$LIB/update.sh"
echo "$NEWVER" > "$ETC/VERSION"
status ok
echo "    Fertig: ${FROM:-?} -> $NEWVER"
rm -rf "$WORK"
# The agent goes last: it may be the one that started us.
if need '^(panel-agent/|systemd/panel-agent\.service$)'; then systemctl restart panel-agent; fi
exit 0
