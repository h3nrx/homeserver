# Homeserver

Turn an old laptop into your own server: website, admin panel, Minecraft, Discord bots, a file host and more. Everything runs at home and is still reachable from anywhere through Tailscale Funnel, with no port forwarding on your router.

The admin panel speaks English and German (switch under Design → Language).

## What's inside

- **Start page** with your Discord status, a clock and a calm animated background.
- **Admin panel** (installable as an app):
  - System: CPU, RAM, disk, network, temperature, blocked logins, a status screen on the laptop's own display.
  - Minecraft (Paper): start and stop, console, players, whitelist, plugins from Modrinth, world reset.
  - Discord bots: upload Python bots, start them, read their logs.
  - File Host: folders, drag & drop, storage limit, private files with share links, uploads from iPhone/iPad via a shortcut.
  - Clips, Pastebin, Music Radio, File Converter, Downloader, Discord Compressor, Background Remover, Image Upscaler.
  - Training, Grade calculator, Wallet (watch-only, no private keys), Camera (off by default), Design and Power.
  - **One-click updates** for the server software and for Ubuntu.
- **Security**: firewall, hardened SSH, automatic security updates, login lockout after 3 wrong passwords. The containers run read-only and without extra privileges.

## What you need

- A laptop or PC with **Ubuntu Server 26.04 LTS** (freshly installed, connected to the internet).
- A free **Tailscale** account (for the public address).
- About 20 GB of free disk space, more is better.

## Install

On the server, as your normal user (not with `sudo`):

```bash
curl -fsSL https://github.com/h3nrx/homeserver/archive/refs/heads/main.tar.gz | tar -xz
cd homeserver-main
bash install.sh
```

The installer asks for:

1. your Ubuntu password (for `sudo`; you won't see it while typing),
2. a name for your server (shown on the website and in the panel),
3. optionally your Discord user ID (for the status on the start page),
4. a new admin password for the panel (at least 12 characters, pick a different one than your Ubuntu password),
5. a Tailscale sign-in: open the link it shows and sign in. If it says "Funnel is not enabled", open that link too and click Enable.

At the end it shows the addresses of your website and admin panel. Then restart once:

```bash
sudo reboot
```

You can run `install.sh` again at any time. Passwords, data and settings are kept.

### Optional extras

- **Minecraft from outside your home** (without opening ports): create a free [playit.gg](https://playit.gg) agent, then run `bash ~/playit/set-secret.sh` and paste its secret key.
- **Wallet** (watch-only Bitcoin + Litecoin): run `python3 ~/wallet/new-wallet.py` directly at the laptop. It shows 12 words once – write them on paper. Only the public addresses are saved.
- **Camera tab**: `touch ~/panel-agent/camera.on`, then reload the admin. Off again: `rm ~/panel-agent/camera.on`.

## Updates

In the admin panel go to **Updates** and click **Install**. An update works like this:

1. The new version is downloaded from GitHub and checked.
2. Every file that gets replaced is backed up.
3. Only the parts that changed are restarted.
4. If the panel doesn't answer afterwards, everything is **rolled back automatically**.

An update never touches: `.env` files, the admin password, uploaded files, bots, music, the Minecraft world and your own logos.

You can also do it by hand (`v1.2.3` stands for the version you want):

```bash
sudo bash /usr/local/lib/homeserver/update.sh v1.2.3
```

The log is at `/var/lib/homeserver/update.log`.

### Servers that were set up with the old ZIP kit

Switch over once, then you get the update button:

```bash
curl -fsSL https://raw.githubusercontent.com/h3nrx/homeserver/main/bootstrap.sh -o bootstrap.sh
bash bootstrap.sh
```

The script reads name, logo, Discord ID and address from your current files, shows them to you to check and then installs the newest version.

## Your own settings

Everything that belongs only to your server is in `/etc/homeserver/config.env`:

| Key | Meaning |
| --- | --- |
| `BRAND` | Name on the website and in the panel |
| `SLUG` | Short name without special characters (for internal names) |
| `ICON` | Logo prefix: the pictures are called `<ICON>-logo-64.png`, `-192`, `-512`, `<ICON>-app-192.png`, `-512`, `<ICON>-apple-touch.png` |
| `PUBLIC_URL` | Public address (set by the installer) |
| `DISCORD_ID` | Discord user ID for the status on the start page (empty = off) |
| `MC_MEMORY` | RAM for Minecraft, e.g. `4G` |
| `REPO` | Where updates come from |

**Your own logo:** put PNGs with the names above into `~/bio/site/img/` and change `ICON`. Then run the update once by hand with the current version (see above), so the new names get filled in everywhere. Updates never overwrite your logos.

## Layout

```
install.sh       first install
update.sh        updater (copied to /usr/local/lib/homeserver)
bootstrap.sh     switch-over for older kit installs
server/
  bio/           website, admin panel (Node), nginx, media service (Python)
  panel-agent/   small root helper for systemd, stats, updates, the laptop screen
  minecraft/     Paper server (Docker)
  playit/        optional tunnel for Minecraft
  wallet/        offline wallet generator (seed on paper only)
  systemd/       services
```

Placeholders like `__BRAND__` or `__HOME__` in `server/` are filled in from `config.env` during the install and every update.

## Publishing a new version (for maintainers)

1. Make and test your changes in `server/`.
2. Bump `VERSION` and add to `CHANGELOG.md`.
3. Commit + tag `vX.Y.Z` + a GitHub release with short release notes. The notes show up in the panel: write them in English and put a German version inside `<details><summary>Deutsch</summary> … </details>` – German admins see that part.
