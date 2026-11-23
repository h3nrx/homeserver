#!/bin/bash
# =====================================================================
#  Homeserver - Umstieg fuer schon laufende Server (einmalig)
#
#  Fuer Server, die frueher mit dem ZIP-Kit eingerichtet wurden und noch
#  keinen "Updates"-Knopf haben. Liest Name, Logo, Discord-ID usw. aus den
#  vorhandenen Dateien, speichert sie in /etc/homeserver/config.env und
#  installiert dann die neueste Version. Danach gibt es Updates per Klick
#  im Admin-Panel.
#
#  Starten (als normaler Benutzer, NICHT als root):
#      curl -fsSL https://raw.githubusercontent.com/h3nrx/homeserver/main/bootstrap.sh -o bootstrap.sh
#      bash bootstrap.sh
# =====================================================================
set -euo pipefail
REPO="h3nrx/homeserver"

c_ok=$'\e[32m'; c_hd=$'\e[1;36m'; c_wn=$'\e[33m'; c_er=$'\e[31m'; c_0=$'\e[0m'
ok()   { echo "${c_ok}    OK: $*${c_0}"; }
warn() { echo "${c_wn}    HINWEIS: $*${c_0}"; }
die()  { echo "${c_er}FEHLER: $*${c_0}"; exit 1; }

[ "$(id -u)" -ne 0 ] || die "Bitte NICHT mit sudo starten. Einfach: bash bootstrap.sh"
HOMEDIR="$HOME"; OWNER="$(id -un)"
IDX="$HOMEDIR/bio/site/index.html"
[ -f "$IDX" ] && [ -f "$HOMEDIR/bio/compose.yaml" ] || die "Kein Server in $HOMEDIR gefunden (bio/site/index.html fehlt)."
command -v python3 >/dev/null || die "python3 fehlt."

if [ -r /etc/homeserver/config.env ]; then
  echo "    Dieser Server ist schon umgestellt. Updates gibt es im Admin-Panel unter \"Updates\"."
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

[[ "$BRAND" =~ ^[A-Za-z0-9][A-Za-z0-9\ ._-]{0,19}$ ]] || die "Servername nicht erkannt ($BRAND)."
[ -n "$SLUG" ] || SLUG="$(printf '%s' "$BRAND" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9')"
[ -n "$ICON" ] || ICON=default
[ -n "$MC_MEMORY" ] || MC_MEMORY=4G
[ -n "$TITLE_SIZE" ] || TITLE_SIZE=.135
[ -n "$OS_TITLE_SIZE" ] || OS_TITLE_SIZE="min(14.2vw, 23vh)"

echo
echo "${c_hd}  Umstieg auf Updates per Knopfdruck${c_0}"
echo
echo "    Erkannt:"
echo "      Name:          $BRAND"
echo "      Logo:          img/$ICON-logo-*.png"
echo "      Adresse:       ${PUBLIC_URL:-(keine)}"
echo "      Discord-ID:    ${DISCORD_ID:-(keine)}"
echo "      Minecraft-RAM: $MC_MEMORY"
echo "      Updates von:   github.com/$REPO"
echo
read -rp "    Stimmt das? Dann geht's los. [j/N] " a
[[ "$a" =~ ^[jJyY] ]] || die "Abgebrochen. Es wurde nichts geaendert."

echo "    Gleich wird nach deinem Ubuntu-Passwort gefragt (Tippen ist unsichtbar)."
sudo -v || die "sudo hat nicht geklappt."

TAG="$(curl -fsSL -H 'Accept: application/vnd.github+json' "https://api.github.com/repos/$REPO/releases/latest" \
  | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"])')" || die "Neueste Version nicht gefunden."
[[ "$TAG" =~ ^v[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]] || die "Komische Versionsnummer: $TAG"
ok "Neueste Version: $TAG"

TMP="$(mktemp -d)"; trap 'rm -rf "$TMP"' EXIT
curl -fsSL "https://codeload.github.com/$REPO/tar.gz/refs/tags/$TAG" | tar -xz -C "$TMP" --strip-components=1 \
  || die "Download hat nicht geklappt."

sudo install -d -m 755 /etc/homeserver /usr/local/lib/homeserver
for k in HOMEDIR OWNER BRAND SLUG ICON PUBLIC_URL DISCORD_ID MC_MEMORY TITLE_SIZE OS_TITLE_SIZE REPO; do
  printf '%s=%q\n' "$k" "${!k-}"
done | sudo tee /etc/homeserver/config.env >/dev/null
sudo chmod 644 /etc/homeserver/config.env
sudo install -m 755 "$TMP/update.sh" /usr/local/lib/homeserver/update.sh
ok "Einstellungen gespeichert (/etc/homeserver/config.env)"

echo "    Installiere $TAG (Sicherung + automatisches Zuruecksetzen, falls was schiefgeht) ..."
if sudo bash /usr/local/lib/homeserver/update.sh "$TAG"; then
  echo
  echo "${c_ok}  FERTIG! Ab jetzt: Admin-Panel -> Updates -> Install.${c_0}"
  echo "  Du musst dich im Panel einmal neu einloggen."
else
  echo
  warn "Das Update hat nicht geklappt, die alte Version laeuft weiter. Log: /var/lib/homeserver/update.log"
  exit 1
fi
