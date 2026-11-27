#!/bin/bash
# =====================================================================
#  Homeserver - Updater
#
#    sudo bash update.sh v1.2.3              install a version from GitHub
#    sudo bash update.sh --local <folder>    install from an unpacked copy
#    sudo bash update.sh --files-only <folder>
#                                            only copy the files (for install.sh)
#
#  Normally the admin panel starts this script (Updates -> Install).
#  Steps: download, check, back up the files that get replaced,
#  install, restart the services that changed, health check.
#  If the panel doesn't answer afterwards, everything is rolled back.
#
#  Never touched: .env files, passwords (panel-data), uploaded files,
#  bots, music, the Minecraft world and your own logos.
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

die() { echo "ERROR: $*" >&2; exit 1; }
[ "$(id -u)" -eq 0 ] || [ -n "${HS_CONF:-}" ] || die "Please run this with sudo."
HEALTH_TRIES=${HS_HEALTH_TRIES:-45}
[ -r "$CONF" ] || die "$CONF is missing. Run install.sh or bootstrap.sh first."
# shellcheck disable=SC1090
. "$CONF"
: "${HOMEDIR:?HOMEDIR is missing in $CONF}" "${OWNER:?OWNER is missing in $CONF}"
REPO="${REPO:-}"

MODE=release TAG="" SRC=""
case "${1:-}" in
  --local)      MODE=local; SRC="${2:-}"; [ -d "$SRC" ] || die "Folder is missing: $SRC" ;;
  --files-only) MODE=files; SRC="${2:-}"; [ -d "$SRC" ] || die "Folder is missing: $SRC" ;;
  v*)           TAG="$1" ;;
  *)            die "Usage: sudo bash update.sh v1.2.3" ;;
esac
if [ "$MODE" = release ]; then
  [[ $TAG =~ ^v[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || die "Invalid version: $TAG"
  [[ $REPO =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]] || die "REPO isn't set in $CONF."
fi

mkdir -p "$VAR"
exec 9>"$VAR/update.lock"
flock -n 9 || die "An update is already running."
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
fail_early() { echo "STOPPED: $1"; status failed "$1"; [ "$MODE" = release ] && rm -rf "$WORK"; exit 1; }

# ---------- 1. get the source ----------
step download
if [ "$MODE" = release ]; then
  rm -rf "$WORK"; mkdir -p "$WORK/src"
  KEY="$(tr -d ' \r\n' < "$VAR/github.token" 2>/dev/null || true)"
  if [[ $KEY =~ ^github_pat_[A-Za-z0-9_]+$ ]]; then
    # private repo: the key never shows up in the process list, and curl
    # doesn't send it on to other servers when redirected
    curl -fsSL --retry 3 --max-time 600 -H @<(printf 'Authorization: Bearer %s\n' "$KEY") \
      "https://api.github.com/repos/$REPO/tarball/$TAG" -o "$WORK/src.tgz" \
      || fail_early "The download from GitHub didn't work (access key expired?)."
  else
    curl -fsSL --retry 3 --max-time 600 "https://codeload.github.com/$REPO/tar.gz/refs/tags/$TAG" -o "$WORK/src.tgz" \
      || fail_early "The download from GitHub didn't work."
  fi
  unset KEY
  tar -xzf "$WORK/src.tgz" -C "$WORK/src" --strip-components=1 --no-same-owner --no-same-permissions \
    || fail_early "The package is broken."
  SRC="$WORK/src"
else
  SRC="$(cd "$SRC" && pwd)"
  mkdir -p "$WORK"
fi

# ---------- 2. check + prepare ----------
step check
[ -f "$SRC/VERSION" ] && [ -f "$SRC/server/bio/panel/server.js" ] && [ -f "$SRC/update.sh" ] \
  || fail_early "The package is incomplete."
NEWVER="$(tr -d ' \r\n' < "$SRC/VERSION")"
[[ $NEWVER =~ ^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || fail_early "VERSION in the package is invalid."
[ "$MODE" != release ] || [ "v$NEWVER" = "$TAG" ] || fail_early "Package version ($NEWVER) doesn't match $TAG."
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
[ -z "$left" ] || fail_early "Placeholders not filled in: ${left//$STAGE\//}"

while IFS= read -r -d '' f; do bash -n "$f" || fail_early "Syntax error in ${f#"$STAGE"/}"; done \
  < <(find "$STAGE" -name '*.sh' -print0)
python3 - "$STAGE" <<'PY' || fail_early "Python syntax error in the package."
import ast, pathlib, sys
for f in pathlib.Path(sys.argv[1]).rglob("*.py"):
    ast.parse(f.read_text(encoding="utf-8"), str(f))
PY
if command -v docker >/dev/null && docker image inspect node:22-alpine >/dev/null 2>&1; then
  docker run --rm --network none -v "$STAGE/bio/panel:/c:ro" node:22-alpine node --check /c/server.js >/dev/null \
    || fail_early "JavaScript syntax error in server.js."
fi

dest() { case $1 in systemd/*) echo "$SYSD/${1#systemd/}" ;; *) echo "$HOMEDIR/$1" ;; esac; }
mapfile -t FILES < <(cd "$STAGE" && find . -type f -printf '%P\n' | sort)
CHANGED=()
for r in "${FILES[@]}"; do cmp -s "$STAGE/$r" "$(dest "$r")" || CHANGED+=("$r"); done
echo "    $NEWVER: ${#CHANGED[@]} of ${#FILES[@]} files are new or changed"
need() { printf '%s\n' "${CHANGED[@]}" | grep -qE "$1"; }

# ---------- 3. backup ----------
step backup
rm -rf "$BACK"; mkdir -p "$BACK/files"; : > "$BACK/new.txt"
printf '%s\n' "${CHANGED[@]}" > "$BACK/changed.txt"
echo "${FROM:-unknown}" > "$BACK/FROM"
for r in "${CHANGED[@]}"; do
  d="$(dest "$r")"
  if [ -e "$d" ]; then
    mkdir -p "$BACK/files/$(dirname "$r")"
    cp -a "$d" "$BACK/files/$r" || fail_early "Backing up $r didn't work."
  else
    echo "$r" >> "$BACK/new.txt"
  fi
done

# ---------- 4. install ----------
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
  echo "--> Restoring the backup"
  local r
  while IFS= read -r r; do [ -n "$r" ] && cp "$BACK/files/$r" "$(dest "$r")"; done \
    < <(cd "$BACK/files" && find . -type f -printf '%P\n')
  while IFS= read -r r; do [ -n "$r" ] && rm -f "$(dest "$r")"; done < "$BACK/new.txt"
}

apply() { # restart only what changed
  systemctl daemon-reload
  if need '^bio/nginx\.conf$'; then docker exec bio nginx -t || return 1; fi
  # the media image only gets rebuilt when its Dockerfile changed (takes minutes)
  if need '^bio/media/Dockerfile$'; then
    (cd "$HOMEDIR/bio" && docker compose up -d --build) || return 1
  elif need '^bio/compose\.yaml$'; then
    (cd "$HOMEDIR/bio" && docker compose up -d) || return 1
  fi
  if need '^bio/panel/'; then docker restart panel >/dev/null || return 1; fi
  if need '^bio/media/'; then docker restart media >/dev/null || return 1; fi
  if need '^bio/(nginx\.conf|compose\.yaml)$'; then docker exec bio nginx -s reload || return 1; fi
  if need '^playit/compose\.yaml$' && [ "$(docker inspect -f '{{.State.Running}}' playit 2>/dev/null)" = true ]; then
    (cd "$HOMEDIR/playit" && docker compose up -d) || return 1
  fi
  if need '^minecraft/'; then echo "    Minecraft changes take effect at the next Minecraft restart."; fi
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
  apply || echo "    WARNING: restarting after the rollback had errors"
  healthy "" || echo "    WARNING: the panel doesn't answer with the old version either"
  status rolledback "$why"
  rm -rf "$WORK"
  exit 1
}

step install
for r in "${CHANGED[@]}"; do
  put "$r" "$STAGE" || { [ "$MODE" = files ] && die "Copying $r didn't work."; rollback "Copying $r didn't work."; }
done

if [ "$MODE" = files ]; then
  echo "    Files are in place ($NEWVER)."
  install -d -m 755 "$LIB"
  install -m 755 "$SRC/update.sh" "$LIB/update.sh.new" && mv -f "$LIB/update.sh.new" "$LIB/update.sh"
  echo "$NEWVER" > "$ETC/VERSION"
  rm -rf "$STAGE"
  exit 0
fi

# ---------- 5. restart services ----------
step restart
apply || rollback "A service wouldn't start with the new version."

# ---------- 6. health check ----------
step health
healthy "$NEWVER" || rollback "The panel didn't answer after the update."

# ---------- done ----------
STEP=done
install -d -m 755 "$LIB"
install -m 755 "$SRC/update.sh" "$LIB/update.sh.new" && mv -f "$LIB/update.sh.new" "$LIB/update.sh"
echo "$NEWVER" > "$ETC/VERSION"
status ok
echo "    Done: ${FROM:-?} -> $NEWVER"
rm -rf "$WORK"
if need '^(panel-agent/screen\.py|systemd/homeserver-screen\.service)$' && systemctl is-active --quiet homeserver-screen; then
  systemctl restart homeserver-screen || true
fi
# The agent goes last: it may be the one that started us.
if need '^(panel-agent/|systemd/panel-agent\.service$)'; then systemctl restart panel-agent; fi
exit 0
