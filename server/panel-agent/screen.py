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

W, G, D, R = "\033[97m", "\033[37m", "\033[90m", "\033[0m"   # white, grey, dark, reset
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
DAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
MONTHS = ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]


def big(text: str) -> list[str]:
    rows = []
    for y in range(5):
        rows.append("  ".join(FONT[c][y].replace("#", "██").replace(" ", "  ") for c in text))
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


def bar(frac: float, width: int = 30) -> str:
    frac = max(0.0, min(1.0, frac))
    n = round(frac * width)
    return f"{W}{'█' * n}{D}{'░' * (width - n)}{R}"


def gb(b: float) -> str:
    return f"{b / 1024 ** 3:.1f}".replace(".", ",")


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
    link = "kein Netz"
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
            return ip, f"WLAN {W}{'█' * n}{D}{'░' * (4 - n)}{R}"
        link = "LAN"
    return ip, link


def uptime(sec: float) -> str:
    d, h, m = int(sec // 86400), int(sec % 86400 // 3600), int(sec % 3600 // 60)
    return f"{d} T {h} Std" if d else f"{h} Std {m} Min"


def frame(cols: int, lines: int) -> list[str]:
    st, pub, su = read_json(STATS), public(), read_json(SYS)
    now = time.localtime()
    width = min(76, cols - 4)
    out = []
    date = f"{DAYS[now.tm_wday]}, {now.tm_mday}. {MONTHS[now.tm_mon - 1]}"
    out.append(f"{BOLD}{W}{BRAND}{R}" + " " * max(1, width - len(BRAND) - len(date)) + f"{G}{date}{R}")
    out.append(f"{D}{'─' * width}{R}")
    out.append("")
    for row in big(time.strftime("%H:%M", now)):
        out.append(" " * max(0, (width - len(row)) // 2) + f"{W}{row}{R}")
    out.append("")
    out.append(f"{D}{'─' * width}{R}")
    out.append("")
    fresh = st and time.time() * 1000 - st.get("ts", 0) < 30000
    if fresh:
        cpu = (st.get("cpu") or {}).get("total") or 0
        mt, mu = st.get("mem_total") or 1, st.get("mem_used") or 0
        disk = next((x for x in st.get("disks") or [] if x.get("mount") == "/"), None)
        bw = max(10, min(30, width - 34))
        out.append(f"{G}  cpu   {R}{bar(cpu / 100, bw)}   {W}{round(cpu):>3} %{R}")
        out.append(f"{G}  ram   {R}{bar(mu / mt, bw)}   {W}{gb(mu)}{G} / {gb(mt)} GB{R}")
        if disk:
            out.append(f"{G}  disk  {R}{bar(disk['used'] / max(1, disk['total']), bw)}   {W}{round(disk['used'] / 1024 ** 3)}{G} / {round(disk['total'] / 1024 ** 3)} GB{R}")
        temp = st.get("temp_c")
        out.append(f"{G}  temp  {W}{round(temp) if temp is not None else '–'} °C{R}" + " " * 9 + f"{G}an seit {W}{uptime(st.get('uptime') or 0)}{R}")
    else:
        out.append(f"{G}  Werte werden geladen…{R}")
    out.append("")
    mc = (pub or {}).get("mc") or {}
    n = int(mc.get("online") or 0)
    mtxt = (f"{W}online{G} · " + ("niemand spielt" if not n else "1 spielt" if n == 1 else f"{n} spielen")) if mc.get("active") else f"{D}aus"
    out.append(f"{G}  minecraft   {mtxt}{R}")
    out.append(f"{G}  website     " + (f"{W}läuft{R}" if pub else f"\033[91mnicht erreichbar{R}"))
    if su.get("state") in ("starting", "running"):
        utxt = f"{W}wird aktualisiert…"
    elif Path("/var/run/reboot-required").exists():
        utxt = f"{W}Neustart nötig"
    else:
        utxt = f"{W}aktuell"
    out.append(f"{G}  ubuntu      {utxt}{R}")
    out.append("")
    out.append(f"{D}{'─' * width}{R}")
    ip, link = address()
    out.append(f"{G}  {ip or '–'}{R}" + " " * max(1, width - 2 - len(ip or "–") - vis(link)) + f"{G}{link}{R}")
    # centre the block on the screen
    left = " " * max(0, (cols - width) // 2)
    top = max(0, (lines - len(out)) // 2)
    return [""] * top + [left + x for x in out]


def main() -> None:
    out = sys.stdout
    stop = lambda *_: (out.write("\033[0m\033[2J\033[H\033[?25h"), out.flush(), sys.exit(0))
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    out.write("\033[?25l\033[2J")   # hide the cursor
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
