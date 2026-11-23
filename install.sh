#!/bin/bash
# =====================================================================
#  Homeserver - Installer
#  Richtet alles ein: Updates, Docker, Tailscale, Website + Admin-Panel,
#  Minecraft, Discord-Bots, Wallet, Sicherheit, Updates per Knopfdruck.
#
#  Starten (als normaler Benutzer, NICHT als root), im entpackten Ordner:
#      bash install.sh
#
#  Du kannst das Skript gefahrlos nochmal starten. Passwoerter, Daten
#  und Einstellungen bleiben dabei erhalten.
# =====================================================================
set -euo pipefail

KIT="$(cd "$(dirname "$0")" && pwd)"
SRC="$KIT/server"
ME="$(id -un)"
HOMEDIR="$HOME"

# ---------- Hilfsfunktionen ----------
c_ok=$'\e[32m'; c_hd=$'\e[1;36m'; c_wn=$'\e[33m'; c_er=$'\e[31m'; c_0=$'\e[0m'
step() { echo; echo "${c_hd}==> $*${c_0}"; }
ok()   { echo "${c_ok}    OK: $*${c_0}"; }
warn() { echo "${c_wn}    HINWEIS: $*${c_0}"; }
die()  { echo "${c_er}FEHLER: $*${c_0}"; exit 1; }
ask_yes() { local a; read -rp "    $1 [j/N] " a; [[ "$a" =~ ^[jJyY] ]]; }

CONF=/etc/homeserver/config.env
CONF_KEYS=(HOMEDIR OWNER BRAND SLUG ICON PUBLIC_URL DISCORD_ID MC_MEMORY TITLE_SIZE OS_TITLE_SIZE REPO)
write_conf() {
  local k
  sudo install -d -m 755 /etc/homeserver
  for k in "${CONF_KEYS[@]}"; do printf '%s=%q\n' "$k" "${!k-}"; done | sudo tee "$CONF" >/dev/null
  sudo chmod 644 "$CONF"
}

# ---------- Vorab-Checks ----------
[ "$(id -u)" -ne 0 ] || die "Bitte NICHT mit sudo starten. Einfach: bash install.sh"
{ [ -d "$SRC/bio" ] && [ -f "$KIT/update.sh" ]; } || die "Ordner 'server' oder update.sh fehlt. Starte das Skript im entpackten Ordner."
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || die "Das ist kein Ubuntu. Dieses Skript ist fuer Ubuntu Server 26.04 LTS."
[ "$(id -u)" -eq 1000 ] || warn "Dein Benutzer hat nicht die ID 1000. Die Container laufen mit 1000 - das kann Rechte-Probleme machen."

# Windows-Zeilenenden entfernen (falls die Dateien ueber Windows kamen)
find "$KIT" -type f \( -name '*.sh' -o -name '*.py' -o -name '*.js' -o -name '*.yaml' -o -name '*.conf' -o -name '*.service' -o -name '*.html' \) -exec sed -i 's/\r$//' {} +

echo
echo "${c_hd}  Homeserver Installer  (Version $(cat "$KIT/VERSION" 2>/dev/null || echo ?))${c_0}"
echo "  Benutzer: $ME   Ordner: $HOMEDIR"
echo "  Dauer: ca. 15-30 Minuten (je nach Internet). Zwischendurch wirst du"
echo "  ein paar Sachen gefragt. Einfach lesen und antworten."
echo
echo "  Gleich wird nach deinem Passwort gefragt (das vom Ubuntu-Login)."
echo "  Beim Tippen siehst du KEINE Zeichen - das ist normal."
sudo -v || die "sudo hat nicht geklappt."
# sudo waehrend der Installation wach halten
( while true; do sudo -n true; sleep 50; kill -0 "$$" 2>/dev/null || exit; done ) 2>/dev/null &

# ---------- Name + Einstellungen (bleiben bei Updates erhalten) ----------
HOMEDIR="$HOME"; OWNER="$ME"
REPO="$(cat "$KIT/REPO" 2>/dev/null | tr -d ' \r\n' || true)"
if [ -r "$CONF" ]; then
  # shellcheck disable=SC1090
  . "$CONF"
  ok "Einstellungen von vorher gefunden: $BRAND"
else
  echo
  echo "    Wie soll dein Server heissen? Steht auf der Website und im Panel."
  echo "    Erlaubt: Buchstaben, Zahlen, Leerzeichen, Punkt, _ und - (max. 20 Zeichen)."
  until read -rp "    Name: " BRAND && [[ "$BRAND" =~ ^[A-Za-z0-9][A-Za-z0-9\ ._-]{0,19}$ ]]; do echo "    Bitte nochmal."; done
  SLUG="$(printf '%s' "$BRAND" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9')"; [ -n "$SLUG" ] || SLUG=homeserver
  ICON=default
  # Titelgroesse passend zur Namenslaenge
  TITLE_SIZE="$(python3 -c 'import sys; n=max(len(sys.argv[1]),1); print(".135" if n<=6 else min(.135, round(.74/n, 3)))' "$BRAND")"
  OS_TITLE_SIZE="$(python3 -c 'import sys; n=max(len(sys.argv[1]),1); vw=14.2 if n<=6 else round(80/n, 1); print(f"min({vw}vw, {round(vw*1.62)}vh)")' "$BRAND")"
  echo
  echo "    Optional: Discord-Status auf deiner Startseite anzeigen."
  echo "    Dafuer brauchst du deine Discord-User-ID (Discord: Einstellungen -> Erweitert ->"
  echo "    Entwicklermodus an, dann Rechtsklick auf dich -> User-ID kopieren)."
  read -rp "    Discord-User-ID (leer lassen = ueberspringen): " DISCORD_ID || true
  [[ "${DISCORD_ID:-}" =~ ^[0-9]{15,22}$ ]] || DISCORD_ID=""
  PUBLIC_URL=""
  # RAM fuer Minecraft: ca. die Haelfte, max 6G, min 2G
  RAM_GB=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
  MC_RAM=$(( RAM_GB / 2 )); [ "$MC_RAM" -gt 6 ] && MC_RAM=6; [ "$MC_RAM" -lt 2 ] && MC_RAM=2
  MC_MEMORY="${MC_RAM}G"
  write_conf
  ok "Einstellungen gespeichert in $CONF"
fi

# ---------- 1. System-Updates + Grundpakete ----------
step "1/11  System-Updates und Grundpakete"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y
sudo apt-get -o Dpkg::Options::="--force-confold" upgrade -y
sudo apt-get install -y ca-certificates curl gnupg git python3 python3-venv ufw \
  unattended-upgrades openssh-server openssl unzip htop
ok "System ist aktuell"

# ---------- 2. Docker ----------
step "2/11  Docker installieren"
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  ok "Docker ist schon da ($(docker --version))"
else
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  if sudo apt-get update -y && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; then
    ok "Docker (offizielle Quelle) installiert"
  else
    warn "Docker-Quelle hat nicht geklappt, nehme die Ubuntu-Pakete"
    sudo rm -f /etc/apt/sources.list.d/docker.list
    sudo apt-get update -y
    sudo apt-get install -y docker.io docker-compose-v2 docker-buildx
  fi
fi
sudo install -d /etc/docker
sudo tee /etc/docker/daemon.json >/dev/null <<'JSON'
{
  "live-restore": true,
  "log-driver": "json-file",
  "log-opts": { "max-size": "10m", "max-file": "3" }
}
JSON
sudo systemctl enable docker >/dev/null 2>&1 || true
sudo systemctl restart docker
sudo usermod -aG docker "$ME"
ok "Docker laeuft"

# ---------- 3. Tailscale ----------
step "3/11  Tailscale installieren"
if command -v tailscale >/dev/null; then
  ok "Tailscale ist schon da"
else
  curl -fsSL https://tailscale.com/install.sh | sh
  ok "Tailscale installiert"
fi

# ---------- 4. Dateien kopieren ----------
step "4/11  Server-Dateien kopieren"
sudo bash "$KIT/update.sh" --files-only "$KIT" || die "Dateien kopieren hat nicht geklappt."
chmod +x "$HOMEDIR"/bio/set-password.sh "$HOMEDIR"/playit/set-secret.sh \
  "$HOMEDIR"/minecraft/boot.sh "$HOMEDIR"/panel-agent/*.sh "$HOMEDIR"/panel-agent/agent.py "$HOMEDIR"/wallet/new-wallet.py
ok "Dateien liegen in $HOMEDIR (Minecraft bekommt $MC_MEMORY RAM)"

# ---------- 5. Ordner + Rechte ----------
step "5/11  Ordner anlegen"
mkdir -p "$HOMEDIR"/files/.panel "$HOMEDIR"/bots "$HOMEDIR"/music \
         "$HOMEDIR"/minecraft/control "$HOMEDIR"/minecraft/data/logs "$HOMEDIR"/minecraft/data/plugins \
         "$HOMEDIR"/bio/panel-data "$HOMEDIR"/backups
chmod 775 "$HOMEDIR"/files "$HOMEDIR"/music
chmod 750 "$HOMEDIR"/minecraft/control
sudo install -d -o root -g "$ME" -m 755 /var/lib/panel
sudo install -d -o "$ME" -g "$ME" -m 770 /var/lib/panel/commands /var/lib/panel/responses
ok "Ordner fertig"

# ---------- 6. Geheime Schluessel (.env) ----------
step "6/11  Interne Schluessel erzeugen"
LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
umask 077
if [ ! -s "$HOMEDIR/minecraft/.env" ]; then
  printf 'RCON_PASSWORD=%s\n' "$(openssl rand -hex 24)" > "$HOMEDIR/minecraft/.env"
  ok "Minecraft-RCON-Passwort erzeugt (brauchst du nie selbst)"
else ok "minecraft/.env gibt es schon - bleibt"; fi
if [ ! -s "$HOMEDIR/bio/.env" ]; then
  {
    printf 'SERVER_ADDRESS=\n'
    printf 'LOCAL_ADDRESS=%s\n' "${LAN_IP:-}"
    printf 'MEDIA_TOKEN=%s\n' "$(openssl rand -hex 32)"
  } > "$HOMEDIR/bio/.env"
  ok "bio/.env erzeugt (Heim-Adresse: ${LAN_IP:-unbekannt})"
else ok "bio/.env gibt es schon - bleibt"; fi
umask 022

# ---------- 7. Sicherheit ----------
step "7/11  Sicherheit einrichten (Firewall, SSH, Updates)"
LAN_NET=""
if [ -n "${LAN_IP:-}" ]; then
  CIDR="$(ip -4 -o addr show | awk -v ip="$LAN_IP" '$4 ~ "^"ip"/" {print $4; exit}')"
  LAN_NET="$(python3 -c 'import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)' "$CIDR" 2>/dev/null || true)"
fi
sudo ufw --force reset >/dev/null
sudo ufw default deny incoming >/dev/null
sudo ufw default allow outgoing >/dev/null
if [ -n "$LAN_NET" ]; then
  sudo ufw allow from "$LAN_NET" to any port 22 proto tcp comment 'SSH nur aus dem Heimnetz' >/dev/null
  ok "SSH nur aus deinem Heimnetz ($LAN_NET) erlaubt"
else
  sudo ufw allow 22/tcp >/dev/null
  warn "Heimnetz nicht erkannt - SSH ist von ueberall erlaubt (Router laesst trotzdem nichts rein)"
fi
sudo ufw --force enable >/dev/null
ok "Firewall an"

sudo tee /etc/ssh/sshd_config.d/99-homeserver.conf >/dev/null <<EOF
# Homeserver hardening
X11Forwarding no
MaxAuthTries 3
LoginGraceTime 30
AllowUsers $ME
ClientAliveInterval 300
ClientAliveCountMax 2
AllowAgentForwarding no
PermitUserEnvironment no
EOF
if sudo sshd -t; then sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd 2>/dev/null || true; ok "SSH gehaertet"
else sudo rm -f /etc/ssh/sshd_config.d/99-homeserver.conf; warn "SSH-Einstellung fehlerhaft - zurueckgenommen"; fi

sudo tee /etc/sysctl.d/99-homeserver.conf >/dev/null <<'EOF'
# Homeserver hardening
net.ipv4.conf.all.send_redirects = 0
net.ipv4.conf.default.send_redirects = 0
net.ipv4.conf.default.accept_redirects = 0
net.ipv6.conf.all.accept_redirects = 0
net.ipv6.conf.default.accept_redirects = 0
net.ipv4.conf.all.secure_redirects = 0
net.ipv4.conf.all.log_martians = 1
net.ipv4.icmp_echo_ignore_broadcasts = 1
kernel.kptr_restrict = 2
fs.protected_fifos = 2
fs.protected_regular = 2
fs.suid_dumpable = 0
EOF
sudo sysctl --system >/dev/null

sudo tee /etc/apt/apt.conf.d/52homeserver-unattended >/dev/null <<'EOF'
// Homeserver: auch Docker- und Tailscale-Updates automatisch einspielen
Unattended-Upgrade::Origins-Pattern {
  "origin=Docker,archive=${distro_codename}";
  "origin=Tailscale";
};
Unattended-Upgrade::Remove-Unused-Kernel-Packages "true";
Unattended-Upgrade::Remove-Unused-Dependencies "true";
Unattended-Upgrade::Automatic-Reboot "false";
EOF
echo unattended-upgrades unattended-upgrades/enable_auto_updates boolean true | sudo debconf-set-selections
sudo dpkg-reconfigure -f noninteractive unattended-upgrades >/dev/null 2>&1 || true
ok "Automatische Sicherheits-Updates an"

# Laptop: Deckel zu = einfach weiterlaufen, nie schlafen gehen
sudo install -d /etc/systemd/logind.conf.d
sudo tee /etc/systemd/logind.conf.d/99-homeserver-lid.conf >/dev/null <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true
ok "Deckel zuklappen ist ok - der Server schlaeft nie ein (gilt ab dem naechsten Neustart)"

# ---------- 8. systemd-Dienste ----------
step "8/11  Hintergrund-Dienste einrichten"
# (die Dienst-Dateien hat Schritt 4 schon nach /etc/systemd/system gelegt)
sudo systemctl daemon-reload
sudo systemctl enable panel-agent.service >/dev/null 2>&1
sudo systemctl restart panel-agent.service
ok "Panel-Agent laeuft (Minecraft startest du spaeter im Admin-Panel)"

# ---------- 9. Container bauen + starten ----------
step "9/11  Container laden und starten (dauert ein paar Minuten)"
cd "$HOMEDIR/minecraft"
sudo docker compose pull
sudo docker compose create          # legt auch das Netzwerk 'mcnet' an
ok "Minecraft vorbereitet (noch aus)"
cd "$HOMEDIR/bio"
sudo docker compose build --pull
sudo docker compose up -d
ok "Website, Admin-Panel und Media-Dienst laufen"

# ---------- 10. Admin-Passwort + Discord ----------
step "10/11  Admin-Passwort fuer das Panel"
if [ -s "$HOMEDIR/bio/panel-data/auth.json" ] && ! ask_yes "Es gibt schon ein Admin-Passwort. Neu setzen?"; then
  ok "Admin-Passwort bleibt"
else
  echo "    Das ist das Passwort fuer https://.../admin/ (mind. 12 Zeichen)."
  echo "    Nimm ein ANDERES als dein Ubuntu-Passwort und schreib es dir auf."
  until sudo bash "$HOMEDIR/bio/set-password.sh"; do echo "    Nochmal versuchen:"; done
  sudo chown -R "$ME:$ME" "$HOMEDIR/bio/panel-data"
  chmod 755 "$HOMEDIR/bio/panel-data"; chmod 600 "$HOMEDIR/bio/panel-data/auth.json"
fi


# ---------- 11. Tailscale + Funnel (oeffentlich machen) ----------
step "11/11  Server ins Internet bringen (Tailscale Funnel)"
if ! sudo tailscale status >/dev/null 2>&1; then
  echo "    Gleich erscheint ein Link (https://login.tailscale.com/...)."
  echo "    Den am PC/Handy oeffnen und mit deinem Tailscale-Konto anmelden."
  sudo tailscale up
fi
ok "Tailscale verbunden"
echo "    Jetzt wird Funnel eingeschaltet. Kommt ein Link 'Funnel is not enabled',"
echo "    diesen oeffnen, freigeben (Enable) - das Skript macht dann von alleine weiter."
sudo tailscale funnel --bg http://127.0.0.1:8080
DNS="$(sudo tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
PUBLIC=""
if [ -n "$DNS" ]; then
  PUBLIC="https://$DNS"
  if [ "${PUBLIC_URL:-}" != "$PUBLIC" ]; then
    PUBLIC_URL="$PUBLIC"; write_conf
    sudo bash "$KIT/update.sh" --files-only "$KIT" >/dev/null || warn "Adresse konnte nicht eingetragen werden"
  fi
fi

# ---------- Fertig ----------
echo
echo "${c_ok}=====================================================================${c_0}"
echo "${c_ok}  FERTIG! Dein Server \"$BRAND\" laeuft.${c_0}"
echo "${c_ok}=====================================================================${c_0}"
echo
if [ -n "$PUBLIC" ]; then
  echo "  Website:      $PUBLIC/"
  echo "  Admin-Panel:  $PUBLIC/admin/"
fi
echo "  Im Heimnetz:  ssh $ME@${LAN_IP:-<IP>}"
echo
echo "  Updates: im Admin-Panel unter \"Updates\" (ein Klick)."
echo
echo "  Naechste Schritte:"
echo "   - Admin-Panel oeffnen, einloggen, Minecraft starten"
echo "   - Optional: playit fuer Minecraft von aussen  (bash ~/playit/set-secret.sh)"
echo "   - Optional: Wallet anlegen  (python3 ~/wallet/new-wallet.py - NUR direkt am Laptop!)"
echo
echo "  WICHTIG: Einmal neu starten, damit alles sauber greift:  sudo reboot"
echo
