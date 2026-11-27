#!/usr/bin/env python3
"""Status screen for the laptop's own display (tty1).

Started by homeserver-screen.service, switched on and off in the admin
(System). Only reads: the agent's stats, the public status of the site, the
Discord bot units and the Ubuntu update state. Draws a calm page: four big
numbers, a line of status dots, the connection and the uptime, refreshed every 2 seconds.
At the bottom a cat sits next to a command line: typing / lists a few fixed
commands (Minecraft, bots, the cat). It is no shell, nothing else can be run.
"""
import json
import os
import random
import re
import select
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


# ------------------------------ the bottom ------------------------------
# A cat sitting at the right edge (facing left, towards the command line), and
# a line for a few fixed commands. Rows of the bottom strip, top to bottom:
# 0 room for the cat's "z", 1-3 the cat, 2 messages, 3 the prompt, 4 the ground.
CAT = ["      /\\_/\\ ", "  ,__/ o.o )", " (___(_)(_) "]
MIRROR = str.maketrans("/\\()<>,", "\\/)(><,")
CAT_W = 12
STRIP = 5
MC_UNIT = "minecraft.service"

LANG_CMDS = {
    "de": [("/minecraft start", "mc_start", "Minecraft-Server starten"),
           ("/minecraft stop", "mc_stop", "Minecraft-Server stoppen"),
           ("/minecraft neustart", "mc_restart", "Minecraft-Server neu starten"),
           ("/bots neustart", "bots_restart", "alle laufenden Discord-Bots neu starten"),
           ("/katze schlafen", "cat_sleep", "die Katze schlafen legen"),
           ("/katze wach", "cat_wake", "die Katze aufwecken"),
           ("/hilfe", "help", "alle Befehle zeigen")],
    "en": [("/minecraft start", "mc_start", "start the Minecraft server"),
           ("/minecraft stop", "mc_stop", "stop the Minecraft server"),
           ("/minecraft restart", "mc_restart", "restart the Minecraft server"),
           ("/bots restart", "bots_restart", "restart all running Discord bots"),
           ("/cat sleep", "cat_sleep", "put the cat to sleep"),
           ("/cat wake", "cat_wake", "wake the cat up"),
           ("/help", "help", "show all commands")],
}
ALIASES = {name: key for cmds in LANG_CMDS.values() for name, key, _ in cmds}
SAY = {
    "de": {"hint": "/ für Befehle", "unknown": "Unbekannter Befehl. Tippe / für alle.", "failed": "Hat nicht geklappt.",
           "mc_start": "Minecraft startet …", "mc_stop": "Minecraft wird gestoppt …", "mc_restart": "Minecraft startet neu …",
           "bots": "{n} Bots starten neu …", "bot1": "1 Bot startet neu …", "nobots": "Es läuft gerade kein Bot.",
           "cat_sleep": "Pssst, die Katze schläft.", "cat_wake": "Die Katze ist wach."},
    "en": {"hint": "/ for commands", "unknown": "Unknown command. Type / for all of them.", "failed": "That didn't work.",
           "mc_start": "Minecraft is starting …", "mc_stop": "Minecraft is stopping …", "mc_restart": "Minecraft is restarting …",
           "bots": "{n} bots are restarting …", "bot1": "1 bot is restarting …", "nobots": "No bot is running right now.",
           "cat_sleep": "Shh, the cat is asleep.", "cat_wake": "The cat is awake."},
}


def lang() -> str:
    return "de" if T is TEXT["de"] else "en"


def ctl(*args: str) -> bool:
    try:
        return subprocess.run(["systemctl", *args], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


class Cat:
    """Sits, blinks, flicks its tail, twitches an ear, and now and then naps."""

    def __init__(self):
        self.state, self.t, self.tick = "sit", random.randint(150, 400), 0
        self.blink = self.tail = self.ear = 0

    def sleep(self, ticks: int) -> None:
        self.state, self.t = "sleep", ticks

    def wake(self) -> None:
        self.state, self.t = "sit", random.randint(150, 400)

    def step(self) -> None:
        self.tick += 1
        self.t -= 1
        r = random.random()
        self.blink, self.tail, self.ear = max(0, self.blink - 1), max(0, self.tail - 1), max(0, self.ear - 1)
        if self.state == "sit":
            if r < 0.025:
                self.blink = 2
            elif r < 0.045:
                self.tail = 5
            elif r < 0.052:
                self.ear = 2
            if self.t <= 0:
                self.sleep(random.randint(200, 600)) if random.random() < 0.35 else self.wake()
        elif self.t <= 0:
            self.wake()
            self.blink = 3      # still a bit sleepy

    def draw(self, g: "Grid", x: int) -> None:
        rows = list(CAT)
        if self.state == "sleep" or self.blink:
            rows[1] = rows[1].replace("o.o", "-.-")
        if self.tail or self.state == "sleep" and (self.tick // 10) % 4 == 0:
            rows[1] = rows[1].replace(",", "~")
        if self.ear:
            rows[0] = rows[0][:-2] + "| "
        for i, line in enumerate(r.ljust(CAT_W)[::-1].translate(MIRROR) for r in rows):    # facing left
            for j, ch in enumerate(line):
                if ch != " ":
                    g.put(x + j, 1 + i, ch, "37")
        if self.state == "sleep":
            phase = (self.tick // 6) % 3
            if phase == 1:
                g.put(x - 1, 1, "z", DIM)
            elif phase == 2:
                g.put(x - 2, 0, "Z", DIM)


class Prompt:
    """The command line: typing, the / list, running a command, a short answer."""

    def __init__(self, cat: Cat):
        self.cat, self.text, self.sel, self.msg, self.msg_colour, self.msg_until = cat, "", 0, "", FG, 0.0
        self.esc = ""

    def suggestions(self) -> list[tuple[str, str, str]]:
        if not self.text.startswith("/"):
            return []
        q = self.text.lower()
        return [c for c in LANG_CMDS[lang()] if c[0].startswith(q) and c[0] != q] or \
            ([] if q in ALIASES else [c for c in LANG_CMDS[lang()] if any(w.startswith(q[1:]) for w in c[0][1:].split())])

    def say(self, text: str, colour: str = FG) -> None:
        self.msg, self.msg_colour, self.msg_until = text, colour, time.time() + 8

    def feed(self, data: str) -> None:
        data = self.esc + data
        self.esc = ""
        i = 0
        while i < len(data):
            ch = data[i]
            if ch == "\x1b":
                seq = data[i:i + 3]
                if len(seq) < 3 and i + len(seq) == len(data) and seq != "\x1b":
                    self.esc = seq          # rest of an arrow key comes with the next read
                    return
                if seq in ("\x1b[A", "\x1b[B"):
                    n = len(self.suggestions())
                    if n:
                        self.sel = (self.sel + (1 if seq == "\x1b[B" else -1)) % n
                    i += 3
                    continue
                if seq.startswith("\x1b[") and len(seq) == 3:
                    i += 3              # other keys: ignored
                    continue
                self.text, self.sel = "", 0     # Esc alone: clear
            elif ch in ("\x7f", "\x08"):
                self.text, self.sel = self.text[:-1], 0
            elif ch in ("\r", "\n"):
                self.enter()
            elif ch == "\t":
                sug = self.suggestions()
                if sug:
                    self.text, self.sel = sug[min(self.sel, len(sug) - 1)][0], 0
            elif ch in ("\x03", "\x15"):  # Ctrl+C, Ctrl+U
                self.text, self.sel = "", 0
            elif ch >= " " and len(self.text) < 40:
                if not self.text and ch != "/":
                    self.text = "/"
                self.text, self.sel = self.text + ch, 0
            i += 1

    def enter(self) -> None:
        q = " ".join(self.text.lower().split())
        if not q:
            return
        if q in ALIASES:
            self.text, self.sel = "", 0
            self.run(ALIASES[q])
            return
        sug = self.suggestions()
        if sug:                          # first fill in the picked command, Enter again runs it
            self.text, self.sel = sug[min(self.sel, len(sug) - 1)][0], 0
        else:
            self.text, self.sel = "", 0
            self.say(SAY[lang()]["unknown"], WARN)

    def run(self, key: str) -> None:
        say = SAY[lang()]
        if key == "help":
            self.text = "/"
        elif key.startswith("mc_"):
            ctl("reset-failed", MC_UNIT)
            verb = {"mc_start": "start", "mc_stop": "stop", "mc_restart": "restart"}[key]
            ok = ctl(verb, "--no-block", MC_UNIT)
            self.say(say[key] if ok else say["failed"], OK if ok else BAD)
        elif key == "bots_restart":
            units = []
            try:
                out = subprocess.run(["systemctl", "list-units", "--all", "--plain", "--no-legend", "discord-bot@*"],
                                     capture_output=True, text=True, timeout=10).stdout
                units = [l.split()[0] for l in out.splitlines() if len(l.split()) > 2 and l.split()[2] in ("active", "failed")]
            except (OSError, subprocess.SubprocessError):
                pass
            if not units:
                self.say(say["nobots"], DIM)
                return
            ok = all([ctl("reset-failed", u) or True for u in units]) and all([ctl("restart", "--no-block", u) for u in units])
            self.say((say["bot1"] if len(units) == 1 else say["bots"].format(n=len(units))) if ok else say["failed"], OK if ok else BAD)
        elif key == "cat_sleep":
            self.cat.sleep(random.randint(400, 800))
            self.say(say["cat_sleep"], DIM)
        elif key == "cat_wake":
            self.cat.wake()
            self.say(say["cat_wake"], DIM)

    def draw(self, g: "Grid", m: int, width: int) -> None:
        if self.msg and time.time() < self.msg_until:
            g.put(m, 2, self.msg[:width], self.msg_colour)
        x = g.put(m, 3, "› ", ACC)
        if self.text:
            x = g.put(x, 3, self.text[-(width - 4):], FG)
        if int(time.time() * 2) % 2 == 0:
            g.put(x, 3, "_", ACC)
        if not self.text:
            g.put(x + 2, 3, SAY[lang()]["hint"], DIM)

    def popup(self, m: int, room: int) -> list:
        """The / list as painters, bottom entry last; it ends right above the prompt."""
        sug = self.suggestions()
        width = max((len(c[0]) for c in sug), default=0) + 3
        sel = min(self.sel, len(sug) - 1)

        def painter(i, name, desc):
            def paint(g, y):
                on = i == sel
                g.put(m, y, ("› " if on else "  ") + name, ACC if on else FG)
                g.put(m + 2 + width, y, desc, FG if on else DIM)
            return paint
        return [painter(i, name, desc) for i, (name, _, desc) in enumerate(sug)][-room:]


def bottom(cols: int, cat: Cat, prompt: Prompt, pop: list) -> list[str]:
    m = 6 if cols >= 80 else 1
    g = Grid(cols, STRIP)
    cx = cols - m - CAT_W
    g.put(m, STRIP - 1, "─" * (cols - 2 * m), DIM)
    prompt.draw(g, m, cx - m - 2)
    for i, paint in enumerate(pop[-3:][::-1]):   # the last three entries sit in rows 2, 1, 0
        g.put(0, 2 - i, " " * (cx - 1))
        paint(g, 2 - i)
    cat.draw(g, cx)
    return g.render()


def main() -> None:
    out = sys.stdout
    fd, saved = None, None
    try:                                # keys one by one, no echo, Ctrl+C doesn't stop the screen
        import termios
        fd = sys.stdin.fileno()
        saved = termios.tcgetattr(fd)
        mode = termios.tcgetattr(fd)
        mode[3] &= ~(termios.ICANON | termios.ECHO | termios.ISIG)
        termios.tcsetattr(fd, termios.TCSANOW, mode)
    except Exception:
        fd = None

    def stop(*_):
        if saved is not None:
            termios.tcsetattr(fd, termios.TCSANOW, saved)
        out.write("\033]R\033[0m\033[2J\033[H\033[?25h")
        out.flush()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    out.write(SET_PALETTE + "\033[0m\033[?25l\033[2J")   # own colours, no cursor
    last, due, tick, shown = None, 0.0, 0.0, 0
    cat = Cat()
    prompt = Prompt(cat)
    while True:
        cols, lines = shutil.get_terminal_size((120, 33))
        if (cols, lines) != last:
            out.write("\033[2J")
            last, due = (cols, lines), 0.0
        strip = lines - STRIP - 1 if lines >= 26 and cols >= 60 else lines
        pop = prompt.popup(6 if cols >= 80 else 1, strip - 1) if strip < lines else []
        extra = pop[:-3]                # entries that don't fit into the strip go above it
        if len(extra) < shown:
            due = 0.0                   # the list got shorter: bring the values back underneath
        buf = ""
        if time.time() >= due:          # the values: every 2 seconds
            try:
                rows = frame(cols, lines)
            except Exception as e:      # never die because one value was odd
                rows = [f"  {e}"]
            rows = (rows + [""] * lines)[:strip]
            buf += "\033[H" + "\n".join(r + "\033[K" for r in rows)
            due = time.time() - time.time() % 2 + 2.05
        if strip < lines:
            if time.time() >= tick:     # the cat: about 7 steps a second
                cat.step()
                tick = time.time() + 0.15
            for i, r in enumerate(bottom(cols, cat, prompt, pop)):
                buf += f"\033[{strip + i + 1};1H{r}\033[K"
            for i, paint in enumerate(extra):
                g = Grid(cols, 1)
                paint(g, 0)
                buf += f"\033[{strip - len(extra) + i + 1};1H{g.render()[0]}\033[K"
            shown = len(extra)
        out.write(buf)
        out.flush()
        if fd is not None:
            try:
                ready = select.select([fd], [], [], 0.15)[0]
            except (OSError, ValueError):
                ready = []
            if ready:
                data = os.read(fd, 64)
                if data:
                    prompt.feed(data.decode("utf-8", "ignore"))
                else:
                    time.sleep(0.15)    # the terminal went away; keep drawing
        else:
            time.sleep(0.15)

if __name__ == "__main__":
    main()
