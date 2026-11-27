#!/usr/bin/env python3
"""Status screen for the laptop's own display (tty1).

Started by homeserver-screen.service, switched on and off in the admin
(System). Only reads: the agent's stats, the public status of the site, the
Discord bot units and the Ubuntu update state. Draws a quiet page in the style
of neofetch, refreshed every 2 seconds.
"""
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

BRAND = "__BRAND__"
OWNER = "__USER__"
STATS = Path("/var/lib/panel/stats.json")
SYS = Path("/var/lib/panel/sysupdate.json")
PREFS = Path("__HOME__/files/.panel/prefs.json")   # the language picked in the admin
UBUNTU = Path("/var/lib/update-notifier/updates-available")

# The Linux console has 16 colours that can be redefined: a soft pastel set on
# a dark violet-grey, the same tone for normal and bold so nothing gets louder.
PALETTE = {0: "1e1e2e", 1: "f38ba8", 2: "a6e3a1", 3: "f9e2af", 4: "89b4fa", 5: "cba6f7", 6: "94e2d5", 7: "a6adc8",
           8: "585b70", 15: "cdd6f4"}
SET_PALETTE = "".join(f"\033]P{n:x}{c}" for n, c in PALETTE.items()) + \
    "".join(f"\033]P{n + 8:x}{PALETTE[n]}" for n in range(1, 7))
W, G, D, R = "\033[97m", "\033[37m", "\033[90m", "\033[0m"   # text, soft, dim, reset
RED, GREEN, AMBER, BLUE, LILAC, TEAL = (f"\033[3{n}m" for n in range(1, 7))
BOLD = "\033[1m"

# 5-row block letters for the name; every pixel is one character
GLYPHS = {
    "A": [" ### ", "#   #", "#####", "#   #", "#   #"], "B": ["#### ", "#   #", "#### ", "#   #", "#### "],
    "C": [" ####", "#    ", "#    ", "#    ", " ####"], "D": ["#### ", "#   #", "#   #", "#   #", "#### "],
    "E": ["#####", "#    ", "#### ", "#    ", "#####"], "F": ["#####", "#    ", "#### ", "#    ", "#    "],
    "G": [" ####", "#    ", "#  ##", "#   #", " ####"], "H": ["#   #", "#   #", "#####", "#   #", "#   #"],
    "I": ["###", " # ", " # ", " # ", "###"], "J": ["  ###", "   # ", "   # ", "#  # ", " ##  "],
    "K": ["#   #", "#  # ", "###  ", "#  # ", "#   #"], "L": ["#    ", "#    ", "#    ", "#    ", "#####"],
    "M": ["#   #", "## ##", "# # #", "#   #", "#   #"], "N": ["#   #", "##  #", "# # #", "#  ##", "#   #"],
    "O": [" ### ", "#   #", "#   #", "#   #", " ### "], "P": ["#### ", "#   #", "#### ", "#    ", "#    "],
    "Q": [" ### ", "#   #", "# # #", "#  # ", " ## #"], "R": ["#### ", "#   #", "#### ", "#  # ", "#   #"],
    "S": [" ####", "#    ", " ### ", "    #", "#### "], "T": ["#####", "  #  ", "  #  ", "  #  ", "  #  "],
    "U": ["#   #", "#   #", "#   #", "#   #", " ### "], "V": ["#   #", "#   #", "#   #", " # # ", "  #  "],
    "W": ["#   #", "#   #", "# # #", "## ##", "#   #"], "X": ["#   #", " # # ", "  #  ", " # # ", "#   #"],
    "Y": ["#   #", " # # ", "  #  ", "  #  ", "  #  "], "Z": ["#####", "   # ", "  #  ", " #   ", "#####"],
    "0": [" ### ", "#  ##", "# # #", "##  #", " ### "], "1": ["  #  ", " ##  ", "  #  ", "  #  ", " ### "],
    "2": ["#### ", "    #", " ### ", "#    ", "#####"], "3": ["#### ", "    #", " ### ", "    #", "#### "],
    "4": ["#   #", "#   #", "#####", "    #", "    #"], "5": ["#####", "#    ", "#### ", "    #", "#### "],
    "6": [" ### ", "#    ", "#### ", "#   #", " ### "], "7": ["#####", "    #", "   # ", "  #  ", "  #  "],
    "8": [" ### ", "#   #", " ### ", "#   #", " ### "], "9": [" ### ", "#   #", " ####", "    #", " ### "],
    ".": ["  ", "  ", "  ", "  ", "##"], "-": ["    ", "    ", "####", "    ", "    "],
    "_": ["     ", "     ", "     ", "     ", "#####"], " ": ["   "] * 5,
}
TEXT = {
    "de": {
        "days": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
        "months": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
        "date": "{d}. {month}", "dec": ",", "dh": "{d} Tage, {h} Std", "d1": "1 Tag, {h} Std", "hm": "{h} Std {m} Min",
        "k_up": "an seit", "k_cpu": "cpu", "k_ram": "ram", "k_disk": "speicher", "k_svc": "dienste", "k_mc": "minecraft",
        "k_upd": "updates", "k_net": "netz",
        "loading": "Werte werden geladen…", "online": "online", "nobody": "niemand spielt", "one": "1 spielt", "many": "{n} spielen",
        "off": "aus", "all_ok": "alles läuft", "bots": "{n} Bots", "bot1": "1 Bot", "crash1": "1 Bot abgestürzt", "crash": "{n} Bots abgestürzt",
        "web_down": "Website nicht erreichbar", "updating": "wird aktualisiert…", "reboot": "Neustart nötig",
        "current": "aktuell", "ready": "{n} bereit", "sec": "{n} Sicherheit", "nonet": "kein Netz",
    },
    "en": {
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "months": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
        "date": "{d} {month}", "dec": ".", "dh": "{d} days, {h} h", "d1": "1 day, {h} h", "hm": "{h} h {m} min",
        "k_up": "uptime", "k_cpu": "cpu", "k_ram": "ram", "k_disk": "disk", "k_svc": "services", "k_mc": "minecraft",
        "k_upd": "updates", "k_net": "network",
        "loading": "Loading…", "online": "online", "nobody": "nobody playing", "one": "1 playing", "many": "{n} playing",
        "off": "off", "all_ok": "all running", "bots": "{n} bots", "bot1": "1 bot", "crash1": "1 bot crashed", "crash": "{n} bots crashed",
        "web_down": "website not reachable", "updating": "updating…", "reboot": "restart needed",
        "current": "up to date", "ready": "{n} ready", "sec": "{n} security", "nonet": "no network",
    },
}
T = TEXT["de"]


def language() -> dict:
    try:
        lang = json.loads(PREFS.read_text()).get("lang")
    except (OSError, ValueError, AttributeError):
        lang = None
    return TEXT.get(lang) or TEXT["de" if os.environ.get("LANG", "").startswith("de") else "en"]


def logo(text: str) -> list[str]:
    rows = [[] for _ in range(5)]
    for c in text.upper():
        g = GLYPHS.get(c, GLYPHS[" "])
        for y in range(5):
            rows[y].append(g[y].replace("#", "█"))
    return [" ".join(r).rstrip() for r in rows]


def vis(s: str) -> int:                 # visible length, without colour codes
    out, skip = 0, False
    for ch in s:
        if ch == "\033":
            skip = True
        elif skip:
            skip = ch != "m"
        else:
            out += 1
    return out


def level(frac: float, normal: str) -> str:
    return RED if frac >= 0.92 else AMBER if frac >= 0.8 else normal


def gb(b: float) -> str:
    return f"{b / 1024 ** 3:.1f}".replace(".", T["dec"])


def rate(b: float) -> str:
    if b >= 1024 ** 2:
        return f"{b / 1024 ** 2:.1f} MB/s".replace(".", T["dec"])
    return f"{round(b / 1024)} kB/s"


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def public() -> dict | None:
    try:
        with urllib.request.urlopen("http://127.0.0.1:8080/api/public", timeout=2) as r:
            return json.loads(r.read(4096))
    except Exception:
        return None


_bots = {"t": 0.0, "v": (0, 0)}


def bots() -> tuple[int, int]:
    """(running, crashed) Discord bots, asked every 10 seconds."""
    if time.time() - _bots["t"] > 10:
        _bots["t"] = time.time()
        try:
            out = subprocess.run(["systemctl", "list-units", "--all", "--plain", "--no-legend", "discord-bot@*"],
                                 capture_output=True, text=True, timeout=5).stdout
            states = [line.split()[2] for line in out.splitlines() if len(line.split()) > 2]
            _bots["v"] = (states.count("active"), states.count("failed"))
        except (OSError, subprocess.SubprocessError):
            pass
    return _bots["v"]


def ubuntu_waiting() -> tuple[int, int] | None:
    """(updates, security) from Ubuntu's own daily check; None when unknown or older than the last install."""
    try:
        if UBUNTU.stat().st_mtime < Path("/var/lib/dpkg/status").stat().st_mtime:
            return None
        txt = UBUNTU.read_text()
    except OSError:
        return None
    n = re.search(r"(\d+) updates? can be applied immediately", txt)
    s = re.search(r"(\d+) of these updates? (?:is a|are) (?:standard )?security", txt)
    if n is None and "can be applied" not in txt:
        return (0, 0) if txt.strip() == "" or "0 updates" in txt else None
    return (int(n[1]) if n else 0, int(s[1]) if s else 0)


def address() -> tuple[str, str]:
    """Local address and how the laptop is connected."""
    ip = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # nothing is sent
            ip = s.getsockname()[0]
    except OSError:
        pass
    link = f"{RED}{T['nonet']}{R}"
    for dev in sorted(os.listdir("/sys/class/net")):
        try:
            up = Path(f"/sys/class/net/{dev}/operstate").read_text().strip() == "up"
        except OSError:
            continue
        if not up or dev.startswith(("lo", "docker", "br-", "veth", "tailscale")):
            continue
        if Path(f"/sys/class/net/{dev}/wireless").exists():
            q = 0
            try:
                for line in Path("/proc/net/wireless").read_text().splitlines()[2:]:
                    if line.split(":")[0].strip() == dev:
                        q = float(line.split()[2].rstrip("."))
            except (OSError, ValueError, IndexError):
                pass
            n = 4 if q >= 55 else 3 if q >= 40 else 2 if q >= 25 else 1
            return ip, f"{W}WLAN {LILAC}{'▮' * n}{D}{'▮' * (4 - n)}{R}"
        link = f"{W}LAN{R}"
    return ip, link


def uptime(sec: float) -> str:
    d, h, m = int(sec // 86400), int(sec % 86400 // 3600), int(sec % 3600 // 60)
    return T["d1"].format(h=h) if d == 1 else T["dh"].format(d=d, h=h) if d else T["hm"].format(h=h, m=m)


def frame(cols: int, lines: int) -> list[str]:
    global T
    T = language()
    st, pub, su = read_json(STATS), public(), read_json(SYS)
    now = time.localtime()
    fresh = st and time.time() * 1000 - st.get("ts", 0) < 30000
    dot = f" {D}·{R} "

    rows = []                                   # (key, value) on the right
    if fresh:
        cpu = (st.get("cpu") or {}).get("total") or 0
        temp = st.get("temp_c")
        tc = RED if (temp or 0) >= 85 else AMBER if (temp or 0) >= 70 else GREEN
        mt, mu = st.get("mem_total") or 1, st.get("mem_used") or 0
        disk = next((x for x in st.get("disks") or [] if x.get("mount") == "/"), None)
        rows.append((T["k_up"], f"{W}{uptime(st.get('uptime') or 0)}"))
        rows.append((T["k_cpu"], f"{level(cpu / 100, GREEN)}{round(cpu)} %" + (f"{dot}{tc}{round(temp)} °C" if temp is not None else "")))
        rows.append((T["k_ram"], f"{level(mu / mt, BLUE)}{gb(mu)} / {gb(mt)} GB"))
        if disk:
            f = disk["used"] / max(1, disk["total"])
            rows.append((T["k_disk"], f"{level(f, W)}{round(disk['used'] / 1024 ** 3)} / {round(disk['total'] / 1024 ** 3)} GB"))
    else:
        rows.append(("", f"{G}{T['loading']}"))

    run, crashed = bots()
    if not pub:
        svc = f"{RED}{T['web_down']}"
    elif crashed:
        svc = f"{AMBER}{T['crash1'] if crashed == 1 else T['crash'].format(n=crashed)}"
    else:
        svc = f"{GREEN}{T['all_ok']}" + (f"{dot}{W}{T['bot1'] if run == 1 else T['bots'].format(n=run)}" if run else "")
    rows.append((T["k_svc"], svc))

    mc = (pub or {}).get("mc") or {}
    n = int(mc.get("online") or 0)
    rows.append((T["k_mc"], f"{GREEN}{T['online']}{dot}{W}" + (T["nobody"] if not n else T["one"] if n == 1 else T["many"].format(n=n))
                 if mc.get("active") else f"{D}{T['off']}"))

    waiting = ubuntu_waiting()
    if su.get("state") in ("starting", "running"):
        rows.append((T["k_upd"], f"{BLUE}{T['updating']}"))
    elif Path("/var/run/reboot-required").exists():
        rows.append((T["k_upd"], f"{AMBER}{T['reboot']}"))
    elif waiting and waiting[0]:
        rows.append((T["k_upd"], f"{AMBER}{T['ready'].format(n=waiting[0])}" + (f"{dot}{RED}{T['sec'].format(n=waiting[1])}" if waiting[1] else "")))
    elif waiting is not None:
        rows.append((T["k_upd"], f"{GREEN}{T['current']}"))

    ip, link = address()
    net = (st or {}).get("net") or {}
    traffic = f"{dot}{TEAL}↓ {W}{rate(net.get('rx_rate') or 0)}  {LILAC}↑ {W}{rate(net.get('tx_rate') or 0)}" if fresh else ""
    rows.append((T["k_net"], f"{link}{dot}{W}{ip or '–'}{traffic}"))

    # left column: time and date, next to the first rows
    left = ["", f"{BOLD}{LILAC}{time.strftime('%H:%M', now)}", "", f"{G}{T['days'][now.tm_wday]}",
            f"{G}{T['date'].format(d=now.tm_mday, month=T['months'][now.tm_mon - 1])}"]
    lw, kw = 22, 12
    who = f"{LILAC}{OWNER}{D}@{BLUE}{BRAND}"
    body = [(left[0], who), (left[1], f"{D}{'─' * vis(who)}")]
    for i, (k, v) in enumerate(rows):
        body.append((left[i + 2] if i + 2 < len(left) else "", f"{LILAC}{k:<{kw}}{R}{v}" if k else v))
    text = [f"{l}{R}" + " " * max(1, lw - vis(l)) + f"{v}{R}" for l, v in body]

    art = logo(BRAND)
    out = ([f"{LILAC}{r}{R}" for r in art] + [""] if max(map(len, art)) <= cols - 4 else [f"{BOLD}{LILAC}{BRAND}{R}", ""]) + text
    width = max(vis(x) for x in out)
    pad = " " * max(0, (cols - width) // 2)
    top = max(0, (lines - len(out)) // 2)
    return [""] * top + [pad + x for x in out]


def main() -> None:
    out = sys.stdout
    stop = lambda *_: (out.write("\033]R\033[0m\033[2J\033[H\033[?25h"), out.flush(), sys.exit(0))
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    out.write(SET_PALETTE + "\033[0m\033[?25l\033[2J")   # own colours, no cursor
    last = None
    while True:
        cols, lines = shutil.get_terminal_size((120, 33))
        try:
            rows = frame(cols, lines)
        except Exception as e:      # never die because one value was odd
            rows = [f"  {e}"]
        if (cols, lines) != last:
            out.write("\033[2J")
            last = (cols, lines)
        rows = (rows + [""] * lines)[:lines]
        out.write("\033[H" + "\n".join(r + "\033[K" for r in rows[:-1]) + rows[-1] + "\033[K")
        out.flush()
        time.sleep(2 - time.time() % 2 + 0.05)


if __name__ == "__main__":
    main()
