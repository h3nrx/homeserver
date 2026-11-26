#!/usr/bin/env python3
"""Status screen for the laptop's own display (tty1).

Started by homeserver-screen.service, switched on and off in the admin
(System). Only reads: the agent's stats, the public status of the site and the
Ubuntu update state. Draws a quiet black page, refreshed every 2 seconds.
"""
import json
import os
import shutil
import signal
import socket
import sys
import time
import urllib.request
from pathlib import Path

BRAND = "__BRAND__"
STATS = Path("/var/lib/panel/stats.json")
SYS = Path("/var/lib/panel/sysupdate.json")
PREFS = Path("__HOME__/files/.panel/prefs.json")   # the language picked in the admin

# The Linux console has 16 colours that can be redefined. Soft tones like in
# the admin, the same tone for normal and bold, so nothing gets louder.
PALETTE = {0: "0a0a0b", 1: "f87171", 2: "4ade80", 3: "fbbf24", 4: "60a5fa", 5: "a78bfa", 6: "2dd4bf", 7: "8d8d8a",
           8: "2a2a2d", 15: "f1f1ef"}
SET_PALETTE = "".join(f"\033]P{n:x}{c}" for n, c in PALETTE.items()) + \
    "".join(f"\033]P{n + 8:x}{PALETTE[n]}" for n in range(1, 7))
W, G, D, R = "\033[97m", "\033[37m", "\033[90m", "\033[0m"   # white, grey, dark, reset
RED, GREEN, AMBER, BLUE, VIOLET, TEAL = (f"\033[3{n}m" for n in range(1, 7))
BOLD = "\033[1m"

# 3x5 pixel digits, every pixel two characters wide
FONT = {
    "0": ["###", "# #", "# #", "# #", "###"], "1": ["  #", "  #", "  #", "  #", "  #"],
    "2": ["###", "  #", "###", "#  ", "###"], "3": ["###", "  #", "###", "  #", "###"],
    "4": ["# #", "# #", "###", "  #", "  #"], "5": ["###", "#  ", "###", "  #", "###"],
    "6": ["###", "#  ", "###", "# #", "###"], "7": ["###", "  #", "  #", "  #", "  #"],
    "8": ["###", "# #", "###", "# #", "###"], "9": ["###", "# #", "###", "  #", "###"],
    ":": [" ", "#", " ", "#", " "],
}
TEXT = {
    "de": {
        "days": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
        "months": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
        "date": "{day}, {d}. {month}", "dec": ",", "up": "an seit", "dh": "{d} T {h} Std", "hm": "{h} Std {m} Min",
        "loading": "Werte werden geladen…", "online": "online", "nobody": "niemand spielt", "one": "1 spielt", "many": "{n} spielen",
        "off": "aus", "runs": "läuft", "down": "nicht erreichbar", "updating": "wird aktualisiert…", "reboot": "Neustart nötig",
        "current": "aktuell", "nonet": "kein Netz",
    },
    "en": {
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "months": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
        "date": "{day}, {d} {month}", "dec": ".", "up": "up for", "dh": "{d} d {h} h", "hm": "{h} h {m} min",
        "loading": "Loading…", "online": "online", "nobody": "nobody playing", "one": "1 playing", "many": "{n} playing",
        "off": "off", "runs": "running", "down": "not reachable", "updating": "updating…", "reboot": "restart needed",
        "current": "up to date", "nonet": "no network",
    },
}
T = TEXT["de"]


def language() -> dict:
    try:
        lang = json.loads(PREFS.read_text()).get("lang")
    except (OSError, ValueError, AttributeError):
        lang = None
    return TEXT.get(lang) or TEXT["de" if os.environ.get("LANG", "").startswith("de") else "en"]


def big(text: str) -> list[str]:
    rows = []
    for y in range(5):
        cells = []
        for c in text:
            colour = TEAL if c == ":" else W
            cells.append(colour + FONT[c][y].replace("#", "██").replace(" ", "  "))
        rows.append("  ".join(cells) + R)
    return rows


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


def bar(frac: float, colour: str, width: int = 30) -> str:
    frac = max(0.0, min(1.0, frac))
    if frac >= 0.92:
        colour = RED
    elif frac >= 0.8:
        colour = AMBER
    n = round(frac * width)
    if frac > 0 and n == 0:
        n = 1
    return f"{colour}{'━' * n}{D}{'━' * (width - n)}{R}"


def dot(colour: str) -> str:
    return f"{colour}●{R}"


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
            return ip, f"{G}WLAN {TEAL}{'▮' * n}{D}{'▮' * (4 - n)}{R}"
        link = f"{G}LAN {dot(GREEN)}"
    return ip, link


def uptime(sec: float) -> str:
    d, h, m = int(sec // 86400), int(sec % 86400 // 3600), int(sec % 3600 // 60)
    return T["dh"].format(d=d, h=h) if d else T["hm"].format(h=h, m=m)


def frame(cols: int, lines: int) -> list[str]:
    global T
    T = language()
    st, pub, su = read_json(STATS), public(), read_json(SYS)
    now = time.localtime()
    width = min(76, cols - 4)
    out = []
    date = T["date"].format(day=T["days"][now.tm_wday], d=now.tm_mday, month=T["months"][now.tm_mon - 1])
    out.append(f"{TEAL}●{R} {BOLD}{W}{BRAND}{R}" + " " * max(1, width - len(BRAND) - 2 - len(date)) + f"{G}{date}{R}")
    out.append(f"{D}{'─' * width}{R}")
    out.append("")
    for row in big(time.strftime("%H:%M", now)):
        out.append(" " * max(0, (width - vis(row)) // 2) + row)
    out.append("")
    out.append(f"{D}{'─' * width}{R}")
    out.append("")
    fresh = st and time.time() * 1000 - st.get("ts", 0) < 30000
    if fresh:
        cpu = (st.get("cpu") or {}).get("total") or 0
        mt, mu = st.get("mem_total") or 1, st.get("mem_used") or 0
        disk = next((x for x in st.get("disks") or [] if x.get("mount") == "/"), None)
        bw = max(10, min(30, width - 34))
        out.append(f"{G}  cpu   {R}{bar(cpu / 100, BLUE, bw)}   {W}{round(cpu):>3} %{R}")
        out.append(f"{G}  ram   {R}{bar(mu / mt, VIOLET, bw)}   {W}{gb(mu)}{G} / {gb(mt)} GB{R}")
        if disk:
            out.append(f"{G}  disk  {R}{bar(disk['used'] / max(1, disk['total']), TEAL, bw)}   {W}{round(disk['used'] / 1024 ** 3)}{G} / {round(disk['total'] / 1024 ** 3)} GB{R}")
        temp = st.get("temp_c")
        tc = RED if (temp or 0) >= 85 else AMBER if (temp or 0) >= 70 else W
        out.append(f"{G}  temp  {tc}{round(temp) if temp is not None else '–'} °C{R}" + " " * 9 + f"{G}{T['up']} {W}{uptime(st.get('uptime') or 0)}{R}")
    else:
        out.append(f"{G}  {T['loading']}{R}")
    out.append("")
    mc = (pub or {}).get("mc") or {}
    n = int(mc.get("online") or 0)
    if mc.get("active"):
        mtxt = f"{dot(GREEN)} {W}{T['online']}{G} · " + (T["nobody"] if not n else T["one"] if n == 1 else T["many"].format(n=n))
    else:
        mtxt = f"{dot(D)} {G}{T['off']}"
    out.append(f"{G}  minecraft   {mtxt}{R}")
    out.append(f"{G}  website     " + (f"{dot(GREEN)} {W}{T['runs']}{R}" if pub else f"{dot(RED)} {RED}{T['down']}{R}"))
    if su.get("state") in ("starting", "running"):
        utxt = f"{dot(BLUE)} {W}{T['updating']}"
    elif Path("/var/run/reboot-required").exists():
        utxt = f"{dot(AMBER)} {W}{T['reboot']}"
    else:
        utxt = f"{dot(GREEN)} {W}{T['current']}"
    out.append(f"{G}  ubuntu      {utxt}{R}")
    out.append("")
    out.append(f"{D}{'─' * width}{R}")
    ip, link = address()
    net = (st or {}).get("net") or {}
    traffic = f"{TEAL}↓ {W}{rate(net.get('rx_rate') or 0)}   {VIOLET}↑ {W}{rate(net.get('tx_rate') or 0)}{R}" if fresh else ""
    left = f"{G}  {ip or '–'}{R}"
    right = f"{traffic}     {link}" if traffic else link
    out.append(left + " " * max(1, width - vis(left) - vis(right)) + right)
    # centre the block on the screen
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
