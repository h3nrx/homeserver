#!/usr/bin/env python3
"""
panel-agent: runs as root on the host and does what the sandboxed admin
panel container may not: collect system stats and control the systemd units
for Minecraft and the Discord bots.

IPC lives in /var/lib/panel:
  commands/<id>.json   request written by the panel
  responses/<id>.json  answer read by the panel
  stats.json           refreshed every 2 seconds, with ~3 minutes of history

Every name that reaches systemctl or the filesystem is validated first.
Long-running systemd jobs are queued with --no-block so the agent never
stalls; the panel polls for the resulting state instead.
"""
from __future__ import annotations

import collections
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

STATE = Path("/var/lib/panel")
CMD_DIR = STATE / "commands"
RESP_DIR = STATE / "responses"
STATS_FILE = STATE / "stats.json"
POWER_FILE = STATE / "power.json"
UPDATE_FILE = STATE / "update.json"
UPDATE_SCRIPT = Path("/usr/local/lib/homeserver/update.sh")
UPDATE_UNIT = "homeserver-update"
TAG = re.compile(r"^v\d{1,3}\.\d{1,3}\.\d{1,3}$")

BOTS_DIR = Path("__HOME__/bots")
OWNER_UID = 1000
OWNER_GID = 1000

MC_UNIT = "minecraft.service"
MC_CONTAINER = "minecraft"

BOT_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")
SEGMENT = r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}"
MAIN_PATH = re.compile(rf"^({SEGMENT}/){{0,5}}{SEGMENT}\.py$")

STATS_INTERVAL = 2.0
HISTORY_LEN = 90
RESPONSE_TTL = 120
LOG_TRIM_AT = 4 * 1024 * 1024
LOG_KEEP = 1024 * 1024

TEMPLATE_BOT = '''import os

import discord

intents = discord.Intents.default()
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    print(f"Logged in as {client.user} ({client.user.id})")


client.run(os.environ["DISCORD_TOKEN"])
'''


# ------------------------------- helpers --------------------------------
def log(msg: str) -> None:
    print(f"[panel-agent] {msg}", flush=True)


def run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def systemctl(*args: str, timeout: int = 30) -> subprocess.CompletedProcess:
    return run(["systemctl", *args], timeout=timeout)


def check(result: subprocess.CompletedProcess, what: str) -> None:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip().splitlines()
        raise RuntimeError(f"{what} failed: {detail[-1] if detail else 'unknown error'}")


def atomic_write(path: Path, data: str, mode: int = 0o644) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(data, encoding="utf-8")
    os.chmod(tmp, mode)
    tmp.replace(path)


def chown(path: Path) -> None:
    try:
        os.chown(path, OWNER_UID, OWNER_GID)
    except OSError:
        pass


# -------------------------------- stats ---------------------------------
_prev_cpu: dict | None = None
_prev_net: dict | None = None
history = {k: collections.deque(maxlen=HISTORY_LEN) for k in ("cpu", "mem", "rx", "tx")}


def read_cpu() -> dict:
    global _prev_cpu
    with open("/proc/stat") as f:
        rows = [l.split() for l in f if l.startswith("cpu")]
    cur = {r[0]: [int(x) for x in r[1:]] for r in rows}
    prev, _prev_cpu = _prev_cpu, cur

    def usage(name: str) -> float:
        if not prev or name not in prev:
            return 0.0
        a, b = prev[name], cur[name]
        idle = (b[3] + b[4]) - (a[3] + a[4])
        total = sum(b) - sum(a)
        return 0.0 if total <= 0 else max(0.0, min(100.0, (1 - idle / total) * 100))

    cores = sorted((n for n in cur if n != "cpu"), key=lambda n: int(n[3:]))
    return {"total": round(usage("cpu"), 1), "cores": [round(usage(n), 1) for n in cores]}


def read_cpu_meta() -> dict:
    model, mhz = "", 0.0
    try:
        with open("/proc/cpuinfo") as f:
            for line in f:
                if line.startswith("model name") and not model:
                    model = line.split(":", 1)[1].strip()
                elif line.startswith("cpu MHz"):
                    try:
                        mhz = max(mhz, float(line.split(":", 1)[1]))
                    except ValueError:
                        pass
    except OSError:
        pass
    return {"model": model, "mhz": round(mhz)}


def read_memory() -> dict:
    info = {}
    with open("/proc/meminfo") as f:
        for line in f:
            key, _, rest = line.partition(":")
            try:
                info[key.strip()] = int(rest.split()[0]) * 1024
            except (IndexError, ValueError):
                pass
    total, avail = info.get("MemTotal", 0), info.get("MemAvailable", 0)
    swap_total, swap_free = info.get("SwapTotal", 0), info.get("SwapFree", 0)
    return {
        "mem_total": total,
        "mem_used": max(0, total - avail),
        "mem_free": avail,
        "swap_total": swap_total,
        "swap_used": max(0, swap_total - swap_free),
    }


def read_disks() -> list[dict]:
    seen, out = set(), []
    for mount in ("/", "/home", "/boot"):
        try:
            st = os.statvfs(mount)
        except OSError:
            continue
        key = (st.f_blocks, st.f_fsid)
        total = st.f_blocks * st.f_frsize
        if key in seen or not total:
            continue
        seen.add(key)
        free = st.f_bavail * st.f_frsize
        out.append({"mount": mount, "total": total, "used": total - free, "free": free})
    return out


def read_net() -> dict:
    global _prev_net
    now, data = time.time(), {}
    with open("/proc/net/dev") as f:
        for line in list(f)[2:]:
            name, _, rest = line.partition(":")
            name = name.strip()
            if name == "lo" or name.startswith(("docker", "br-", "veth", "tailscale")):
                continue
            parts = rest.split()
            data[name] = (int(parts[0]), int(parts[8]))
    prev, _prev_net = _prev_net, (now, data)
    ifaces, rx_rate, tx_rate = [], 0.0, 0.0
    for name, (rx, tx) in data.items():
        r = t = 0.0
        if prev and name in prev[1]:
            dt = max(0.001, now - prev[0])
            r = max(0, rx - prev[1][name][0]) / dt
            t = max(0, tx - prev[1][name][1]) / dt
        ifaces.append({"name": name, "rx": rx, "tx": tx, "rx_rate": round(r), "tx_rate": round(t)})
        rx_rate += r
        tx_rate += t
    return {"interfaces": ifaces, "rx_rate": round(rx_rate), "tx_rate": round(tx_rate)}


def read_temp() -> float | None:
    """CPU package temperature; falls back to the hottest thermal zone."""
    for hw in Path("/sys/class/hwmon").glob("hwmon*"):
        try:
            if (hw / "name").read_text().strip() != "coretemp":
                continue
            for label in hw.glob("temp*_label"):
                if label.read_text().strip().startswith("Package"):
                    return round(int((hw / label.name.replace("label", "input")).read_text()) / 1000, 1)
        except (OSError, ValueError):
            continue
    best = None
    for zone in Path("/sys/class/thermal").glob("thermal_zone*"):
        try:
            if (zone / "type").read_text().strip() == "x86_pkg_temp":
                return round(int((zone / "temp").read_text()) / 1000, 1)
            t = int((zone / "temp").read_text()) / 1000
            best = t if best is None or t > best else best
        except (OSError, ValueError):
            continue
    return round(best, 1) if best is not None else None


def gather_stats() -> dict:
    with open("/proc/uptime") as f:
        uptime = int(float(f.read().split()[0]))
    with open("/proc/loadavg") as f:
        load = [float(x) for x in f.read().split()[:3]]
    cpu = {**read_cpu(), **read_cpu_meta()}
    mem = read_memory()
    net = read_net()
    history["cpu"].append(cpu["total"])
    history["mem"].append(round(mem["mem_used"] / mem["mem_total"] * 100, 1) if mem["mem_total"] else 0)
    history["rx"].append(net["rx_rate"])
    history["tx"].append(net["tx_rate"])
    return {
        "ts": int(time.time() * 1000),
        "host": socket.gethostname(),
        "uptime": uptime,
        "load": load,
        "cpu": cpu,
        "temp_c": read_temp(),
        **mem,
        "disks": read_disks(),
        "net": net,
        "history": {k: list(v) for k, v in history.items()},
    }


# ------------------------------ minecraft -------------------------------
def unit_state(unit: str) -> str:
    return systemctl("is-active", unit).stdout.strip() or "unknown"


def mc_status(_p=None) -> dict:
    r = run(["docker", "inspect", "-f", "{{.State.Status}}", MC_CONTAINER], timeout=10)
    container = r.stdout.strip() if r.returncode == 0 else "missing"
    return {"unit": unit_state(MC_UNIT), "container": container}


def mc_start(_p=None) -> dict:
    if unit_state(MC_UNIT) == "active":
        # Unit is up but the container stopped (e.g. /stop in game): recreate it.
        check(systemctl("restart", "--no-block", MC_UNIT), "Starting the server")
    else:
        systemctl("reset-failed", MC_UNIT)
        check(systemctl("start", "--no-block", MC_UNIT), "Starting the server")
    return mc_status()


def mc_stop(_p=None) -> dict:
    check(systemctl("stop", "--no-block", MC_UNIT), "Stopping the server")
    return mc_status()


def mc_restart(_p=None) -> dict:
    systemctl("reset-failed", MC_UNIT)
    check(systemctl("restart", "--no-block", MC_UNIT), "Restarting the server")
    return mc_status()


# --------------------------------- power --------------------------------
# Shutdown/restart of the whole laptop. Minecraft is stopped first and the
# agent waits until the world is saved; only then is the power job queued.
# Progress goes to power.json so the panel can show it while it happens.
POWER_MODES = {"poweroff", "reboot"}
_power_lock = threading.Lock()


def write_power(phase: str, mode: str, **extra) -> None:
    atomic_write(POWER_FILE, json.dumps({"phase": phase, "mode": mode, "since": int(time.time() * 1000), **extra}))


def _power_job(mode: str) -> None:
    try:
        stopped_mc = False
        if unit_state(MC_UNIT) in ("active", "activating", "deactivating", "reloading"):
            write_power("stopping-minecraft", mode)
            log(f"power: stopping minecraft before {mode}")
            r = systemctl("stop", MC_UNIT, timeout=200)
            if r.returncode != 0:
                log(f"power: stopping minecraft returned {r.returncode}: {(r.stderr or '').strip()}")
            stopped_mc = True
        # Belt and braces: never leave a running world to a hard kill.
        r = run(["docker", "inspect", "-f", "{{.State.Status}}", MC_CONTAINER], timeout=10)
        if r.returncode == 0 and r.stdout.strip() == "running":
            write_power("stopping-minecraft", mode)
            log("power: minecraft container still running, stopping it")
            run(["docker", "stop", "-t", "90", MC_CONTAINER], timeout=120)
            stopped_mc = True
        write_power("shutting-down" if mode == "poweroff" else "restarting", mode, stopped_minecraft=stopped_mc)
        log(f"power: {mode}")
        time.sleep(1.5)  # give the panel a moment to show the last step
        check(systemctl(mode, "--no-block"), "Shutting down" if mode == "poweroff" else "Restarting")
    except Exception as e:
        log(f"power: {mode} failed: {e}")
        write_power("failed", mode, error=str(e))
        _power_lock.release()


def power(p) -> dict:
    mode = p.get("mode")
    if mode not in POWER_MODES:
        raise ValueError("Unknown power action.")
    if not _power_lock.acquire(blocking=False):
        raise ValueError("The laptop is already shutting down or restarting.")
    write_power("starting", mode)
    threading.Thread(target=_power_job, args=(mode,), daemon=True).start()
    return {"phase": "starting", "mode": mode}


# --------------------------------- bots ---------------------------------
def bot_dir(bot_id) -> Path:
    if not isinstance(bot_id, str) or not BOT_ID.match(bot_id):
        raise ValueError("Invalid bot id.")
    return BOTS_DIR / bot_id


def bot_unit(bot_id: str) -> str:
    return f"discord-bot@{bot_id}.service"


def read_meta(d: Path) -> dict:
    try:
        return json.loads((d / "meta.json").read_text())
    except Exception:
        return {}


def write_meta(d: Path, meta: dict) -> None:
    tmp = d / "meta.json.tmp"
    tmp.write_text(json.dumps(meta))
    chown(tmp)
    tmp.replace(d / "meta.json")


def unit_states(units: list[str]) -> list[tuple[str, bool]]:
    if not units:
        return []
    r = systemctl("show", "--property=ActiveState,UnitFileState", "--", *units, timeout=15)
    blocks = r.stdout.strip("\n").split("\n\n") if r.stdout.strip() else []
    out = []
    for i in range(len(units)):
        props = {}
        if i < len(blocks):
            for line in blocks[i].splitlines():
                k, _, v = line.partition("=")
                props[k] = v
        out.append((props.get("ActiveState", "unknown"), props.get("UnitFileState") == "enabled"))
    return out


def describe(bot_id: str, state: str, enabled: bool) -> dict:
    d = bot_dir(bot_id)
    meta = read_meta(d)
    main = meta.get("main") or "bot.py"
    return {
        "id": bot_id,
        "name": meta.get("name") or bot_id,
        "main": main,
        "main_exists": (d / main).is_file(),
        "created": meta.get("created"),
        "state": state,
        "enabled": enabled,
    }


def bot_info(p) -> dict:
    d = bot_dir(p.get("id"))
    if not d.is_dir():
        raise ValueError("Bot not found.")
    (state, enabled), = unit_states([bot_unit(d.name)])
    return describe(d.name, state, enabled)


def bot_list(_p=None) -> dict:
    if not BOTS_DIR.is_dir():
        return {"bots": []}
    ids = sorted(d.name for d in BOTS_DIR.iterdir() if d.is_dir() and BOT_ID.match(d.name))
    states = unit_states([bot_unit(i) for i in ids])
    return {"bots": [describe(i, s, e) for i, (s, e) in zip(ids, states)]}


def bot_create(p) -> dict:
    d = bot_dir(p.get("id"))
    if d.exists():
        raise ValueError("A bot with this id already exists.")
    d.mkdir(parents=True, mode=0o750)
    chown(d)
    files = {
        "bot.py": (TEMPLATE_BOT, 0o644),
        "requirements.txt": ("discord.py>=2.4\n", 0o644),
        ".env": ('# Managed by the __BRAND__ panel. Restart the bot after changes.\nDISCORD_TOKEN=""\n', 0o600),
    }
    for name, (content, mode) in files.items():
        path = d / name
        path.write_text(content, encoding="utf-8")
        os.chmod(path, mode)
        chown(path)
    name = str(p.get("name") or d.name)[:64]
    write_meta(d, {"name": name, "main": "bot.py", "created": int(time.time())})
    return bot_info({"id": d.name})


def bot_set_meta(p) -> dict:
    d = bot_dir(p.get("id"))
    if not d.is_dir():
        raise ValueError("Bot not found.")
    meta = read_meta(d)
    if "name" in p:
        name = re.sub(r"[\x00-\x1f\x7f]", "", str(p["name"])).strip()[:64]
        meta["name"] = name or d.name
    if "main" in p:
        main = str(p["main"])
        if not MAIN_PATH.match(main):
            raise ValueError("The main file must be a .py file in the bot folder.")
        meta["main"] = main
    write_meta(d, meta)
    return bot_info(p)


def trim_log(path: Path, limit: int = LOG_TRIM_AT) -> None:
    """Shrink a log in place so systemd's append handle keeps working."""
    try:
        if path.stat().st_size <= limit:
            return
        with open(path, "r+b") as f:
            f.seek(-LOG_KEEP, os.SEEK_END)
            tail = f.read()
            cut = tail.find(b"\n")
            tail = tail[cut + 1:] if cut >= 0 else tail
            f.seek(0)
            f.write(b"[panel] Older output was trimmed.\n" + tail)
            f.truncate()
    except OSError:
        pass


def bot_start(p) -> dict:
    d = bot_dir(p.get("id"))
    if not d.is_dir():
        raise ValueError("Bot not found.")
    unit = bot_unit(d.name)
    trim_log(d / "bot.log", 2 * 1024 * 1024)
    systemctl("reset-failed", unit)
    check(systemctl("enable", unit), "Enabling the bot")
    check(systemctl("start", "--no-block", unit), "Starting the bot")
    return bot_info(p)


def bot_stop(p) -> dict:
    d = bot_dir(p.get("id"))
    unit = bot_unit(d.name)
    check(systemctl("disable", unit), "Disabling the bot")
    check(systemctl("stop", "--no-block", unit), "Stopping the bot")
    return bot_info(p)


def bot_restart(p) -> dict:
    d = bot_dir(p.get("id"))
    if not d.is_dir():
        raise ValueError("Bot not found.")
    unit = bot_unit(d.name)
    trim_log(d / "bot.log", 2 * 1024 * 1024)
    systemctl("reset-failed", unit)
    check(systemctl("enable", unit), "Enabling the bot")
    check(systemctl("restart", "--no-block", unit), "Restarting the bot")
    return bot_info(p)


def bot_delete(p) -> dict:
    d = bot_dir(p.get("id"))
    unit = bot_unit(d.name)
    systemctl("disable", unit)
    systemctl("stop", unit, timeout=30)
    systemctl("reset-failed", unit)
    if d.exists():
        shutil.rmtree(d)
    return {"ok": True}


def bot_logs(p) -> dict:
    d = bot_dir(p.get("id"))
    lines = max(10, min(1000, int(p.get("lines", 400))))
    path = d / "bot.log"
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            if size > 256 * 1024:
                f.seek(-256 * 1024, os.SEEK_END)
            text = f.read().decode("utf-8", errors="replace")
    except OSError:
        return {"lines": []}
    out = [l for l in text.splitlines() if l.strip()]
    return {"lines": out[-lines:]}


def bot_clear_logs(p) -> dict:
    d = bot_dir(p.get("id"))
    path = d / "bot.log"
    if path.exists():
        with open(path, "r+b") as f:
            f.truncate(0)
    return {"ok": True}


# -------------------------------- updates --------------------------------
def update_install(p) -> dict:
    """Start update.sh as its own transient unit, so it survives the
    restart of this agent (which happens near the end of an update)."""
    tag = p.get("tag")
    if not isinstance(tag, str) or not TAG.match(tag):
        raise ValueError("Invalid version.")
    if not UPDATE_SCRIPT.is_file():
        raise ValueError("The updater isn't installed on this server yet.")
    if sys_status().get("state") in ("starting", "running"):
        raise ValueError("Ubuntu is being updated right now. Try again in a few minutes.")
    if unit_state(UPDATE_UNIT + ".service") in ("active", "activating", "deactivating"):
        raise ValueError("An update is already running.")
    systemctl("reset-failed", UPDATE_UNIT + ".service")
    atomic_write(UPDATE_FILE, json.dumps({"state": "starting", "step": "start", "to": tag, "since": int(time.time() * 1000)}))
    check(run(["systemd-run", f"--unit={UPDATE_UNIT}", "--collect", "--quiet",
               "--property=TimeoutStopSec=120", "/bin/bash", str(UPDATE_SCRIPT), tag], timeout=20), "Starting the update")
    log(f"update: started {tag}")
    return {"state": "starting", "to": tag}


# Updates may come from a private repo. The read-only key is only readable by
# root and is never handed back to the panel.
UPDATE_REPO = "__REPO__"
UPDATE_KEY = Path("/var/lib/homeserver/github.token")
UPDATE_KEY_RE = re.compile(r"github_pat_[A-Za-z0-9_]{20,240}")


def read_update_key() -> str:
    try:
        key = UPDATE_KEY.read_text(encoding="ascii").strip()
    except (OSError, ValueError):
        return ""
    return key if UPDATE_KEY_RE.fullmatch(key) else ""


def github_latest(key: str) -> tuple[int, dict | None]:
    import urllib.error
    import urllib.request

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):   # would send the key along
            return None

    headers = {"User-Agent": "homeserver-updater", "Accept": "application/vnd.github+json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases/latest", headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=12) as r:
            d = json.loads(r.read(2_000_000))
    except urllib.error.HTTPError as e:
        return e.code, None
    except (OSError, ValueError):
        return 0, None
    rel = {k: d.get(k) for k in ("tag_name", "name", "body", "published_at", "html_url")}
    # the releases before it too, so a server that skipped some sees everything that's new at once
    req = urllib.request.Request(f"https://api.github.com/repos/{UPDATE_REPO}/releases?per_page=40", headers=headers)
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=12) as r:
            rel["history"] = [{k: x.get(k) for k in ("tag_name", "name", "body")}
                              for x in json.loads(r.read(4_000_000)) if isinstance(x, dict) and not x.get("draft")]
    except (OSError, ValueError):
        rel["history"] = []
    return 200, rel


def update_release(p) -> dict:
    key = read_update_key()
    status, rel = github_latest(key)
    return {"status": status, "key": bool(key), "release": rel}


def update_key(p) -> dict:
    key = p.get("key")
    if key == "":
        UPDATE_KEY.unlink(missing_ok=True)
        log("update: access key removed")
        return {"key": False}
    if not isinstance(key, str) or not UPDATE_KEY_RE.fullmatch(key):
        raise ValueError("That isn't a fine-grained GitHub key (it starts with github_pat_).")
    status, _ = github_latest(key)
    if status == 0:
        raise ValueError("GitHub didn't answer in time.")
    if status != 200:
        raise ValueError("GitHub doesn't accept this key for the repository. Check the repository and the permission Contents: Read-only.")
    UPDATE_KEY.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
    tmp = UPDATE_KEY.with_name("github.token.tmp")
    tmp.unlink(missing_ok=True)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(key + "\n")
    tmp.replace(UPDATE_KEY)
    log("update: access key saved")
    return {"key": True}


# ---- Ubuntu updates: only what "apt upgrade" would do, nothing is removed ----
SYS_FILE = STATE / "sysupdate.json"
SYS_UNIT = "homeserver-sysupdate"
SYS_LOG = "/var/log/homeserver-sysupdate.log"
SYS_INST = re.compile(r"^Inst (\S+) (?:\[([^\]]*)\] )?\((\S+) ([^\[)]*)")
SYS_SCRIPT = r"""
S=/var/lib/panel/sysupdate.json
say() { printf '{"state":"%s","step":"%s","since":%s,"error":"%s"}' "$1" "$2" "$SINCE" "$3" > "$S.tmp" && chmod 644 "$S.tmp" && mv "$S.tmp" "$S"; }
export LC_ALL=C DEBIAN_FRONTEND=noninteractive NEEDRESTART_SUSPEND=1 APT_LISTCHANGES_FRONTEND=none
exec > "$LOG" 2>&1
A="apt-get -y -o DPkg::Lock::Timeout=900 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold"
say running list ""
$A update || { say failed list "Couldn't load the package list."; exit 1; }
say running install ""
$A --with-new-pkgs upgrade || { dpkg --configure -a --force-confdef --force-confold; say failed install "apt reported an error."; exit 1; }
apt-get clean
say ok done ""
"""


def sys_status() -> dict:
    try:
        st = json.loads(SYS_FILE.read_text())
    except (OSError, ValueError):
        return {"state": "idle"}
    if st.get("state") in ("starting", "running") and unit_state(SYS_UNIT + ".service") not in ("active", "activating"):
        try:                                    # it may have finished just now
            st = json.loads(SYS_FILE.read_text())
        except (OSError, ValueError):
            pass
        if st.get("state") in ("starting", "running"):
            st.update(state="failed", error="It was interrupted.")
    return st


def sys_updates(p) -> dict:
    if not shutil.which("apt-get"):
        return {"supported": False}
    out = {"supported": True, "status": sys_status(), "error": ""}
    if out["status"].get("state") in ("starting", "running"):
        return out
    env = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}
    if p.get("refresh"):
        r = subprocess.run(["systemd-run", "--wait", "--pipe", "--quiet", "--collect", "/usr/bin/apt-get", "-o", "DPkg::Lock::Timeout=60", "-o", "APT::Update::Error-Mode=any", "update"],
                           capture_output=True, text=True, timeout=100, env=env)
        if r.returncode != 0:
            out["error"] = "Couldn't load the package list."
    # Run outside this service's own mount namespace (PrivateTmp): in there apt
    # thinks it is in a chroot and ignores phased updates, which the real
    # install then skips, so they would show up forever.
    r = subprocess.run(["systemd-run", "--wait", "--pipe", "--quiet", "--collect", "--setenv=LC_ALL=C", "/usr/bin/apt-get", "-s", "-o", "Debug::NoLocking=1", "--with-new-pkgs", "upgrade"],
                       capture_output=True, text=True, timeout=90, env=env)
    if r.returncode != 0:
        out["error"] = "apt reported an error."
    lines, held, skip = r.stdout.splitlines(), set(), False
    for line in lines:                  # names under "...deferred due to phasing:"
        if line.endswith(":"):
            skip = "phasing" in line
        elif skip and line.startswith(" "):
            held.update(line.split())
        else:
            skip = False
    found = [m for m in map(SYS_INST.match, lines) if m and m[1] not in held]
    out["packages"] = [{"name": m[1], "version": m[3], "security": "-security" in m[4]} for m in found][:400]
    out["reboot"] = Path("/var/run/reboot-required").exists()
    try:
        out["checked"] = int(max(Path(x).stat().st_mtime for x in ("/var/lib/apt/lists", "/var/lib/apt/periodic/update-success-stamp") if Path(x).exists()) * 1000)
    except ValueError:
        out["checked"] = None
    return out


def sys_install(_p=None) -> dict:
    if unit_state(UPDATE_UNIT + ".service") in ("active", "activating", "deactivating") or sys_status().get("state") in ("starting", "running"):
        raise ValueError("An update is already running.")
    names = {x["name"] for x in sys_updates({}).get("packages", [])}
    if not names:
        raise ValueError("Everything is already up to date.")
    # without live-restore a Docker update stops every container
    if names & {"docker-ce", "docker.io", "containerd.io", "containerd"} and unit_state(MC_UNIT) == "active" \
            and run(["docker", "info", "--format", "{{.LiveRestoreEnabled}}"]).stdout.strip() != "true":
        raise ValueError("Stop Minecraft first. Docker restarts with this update.")
    systemctl("reset-failed", SYS_UNIT + ".service")
    since = int(time.time() * 1000)
    atomic_write(SYS_FILE, json.dumps({"state": "starting", "step": "list", "since": since, "error": ""}))
    check(run(["systemd-run", f"--unit={SYS_UNIT}", "--collect", "--quiet", "--property=TimeoutStopSec=1800", f"--setenv=SINCE={since}", f"--setenv=LOG={SYS_LOG}",
               "/bin/bash", "-c", SYS_SCRIPT.replace("$", "$$")], timeout=20), "Starting the update")
    log(f"ubuntu update: {len(names)} packages")
    return sys_status()


# ---- status screen on the laptop's own display (tty1) ----
SCREEN_UNIT = "homeserver-screen.service"
SCREEN_DESIGN = Path("/var/lib/homeserver-screen/design")   # read by screen.py, also set there with /design
SCREEN_DESIGNS = ("numbers", "name")


def screen_get(_p=None) -> dict:
    ok = Path("/dev/tty1").exists() and Path(f"/etc/systemd/system/{SCREEN_UNIT}").exists()
    try:
        design = SCREEN_DESIGN.read_text().strip()
    except OSError:
        design = ""
    return {"supported": ok, "on": ok and systemctl("is-enabled", SCREEN_UNIT).stdout.strip() == "enabled",
            "running": ok and unit_state(SCREEN_UNIT) == "active",
            "design": design if design in SCREEN_DESIGNS else "numbers"}


def screen_set(p) -> dict:
    if not screen_get()["supported"]:
        raise ValueError("This server has no screen of its own.")
    if "design" in p:                   # only the look changes; the screen picks it up within 2 seconds
        if p["design"] not in SCREEN_DESIGNS:
            raise ValueError("Unknown design.")
        SCREEN_DESIGN.parent.mkdir(parents=True, exist_ok=True)
        tmp = SCREEN_DESIGN.with_name(".design.tmp")
        tmp.write_text(p["design"] + "\n")
        tmp.rename(SCREEN_DESIGN)
        log(f"screen design {p['design']}")
        return screen_get()
    if p.get("on"):
        check(systemctl("enable", "--now", SCREEN_UNIT), "Switching the screen on")
    else:
        check(systemctl("disable", "--now", SCREEN_UNIT), "Switching the screen off")
        systemctl("start", "getty@tty1.service")     # the normal login comes back
    log(f"screen {'on' if p.get('on') else 'off'}")
    return screen_get()


# -------------------------------- commands -------------------------------
HANDLERS = {
    "mc_status": mc_status,
    "mc_start": mc_start,
    "mc_stop": mc_stop,
    "mc_restart": mc_restart,
    "power": power,
    "update_install": update_install,
    "update_release": update_release,
    "update_key": update_key,
    "screen_get": screen_get,
    "screen_set": screen_set,
    "sys_updates": sys_updates,
    "sys_install": sys_install,
    "bot_list": bot_list,
    "bot_info": bot_info,
    "bot_create": bot_create,
    "bot_set_meta": bot_set_meta,
    "bot_start": bot_start,
    "bot_stop": bot_stop,
    "bot_restart": bot_restart,
    "bot_delete": bot_delete,
    "bot_logs": bot_logs,
    "bot_clear_logs": bot_clear_logs,
}


def process_command(work: Path, name: str) -> None:
    try:
        payload = json.loads(work.read_text(encoding="utf-8"))
        handler = HANDLERS.get(payload.get("action"))
        if not handler:
            response = {"ok": False, "error": "Unknown action."}
        else:
            response = {"ok": True, "data": handler(payload)}
    except (ValueError, RuntimeError) as e:
        response = {"ok": False, "error": str(e)}
    except Exception as e:
        log(f"command {name} crashed: {e}\n{traceback.format_exc()}")
        response = {"ok": False, "error": "The host agent hit an unexpected error."}
    try:
        atomic_write(RESP_DIR / name, json.dumps(response))
        chown(RESP_DIR / name)
    finally:
        work.unlink(missing_ok=True)


def cleanup_responses() -> None:
    cutoff = time.time() - RESPONSE_TTL
    for f in RESP_DIR.glob("*.json"):
        try:
            if f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass


def trim_bot_logs() -> None:
    if BOTS_DIR.is_dir():
        for d in BOTS_DIR.iterdir():
            if d.is_dir():
                trim_log(d / "bot.log")


# --------------------------------- main ---------------------------------
_stop = False


def on_signal(signum, _frame):
    global _stop
    _stop = True
    log(f"signal {signum}, shutting down")


def ensure_dirs() -> None:
    for d in (STATE, CMD_DIR, RESP_DIR, BOTS_DIR):
        d.mkdir(parents=True, exist_ok=True)
    os.chown(STATE, 0, OWNER_GID)
    os.chmod(STATE, 0o755)
    for d in (CMD_DIR, RESP_DIR):
        os.chown(d, OWNER_UID, OWNER_GID)
        os.chmod(d, 0o770)
    chown(BOTS_DIR)
    for f in CMD_DIR.glob("*.work"):
        f.unlink(missing_ok=True)
    # A fresh start means any earlier shutdown/restart is over.
    POWER_FILE.unlink(missing_ok=True)


# ---- live camera (off unless camera.on exists next to the agent) ----
CAMERA_ON = Path("__HOME__/panel-agent/camera.on")
CAMERA_PY = Path("__HOME__/panel-agent/camera.py")
CAMERA_DIR = STATE / "camera"


def camera_loop() -> None:
    import grp
    proc = None
    while not _stop:
        try:
            want = CAMERA_ON.is_file() and CAMERA_PY.is_file()
            if proc and proc.poll() is not None:
                log(f"camera ended ({proc.returncode})")
                proc = None
                time.sleep(10)
            if want and not proc:
                CAMERA_DIR.mkdir(mode=0o700, exist_ok=True)
                os.chown(CAMERA_DIR, OWNER_UID, OWNER_GID)
                proc = subprocess.Popen(["/usr/bin/python3", "-I", str(CAMERA_PY)], user=OWNER_UID, group=OWNER_GID,
                                        extra_groups=[grp.getgrnam("video").gr_gid], cwd="/", stdin=subprocess.DEVNULL,
                                        env={"PATH": "/usr/bin:/bin", "CAMERA_SOCK": str(CAMERA_DIR / "camera.sock")})
                log("camera started")
            elif not want and proc:
                proc.terminate()
                proc.wait(timeout=10)
                proc = None
                log("camera stopped")
            if not want:
                (CAMERA_DIR / "camera.sock").unlink(missing_ok=True)
        except Exception as e:
            log(f"camera: {e}")
            time.sleep(30)
        time.sleep(3)


def main() -> None:
    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)
    ensure_dirs()
    log("started")
    threading.Thread(target=camera_loop, daemon=True).start()
    pool = ThreadPoolExecutor(max_workers=4)
    next_stats = next_cleanup = 0.0
    while not _stop:
        now = time.time()
        if now >= next_stats:
            try:
                atomic_write(STATS_FILE, json.dumps(gather_stats()))
            except Exception as e:
                log(f"stats failed: {e}")
            next_stats = now + STATS_INTERVAL
        if now >= next_cleanup:
            cleanup_responses()
            trim_bot_logs()
            next_cleanup = now + 60
        for f in sorted(CMD_DIR.glob("*.json")):
            work = f.with_name(f.name + ".work")
            try:
                f.rename(work)
            except FileNotFoundError:
                continue
            pool.submit(process_command, work, f.name)
        time.sleep(0.1)
    pool.shutdown(wait=False, cancel_futures=True)
    log("stopped")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        pass
    except Exception as e:
        log(f"FATAL: {e}\n{traceback.format_exc()}")
        sys.exit(1)
