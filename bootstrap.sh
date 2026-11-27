#!/bin/bash
# =====================================================================
#  Homeserver - switch-over for servers that already run (one time)
#
#  For servers that were set up with the old ZIP kit and don't have an
#  "Updates" button yet. Reads name, logo, Discord ID etc. from the
#  existing files, saves them in /etc/homeserver/config.env and then
#  installs the newest version. After that, updates are one click in
#  the admin panel.
#
#  Run it as your normal user (NOT as root):
#      curl -fsSL https://raw.githubusercontent.com/h3nrx/homeserver/main/bootstrap.sh -o bootstrap.sh
#      bash bootstrap.sh
# =====================================================================
set -euo pipefail
REPO="h3nrx/homeserver"

c_ok=$'\e[32m'; c_hd=$'\e[1;36m'; c_wn=$'\e[33m'; c_er=$'\e[31m'; c_0=$'\e[0m'
ok()   { echo "${c_ok}    OK: $*${c_0}"; }
warn() { echo "${c_wn}    NOTE: $*${c_0}"; }
die()  { echo "${c_er}ERROR: $*${c_0}"; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Please do NOT run this with sudo. Just: bash bootstrap.sh"
HOMEDIR="$HOME"; OWNER="$(id -un)"
IDX="$HOMEDIR/bio/site/index.html"
[ -f "$IDX" ] && [ -f "$HOMEDIR/bio/compose.yaml" ] || die "No server found in $HOMEDIR (bio/site/index.html is missing)."
command -v python3 >/dev/null || die "python3 is missing."

if [ -r /etc/homeserver/config.env ]; then
  echo "    This server has already been switched over. Updates are in the admin panel under \"Updates\"."
  exit 0
fi

pick() { grep -oP "$1" "$2" 2>/dev/null | head -1 || true; }
BRAND="$(pick '<title>\K[^<]+' "$IDX")"
ICON="$(pick 'rel="icon" type="image/png" href="img/\K[A-Za-z0-9]+(?=-logo-64\.png)' "$IDX")"
SLUG="$(pick 'image: \K[a-z0-9]+(?=-media)' "$HOMEDIR/bio/compose.yaml")"
DISCORD_ID="$(pick 'const DISCORD_ID = "\K[0-9]*' "$IDX")"
PUBLIC_URL="$(pick 'property="og:image" content="\Khttps://[^"]+(?=/img/og\.jpg)' "$IDX")"
MC_MEMORY="$(pick 'MEMORY: "\K[0-9]+[GM]' "$HOMEDIR/minecraft/compose.yaml")"
TITLE_SIZE="$(pick 'h1 \{ margin: 0; font-size: calc\(var\(--w\) \* \K[.0-9]+' "$IDX")"
OS_TITLE_SIZE="$(pick '\.os-title h2 \{ margin: 0; font-size: \K[^;]+' "$IDX")"

[[ "$BRAND" =~ ^[A-Za-z0-9][A-Za-z0-9\ ._-]{0,19}$ ]] || die "Couldn't read the server name ($BRAND)."
[ -n "$SLUG" ] || SLUG="$(printf '%s' "$BRAND" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9')"
[ -n "$ICON" ] || ICON=default
[ -n "$MC_MEMORY" ] || MC_MEMORY=4G
[ -n "$TITLE_SIZE" ] || TITLE_SIZE=.135
[ -n "$OS_TITLE_SIZE" ] || OS_TITLE_SIZE="min(14.2vw, 23vh)"

echo
echo "${c_hd}  Switching to one-click updates${c_0}"
echo
echo "    Found:"
echo "      Name:          $BRAND"
echo "      Logo:          img/$ICON-logo-*.png"
echo "      Address:       ${PUBLIC_URL:-(none)}"
echo "      Discord ID:    ${DISCORD_ID:-(none)}"
echo "      Minecraft RAM: $MC_MEMORY"
echo "      Updates from:  github.com/$REPO"
echo
read -rp "    Is that right? Then let's go. [y/N] " a
[[ "$a" =~ ^[jJyY] ]] || die "Cancelled. Nothing was changed."

echo "    You'll be asked for your Ubuntu password next (typing is invisible)."
sudo -v || die "sudo didn't work."

TAG="$(curl -fsSL -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$REPO/releases/latest" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')" || die "Couldn't find the newest version."
[[ "$TAG" =~ ^v[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || die "Odd version number: $TAG"
ok "Newest version: $TAG"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/tags/$TAG" | tar -xz -C "$TMP" --strip-components=1 \
  || die "The download didn't work."

sudo install -d -m 755 /etc/homeserver /usr/local/lib/homeserver
for k in HOMEDIR OWNER BRAND SLUG ICON PUBLIC_URL DISCORD_ID MC_MEMORY TITLE_SIZE OS_TITLE_SIZE REPO; do
  printf '%s=%q\n' "$k" "${!k-}"
done | sudo tee /etc/homeserver/config.env >/dev/null
sudo chmod 644 /etc/homeserver/config.env
sudo install -m 755 "$TMP/update.sh" /usr/local/lib/homeserver/update.sh
ok "Settings saved (/etc/homeserver/config.env)"

echo "    Installing $TAG (with a backup + automatic rollback if anything goes wrong) ..."
if sudo bash /usr/local/lib/homeserver/update.sh "$TAG"; then
  echo
  echo "${c_ok}  DONE! From now on: admin panel -> Updates -> Install.${c_0}"
  echo "  You have to sign in to the panel once more."
else
  echo
  warn "The update didn't work, the old version keeps running. Log: /var/lib/homeserver/update.log"
  exit 1
fi
