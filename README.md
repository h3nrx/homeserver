# Homeserver

Ein alter Laptop wird zum eigenen Server: Website, Admin-Panel, Minecraft, Discord-Bots, File Host und mehr. Alles läuft zu Hause und ist trotzdem von überall erreichbar, über Tailscale Funnel, ohne Portfreigabe am Router.

## Was drin ist

- **Startseite** mit Discord-Status, Uhr, Wallpapers und einem kleinen „Desktop“.
- **Admin-Panel** (als App installierbar):
  - System: CPU, RAM, Speicher, Netzwerk, Temperatur.
  - Minecraft (Paper): Start und Stopp, Konsole, Spieler, Whitelist, Plugins von Modrinth.
  - Discord-Bots: Python-Bots hochladen, starten, Logs ansehen.
  - File Host: Ordner, Verschieben per Drag & Drop, Speicherlimit, Upload vom iPhone/iPad per Kurzbefehl.
  - Clips, Pastebin, Musik-Radio, Datei-Konverter, Downloader, Discord-Kompressor, Hintergrund-Entferner, Upscaler.
  - Training, Wallet (nur Beobachten, keine privaten Schlüssel), Design und Power.
  - **Updates per Knopfdruck**.
- **Sicherheit**: Firewall, gehärtetes SSH, automatische Sicherheits-Updates. Die Container laufen read-only und ohne Sonderrechte.

## Voraussetzungen

- Ein Laptop oder PC mit **Ubuntu Server 26.04 LTS** (frisch installiert, mit Internet).
- Ein kostenloses **Tailscale**-Konto (für die öffentliche Adresse).
- Ca. 20 GB freier Speicher, besser mehr.

## Installieren

Auf dem Server, als normaler Benutzer (nicht `sudo`):

```bash
curl -fsSL https://github.com/h3nrx/homeserver/archive/refs/heads/main.tar.gz | tar -xz
cd homeserver-main
bash install.sh
```

Der Installer fragt nach einem Namen für deinen Server, optional nach deiner Discord-User-ID und nach dem Ubuntu-Passwort. Am Ende zeigt er die Adressen von Website und Admin-Panel. Danach einmal `sudo reboot`.

Du kannst `install.sh` jederzeit nochmal starten. Passwörter, Daten und Einstellungen bleiben erhalten.

## Updates

Im Admin-Panel auf **Updates** gehen und dann auf **Install** klicken. Das Update läuft so ab:

1. Die neue Version wird von GitHub geladen und geprüft.
2. Alle Dateien, die ersetzt werden, werden gesichert.
3. Nur die Teile, die sich geändert haben, werden neu gestartet.
4. Antwortet das Panel danach nicht, wird **automatisch zurückgesetzt**.

Ein Update fasst nie an: `.env`-Dateien, das Admin-Passwort, hochgeladene Dateien, Bots, Musik, die Minecraft-Welt und eigene Logos.

Per Hand geht es auch so (`v1.2.3` steht für die gewünschte Version):

```bash
sudo bash /usr/local/lib/homeserver/update.sh v1.2.3
```

Das Log liegt unter `/var/lib/homeserver/update.log`.

### Server, die noch mit dem alten ZIP-Kit eingerichtet wurden

Einmal umstellen, danach gibt es den Update-Knopf:

```bash
curl -fsSL https://raw.githubusercontent.com/h3nrx/homeserver/main/bootstrap.sh -o bootstrap.sh
bash bootstrap.sh
```

Das Skript liest Name, Logo, Discord-ID und Adresse aus deinen jetzigen Dateien, zeigt sie dir zur Kontrolle und installiert dann die neueste Version.

## Eigene Einstellungen

Alles, was nur zu deinem Server gehört, steht in `/etc/homeserver/config.env`:

| Eintrag | Bedeutung |
| --- | --- |
| `BRAND` | Name auf Website und Panel |
| `SLUG` | Kurzname ohne Sonderzeichen (für interne Namen) |
| `ICON` | Logo-Präfix: Bilder heißen `<ICON>-logo-64.png`, `-192`, `-512`, `<ICON>-app-192.png`, `-512`, `<ICON>-apple-touch.png` |
| `PUBLIC_URL` | Öffentliche Adresse (wird vom Installer gesetzt) |
| `DISCORD_ID` | Discord-User-ID für den Status auf der Startseite (leer = aus) |
| `MC_MEMORY` | RAM für Minecraft, z. B. `4G` |
| `REPO` | Woher Updates kommen |

**Eigenes Logo:** PNGs mit den Namen oben nach `~/bio/site/img/` legen und `ICON` anpassen. Danach das Update einmal per Hand mit der aktuellen Version ausführen (siehe oben), damit die neuen Namen überall eingetragen werden. Updates überschreiben deine Logos nie.

## Aufbau

```
install.sh       Erstinstallation
update.sh        Updater (wird nach /usr/local/lib/homeserver kopiert)
bootstrap.sh     Umstieg fuer aeltere Kit-Installationen
server/
  bio/           Website, Admin-Panel (Node), nginx, Media-Dienst (Python)
  panel-agent/   kleiner Root-Helfer fuer systemd, Statistiken, Updates
  minecraft/     Paper-Server (Docker)
  playit/        optionaler Tunnel fuer Minecraft
  wallet/        Offline-Wallet-Generator (Seed nur auf Papier)
  systemd/       Dienste
```

Platzhalter wie `__BRAND__` oder `__HOME__` in `server/` werden beim Installieren und bei jedem Update aus `config.env` ausgefüllt.

## Neue Version veröffentlichen (für Maintainer)

1. Änderungen in `server/` machen und testen.
2. `VERSION` hochzählen und `CHANGELOG.md` ergänzen.
3. Commit + Tag `vX.Y.Z` + GitHub-Release mit kurzen Release-Notes. Die Notes erscheinen im Panel.
