#!/bin/bash
# =====================================================================
#  Homeserver - Installer
#  Sets up everything: updates, Docker, Tailscale, website + admin panel,
#  Minecraft, Discord bots, wallet, security, one-click updates.
#
#  Run it as your normal user (NOT as root), inside the unpacked folder:
#      bash install.sh
#
#  It is safe to run again. Passwords, data and settings are kept.
# =====================================================================
set -euo pipefail

KIT="$(cd "$(dirname "$0")" && pwd)"
SRC="$KIT/server"
ME="$(id -un)"
HOMEDIR="$HOME"

# ---------- helpers ----------
c_ok=$'\e[32m'; c_hd=$'\e[1;36m'; c_wn=$'\e[33m'; c_er=$'\e[31m'; c_0=$'\e[0m'
step() { echo; echo "${c_hd}==> $*${c_0}"; }
ok()   { echo "${c_ok}    OK: $*${c_0}"; }
warn() { echo "${c_wn}    NOTE: $*${c_0}"; }
die()  { echo "${c_er}ERROR: $*${c_0}"; exit 1; }
ask_yes() { local a; read -rp "    $1 [y/N] " a; [[ "$a" =~ ^[jJyY] ]]; }

CONF=/etc/homeserver/config.env
CONF_KEYS=(HOMEDIR OWNER BRAND SLUG ICON PUBLIC_URL DISCORD_ID MC_MEMORY TITLE_SIZE OS_TITLE_SIZE REPO)
write_conf() {
  local k
  sudo install -d -m 755 /etc/homeserver
  for k in "${CONF_KEYS[@]}"; do printf '%s=%q\n' "$k" "${!k-}"; done | sudo tee "$CONF" >/dev/null
  sudo chmod 644 "$CONF"
}

# ---------- checks ----------
[ "$(id -u)" -ne 0 ] || die "Please do NOT run this with sudo. Just: bash install.sh"
{ [ -d "$SRC/bio" ] && [ -f "$KIT/update.sh" ]; } || die "The 'server' folder or update.sh is missing. Run the script inside the unpacked folder."
. /etc/os-release
[ "${ID:-}" = "ubuntu" ] || die "This isn't Ubuntu. This script is made for Ubuntu Server 26.04 LTS."
[ "$(id -u)" -eq 1000 ] || warn "Your user doesn't have the ID 1000. The containers run as 1000, which can cause permission problems."

# strip Windows line endings (in case the files came over Windows)
find "$KIT" -type f \( -name '*.sh' -o -name '*.py' -o -name '*.js' -o -name '*.yaml' -o -name '*.conf' -o -name '*.service' -o -name '*.html' \) -exec sed -i 's/\r$//' {} +

echo
echo "${c_hd}  Homeserver Installer  (version $(cat "$KIT/VERSION" 2>/dev/null || echo ?))${c_0}"
echo "  User: $ME   Folder: $HOMEDIR"
echo "  Takes about 15-30 minutes (depends on your internet). You'll be asked"
echo "  a few things along the way. Just read and answer."
echo
echo "  You'll be asked for your password next (the one of your Ubuntu login)."
echo "  You WON'T see any characters while typing - that's normal."
sudo -v || die "sudo didn't work."
# keep sudo awake during the install
( while true; do sudo -n true; sleep 50; kill -0 "$$" 2>/dev/null || exit; done ) 2>/dev/null &

# ---------- name + settings (kept across updates) ----------
HOMEDIR="$HOME"; OWNER="$ME"
REPO="$(cat "$KIT/REPO" 2>/dev/null | tr -d ' \r\n' || true)"
if [ -r "$CONF" ]; then
  # shellcheck disable=SC1090
  . "$CONF"
  ok "Found your earlier settings: $BRAND"
else
  echo
  echo "    What should your server be called? It's shown on the website and in the panel."
  echo "    Allowed: letters, numbers, spaces, dot, _ and - (max. 20 characters)."
  until read -rp "    Name: " BRAND && [[ "$BRAND" =~ ^[A-Za-z0-9][A-Za-z0-9\ ._-]{0,19}$ ]]; do echo "    Please try again."; done
  SLUG="$(printf '%s' "$BRAND" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9')"; [ -n "$SLUG" ] || SLUG=homeserver
  ICON=default
  # title size to fit the length of the name
  TITLE_SIZE="$(python3 -c 'import sys; n=max(len(sys.argv[1]),1); print(".135" if n<=6 else min(.135, round(.74/n, 3)))' "$BRAND")"
  OS_TITLE_SIZE="$(python3 -c 'import sys; n=max(len(sys.argv[1]),1); vw=14.2 if n<=6 else round(80/n, 1); print(f"min({vw}vw, {round(vw*1.62)}vh)")' "$BRAND")"
  echo
  echo "    Optional: show your Discord status on your start page."
  echo "    For that you need your Discord user ID (Discord: Settings -> Advanced ->"
  echo "    turn on Developer Mode, then right-click yourself -> Copy User ID)."
  read -rp "    Discord user ID (leave empty to skip): " DISCORD_ID || true
  [[ "${DISCORD_ID:-}" =~ ^[0-9]{15,22}$ ]] || DISCORD_ID=""
  PUBLIC_URL=""
  # RAM for Minecraft: about half, max 6G, min 2G
  RAM_GB=$(( $(awk '/MemTotal/ {print $2}' /proc/meminfo) / 1024 / 1024 ))
  MC_RAM=$(( RAM_GB / 2 )); [ "$MC_RAM" -gt 6 ] && MC_RAM=6; [ "$MC_RAM" -lt 2 ] && MC_RAM=2
  MC_MEMORY="${MC_RAM}G"
  write_conf
  ok "Settings saved in $CONF"
fi

# ---------- 1. system updates + base packages ----------
step "1/11  System updates and base packages"
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -y
sudo apt-get -o Dpkg::Options::="--force-confold" upgrade -y
sudo apt-get install -y ca-certificates curl gnupg git python3 python3-venv ufw \
  unattended-upgrades openssh-server openssl unzip htop
ok "System is up to date"

# ---------- 2. Docker ----------
step "2/11  Installing Docker"
if command -v docker >/dev/null && docker compose version >/dev/null 2>&1; then
  ok "Docker is already there ($(docker --version))"
else
  sudo install -m 0755 -d /etc/apt/keyrings
  sudo curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  sudo chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu ${VERSION_CODENAME} stable" \
    | sudo tee /etc/apt/sources.list.d/docker.list >/dev/null
  if sudo apt-get update -y && sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin; then
    ok "Docker (official source) installed"
  else
    warn "The Docker source didn't work, using the Ubuntu packages instead"
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
ok "Docker is running"

# ---------- 3. Tailscale ----------
step "3/11  Installing Tailscale"
if command -v tailscale >/dev/null; then
  ok "Tailscale is already there"
else
  curl -fsSL https://tailscale.com/install.sh | sh
  ok "Tailscale installed"
fi

# ---------- 4. copy files ----------
step "4/11  Copying the server files"
sudo bash "$KIT/update.sh" --files-only "$KIT" || die "Copying the files didn't work."
chmod +x "$HOMEDIR"/bio/set-password.sh "$HOMEDIR"/playit/set-secret.sh \
  "$HOMEDIR"/minecraft/boot.sh "$HOMEDIR"/panel-agent/*.sh "$HOMEDIR"/panel-agent/agent.py "$HOMEDIR"/wallet/new-wallet.py
ok "Files are in $HOMEDIR (Minecraft gets $MC_MEMORY RAM)"

# ---------- 5. folders + permissions ----------
step "5/11  Creating folders"
mkdir -p "$HOMEDIR"/files/.panel "$HOMEDIR"/bots "$HOMEDIR"/music \
         "$HOMEDIR"/minecraft/control "$HOMEDIR"/minecraft/data/logs "$HOMEDIR"/minecraft/data/plugins \
         "$HOMEDIR"/bio/panel-data "$HOMEDIR"/backups
chmod 775 "$HOMEDIR"/files "$HOMEDIR"/music
chmod 750 "$HOMEDIR"/minecraft/control
sudo install -d -o root -g "$ME" -m 755 /var/lib/panel
sudo install -d -o "$ME" -g "$ME" -m 770 /var/lib/panel/commands /var/lib/panel/responses
ok "Folders ready"

# ---------- 6. secret keys (.env) ----------
step "6/11  Creating internal keys"
LAN_IP="$(ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src"){print $(i+1); exit}}')"
umask 077
if [ ! -s "$HOMEDIR/minecraft/.env" ]; then
  printf 'RCON_PASSWORD=%s\n' "$(openssl rand -hex 24)" > "$HOMEDIR/minecraft/.env"
  ok "Minecraft RCON password created (you never need it yourself)"
else ok "minecraft/.env already exists - kept"; fi
if [ ! -s "$HOMEDIR/bio/.env" ]; then
  {
    printf 'SERVER_ADDRESS=\n'
    printf 'LOCAL_ADDRESS=%s\n' "${LAN_IP:-}"
    printf 'MEDIA_TOKEN=%s\n' "$(openssl rand -hex 32)"
  } > "$HOMEDIR/bio/.env"
  ok "bio/.env created (home address: ${LAN_IP:-unknown})"
else ok "bio/.env already exists - kept"; fi
umask 022

# ---------- 7. security ----------
step "7/11  Setting up security (firewall, SSH, updates)"
LAN_NET=""
if [ -n "${LAN_IP:-}" ]; then
  CIDR="$(ip -4 -o addr show | awk -v ip="$LAN_IP" '$4 ~ "^"ip"/" {print $4; exit}')"
  LAN_NET="$(python3 -c 'import ipaddress,sys; print(ipaddress.ip_interface(sys.argv[1]).network)' "$CIDR" 2>/dev/null || true)"
fi
sudo ufw --force reset >/dev/null
sudo ufw default deny incoming >/dev/null
sudo ufw default allow outgoing >/dev/null
if [ -n "$LAN_NET" ]; then
  sudo ufw allow from "$LAN_NET" to any port 22 proto tcp comment 'SSH from the home network only' >/dev/null
  ok "SSH allowed from your home network only ($LAN_NET)"
else
  sudo ufw allow 22/tcp >/dev/null
  warn "Home network not detected - SSH is allowed from anywhere (your router still lets nothing in)"
fi
sudo ufw --force enable >/dev/null
ok "Firewall on"

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
if sudo sshd -t; then sudo systemctl reload ssh 2>/dev/null || sudo systemctl reload sshd 2>/dev/null || true; ok "SSH hardened"
else sudo rm -f /etc/ssh/sshd_config.d/99-homeserver.conf; warn "SSH setting was invalid - undone"; fi

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
// Homeserver: install Docker and Tailscale updates automatically too
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
ok "Automatic security updates on"

# laptop: closing the lid = keep running, never go to sleep
sudo install -d /etc/systemd/logind.conf.d
sudo tee /etc/systemd/logind.conf.d/99-homeserver-lid.conf >/dev/null <<'EOF'
[Login]
HandleLidSwitch=ignore
HandleLidSwitchExternalPower=ignore
HandleLidSwitchDocked=ignore
EOF
sudo systemctl mask sleep.target suspend.target hibernate.target hybrid-sleep.target >/dev/null 2>&1 || true
ok "Closing the lid is fine - the server never sleeps (from the next restart on)"

# ---------- 8. systemd services ----------
step "8/11  Setting up background services"
# (step 4 already put the service files into /etc/systemd/system)
sudo systemctl daemon-reload
sudo systemctl enable panel-agent.service >/dev/null 2>&1
sudo systemctl restart panel-agent.service
ok "Panel agent is running (you start Minecraft later in the admin panel)"

# ---------- 9. build + start the containers ----------
step "9/11  Loading and starting the containers (takes a few minutes)"
cd "$HOMEDIR/minecraft"
sudo docker compose pull
sudo docker compose create          # also creates the network 'mcnet'
ok "Minecraft prepared (still off)"
cd "$HOMEDIR/bio"
sudo docker compose build --pull
sudo docker compose up -d
ok "Website, admin panel and media service are running"

# ---------- 10. admin password ----------
step "10/11  Admin password for the panel"
if [ -s "$HOMEDIR/bio/panel-data/auth.json" ] && ! ask_yes "There already is an admin password. Set a new one?"; then
  ok "Admin password kept"
else
  echo "    This is the password for https://.../admin/ (at least 12 characters)."
  echo "    Pick a DIFFERENT one than your Ubuntu password and write it down."
  until sudo bash "$HOMEDIR/bio/set-password.sh"; do echo "    Try again:"; done
  sudo chown -R "$ME:$ME" "$HOMEDIR/bio/panel-data"
  chmod 755 "$HOMEDIR/bio/panel-data"; chmod 600 "$HOMEDIR/bio/panel-data/auth.json"
fi


# ---------- 11. Tailscale + Funnel (make it public) ----------
step "11/11  Putting the server on the internet (Tailscale Funnel)"
if ! sudo tailscale status >/dev/null 2>&1; then
  echo "    A link (https://login.tailscale.com/...) shows up next."
  echo "    Open it on your PC/phone and sign in with your Tailscale account."
  sudo tailscale up
fi
ok "Tailscale connected"
echo "    Now Funnel gets switched on. If a link 'Funnel is not enabled' shows up,"
echo "    open it and click Enable - the script then carries on by itself."
sudo tailscale funnel --bg http://127.0.0.1:8080
DNS="$(sudo tailscale status --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null || true)"
PUBLIC=""
if [ -n "$DNS" ]; then
  PUBLIC="https://$DNS"
  if [ "${PUBLIC_URL:-}" != "$PUBLIC" ]; then
    PUBLIC_URL="$PUBLIC"; write_conf
    sudo bash "$KIT/update.sh" --files-only "$KIT" >/dev/null || warn "Couldn't fill in the address"
  fi
fi

# ---------- done ----------
echo
echo "${c_ok}=====================================================================${c_0}"
echo "${c_ok}  DONE! Your server \"$BRAND\" is running.${c_0}"
echo "${c_ok}=====================================================================${c_0}"
echo
if [ -n "$PUBLIC" ]; then
  echo "  Website:      $PUBLIC/"
  echo "  Admin panel:  $PUBLIC/admin/"
fi
echo "  At home:      ssh $ME@${LAN_IP:-<IP>}"
echo
echo "  Updates: in the admin panel under \"Updates\" (one click)."
echo
echo "  Next steps:"
echo "   - Open the admin panel, sign in, start Minecraft"
echo "   - Optional: playit for Minecraft from outside  (bash ~/playit/set-secret.sh)"
echo "   - Optional: create a wallet  (python3 ~/wallet/new-wallet.py - ONLY right at the laptop!)"
echo
echo "  IMPORTANT: restart once so everything takes effect:  sudo reboot"
echo
