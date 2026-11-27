#!/usr/bin/env python3
"""Status screen for the laptop's own display (tty1).

Started by homeserver-screen.service, switched on and off in the admin
(System). Only reads: the agent's stats, the public status of the site, the
Discord bot units and the Ubuntu update state. Draws a calm page: four big
numbers, a line of status dots, the connection and the uptime, refreshed every 2 seconds.
Below that a little cat strolls along the bottom.
"""
import json
import os
import random
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
STATS = Path("/var/lib/panel/stats.json")
SYS = Path("/var/lib/panel/sysupdate.json")
PREFS = Path("__HOME__/files/.panel/prefs.json")   # the language picked in the admin
UBUNTU = Path("/var/lib/update-notifier/updates-available")

# The Linux console has 16 colours that can be redefined: near-black, one soft
# lilac accent, greys, and green / amber / red only where something has a state.
PALETTE = {0: "0f0f11", 1: "f08080", 2: "86d39a", 3: "f0c36b", 4: "89b4fa", 5: "c4b5fd", 6: "7dd3c0", 7: "a6a6ab",
           8: "6b6b70", 15: "e8e8e6"}
SET_PALETTE = "".join(f"\033]P{n:x}{c}" for n, c in PALETTE.items()) + \
    "".join(f"\033]P{n + 8:x}{PALETTE[n]}" for n in range(1, 7))
FG, DIM, ACC, OK, WARN, BAD = "97", "90", "35", "32", "33", "31"   # colour codes for the grid

# 3x5 digits for the big numbers; every pixel becomes two characters
GLYPHS = {
    "0": ["###", "# #", "# #", "# #", "###"], "1": [" # ", "## ", " # ", " # ", "###"], "2": ["###", "  #", "###", "#  ", "###"],
    "3": ["###", "  #", " ##", "  #", "###"], "4": ["# #", "# #", "###", "  #", "  #"], "5": ["###", "#  ", "###", "  #", "###"],
    "6": ["###", "#  ", "###", "# #", "###"], "7": ["###", "  #", "  #", "  #", "  #"], "8": ["###", "# #", "###", "# #", "###"],
    "9": ["###", "# #", "###", "  #", "###"], "-": ["   ", "   ", "###", "   ", "   "],
}
TEXT = {
    "de": {
        "days": ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"],
        "months": ["Januar", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"],
        "date": "{day}, {d}. {month}", "dh": "{d} Tagen, {h} Std", "d1": "1 Tag, {h} Std", "hm": "{h} Std {m} Min", "up": "an seit {t}",
        "cpu": "cpu %", "temp": "°C", "ram": "ram %", "disk": "speicher %", "loading": "Werte werden geladen…",
        "web": "website", "web_down": "website nicht erreichbar", "bot1": "1 Bot", "bots": "{n} Bots",
        "crash1": "1 Bot abgestürzt", "crash": "{n} Bots abgestürzt",
        "mc_off": "minecraft aus", "mc_0": "minecraft · niemand spielt", "mc_1": "minecraft · 1 spielt", "mc_n": "minecraft · {n} spielen",
        "updating": "wird aktualisiert…", "reboot": "Neustart nötig", "current": "ubuntu aktuell",
        "upd1": "1 Ubuntu-Update", "upd": "{n} Ubuntu-Updates", "sec": "{n} Sicherheit", "nonet": "kein Netz",
    },
    "en": {
        "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"],
        "months": ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"],
        "date": "{day}, {d} {month}", "dh": "{d} days, {h} h", "d1": "1 day, {h} h", "hm": "{h} h {m} min", "up": "up {t}",
        "cpu": "cpu %", "temp": "°C", "ram": "ram %", "disk": "disk %", "loading": "Loading…",
        "web": "website", "web_down": "website not reachable", "bot1": "1 bot", "bots": "{n} bots",
        "crash1": "1 bot crashed", "crash": "{n} bots crashed",
        "mc_off": "minecraft off", "mc_0": "minecraft · nobody playing", "mc_1": "minecraft · 1 playing", "mc_n": "minecraft · {n} playing",
        "updating": "updating…", "reboot": "restart needed", "current": "ubuntu up to date",
        "upd1": "1 Ubuntu update", "upd": "{n} Ubuntu updates", "sec": "{n} security", "nonet": "no network",
    },
}
T = TEXT["de"]


def language() -> dict:
    try:
        lang = json.loads(PREFS.read_text()).get("lang")
    except (OSError, ValueError, AttributeError):
        lang = None
    return TEXT.get(lang) or TEXT["de" if os.environ.get("LANG", "").startswith("de") else "en"]


class Grid:
    """The screen as characters with a colour each; turned into lines at the end."""

    def __init__(self, cols: int, lines: int):
        self.cols, self.lines = cols, lines
        self.cells = [[(" ", FG)] * cols for _ in range(lines)]

    def put(self, x: int, y: int, text: str, colour: str = FG) -> int:
        if 0 <= y < self.lines:
            for i, ch in enumerate(text):
                if 0 <= x + i < self.cols:
                    self.cells[y][x + i] = (ch, colour)
        return x + len(text)

    def row(self, y: int, parts: list[tuple[str, str]], x: int | None = None) -> None:
        """Several coloured pieces in one line, centred unless x is given."""
        width = sum(len(t) for t, _ in parts)
        x = (self.cols - width) // 2 if x is None else x
        for text, colour in parts:
            x = self.put(x, y, text, colour)

    def big(self, x: int, y: int, text: str, colour: str) -> None:
        for ch in text:
            g = GLYPHS.get(ch, GLYPHS["-"])
            for r in range(5):
                for i, p in enumerate(g[r]):
                    if p == "#":
                        self.put(x + i * 2, y + r, "██", colour)
            x += len(g[0]) * 2 + 2

    def render(self) -> list[str]:
        out = []
        for cells in self.cells:
            line, cur = "", None
            for ch, colour in cells:
                if colour != cur and ch != " ":
                    line += f"\033[0;{colour}m"
                    cur = colour
                line += ch
            out.append(line.rstrip() + "\033[0m")
        return out


def big_width(text: str) -> int:
    return sum(len(GLYPHS.get(c, GLYPHS["-"])[0]) * 2 + 2 for c in text) - 2


def level(frac: float) -> str:
    return BAD if frac >= 0.92 else WARN if frac >= 0.8 else ACC


def rate(b: float) -> str:
    if b >= 1024 ** 2:
        s = f"{b / 1024 ** 2:.1f} MB/s"
        return s.replace(".", ",") if T is TEXT["de"] else s
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


def address() -> tuple[str, list[tuple[str, str]]]:
    """Local address and how the laptop is connected."""
    ip = ""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))  # nothing is sent
            ip = s.getsockname()[0]
    except OSError:
        pass
    link = [(T["nonet"], BAD)]
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
            return ip, [("WLAN ", DIM), ("▮" * n, ACC), ("▮" * (4 - n), DIM)]
        link = [("LAN", DIM)]
    return ip, link


def uptime(sec: float) -> str:
    d, h, m = int(sec // 86400), int(sec % 86400 // 3600), int(sec % 3600 // 60)
    return T["d1"].format(h=h) if d == 1 else T["dh"].format(d=d, h=h) if d else T["hm"].format(h=h, m=m)


def frame(cols: int, lines: int) -> list[str]:
    global T
    T = language()
    st, pub, su = read_json(STATS), public(), read_json(SYS)
    fresh = bool(st) and time.time() * 1000 - st.get("ts", 0) < 30000
    now = time.localtime()
    g = Grid(cols, lines)
    m = 6 if cols >= 80 else 1

    # top: name on the left, time and date on the right
    g.put(m, 2, BRAND, ACC)
    clock = time.strftime("%H:%M", now)
    date = T["date"].format(day=T["days"][now.tm_wday], d=now.tm_mday, month=T["months"][now.tm_mon - 1])
    g.put(cols - m - len(clock), 2, clock, FG)
    g.put(cols - m - len(date), 3, date, DIM)

    top = max(5, (lines - 23) // 2 + 2)     # leaves the bottom rows to the cat
    if fresh:
        cpu = (st.get("cpu") or {}).get("total") or 0
        temp = st.get("temp_c")
        mt, mu = st.get("mem_total") or 1, st.get("mem_used") or 0
        disk = next((x for x in st.get("disks") or [] if x.get("mount") == "/"), None)
        df = disk["used"] / max(1, disk["total"]) if disk else None
        cells = [
            (f"{round(cpu)}", T["cpu"], level(cpu / 100)),
            (f"{round(temp)}" if temp is not None else "-", T["temp"],
             BAD if (temp or 0) >= 85 else WARN if (temp or 0) >= 70 else ACC),
            (f"{round(mu / mt * 100)}", T["ram"], level(mu / mt)),
            (f"{round(df * 100)}" if df is not None else "-", T["disk"], level(df or 0)),
        ]
        cw = min(28, (cols - 2) // 4)
        x0 = (cols - cw * 4) // 2
        for i, (num, label, colour) in enumerate(cells):
            g.big(x0 + i * cw + (cw - big_width(num)) // 2, top, num, colour)
            g.put(x0 + i * cw + (cw - len(label)) // 2, top + 7, label, DIM)
    else:
        g.row(top + 3, [(T["loading"], DIM)])

    # one line of status dots: only what can go wrong or what's going on
    dots = [(OK, T["web"]) if pub else (BAD, T["web_down"])]
    run, crashed = bots()
    if crashed:
        dots.append((BAD, T["crash1"] if crashed == 1 else T["crash"].format(n=crashed)))
    elif run:
        dots.append((OK, T["bot1"] if run == 1 else T["bots"].format(n=run)))
    mc = (pub or {}).get("mc") or {}
    n = int(mc.get("online") or 0)
    if mc.get("active"):
        dots.append((OK, T["mc_0"] if not n else T["mc_1"] if n == 1 else T["mc_n"].format(n=n)))
    else:
        dots.append((DIM, T["mc_off"]))
    waiting = ubuntu_waiting()
    if su.get("state") in ("starting", "running"):
        dots.append((ACC, T["updating"]))
    elif Path("/var/run/reboot-required").exists():
        dots.append((WARN, T["reboot"]))
    elif waiting and waiting[0]:
        dots.append((WARN, (T["upd1"] if waiting[0] == 1 else T["upd"].format(n=waiting[0]))
                     + (f" · {T['sec'].format(n=waiting[1])}" if waiting[1] else "")))
    elif waiting is not None:
        dots.append((OK, T["current"]))
    parts = []
    for i, (colour, label) in enumerate(dots):
        parts += ([("      ", FG)] if i else []) + [("● ", colour), (label, DIM if colour == DIM else FG)]
    g.row(top + 13, parts)

    # connection, quiet
    ip, link = address()
    net = st.get("net") or {}
    conn = link + [("  ·  ", DIM), (ip or "–", DIM)]
    if fresh:
        conn += [("  ·  ↓ ", DIM), (rate(net.get("rx_rate") or 0), DIM), ("  ↑ ", DIM), (rate(net.get("tx_rate") or 0), DIM)]
    g.row(top + 16, conn)

    if fresh:
        g.row(top + 18, [(T["up"].format(t=uptime(st.get("uptime") or 0)), DIM)])
    return g.render()


# The cat at the bottom, drawn as a little pixel cat facing right (mirrored for left).
# █ body, ▲ ears, ▮ legs, e the eye, t the tail (up or down, it swishes).
CAT = {
    "walk0": ["      ▲ ▲", "t██████e█", " ▮ ▮  ▮ ▮"],
    "walk1": ["      ▲ ▲", "t██████e█", "  ▮    ▮ "],
    "walk2": ["      ▲ ▲", "t██████e█", " ▮▮   ▮▮ "],
    "sit":   ["      ▲ ▲", "    ███e█", "  t█████▮"],
    "sleep": ["         ", "      ▲ ▲", "t██████e█"],
}
CAT_W = 9
STRIP = 5          # rows: room for a hop, 3 rows of cat, the ground
BODY, EYE = "37", "30;47"


class Cat:
    """Walks with moving legs, hops now and then, sits and blinks, sometimes naps. The tail swishes."""

    def __init__(self):
        self.x, self.dir, self.state, self.t, self.tick, self.blink = 10.0, 1, "walk", 0, 0, 0

    def step(self, lo: int, hi: int) -> None:
        self.tick += 1
        self.t -= 1
        r = random.random()
        if self.state in ("sit", "sleep"):
            if self.state == "sit":
                self.blink = self.blink - 1 if self.blink else (2 if r < 0.03 else 0)
            if self.t <= 0:
                self.state, self.blink = "walk", 0
                if random.random() < 0.5:
                    self.dir = -self.dir
            return
        if self.state == "hop":
            self.x += self.dir * 1.0
            if self.t <= 0:
                self.state = "walk"
        else:
            self.x += self.dir * 0.5
            if r < 0.005:
                self.state, self.t = "sit", random.randint(30, 90)
            elif r < 0.0065:
                self.state, self.t = "sleep", random.randint(100, 250)
            elif r < 0.015:
                self.state, self.t = "hop", 4
            elif r < 0.019:
                self.dir = -self.dir
        if self.x <= lo:
            self.x, self.dir = lo, 1
        elif self.x >= hi - CAT_W:
            self.x, self.dir = hi - CAT_W, -1

    def sprite(self) -> list[str]:
        if self.state == "walk":
            rows = CAT[("walk0", "walk1", "walk2", "walk1")[int(self.x * 2) % 4]]   # legs follow the ground, no sliding
        elif self.state == "hop":
            rows = CAT["walk2"] if self.t > 1 else CAT["walk0"]
        else:
            rows = CAT[self.state]
        tail_up = (self.tick // 6) % 2 if self.state != "sleep" else 0
        out = []
        for i, line in enumerate(rows):
            if "t" in line:
                if tail_up and i > 0:     # the tail flicks up into the row above
                    out[i - 1] = out[i - 1][:line.index("t")] + "▮" + out[i - 1][line.index("t") + 1:]
                    line = line.replace("t", " ")
                else:
                    line = line.replace("t", "━")
            out.append(line.ljust(CAT_W))
        return out if self.dir > 0 else [x[::-1] for x in out]

    def draw(self, cols: int, lo: int, hi: int) -> list[str]:
        g = Grid(cols, STRIP)
        g.put(lo, STRIP - 1, "─" * (hi - lo), DIM)
        y = 0 if self.state == "hop" and 1 <= self.t <= 3 else 1
        eye = "━" if self.state == "sleep" or self.blink else "•"
        for i, line in enumerate(self.sprite()):
            for j, ch in enumerate(line):
                if ch == "e":
                    g.put(int(self.x) + j, y + i, eye, EYE)
                elif ch != " ":
                    g.put(int(self.x) + j, y + i, ch, BODY)
        if self.state == "sleep":
            z = "z" if (self.tick // 8) % 2 else "Z"
            zx = int(self.x) + (CAT_W - 1 if self.dir > 0 else 0) + (1 if self.dir > 0 else -1)
            g.put(zx, (self.tick // 8) % 2, z, DIM)
        return g.render()


def main() -> None:
    out = sys.stdout
    stop = lambda *_: (out.write("\033]R\033[0m\033[2J\033[H\033[?25h"), out.flush(), sys.exit(0))
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    out.write(SET_PALETTE + "\033[0m\033[?25l\033[2J")   # own colours, no cursor
    last, due, cat = None, 0.0, Cat()
    while True:
        cols, lines = shutil.get_terminal_size((120, 33))
        if (cols, lines) != last:
            out.write("\033[2J")
            last, due = (cols, lines), 0.0
        strip = lines - STRIP - 1 if lines >= 26 and cols >= 60 else lines
        buf = ""
        if time.time() >= due:              # the values: every 2 seconds
            try:
                rows = frame(cols, lines)
            except Exception as e:          # never die because one value was odd
                rows = [f"  {e}"]
            rows = (rows + [""] * lines)[:strip]
            buf += "\033[H" + "\n".join(r + "\033[K" for r in rows)
            due = time.time() - time.time() % 2 + 2.05
        if strip < lines:                   # the cat: about 7 times a second
            m = 6 if cols >= 80 else 1
            cat.step(m, cols - m)
            for i, r in enumerate(cat.draw(cols, m, cols - m)):
                buf += f"\033[{strip + i + 1};1H{r}\033[K"
        out.write(buf)
        out.flush()
        time.sleep(0.15)

if __name__ == "__main__":
    main()
