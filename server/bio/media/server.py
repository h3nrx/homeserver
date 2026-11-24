#!/usr/bin/env python3
"""__BRAND__ media service.

Radio: YouTube links (livestreams or videos) become MP3 streams. A station only
runs while someone listens: the first listener starts yt-dlp + ffmpeg, every
further listener shares that one encoder, and it stops shortly after the last
one leaves. Phones play the stream in a normal <audio> element, so it keeps
going with the screen off.

Clips: every uploaded clip gets embed.mp4, a small H.264/AAC copy with the
index at the front, which Discord can fetch and play inline (like Medal).

Public paths (proxied by nginx):  /radio/<key>/stations.json|<id>.mp3|<id>.jpg
Internal paths (panel only, token): /internal/...
"""
from __future__ import annotations

import hmac
import json
import os
import queue
import re
import secrets
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

TOKEN = os.environ.get("MEDIA_TOKEN", "")
FILES = Path(os.environ.get("FILES_DIR", "/files"))
MUSIC = Path(os.environ.get("MUSIC_DIR", "/music"))
CLIPS = FILES / "clips"
STATIONS = MUSIC / "stations.json"
LIB = MUSIC / ".lib"           # yt-dlp kept up to date here (YouTube changes often)

ID_RE = re.compile(r"^[A-Za-z0-9_-]{10}$")
KEY_RE = re.compile(r"^[A-Za-z0-9_-]{16}$")
YT_RE = re.compile(r"^https://((www|m|music)\.)?(youtube\.com|youtu\.be)/\S+$", re.I)
LIVE_SUFFIX = re.compile(r"\s+\d{4}-\d{2}-\d{2} \d{2}:\d{2}$")

MAX_STATIONS = 30
MAX_PER_STATION = 10
MAX_LISTENERS = 25
IDLE_STOP = 20                 # seconds an encoder keeps running with nobody listening
BITRATE = "128k"
CHUNK = 4096
RECENT_BYTES = 160 * 1024     # ~10 s at 128 kbit/s
# Discord gives up fetching a video after ~10 s; a home uplink (~7 Mbit/s) only gets
# ~8 MB through in that time, so embeds stay under 7 MB.
EMBED_MAX_BYTES = 7 * 1024 * 1024


def log(*a):
    print("[media]", *a, flush=True)


def child_env() -> dict:
    e = dict(os.environ, HOME="/tmp", XDG_CACHE_HOME="/tmp/cache", DENO_DIR="/tmp/deno")
    if (LIB / "yt_dlp").is_dir():
        e["PYTHONPATH"] = str(LIB)
    return e


def atomic_write(path: Path, data: bytes | str) -> None:
    tmp = path.with_name(path.name + f".tmp{secrets.token_hex(3)}")
    tmp.write_bytes(data.encode() if isinstance(data, str) else data)
    os.replace(tmp, path)


def ytdlp(*args: str, timeout: int = 60) -> str:
    try:
        r = subprocess.run([sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-warnings",
                            "--no-playlist", "--socket-timeout", "15", *args],
                           capture_output=True, text=True, timeout=timeout, env=child_env())
    except subprocess.TimeoutExpired:
        raise RuntimeError("YouTube took too long to answer.")
    if r.returncode != 0:
        lines = [l for l in r.stderr.strip().splitlines() if l.startswith("ERROR")] or r.stderr.strip().splitlines() or ["yt-dlp failed"]
        msg = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[\w-]+:\s*)?", "", lines[-1])
        raise RuntimeError(msg[:220])
    return r.stdout


def update_ytdlp() -> None:
    """Keeps a fresh yt-dlp in /music/.lib; the image's copy is the fallback."""
    try:
        MUSIC.mkdir(parents=True, exist_ok=True)
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check",
                            "--no-cache-dir", "--upgrade", "--target", str(LIB), "yt-dlp[default]"],
                           capture_output=True, text=True, timeout=300, env=child_env())
        if r.returncode == 0:
            v = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True,
                               text=True, timeout=30, env=child_env()).stdout.strip()
            log(f"yt-dlp {v}")
        else:
            log("yt-dlp update failed:", (r.stderr.strip().splitlines() or ["?"])[-1])
    except Exception as e:
        log("yt-dlp update failed:", e)


# ================================ stations ================================
_db_lock = threading.Lock()


def load_db() -> dict:
    try:
        d = json.loads(STATIONS.read_text(encoding="utf-8"))
    except FileNotFoundError:
        d = {}
    d.setdefault("stations", [])
    if not KEY_RE.match(d.get("key", "")):
        d["key"] = secrets.token_urlsafe(12)
    return d


def save_db(d: dict) -> None:
    MUSIC.mkdir(parents=True, exist_ok=True)
    atomic_write(STATIONS, json.dumps(d, indent=1))


def find_station(sid: str) -> dict | None:
    return next((s for s in load_db()["stations"] if s["id"] == sid), None)


def fetch_jpeg(url: str) -> bytes | None:
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=10) as r:
            data = r.read(3 * 1024 * 1024 + 1)
        if data[:3] == b"\xff\xd8\xff" and len(data) <= 3 * 1024 * 1024 and len(data) > 2000:
            return data
    except Exception:
        pass
    return None


def add_station(url: str) -> dict:
    url = url.strip()
    if not YT_RE.match(url):
        raise ValueError("Paste a YouTube link (youtube.com or youtu.be).")
    with _db_lock:
        if len(load_db()["stations"]) >= MAX_STATIONS:
            raise ValueError(f"You can have up to {MAX_STATIONS} stations.")
    info = json.loads(ytdlp("-J", "--skip-download", url, timeout=100))
    if info.get("_type") in ("playlist", "multi_video"):
        raise ValueError("Playlists aren't supported. Use a single video or livestream.")
    if info.get("live_status") == "is_upcoming":
        raise ValueError("That stream hasn't started yet.")
    vid = info.get("id") or ""
    live = bool(info.get("is_live"))
    title = LIVE_SUFFIX.sub("", info.get("title") or "Untitled") if live else (info.get("title") or "Untitled")
    sid = secrets.token_urlsafe(8)[:10]
    st = {
        # Keep the pasted link: a channel /live link follows the stream when it restarts.
        "id": sid, "url": url, "yt": vid,
        "title": title.strip()[:100] or "Untitled",
        "channel": (info.get("channel") or info.get("uploader") or "")[:80],
        "live": live, "duration": info.get("duration") if not live else None,
        "added": int(time.time() * 1000), "thumb": False,
    }
    if re.match(r"^[A-Za-z0-9_-]{11}$", vid):
        for name in ("maxresdefault_live.jpg", "maxresdefault.jpg", "sddefault.jpg", "hqdefault.jpg"):
            data = fetch_jpeg(f"https://i.ytimg.com/vi/{vid}/{name}")
            if data:
                atomic_write(MUSIC / f"{sid}.jpg", data)
                st["thumb"] = True
                break
    with _db_lock:
        d = load_db()
        if any(s.get("yt") == vid and vid for s in d["stations"]):
            (MUSIC / f"{sid}.jpg").unlink(missing_ok=True)
            raise ValueError("That one is already in your list.")
        d["stations"].append(st)
        save_db(d)
    log(f"station added {sid}: {st['title']} ({'live' if live else 'video'})")
    return st


def update_station(sid: str, title: str) -> dict:
    with _db_lock:
        d = load_db()
        st = next((s for s in d["stations"] if s["id"] == sid), None)
        if not st:
            raise LookupError("Station not found.")
        st["title"] = title[:100]
        save_db(d)
    return st


def move_station(sid: str, up: bool) -> None:
    with _db_lock:
        d = load_db()
        ids = [s["id"] for s in d["stations"]]
        if sid not in ids:
            raise LookupError("Station not found.")
        i = ids.index(sid)
        j = i - 1 if up else i + 1
        if 0 <= j < len(ids):
            d["stations"][i], d["stations"][j] = d["stations"][j], d["stations"][i]
            save_db(d)


def delete_station(sid: str) -> None:
    with _db_lock:
        d = load_db()
        n = len(d["stations"])
        d["stations"] = [s for s in d["stations"] if s["id"] != sid]
        if len(d["stations"]) == n:
            raise LookupError("Station not found.")
        save_db(d)
    (MUSIC / f"{sid}.jpg").unlink(missing_ok=True)
    with _run_lock:
        r = _runners.get(sid)
    if r:
        r.shutdown()


def rotate_key() -> str:
    with _db_lock:
        d = load_db()
        d["key"] = secrets.token_urlsafe(12)
        save_db(d)
    with _run_lock:
        runners = list(_runners.values())
    for r in runners:          # old links stop working right away
        r.shutdown()
    return d["key"]


# ================================= radio =================================
_run_lock = threading.Lock()
_runners: dict[str, "Runner"] = {}
_resolved: dict[str, tuple[str, float]] = {}


def resolve(st: dict, fresh: bool = False) -> str:
    hit = _resolved.get(st["id"])
    if hit and not fresh and time.time() - hit[1] < 3600:
        return hit[0]
    out = ytdlp("-g", "-f", "bestaudio/93/92/91/94/95/best[height<=480]/best", st["url"], timeout=90)
    src = out.strip().splitlines()[0]
    _resolved[st["id"]] = (src, time.time())
    return src


class Runner:
    """One encoder per station, shared by all of its listeners."""

    def __init__(self, st: dict):
        self.st = st
        self.listeners: set[queue.Queue] = set()
        # ~10 s of audio sent at once to a new listener, so phones start with a
        # cushion that rides out short dropouts on mobile data.
        self.recent: list[bytes] = []
        self.recent_bytes = 0
        self.proc: subprocess.Popen | None = None
        self.idle_since: float | None = None
        self.stop = False
        self.error = ""
        self.thread = threading.Thread(target=self.run, daemon=True)
        self.thread.start()

    @property
    def on_air(self) -> bool:
        return bool(self.proc and self.proc.poll() is None)

    def shutdown(self) -> None:
        self.stop = True
        self.kill()

    def kill(self) -> None:
        p = self.proc
        if p and p.poll() is None:
            p.kill()

    def push(self, chunk: bytes) -> None:
        self.recent.append(chunk)
        self.recent_bytes += len(chunk)
        while self.recent_bytes > RECENT_BYTES and len(self.recent) > 1:
            self.recent_bytes -= len(self.recent.pop(0))
        with _run_lock:
            targets = list(self.listeners)
        for q in targets:
            try:
                q.put_nowait(chunk)
            except queue.Full:            # this listener stalled; cut it loose
                with _run_lock:
                    self.listeners.discard(q)
                try:
                    while True:
                        q.get_nowait()
                except queue.Empty:
                    pass
                q.put_nowait(None)

    def run(self) -> None:
        fails = 0
        while not self.stop:
            got = 0
            try:
                src = resolve(self.st, fresh=fails > 0)
                cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin"]
                if self.st.get("live"):
                    # Start ~8 segments behind live: the first listener gets a burst
                    # of buffered audio instead of starting on an empty buffer.
                    cmd += ["-live_start_index", "-8"]
                else:
                    cmd += ["-re"]
                cmd += ["-reconnect", "1", "-reconnect_streamed", "1", "-reconnect_delay_max", "5",
                        "-i", src, "-vn", "-ac", "2", "-ar", "44100",
                        "-c:a", "libmp3lame", "-b:a", BITRATE, "-f", "mp3", "pipe:1"]
                self.proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=child_env())
                log(f"on air {self.st['id']} ({self.st['title'][:40]})")
                while not self.stop:
                    chunk = self.proc.stdout.read1(CHUNK)
                    if not chunk:
                        break
                    got += len(chunk)
                    self.push(chunk)
                self.kill()
                err = self.proc.stderr.read().decode(errors="replace").strip().splitlines() if self.proc.stderr else []
                if err and got < 64 * 1024:
                    self.error = err[-1][:200]
            except Exception as e:
                self.error = str(e)[:200]
                self.kill()
            if self.stop:
                break
            if got > 64 * 1024:
                fails = 0                   # a video ended or the stream hiccuped: just go again
                self.error = ""
            else:
                fails += 1
                log(f"station {self.st['id']} failed ({fails}): {self.error}")
                if fails >= 3:
                    break
                time.sleep(2 * fails)
        self.kill()
        with _run_lock:
            if _runners.get(self.st["id"]) is self:
                del _runners[self.st["id"]]
            listeners, self.listeners = list(self.listeners), set()
        for q in listeners:
            try:
                q.put_nowait(None)
            except queue.Full:
                pass
        log(f"off air {self.st['id']}")


class Busy(Exception):
    pass


def attach(sid: str) -> tuple[Runner, queue.Queue]:
    st = find_station(sid)
    if not st:
        raise LookupError("Station not found.")
    with _run_lock:
        total = sum(len(r.listeners) for r in _runners.values())
        r = _runners.get(sid)
        if total >= MAX_LISTENERS or (r and len(r.listeners) >= MAX_PER_STATION):
            raise Busy()
        if r is None or r.stop:
            r = _runners[sid] = Runner(st)
        q: queue.Queue = queue.Queue(maxsize=512)
        for c in r.recent:
            q.put_nowait(c)
        r.listeners.add(q)
        r.idle_since = None
    return r, q


def detach(r: Runner, q: queue.Queue) -> None:
    with _run_lock:
        r.listeners.discard(q)
        if not r.listeners:
            r.idle_since = time.time()


def reaper() -> None:
    """Stops encoders nobody listens to, and refreshes yt-dlp once a day."""
    last_update = time.time()
    while True:
        time.sleep(5)
        now = time.time()
        with _run_lock:
            idle = [r for r in _runners.values() if not r.listeners and r.idle_since and now - r.idle_since > IDLE_STOP]
        for r in idle:
            r.shutdown()
        if int(now) % 300 < 5:
            try:
                dl_cleanup()
            except Exception as e:
                log(f"download cleanup failed: {e}")
        if now - last_update > 86400:
            last_update = now
            update_ytdlp()


def station_view(st: dict, internal: bool) -> dict:
    with _run_lock:
        r = _runners.get(st["id"])
        listeners = len(r.listeners) if r else 0
        on_air = bool(r and r.on_air)
        error = r.error if r else ""
    v = {k: st.get(k) for k in ("id", "title", "channel", "live", "duration", "thumb")}
    v.update(listeners=listeners, on_air=on_air)
    if internal:
        v.update(url=st["url"], added=st.get("added"), error=error)
    return v


# ================================= clips =================================
_clip_q: queue.Queue = queue.Queue()
_clip_pending: set[str] = set()
_clip_lock = threading.Lock()


def enqueue_clip(cid: str, force: bool = False) -> bool:
    d = CLIPS / cid
    if not d.is_dir():
        return False
    if force:
        (d / "meta.json").unlink(missing_ok=True)
    with _clip_lock:
        if cid in _clip_pending:
            return True
        _clip_pending.add(cid)
    write_meta(cid, {"state": "queued"})
    _clip_q.put(cid)
    return True


def write_meta(cid: str, meta: dict) -> None:
    d = CLIPS / cid
    if d.is_dir():
        try:
            old = json.loads((d / "meta.json").read_text())
        except Exception:
            old = {}
        old.update(meta)
        atomic_write(d / "meta.json", json.dumps(old))


def probe(path: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
                       capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise RuntimeError("Couldn't read this video.")
    return json.loads(r.stdout)


def fps_of(v: dict) -> float:
    try:
        n, d = (v.get("avg_frame_rate") or v.get("r_frame_rate") or "0/1").split("/")
        return float(n) / float(d) if float(d) else 0.0
    except Exception:
        return 0.0


def process_clip(cid: str) -> None:
    d = CLIPS / cid
    src = next((p for p in d.glob("clip.*") if p.suffix.lower() in (".mp4", ".webm", ".mov", ".m4v")), None)
    if not src:
        return
    write_meta(cid, {"state": "processing", "progress": 0})
    info = probe(src)
    v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
    a = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
    if not v:
        raise RuntimeError("This file has no video track.")
    dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
    w, h = int(v.get("width") or 0), int(v.get("height") or 0)
    rot = 0
    for sd in v.get("side_data_list") or []:
        if "rotation" in sd:
            rot = abs(int(sd["rotation"]))
    if rot in (90, 270):
        w, h = h, w
    vcodec = v.get("codec_name", "")
    meta = {"duration": round(dur, 1), "w": w, "h": h, "codec": vcodec,
            "web_ok": vcodec in ("h264", "vp8", "vp9", "av1")}

    # Poster from the clip itself (covers HEVC/MOV files the browser can't decode).
    if not (d / "clip.jpg").exists():
        subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-ss", f"{min(1.5, dur / 3):.2f}", "-i", str(src), "-frames:v", "1",
                        "-vf", "scale='min(1280,iw)':-2", "-q:v", "3", str(d / "clip.jpg.part.jpg")],
                       capture_output=True, timeout=120)
        if (d / "clip.jpg.part.jpg").exists():
            os.replace(d / "clip.jpg.part.jpg", d / "clip.jpg")
            meta["poster"] = True

    size = src.stat().st_size
    out = d / "embed.part.mp4"
    copy_ok = (vcodec == "h264" and v.get("pix_fmt") == "yuv420p" and (a is None or a.get("codec_name") == "aac")
               and size <= EMBED_MAX_BYTES and max(w, h) <= 1920 and fps_of(v) <= 61)
    base = ["nice", "-n", "15", "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src),
            "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1"]
    if copy_ok:
        cmd = base + ["-c", "copy", "-movflags", "+faststart", "-f", "mp4", str(out)]
        ew, eh = w, h
    else:
        # Sized to stay under EMBED_MAX_BYTES; long clips drop to 480p/30fps so they still look ok.
        vbit = int(max(220_000, min(4_000_000, (EMBED_MAX_BYTES * 8 * 0.92) / max(dur, 1) - 96_000)))
        lo = vbit < 1_500_000
        short = min(w, h) or 720
        scale = min(1.0, (480 if lo else 720) / short)
        ew, eh = int(w * scale) // 2 * 2, int(h * scale) // 2 * 2
        vf = f"scale={ew}:{eh}:flags=bicubic"
        if lo and fps_of(v) > 31:
            vf += ",fps=30"
        elif fps_of(v) > 61:
            vf += ",fps=60"
        cmd = base + ["-vf", vf, "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-pix_fmt", "yuv420p",
                      "-b:v", str(vbit), "-maxrate", str(int(vbit * 1.4)), "-bufsize", str(vbit * 2), "-threads", "3",
                      "-c:a", "aac", "-b:a", "96k", "-ac", "2", "-movflags", "+faststart",
                      "-progress", "pipe:1", "-nostats", "-f", "mp4", str(out)]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    last = 0.0
    for line in p.stdout:
        if line.startswith("out_time_us=") and dur > 0:
            try:
                prog = min(0.99, int(line.split("=")[1]) / 1e6 / dur)
            except ValueError:
                continue
            if prog - last >= 0.05:
                last = prog
                write_meta(cid, {"progress": round(prog, 2)})
    p.wait()
    if p.returncode != 0 or not out.exists():
        out.unlink(missing_ok=True)
        err = (p.stderr.read().strip().splitlines() or ["ffmpeg failed"])[-1]
        raise RuntimeError(err[:200])
    os.replace(out, d / "embed.mp4")
    meta.update(state="ready", embed=True, ew=ew, eh=eh, embed_size=(d / "embed.mp4").stat().st_size, progress=1)
    write_meta(cid, meta)
    log(f"clip {cid} ready: {'copied' if copy_ok else 'encoded'} {ew}x{eh}, {meta['embed_size'] // 1024} KB")
    make_stream(cid)


# A home connection can't push a 15-20 Mbit/s original fast enough to watch it
# without stops. stream.mp4 is the same clip at up to 1080p and ~4 Mbit/s, with the
# index at the front, so the clip page can start right away and keep playing.
STREAM_BITS = 4_000_000


def make_stream(cid: str) -> None:
    d = CLIPS / cid
    src = next((p for p in d.glob("clip.*") if p.suffix.lower() in (".mp4", ".webm", ".mov", ".m4v")), None)
    if not src or (d / "stream.mp4").exists():
        return
    try:
        info = probe(src)
        v = next((s for s in info["streams"] if s.get("codec_type") == "video"), None)
        if not v:
            return
        dur = float(info.get("format", {}).get("duration") or v.get("duration") or 0)
        rate = src.stat().st_size * 8 / max(dur, 1)
        w, h = int(v.get("width") or 0), int(v.get("height") or 0)
        for sd in v.get("side_data_list") or []:
            if abs(int(sd.get("rotation", 0) or 0)) in (90, 270):
                w, h = h, w
        web = v.get("codec_name") in ("h264", "vp8", "vp9", "av1") and v.get("pix_fmt", "yuv420p") == "yuv420p"
        if web and rate <= STREAM_BITS * 1.5:
            write_meta(cid, {"stream": False})       # the original is light enough already
            return
        short = min(w, h) or 1080
        scale = min(1.0, 1080 / short)
        sw, sh = int(w * scale) // 2 * 2, int(h * scale) // 2 * 2
        vf = f"scale={sw}:{sh}:flags=bicubic"
        if fps_of(v) > 61:
            vf += ",fps=60"
        out = d / "stream.part.mp4"
        cmd = ["nice", "-n", "15", "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(src),
               "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "-1", "-vf", vf,
               "-c:v", "libx264", "-preset", "veryfast", "-profile:v", "high", "-pix_fmt", "yuv420p",
               "-b:v", str(STREAM_BITS), "-maxrate", str(int(STREAM_BITS * 1.25)), "-bufsize", str(STREAM_BITS * 2), "-threads", "3",
               "-c:a", "aac", "-b:a", "160k", "-ac", "2", "-movflags", "+faststart", "-f", "mp4", str(out)]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=max(600, int(dur * 20)))
        if r.returncode != 0 or not out.exists():
            out.unlink(missing_ok=True)
            log(f"clip {cid} stream failed: {(r.stderr.strip().splitlines() or ['ffmpeg failed'])[-1][:160]}")
            return
        os.replace(out, d / "stream.mp4")
        write_meta(cid, {"stream": True, "sw": sw, "sh": sh, "stream_size": (d / "stream.mp4").stat().st_size})
        log(f"clip {cid} stream ready: {sw}x{sh}, {(d / 'stream.mp4').stat().st_size // 1024} KB")
    except Exception as e:
        (d / "stream.part.mp4").unlink(missing_ok=True)
        log(f"clip {cid} stream failed: {e}")


def clip_worker() -> None:
    while True:
        cid = _clip_q.get()
        if isinstance(cid, tuple):
            try:
                make_stream(cid[1])
            except Exception as e:
                log(f"clip {cid[1]} stream failed: {e}")
            continue
        try:
            if (CLIPS / cid).is_dir():
                process_clip(cid)
        except Exception as e:
            log(f"clip {cid} failed: {e}")
            try:
                write_meta(cid, {"state": "failed", "error": str(e)[:200]})
            except Exception:
                pass
        finally:
            with _clip_lock:
                _clip_pending.discard(cid)


def backfill_clips() -> None:
    try:
        idx = json.loads((CLIPS / "index.json").read_text())
    except Exception:
        return
    for cid in idx:
        m = CLIPS / cid / "meta.json"
        try:
            meta = json.loads(m.read_text()) if m.exists() else {}
        except Exception:
            meta = {}
        if meta.get("state") != "ready" or meta.get("embed_size", 0) > EMBED_MAX_BYTES:
            enqueue_clip(cid)      # new, unfinished, or an embed too big for Discord
        elif "stream" not in meta:
            _clip_q.put(("stream", cid))     # older clips: just add the stream copy


# =============================== downloader ===============================
# Paste a link, get MP4 / MP3 / GIF. Files land in /files/downloads/<id>/ and
# nginx serves them at /d/<id>/<name>. Kept for a week.
DL = FILES / "downloads"
DL_JOBS = DL / "jobs.json"
DL_KEEP = 7 * 86400
DL_MAX_TOTAL = 10 * 1024 ** 3
DL_MAX_ACTIVE = 8
GIF_MAX = 30
PLATFORMS = [
    ("youtube", r"(^|\.)(youtube\.com|youtu\.be)$"), ("tiktok", r"(^|\.)tiktok\.com$"),
    ("instagram", r"(^|\.)instagram\.com$"), ("x", r"(^|\.)(twitter\.com|x\.com)$"),
    ("pinterest", r"(^|\.)(pinterest\.[a-z.]+|pin\.it)$"), ("twitch", r"(^|\.)twitch\.tv$"),
    ("reddit", r"(^|\.)(reddit\.com|redd\.it)$"), ("soundcloud", r"(^|\.)soundcloud\.com$"),
    ("vimeo", r"(^|\.)vimeo\.com$"), ("facebook", r"(^|\.)(facebook\.com|fb\.watch)$"),
    ("streamable", r"(^|\.)streamable\.com$"), ("medal", r"(^|\.)medal\.tv$"),
]
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/140.0 Safari/537.36"
_dl_lock = threading.Lock()
_dl_q: queue.Queue = queue.Queue()
_dl_jobs: dict[str, dict] = {}
_dl_procs: dict[str, subprocess.Popen] = {}


def platform_of(url: str) -> str | None:
    host = (urlparse(url).hostname or "").lower()
    return next((name for name, rx in PLATFORMS if re.search(rx, host)), None)


def dl_save() -> None:
    DL.mkdir(parents=True, exist_ok=True)
    with _dl_lock:
        data = json.dumps(list(_dl_jobs.values()))
    atomic_write(DL_JOBS, data)


def dl_load() -> None:
    try:
        jobs = json.loads(DL_JOBS.read_text())
    except Exception:
        jobs = []
    for j in jobs:
        if j.get("state") in ("queued", "downloading", "converting"):
            j.update(state="failed", error="Interrupted by a restart. Try again.")
        _dl_jobs[j["id"]] = j
    dl_save()


def dl_set(jid: str, save: bool = False, **kw) -> None:
    with _dl_lock:
        if jid in _dl_jobs:
            _dl_jobs[jid].update(kw)
    if save:
        dl_save()


def parse_time(v) -> float | None:
    if v in (None, ""):
        return None
    s = str(v).strip()
    m = re.match(r"^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$", s)
    if m:
        return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    if re.match(r"^\d+(\.\d+)?$", s):
        return float(s)
    raise ValueError("Times look like 1:23 or 83.")


def new_download(b: dict) -> dict:
    url = str(b.get("url", "")).strip()
    if not re.match(r"^https?://\S{4,500}$", url):
        raise ValueError("Paste a full link (starting with https://).")
    plat = platform_of(url)
    if not plat:
        raise ValueError("That site isn't supported. Try YouTube, TikTok, Instagram, X, Pinterest, Twitch, Reddit or SoundCloud.")
    fmt = b.get("format")
    if fmt not in ("mp4", "mp3", "gif"):
        raise ValueError("Pick MP4, MP3 or GIF.")
    opts = {}
    if fmt == "mp4":
        q = str(b.get("quality", "best"))
        if q not in ("best", "1080", "720", "480", "360"):
            raise ValueError("Unknown quality.")
        opts["quality"] = q
    elif fmt == "mp3":
        q = str(b.get("quality", "320"))
        if q not in ("320", "192", "128"):
            raise ValueError("Unknown bitrate.")
        opts["quality"] = q
    else:
        w, fps = int(b.get("width") or 480), int(b.get("fps") or 15)
        if w not in (320, 480, 640, 800) or fps not in (10, 15, 24):
            raise ValueError("Unknown GIF size.")
        opts.update(width=w, fps=fps)
    start, end = parse_time(b.get("start")), parse_time(b.get("end"))
    if start is not None or end is not None:
        start = start or 0.0
        if end is not None and end <= start:
            raise ValueError("The end has to come after the start.")
        opts.update(start=start, end=end)
    if fmt == "gif":
        s = opts.get("start") or 0.0
        e = opts.get("end")
        if e is None or e - s > GIF_MAX:
            e = s + (GIF_MAX if e is None else GIF_MAX)
        opts.update(start=s, end=e)
    with _dl_lock:
        active = sum(1 for j in _dl_jobs.values() if j["state"] in ("queued", "downloading", "converting"))
        if active >= DL_MAX_ACTIVE:
            raise ValueError("Lots going on already. Wait for a download to finish.")
        jid = secrets.token_urlsafe(8)[:10]
        job = {"id": jid, "url": url, "platform": plat, "format": fmt, "opts": opts, "state": "queued",
               "progress": 0, "created": int(time.time() * 1000), "title": "", "file": "", "size": 0, "thumb": False}
        _dl_jobs[jid] = job
    dl_save()
    _dl_q.put(jid)
    log(f"download {jid}: {plat} as {fmt}")
    return dict(job)


def ff(*args: str, timeout: int = 1800) -> None:
    r = subprocess.run(["nice", "-n", "15", "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr.strip().splitlines() or ["ffmpeg failed"])[-1][:200])


def make_gif(src: Path, out: Path, width: int, fps: int, start: float | None = None, dur: float | None = None) -> None:
    pre = []
    if start:
        pre += ["-ss", f"{start:.2f}"]
    if dur:
        pre += ["-t", f"{dur:.2f}"]
    vf = (f"fps={fps},scale='min({width},iw)':-2:flags=lanczos,split[a][b];"
          f"[a]palettegen=stats_mode=diff:max_colors=200[p];[b][p]paletteuse=dither=bayer:bayer_scale=4:diff_mode=rectangle")
    ff(*pre, "-i", str(src), "-vf", vf, "-loop", "0", str(out))


def pinterest_media(url: str) -> tuple[str, str]:
    """Pins that are GIFs or pictures: read the original file URL from the page."""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en"})
    with urllib.request.urlopen(req, timeout=20) as r:
        final = r.geturl()
        if not re.search(r"(^|\.)pinterest\.[a-z.]+$", urlparse(final).hostname or ""):
            raise RuntimeError("That Pinterest link doesn't lead to a pin.")
        html = r.read(4 * 1024 * 1024).decode("utf-8", "replace").replace("\\/", "/")
    found = re.findall(r"https://i\.pinimg\.com/originals/[0-9a-f/]+\.(?:gif|jpg|jpeg|png|webp)", html)
    if not found:
        raise RuntimeError("Couldn't find the picture or GIF on that pin.")
    best = next((u for u in found if u.endswith(".gif")), found[0])
    title = ""
    m = re.search(r'<meta[^>]+property="og:title"[^>]+content="([^"]{1,200})"', html)
    if m:
        title = m.group(1)
    return best, title


def fetch_file(url: str, dest: Path, limit: int = 200 * 1024 * 1024) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r, open(dest, "wb") as f:
        n = 0
        while True:
            c = r.read(65536)
            if not c:
                break
            n += len(c)
            if n > limit:
                raise RuntimeError("That file is too big.")
            f.write(c)


def clean_name(title: str, ext: str) -> str:
    t = re.sub(r'[<>:"/\\|?*\x00-\x1f#%&{}$!@`=+]', "", title or "download").strip(" .")[:70] or "download"
    return f"{t}.{ext}"


def run_download(jid: str) -> None:
    with _dl_lock:
        job = dict(_dl_jobs.get(jid) or {})
    if not job:
        return
    d = DL / jid
    d.mkdir(parents=True, exist_ok=True)
    fmt, o, url = job["format"], job["opts"], job["url"]
    dl_set(jid, save=True, state="downloading")
    cmd = [sys.executable, "-m", "yt_dlp", "--ignore-config", "--no-warnings", "--no-playlist", "--newline",
           "--socket-timeout", "20", "--max-filesize", "2G", "--windows-filenames", "--trim-filenames", "90",
           "--no-mtime", "-o", f"{d}/%(title).70B.%(ext)s", "-o", f"thumbnail:{d}/thumb",
           "--write-thumbnail", "--convert-thumbnails", "jpg",
           "--progress-template", "download:DL %(progress._percent_str)s|%(progress._speed_str)s|%(progress._eta_str)s",
           "--print", "video:META %(title)s|%(uploader)s|%(duration)s",
           "--print", "after_move:FILE %(filepath)s", "--no-simulate"]
    if fmt == "mp4":
        q = o.get("quality", "best")
        cmd += ["-f", "bv*+ba/b", "-S", ("vcodec:h264,acodec:m4a" if q == "best" else f"res:{q},vcodec:h264,acodec:m4a"),
                "--merge-output-format", "mp4", "--remux-video", "mp4"]
    elif fmt == "mp3":
        cmd += ["-f", "ba/b", "-x", "--audio-format", "mp3", "--audio-quality", f"{o.get('quality', '320')}K", "--embed-metadata"]
    else:
        cmd += ["-f", "bv*/b", "-S", "res:720,vcodec:h264"]
    if o.get("start") is not None or o.get("end") is not None:
        s = o.get("start") or 0
        e = o.get("end")
        cmd += ["--download-sections", f"*{s}-{e if e is not None else 'inf'}"]
        if fmt == "mp4":
            cmd += ["--force-keyframes-at-cuts"]
    cmd += ["--", url]

    path, err_lines = None, []
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=child_env())
    _dl_procs[jid] = p
    t_err = threading.Thread(target=lambda: err_lines.extend(p.stderr.read().splitlines()), daemon=True)
    t_err.start()
    try:
        for line in p.stdout:
            line = line.rstrip("\n")
            if line.startswith("DL "):
                pct, speed, eta = (line[3:].split("|") + ["", "", ""])[:3]
                try:
                    prog = float(pct.strip().rstrip("%")) / 100
                except ValueError:
                    prog = None
                kw = {"speed": speed.strip() if speed.strip() not in ("NA", "Unknown") else "", "eta": eta.strip() if eta.strip() not in ("NA", "Unknown") else ""}
                if prog is not None:
                    kw["progress"] = round(min(prog, 0.99), 3)
                dl_set(jid, **kw)
            elif line.startswith("META "):
                t, up, dur = (line[5:].split("|") + ["", "", ""])[:3]
                dl_set(jid, title=t[:120], uploader=up[:80] if up != "NA" else "",
                       duration=float(dur) if re.match(r"^\d+(\.\d+)?$", dur) else None)
            elif line.startswith("FILE "):
                path = Path(line[5:].strip())
        p.wait(timeout=3600)
    finally:
        _dl_procs.pop(jid, None)
    t_err.join(timeout=5)
    with _dl_lock:
        if jid not in _dl_jobs:          # deleted while running
            shutil.rmtree(d, ignore_errors=True)
            return
    note = ""
    if p.returncode != 0 or not path or not path.exists():
        msg = next((l for l in reversed(err_lines) if l.startswith("ERROR")), err_lines[-1] if err_lines else "Download failed.")
        msg = re.sub(r"^ERROR:\s*(\[[^\]]+\]\s*[\w-]+:\s*)?", "", msg)
        if job["platform"] == "pinterest":
            # GIF and picture pins have no video for yt-dlp; grab the original file.
            dl_set(jid, state="downloading", progress=0.5)
            murl, ptitle = pinterest_media(url)
            ext = murl.rsplit(".", 1)[-1].lower()
            path = d / clean_name(ptitle or job.get("title") or "pinterest", ext)
            fetch_file(murl, path)
            dl_set(jid, title=(ptitle or "Pinterest pin")[:120])
            if fmt == "mp3":
                raise RuntimeError("This pin has no sound, pick MP4 or GIF.")
            if ext != "gif":
                note = "This pin is a picture, so you get the image."
            elif fmt == "mp4":
                dl_set(jid, state="converting")
                out = path.with_suffix(".mp4")
                ff("-i", str(path), "-movflags", "+faststart", "-pix_fmt", "yuv420p",
                   "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2", "-c:v", "libx264", "-crf", "20", str(out))
                path.unlink(missing_ok=True)
                path = out
        else:
            low = msg.lower()
            if "login" in low or "log in" in low or "cookies" in low or "private" in low:
                msg = "This post needs a login (private or age-restricted), so it can't be downloaded."
            elif "unsupported url" in low:
                msg = "That link isn't a video or post that can be downloaded."
            elif "max-filesize" in low or "larger than max" in low:
                msg = "That's bigger than 2 GB."
            raise RuntimeError(msg[:220])

    if fmt == "gif" and path.suffix.lower() != ".gif" and path.suffix.lower() in (".mp4", ".webm", ".mkv", ".mov"):
        dl_set(jid, state="converting", progress=0.99)
        out = d / clean_name(job.get("title") or _dl_jobs[jid].get("title") or path.stem, "gif")
        # The section was already cut while downloading.
        make_gif(path, out, o["width"], o["fps"], dur=GIF_MAX)
        path.unlink(missing_ok=True)
        path = out
    elif fmt == "gif" and path.suffix.lower() == ".gif" and (o.get("width") or 0) < 800:
        # Pinterest GIF: shrink if it's wider than asked.
        try:
            w = int(json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", str(path)],
                                              capture_output=True, text=True, timeout=30).stdout)["streams"][0]["width"])
        except Exception:
            w = 0
        if w > o["width"]:
            dl_set(jid, state="converting", progress=0.99)
            out = path.with_name(path.stem + f"-{o['width']}.gif")
            make_gif(path, out, o["width"], o["fps"])
            path.unlink(missing_ok=True)
            path = out

    thumb = d / "thumb.jpg"
    for extra in d.glob("thumb.*"):
        if extra.suffix != ".jpg":
            extra.unlink(missing_ok=True)
    if not thumb.exists() and path.suffix.lower() != ".mp3":
        try:
            ff("-i", str(path), "-frames:v", "1", "-vf", "scale='min(480,iw)':-2", "-q:v", "4", str(thumb), timeout=60)
        except Exception:
            pass
    for leftover in d.iterdir():
        if leftover not in (path, thumb):
            leftover.unlink(missing_ok=True)
    dl_set(jid, save=True, state="done", progress=1, file=path.name, size=path.stat().st_size,
           thumb=thumb.exists(), note=note, finished=int(time.time() * 1000), speed="", eta="")
    log(f"download {jid} done: {path.name} ({path.stat().st_size // 1024} KB)")


# ---------------------------- discord compressor ----------------------------
# The panel streams an upload to /files/downloads/<id>/source.<ext>; this squeezes
# it under Discord's upload limit with a two-pass H.264 encode.
CMP_TARGETS = {"10": 10, "50": 50, "500": 500}       # free, Nitro Basic, Nitro (MB)
CMP_SRC_EXT = {"mp4", "mov", "webm", "mkv", "m4v", "avi", "flv", "wmv", "ts", "gif", "mp3", "wav", "m4a", "ogg", "flac", "opus", "aac"}


def new_compress(b: dict) -> dict:
    jid = str(b.get("id", ""))
    if not ID_RE.match(jid):
        raise ValueError("Bad id.")
    src = str(b.get("src", ""))
    if not re.match(r"^source\.[a-z0-9]{2,4}$", src) or src.split(".")[1] not in CMP_SRC_EXT:
        raise ValueError("That file type can't be compressed.")
    if not (DL / jid / src).is_file():
        raise LookupError("Upload not found.")
    target = str(b.get("target", "10"))
    if target not in CMP_TARGETS:
        raise ValueError("Pick 10, 50 or 500 MB.")
    audio = str(b.get("audio", "")) in ("1", "true")
    start, end = parse_time(b.get("start")), parse_time(b.get("end"))
    if end is not None and end <= (start or 0):
        raise ValueError("The end has to come after the start.")
    name = re.sub(r"[\x00-\x1f\x7f]", "", str(b.get("name", "video")))[:120] or "video"
    with _dl_lock:
        if jid in _dl_jobs:
            raise ValueError("Already queued.")
        active = sum(1 for j in _dl_jobs.values() if j["state"] in ("queued", "downloading", "converting"))
        if active >= DL_MAX_ACTIVE:
            raise ValueError("Lots going on already. Wait for a job to finish.")
        job = {"id": jid, "kind": "compress", "url": "", "platform": "upload", "format": "mp3" if audio else "mp4",
               "opts": {"target": int(target), "start": start, "end": end, "src": src, "orig": (DL / jid / src).stat().st_size},
               "state": "queued", "progress": 0, "created": int(time.time() * 1000), "title": name.rsplit(".", 1)[0][:100],
               "file": "", "size": 0, "thumb": False}
        _dl_jobs[jid] = job
    dl_save()
    _dl_q.put(jid)
    log(f"compress {jid}: {name} -> {target} MB {'mp3' if audio else 'mp4'}")
    return dict(job)


def probe_media(p: Path) -> dict:
    r = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(p)],
                       capture_output=True, text=True, timeout=60)
    try:
        d = json.loads(r.stdout)
    except ValueError:
        raise RuntimeError("That file isn't a video or audio file ffmpeg can read.")
    v = next((s for s in d.get("streams", []) if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") != 1), None)
    a = next((s for s in d.get("streams", []) if s.get("codec_type") == "audio"), None)
    dur = float(d.get("format", {}).get("duration") or (v or {}).get("duration") or 0)
    fps = 30.0
    if v and "/" in str(v.get("avg_frame_rate", "")):
        n, dd = v["avg_frame_rate"].split("/")
        fps = float(n) / float(dd) if float(dd) else 30.0
    return {"dur": dur, "w": int((v or {}).get("width") or 0), "h": int((v or {}).get("height") or 0),
            "fps": fps or 30.0, "video": v is not None, "audio": a is not None}


def ff_progress(args: list[str], jid: str, dur: float, lo: float, hi: float) -> None:
    """Runs ffmpeg and maps its progress into [lo, hi] of the job bar."""
    cmd = ["nice", "-n", "15", "ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args]
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, cwd="/tmp")
    _dl_procs[jid] = p
    err: list[str] = []
    t = threading.Thread(target=lambda: err.extend(p.stderr.read().splitlines()), daemon=True)
    t.start()
    try:
        for line in p.stdout:
            if line.startswith("out_time_us=") and dur > 0:
                try:
                    f = min(1.0, int(line.split("=")[1]) / 1e6 / dur)
                except ValueError:
                    continue
                dl_set(jid, progress=round(lo + (hi - lo) * f, 3))
        p.wait()
    finally:
        _dl_procs.pop(jid, None)
    t.join(timeout=5)
    if p.returncode != 0:
        with _dl_lock:
            gone = jid not in _dl_jobs
        if gone:
            raise RuntimeError("Cancelled.")
        raise RuntimeError((err[-1] if err else "ffmpeg failed")[:200])


def run_compress(jid: str) -> None:
    with _dl_lock:
        job = dict(_dl_jobs.get(jid) or {})
    if not job:
        return
    d = DL / jid
    o = job["opts"]
    src = d / o["src"]
    dl_set(jid, save=True, state="converting", progress=0.01)
    info = probe_media(src)
    start = o.get("start") or 0.0
    end = o.get("end")
    full = info["dur"]
    if full <= 0:
        raise RuntimeError("Couldn't read how long that file is.")
    if start >= full:
        raise RuntimeError("The start is after the end of the video.")
    dur = (min(end, full) if end else full) - start
    dl_set(jid, duration=round(full, 2))
    budget = o["target"] * 1_000_000 * 0.94 * 8          # bits, with room for the container
    cut = (["-ss", f"{start:.3f}"] if start else []) + (["-t", f"{dur:.3f}"] if end else [])
    base = job["title"] or "video"
    audio_only = job["format"] == "mp3" or not info["video"]

    if audio_only:
        if not info["audio"]:
            raise RuntimeError("That file has no sound.")
        kbps = int(min(320, budget / dur / 1000))
        if kbps < 32:
            raise RuntimeError(f"Too long for {o['target']} MB even as audio. Cut it shorter.")
        out = d / clean_name(base + " (discord)", "mp3")
        ff_progress([*cut, "-i", str(src), "-vn", "-c:a", "libmp3lame", "-b:a", f"{kbps}k", "-map_metadata", "-1", str(out)],
                    jid, dur, 0.02, 0.99)
    else:
        a_k = 0 if not info["audio"] else (128 if budget / dur > 2_000_000 else 96 if budget / dur > 600_000 else 64)
        v_k = int(budget / dur / 1000) - a_k
        if v_k < 120:
            raise RuntimeError(f"Too long for {o['target']} MB ({int(dur)} s). Cut it shorter or pick a bigger size.")
        # Pick a size that the bitrate can actually carry; never upscale.
        h = 1080 if v_k > 4500 else 720 if v_k > 2200 else 540 if v_k > 1100 else 480 if v_k > 600 else 360 if v_k > 300 else 240
        short = min(info["w"], info["h"]) or h
        h = min(h, short)
        fps = info["fps"] if info["fps"] <= 60 else 60
        if v_k < 1500 and fps > 30:
            fps = 30
        portrait = info["h"] > info["w"]
        scale = f"scale={'%d:-2' % h if portrait else '-2:%d' % h}:flags=lanczos" if short > h else "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        vf = f"{scale},fps={fps:.3f},format=yuv420p"
        preset = "medium" if dur <= 180 else "fast" if dur <= 900 else "veryfast"
        out = d / clean_name(base + " (discord)", "mp4")
        plog = f"/tmp/x264-{jid}"
        for attempt in range(2):
            common = [*cut, "-i", str(src), "-vf", vf, "-c:v", "libx264", "-preset", preset, "-b:v", f"{v_k}k",
                      "-maxrate", f"{int(v_k * 1.5)}k", "-bufsize", f"{v_k * 2}k", "-passlogfile", plog]
            ff_progress([*common, "-pass", "1", "-an", "-f", "mp4", "/dev/null"], jid, dur, 0.02, 0.45)
            aud = ["-c:a", "aac", "-b:a", f"{a_k}k", "-ac", "2"] if a_k else ["-an"]
            ff_progress([*common, "-pass", "2", *aud, "-movflags", "+faststart", str(out)], jid, dur, 0.45, 0.99)
            if out.stat().st_size <= o["target"] * 1_000_000 * 0.985 or attempt:
                break
            v_k = int(v_k * 0.88)              # overshot (rare): one more go a bit smaller
        for f in Path("/tmp").glob(f"x264-{jid}*"):
            f.unlink(missing_ok=True)
    size = out.stat().st_size
    if size > o["target"] * 1_000_000:
        raise RuntimeError(f"Couldn't get it under {o['target']} MB. Cut it shorter.")
    thumb = d / "thumb.jpg"
    if not audio_only:
        try:
            ff("-ss", f"{min(1.0, dur / 3):.2f}", "-i", str(out), "-frames:v", "1", "-vf", "scale='min(480,iw)':-2", "-q:v", "4", str(thumb), timeout=60)
        except Exception:
            pass
    src.unlink(missing_ok=True)
    dl_set(jid, save=True, state="done", progress=1, file=out.name, size=size, thumb=thumb.exists(),
           finished=int(time.time() * 1000), cut_dur=round(dur, 2))
    log(f"compress {jid} done: {o['orig'] // 1024} KB -> {size // 1024} KB")


# ------------------------------ file converter ------------------------------
# The panel uploads a batch as in-01.<ext>, in-02.<ext>… (+ names.json) into
# /files/downloads/<id>/; one job turns it into one file, or a zip for many.
CV_IMG = {"jpg", "jpeg", "png", "webp", "heic", "heif", "gif", "bmp", "tif", "tiff", "avif", "ico"}
CV_VID = {"mp4", "mov", "webm", "mkv", "m4v", "avi", "flv", "wmv", "ts", "3gp"}
CV_AUD = {"mp3", "wav", "m4a", "ogg", "flac", "opus", "aac", "wma"}
CV_TARGETS = {"jpg", "png", "webp", "pdf", "mp4", "mp3", "gif"}
CV_MAX_PAGES = 300


def cv_inputs(d: Path) -> list[tuple[Path, str]]:
    try:
        names = json.loads((d / "names.json").read_text())
    except Exception:
        names = {}
    files = sorted(p for p in d.iterdir() if re.match(r"^in-\d{2}\.[a-z0-9]{2,4}$", p.name))
    return [(p, str(names.get(p.name) or p.stem)) for p in files]


def new_convert(b: dict) -> dict:
    jid = str(b.get("id", ""))
    if not ID_RE.match(jid):
        raise ValueError("Bad id.")
    d = DL / jid
    if not d.is_dir():
        raise LookupError("Upload not found.")
    ins = cv_inputs(d)
    if not ins:
        raise ValueError("Add at least one file.")
    to = str(b.get("to", ""))
    if to not in CV_TARGETS:
        raise ValueError("Pick what to convert to.")
    kinds = {("img" if p.suffix[1:] in CV_IMG else "pdf" if p.suffix == ".pdf" else "vid" if p.suffix[1:] in CV_VID
              else "aud" if p.suffix[1:] in CV_AUD else "?") for p, _ in ins}
    if "?" in kinds:
        raise ValueError("One of the files isn't a picture, PDF, video or audio file.")
    ok = {"jpg": {"img", "pdf"}, "png": {"img", "pdf"}, "webp": {"img", "pdf"}, "pdf": {"img", "pdf"},
          "mp4": {"vid", "img"}, "mp3": {"vid", "aud"}, "gif": {"vid", "img"}}[to]
    if not kinds <= ok:
        raise ValueError({"mp4": "MP4 needs video files (or GIFs).", "mp3": "MP3 needs video or audio files.",
                          "gif": "GIF needs video files.", "pdf": "PDF works with pictures and PDFs."}
                         .get(to, "Pictures and PDFs can become JPG, PNG or WebP."))
    if to in ("mp4", "gif") and "img" in kinds and not all(p.suffix == ".gif" for p, _ in ins if p.suffix[1:] in CV_IMG):
        raise ValueError("Only animated GIFs can become videos.")
    width = int(b.get("width") or 0)
    if width not in (0, 3840, 1920, 1280, 800, 480):
        raise ValueError("Unknown size.")
    title = ins[0][1].rsplit(".", 1)[0][:90] if len(ins) == 1 else f"{ins[0][1].rsplit('.', 1)[0][:70]} +{len(ins) - 1}"
    with _dl_lock:
        if jid in _dl_jobs:
            raise ValueError("Already converting.")
        active = sum(1 for j in _dl_jobs.values() if j["state"] in ("queued", "downloading", "converting"))
        if active >= DL_MAX_ACTIVE:
            raise ValueError("Lots going on already. Wait for a job to finish.")
        job = {"id": jid, "kind": "convert", "url": "", "platform": "upload", "format": to,
               "opts": {"width": width, "count": len(ins), "from": sorted({p.suffix[1:] for p, _ in ins}),
                        "orig": sum(p.stat().st_size for p, _ in ins)},
               "state": "queued", "progress": 0, "created": int(time.time() * 1000), "title": title,
               "file": "", "size": 0, "thumb": False}
        _dl_jobs[jid] = job
    dl_save()
    _dl_q.put(jid)
    log(f"convert {jid}: {len(ins)} file(s) {sorted(kinds)} -> {to}")
    return dict(job)


def cv_open(p: Path):
    from PIL import Image, ImageOps
    import pillow_heif
    pillow_heif.register_heif_opener()
    Image.MAX_IMAGE_PIXELS = 250_000_000
    try:
        im = Image.open(p)
        im.seek(0)
        im = ImageOps.exif_transpose(im)
    except Exception:
        raise RuntimeError(f"Couldn't open {p.name} as a picture.")
    return im


def cv_save(im, out: Path, to: str, width: int) -> None:
    from PIL import Image
    if width and im.width > width:
        im = im.resize((width, max(1, round(im.height * width / im.width))), Image.LANCZOS)
    if to == "jpg" or to == "pdf":
        if im.mode in ("RGBA", "LA", "P"):
            im = im.convert("RGBA")
            bg = Image.new("RGB", im.size, (255, 255, 255))
            bg.paste(im, mask=im.split()[-1])
            im = bg
        elif im.mode != "RGB":
            im = im.convert("RGB")
        if to == "jpg":
            im.save(out, "JPEG", quality=90, optimize=True, progressive=True)
        else:
            im.save(out, "PDF", resolution=150)
    elif to == "png":
        im.save(out, "PNG", optimize=True)
    else:
        if im.mode not in ("RGB", "RGBA"):
            im = im.convert("RGBA")
        im.save(out, "WEBP", quality=86, method=4)


def cv_pdf_pages(src: Path, work: Path) -> list[Path]:
    r = subprocess.run(["pdfinfo", str(src)], capture_output=True, text=True, timeout=60)
    m = re.search(r"^Pages:\s+(\d+)", r.stdout, re.M)
    if not m:
        raise RuntimeError(f"{src.name} isn't a readable PDF (or it has a password).")
    if int(m.group(1)) > CV_MAX_PAGES:
        raise RuntimeError(f"PDFs can have up to {CV_MAX_PAGES} pages.")
    pre = work / src.stem
    r = subprocess.run(["nice", "-n", "15", "pdftoppm", "-r", "150", "-png", str(src), str(pre)], capture_output=True, text=True, timeout=1800)
    if r.returncode != 0:
        raise RuntimeError(f"Couldn't read the pages of {src.name}.")
    return sorted(work.glob(f"{src.stem}-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))


def run_convert(jid: str) -> None:
    import zipfile
    with _dl_lock:
        job = dict(_dl_jobs.get(jid) or {})
    if not job:
        return
    d = DL / jid
    to, width = job["format"], job["opts"]["width"]
    ins = cv_inputs(d)
    work = d / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    dl_set(jid, save=True, state="converting", progress=0.02)
    outs: list[Path] = []
    used: set[str] = set()

    def out_name(stem: str, ext: str) -> Path:
        name = clean_name(stem, ext)
        i = 2
        while name.lower() in used:
            name = clean_name(f"{stem} ({i})", ext)
            i += 1
        used.add(name.lower())
        return work / name

    n = len(ins)
    first_stem = ins[0][1].rsplit(".", 1)[0] or "converted"
    if to == "pdf":
        from pypdf import PdfWriter
        w = PdfWriter()
        for i, (p, name) in enumerate(ins):
            if p.suffix == ".pdf":
                try:
                    w.append(str(p))
                except Exception:
                    raise RuntimeError(f"{name} isn't a readable PDF (or it has a password).")
            else:
                tmp = work / f"page-{i:03d}.pdf"
                cv_save(cv_open(p), tmp, "pdf", width)
                w.append(str(tmp))
            dl_set(jid, progress=round(0.05 + 0.9 * (i + 1) / n, 3))
        out = out_name(first_stem, "pdf")
        with open(out, "wb") as f:
            w.write(f)
        outs.append(out)
    elif to in ("jpg", "png", "webp"):
        for i, (p, name) in enumerate(ins):
            stem = name.rsplit(".", 1)[0]
            if p.suffix == ".pdf":
                pages = cv_pdf_pages(p, work)
                for k, pg in enumerate(pages, 1):
                    from PIL import Image
                    cv_save(Image.open(pg), out_name(f"{stem} p{k}" if len(pages) > 1 else stem, to), to, width)
                    pg.unlink()
            else:
                cv_save(cv_open(p), out_name(stem, to), to, width)
            dl_set(jid, progress=round(0.05 + 0.9 * (i + 1) / n, 3))
        outs = sorted(x for x in work.iterdir() if x.suffix == f".{to}")
    else:
        for i, (p, name) in enumerate(ins):
            stem = name.rsplit(".", 1)[0]
            info = probe_media(p)
            lo, hi = 0.03 + 0.95 * i / n, 0.03 + 0.95 * (i + 1) / n
            if to == "mp4":
                out = out_name(stem, "mp4")
                sc = f"scale='min({width},iw)':-2" if width else "scale=trunc(iw/2)*2:trunc(ih/2)*2"
                aud = ["-c:a", "aac", "-b:a", "160k"] if info["audio"] else ["-an"]
                ff_progress(["-i", str(p), "-vf", f"{sc},format=yuv420p", "-c:v", "libx264", "-preset", "veryfast", "-crf", "22",
                             *aud, "-movflags", "+faststart", str(out)], jid, info["dur"], lo, hi)
            elif to == "mp3":
                if not info["audio"]:
                    raise RuntimeError(f"{name} has no sound.")
                out = out_name(stem, "mp3")
                ff_progress(["-i", str(p), "-vn", "-c:a", "libmp3lame", "-b:a", "256k", str(out)], jid, info["dur"], lo, hi)
            else:
                out = out_name(stem, "gif")
                make_gif(p, out, min(width or 480, 800), 15, dur=GIF_MAX)
                dl_set(jid, progress=round(hi, 3))
            outs.append(out)
    if not outs:
        raise RuntimeError("Nothing came out of that. Check the files.")
    if len(outs) == 1:
        final = d / outs[0].name
        outs[0].replace(final)
    else:
        final = d / clean_name(f"{first_stem} ({len(outs)} files)", "zip")
        with zipfile.ZipFile(final, "w", zipfile.ZIP_STORED if to in ("jpg", "webp", "mp4", "mp3", "gif") else zipfile.ZIP_DEFLATED) as z:
            for o in outs:
                z.write(o, o.name)
    # A small still for the card.
    thumb = d / "thumb.jpg"
    first = final if len(outs) == 1 else outs[0]
    try:
        if to == "pdf":
            subprocess.run(["pdftoppm", "-r", "40", "-jpeg", "-f", "1", "-l", "1", "-singlefile", str(final), str(d / "thumb")],
                           capture_output=True, timeout=60)
        elif to in ("jpg", "png", "webp"):
            cv_save(cv_open(first), thumb, "jpg", 480)
        elif to in ("mp4", "gif"):
            ff("-i", str(first), "-frames:v", "1", "-vf", "scale='min(480,iw)':-2", "-q:v", "4", str(thumb), timeout=60)
    except Exception:
        pass
    for p in d.iterdir():
        if p not in (final, thumb) and p.name != "names.json":
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    dl_set(jid, save=True, state="done", progress=1, file=final.name, size=final.stat().st_size, thumb=thumb.exists(),
           outputs=len(outs), finished=int(time.time() * 1000))
    log(f"convert {jid} done: {final.name} ({final.stat().st_size // 1024} KB)")


# --------------------------- background remover ---------------------------
# ISNet (the model rembg uses for "general use") run directly with onnxruntime.
# Batches are uploaded like converter batches (in-NN.<ext> + names.json).
MODELS = MUSIC / ".models"
CUT_MODEL = MODELS / "isnet-general-use.onnx"
CUT_MODEL_URL = "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx"
CUT_BG = {"none": None, "white": (255, 255, 255), "black": (0, 0, 0)}
_cut_lock = threading.Lock()
_cut_session = None
_cut_used = 0.0
CUT_IDLE = 300          # unload the model after 5 min without work, so radio/downloads keep their RAM


def cut_unload_later(delay: float = CUT_IDLE) -> None:
    def check():
        global _cut_session
        with _cut_lock:
            if _cut_session is None:
                return
            left = CUT_IDLE - (time.time() - _cut_used)
            if left > 1:                    # used again meanwhile: check again later
                cut_unload_later(left + 1)
                return
            _cut_session = None
        import gc, ctypes
        gc.collect()
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)   # hand the freed memory back to the system
        except Exception:
            pass
        log("background model unloaded (idle)")
    t = threading.Timer(delay, check)
    t.daemon = True
    t.start()


def cut_model(jid: str | None = None):
    global _cut_session, _cut_used
    with _cut_lock:
        _cut_used = time.time()
        if _cut_session is not None:
            return _cut_session
        if not CUT_MODEL.exists() or CUT_MODEL.stat().st_size < 50_000_000:
            MODELS.mkdir(parents=True, exist_ok=True)
            log("downloading background model (first time only)")
            if jid:
                dl_set(jid, state="converting", progress=0.01, note="Getting the AI model ready (first time only)…")
            tmp = CUT_MODEL.with_suffix(".part")
            fetch_file(CUT_MODEL_URL, tmp, limit=400 * 1024 * 1024)
            tmp.replace(CUT_MODEL)
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        opts.enable_cpu_mem_arena = False       # don't keep a big memory pool around between pictures
        _cut_session = ort.InferenceSession(str(CUT_MODEL), opts, providers=["CPUExecutionProvider"])
        log("background model loaded")
        return _cut_session


def cut_mask(im):
    """Returns an L-mode alpha mask the size of im."""
    import numpy as np
    from PIL import Image
    sess = cut_model()
    small = im.convert("RGB").resize((1024, 1024), Image.LANCZOS)
    a = np.asarray(small, dtype=np.float32)
    a = a / max(float(a.max()), 1e-6)
    a = (a - np.array([0.485, 0.456, 0.406], dtype=np.float32)) / np.array([1.0, 1.0, 1.0], dtype=np.float32)
    a = a.transpose(2, 0, 1)[None].astype(np.float32)
    out = sess.run(None, {sess.get_inputs()[0].name: a})[0][0][0]
    lo, hi = float(out.min()), float(out.max())
    out = (out - lo) / max(hi - lo, 1e-6)
    return Image.fromarray((out * 255).clip(0, 255).astype("uint8"), "L").resize(im.size, Image.LANCZOS)


def new_cutout(b: dict) -> dict:
    jid = str(b.get("id", ""))
    if not ID_RE.match(jid):
        raise ValueError("Bad id.")
    d = DL / jid
    if not d.is_dir():
        raise LookupError("Upload not found.")
    ins = cv_inputs(d)
    if not ins:
        raise ValueError("Add at least one picture.")
    if any(p.suffix[1:] not in CV_IMG for p, _ in ins):
        raise ValueError("Only pictures work here (JPG, PNG, WebP, HEIC…).")
    bg = str(b.get("bg", "none"))
    if bg not in CUT_BG:
        raise ValueError("Unknown background.")
    crop = str(b.get("crop", "")) in ("1", "true", "True")
    title = ins[0][1].rsplit(".", 1)[0][:90] + (f" +{len(ins) - 1}" if len(ins) > 1 else "")
    with _dl_lock:
        if jid in _dl_jobs:
            raise ValueError("Already working on it.")
        active = sum(1 for j in _dl_jobs.values() if j["state"] in ("queued", "downloading", "converting"))
        if active >= DL_MAX_ACTIVE:
            raise ValueError("Lots going on already. Wait for a job to finish.")
        job = {"id": jid, "kind": "cutout", "url": "", "platform": "upload", "format": "png" if bg == "none" else "jpg",
               "opts": {"bg": bg, "crop": crop, "count": len(ins), "orig": sum(p.stat().st_size for p, _ in ins)},
               "state": "queued", "progress": 0, "created": int(time.time() * 1000), "title": title,
               "file": "", "size": 0, "thumb": False}
        _dl_jobs[jid] = job
    dl_save()
    _dl_q.put(jid)
    log(f"cutout {jid}: {len(ins)} picture(s), bg {bg}{', crop' if crop else ''}")
    return dict(job)


def run_cutout(jid: str) -> None:
    import zipfile
    from PIL import Image
    with _dl_lock:
        job = dict(_dl_jobs.get(jid) or {})
    if not job:
        return
    d = DL / jid
    o = job["opts"]
    ins = cv_inputs(d)
    work = d / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    dl_set(jid, save=True, state="converting", progress=0.02)
    cut_model(jid)
    cut_unload_later()
    dl_set(jid, note="")
    outs, used = [], set()
    for i, (p, name) in enumerate(ins):
        im = cv_open(p)
        if max(im.size) > 4096:                  # keeps memory and time sane
            im.thumbnail((4096, 4096), Image.LANCZOS)
        im = im.convert("RGBA")
        mask = cut_mask(im)
        im.putalpha(mask)
        if o["crop"]:
            box = mask.point(lambda v: 255 if v > 24 else 0).getbbox()
            if box:
                pad = round(max(im.size) * 0.02)
                box = (max(0, box[0] - pad), max(0, box[1] - pad), min(im.width, box[2] + pad), min(im.height, box[3] + pad))
                im = im.crop(box)
        stem = name.rsplit(".", 1)[0] + " (no bg)"
        fname = clean_name(stem, job["format"])
        k = 2
        while fname.lower() in used:
            fname = clean_name(f"{stem} {k}", job["format"])
            k += 1
        used.add(fname.lower())
        out = work / fname
        if o["bg"] == "none":
            im.save(out, "PNG", optimize=True)
        else:
            flat = Image.new("RGB", im.size, CUT_BG[o["bg"]])
            flat.paste(im, mask=im.split()[-1])
            flat.save(out, "JPEG", quality=92)
        outs.append(out)
        dl_set(jid, progress=round(0.05 + 0.93 * (i + 1) / len(ins), 3))
    if len(outs) == 1:
        final = d / outs[0].name
        outs[0].replace(final)
        first = final
    else:
        final = d / clean_name(f"{ins[0][1].rsplit('.', 1)[0]} no bg ({len(outs)} files)", "zip")
        with zipfile.ZipFile(final, "w", zipfile.ZIP_STORED) as z:
            for x in outs:
                z.write(x, x.name)
        first = outs[0]
    # Card preview: the cut-out on a dark checkerboard so it reads as transparent.
    thumb = d / "thumb.jpg"
    try:
        im = Image.open(first).convert("RGBA")
        im.thumbnail((480, 480), Image.LANCZOS)
        board = Image.new("RGB", im.size, (34, 38, 50))
        tile = 12
        dark = Image.new("RGB", (tile, tile), (24, 27, 37))
        for y in range(0, im.height, tile):
            for x in range((y // tile) % 2 * tile, im.width, tile * 2):
                board.paste(dark, (x, y))
        board.paste(im, mask=im.split()[-1])
        board.save(thumb, "JPEG", quality=85)
    except Exception:
        pass
    for p in d.iterdir():
        if p not in (final, thumb) and p.name != "names.json":
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    dl_set(jid, save=True, state="done", progress=1, file=final.name, size=final.stat().st_size, thumb=thumb.exists(),
           outputs=len(outs), finished=int(time.time() * 1000))
    log(f"cutout {jid} done: {final.name}")


# ============================== Image upscaler ==============================
# Real-ESRGAN general x4v3 (4.8 MB). The ONNX export takes fixed 128x128 tiles,
# so the picture is cut into overlapping tiles and stitched back together.
UP_MODEL = MODELS / "realesr-general-x4v3.onnx"
UP_MODEL_URL = "https://huggingface.co/tamnvcc/Real-ESRGAN-General-x4v3_float/resolve/main/onnx/model.onnx"
UP_TILE, UP_PAD = 128, 8              # tile size and margin (input pixels) thrown away between tiles
UP_MAX_OUT = 8000                     # longest side of the result
UP_MAX_FILES = 10
_up_lock = threading.Lock()
_up_session = None
_up_used = 0.0


def up_unload_later(delay: float = CUT_IDLE) -> None:
    def check():
        global _up_session
        with _up_lock:
            if _up_session is None:
                return
            left = CUT_IDLE - (time.time() - _up_used)
            if left > 1:
                up_unload_later(left + 1)
                return
            _up_session = None
        import gc, ctypes
        gc.collect()
        try:
            ctypes.CDLL("libc.so.6").malloc_trim(0)
        except Exception:
            pass
        log("upscale model unloaded (idle)")
    t = threading.Timer(delay, check)
    t.daemon = True
    t.start()


def up_model(jid: str | None = None):
    global _up_session, _up_used
    with _up_lock:
        _up_used = time.time()
        if _up_session is not None:
            return _up_session
        if not UP_MODEL.exists() or UP_MODEL.stat().st_size < 1_000_000:
            MODELS.mkdir(parents=True, exist_ok=True)
            log("downloading upscale model (first time only)")
            if jid:
                dl_set(jid, note="Getting the AI model ready (first time only)…")
            tmp = UP_MODEL.with_suffix(".part")
            fetch_file(UP_MODEL_URL, tmp, limit=50 * 1024 * 1024)
            tmp.replace(UP_MODEL)
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = 2
        opts.inter_op_num_threads = 1
        opts.enable_cpu_mem_arena = False
        _up_session = ort.InferenceSession(str(UP_MODEL), opts, providers=["CPUExecutionProvider"])
        log("upscale model loaded")
        return _up_session


def up_spans(n: int) -> list[tuple[int, int, int]]:
    """Tiles along one axis: (tile start, keep from, keep to) in input pixels."""
    step = UP_TILE - 2 * UP_PAD
    starts = list(range(0, max(n - UP_TILE, 0), step)) + [max(n - UP_TILE, 0)]
    starts = sorted(set(starts))
    out, prev = [], 0
    for i, s in enumerate(starts):
        end = n if i == len(starts) - 1 else min(n, s + UP_TILE - UP_PAD)
        out.append((s, prev, end))
        prev = end
    return out


def up_tiles(n_w: int, n_h: int) -> int:
    return len(up_spans(n_w)) * len(up_spans(n_h))


def up_rgb(im, tick) -> "Image.Image":
    """4x upscale of an RGB PIL image. tick() is called after every tile."""
    import numpy as np
    from PIL import Image
    sess = up_model()
    name = sess.get_inputs()[0].name
    a = np.asarray(im.convert("RGB"), dtype=np.float32) / 255.0
    h, w = a.shape[:2]
    ph, pw = max(h, UP_TILE), max(w, UP_TILE)
    if (ph, pw) != (h, w):                        # tiny pictures: pad up to one tile
        a = np.pad(a, ((0, ph - h), (0, pw - w), (0, 0)), mode="edge")
    out = np.zeros((ph * 4, pw * 4, 3), dtype=np.uint8)
    for ty, y0, y1 in up_spans(ph):
        for tx, x0, x1 in up_spans(pw):
            tile = a[ty:ty + UP_TILE, tx:tx + UP_TILE].transpose(2, 0, 1)[None]
            r = sess.run(None, {name: np.ascontiguousarray(tile)})[0][0].transpose(1, 2, 0)
            r = r[(y0 - ty) * 4:(y1 - ty) * 4, (x0 - tx) * 4:(x1 - tx) * 4]
            out[y0 * 4:y1 * 4, x0 * 4:x1 * 4] = (r.clip(0, 1) * 255 + 0.5).astype(np.uint8)
            tick()
    return Image.fromarray(out[:h * 4, :w * 4], "RGB")


def up_prepare(im):
    """Normalises the mode and shrinks inputs whose 4x result would pass UP_MAX_OUT
    (2x also runs the 4x model and halves the result afterwards)."""
    from PIL import Image
    if im.mode == "P" and "transparency" in im.info:
        im = im.convert("RGBA")
    elif im.mode not in ("RGB", "RGBA", "LA"):
        im = im.convert("RGBA" if "A" in im.getbands() else "RGB")
    if max(im.size) * 4 > UP_MAX_OUT:
        f = UP_MAX_OUT / 4 / max(im.size)
        im = im.resize((max(1, round(im.width * f)), max(1, round(im.height * f))), Image.LANCZOS)
    else:
        im.load()
    return im


def new_upscale(b: dict) -> dict:
    jid = str(b.get("id", ""))
    if not ID_RE.match(jid):
        raise ValueError("Bad id.")
    d = DL / jid
    if not d.is_dir():
        raise LookupError("Upload not found.")
    ins = cv_inputs(d)
    if not ins:
        raise ValueError("Add at least one picture.")
    if len(ins) > UP_MAX_FILES:
        raise ValueError(f"Up to {UP_MAX_FILES} pictures at once.")
    if any(p.suffix[1:] not in CV_IMG for p, _ in ins):
        raise ValueError("Only pictures work here (JPG, PNG, WebP, HEIC…).")
    scale = int(b.get("scale", 4)) if str(b.get("scale", "4")) in ("2", "4") else 4
    fmt = str(b.get("to", "jpg"))
    if fmt not in ("jpg", "png"):
        raise ValueError("Unknown format.")
    title = ins[0][1].rsplit(".", 1)[0][:90] + (f" +{len(ins) - 1}" if len(ins) > 1 else "")
    with _dl_lock:
        if jid in _dl_jobs:
            raise ValueError("Already working on it.")
        active = sum(1 for j in _dl_jobs.values() if j["state"] in ("queued", "downloading", "converting"))
        if active >= DL_MAX_ACTIVE:
            raise ValueError("Lots going on already. Wait for a job to finish.")
        job = {"id": jid, "kind": "upscale", "url": "", "platform": "upload", "format": fmt,
               "opts": {"scale": scale, "count": len(ins), "orig": sum(p.stat().st_size for p, _ in ins)},
               "state": "queued", "progress": 0, "created": int(time.time() * 1000), "title": title,
               "file": "", "size": 0, "thumb": False}
        _dl_jobs[jid] = job
    dl_save()
    _dl_q.put(jid)
    log(f"upscale {jid}: {len(ins)} picture(s), {scale}x {fmt}")
    return dict(job)


def run_upscale(jid: str) -> None:
    import zipfile
    from PIL import Image
    with _dl_lock:
        job = dict(_dl_jobs.get(jid) or {})
    if not job:
        return
    d = DL / jid
    scale, fmt = job["opts"]["scale"], job["format"]
    ins = cv_inputs(d)
    work = d / "work"
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir()
    dl_set(jid, save=True, state="converting", progress=0.01)
    up_model(jid)
    up_unload_later()
    dl_set(jid, note="")
    # Open everything first so the progress bar can count tiles over all pictures.
    pics = []
    for p, name in ins:
        im = up_prepare(cv_open(p))
        pics.append((im, name))
    total = sum(up_tiles(max(im.width, UP_TILE), max(im.height, UP_TILE)) for im, _ in pics) or 1
    done = [0]

    def tick():
        done[0] += 1
        if done[0] % 4 == 0 or done[0] == total:
            dl_set(jid, progress=round(0.02 + 0.95 * done[0] / total, 3))

    outs, used, sizes = [], set(), []
    for im, name in pics:
        alpha = im.getchannel("A") if im.mode in ("RGBA", "LA") else None
        if alpha is not None and alpha.getextrema()[0] == 255:   # fully opaque: no need to carry it
            alpha = None
        big = up_rgb(im, tick)
        if alpha is not None:                    # cut-outs keep their transparency
            big.putalpha(alpha.resize(big.size, Image.LANCZOS))
        if scale == 2:
            big = big.resize((max(1, big.width // 2), max(1, big.height // 2)), Image.LANCZOS)
        stem = name.rsplit(".", 1)[0] + f" ({scale}x)"
        fname = clean_name(stem, fmt)
        k = 2
        while fname.lower() in used:
            fname = clean_name(f"{stem} {k}", fmt)
            k += 1
        used.add(fname.lower())
        out = work / fname
        if fmt == "png":
            big.save(out, "PNG", compress_level=6)
        else:
            if big.mode == "RGBA":
                flat = Image.new("RGB", big.size, (255, 255, 255))
                flat.paste(big, mask=big.getchannel("A"))
                big = flat
            big.save(out, "JPEG", quality=93, subsampling=0)
        sizes.append(f"{im.width}×{im.height} → {big.width}×{big.height}")
        outs.append(out)
        del big
    if len(outs) == 1:
        final = d / outs[0].name
        outs[0].replace(final)
        first = final
    else:
        final = d / clean_name(f"{ins[0][1].rsplit('.', 1)[0]} {scale}x ({len(outs)} files)", "zip")
        with zipfile.ZipFile(final, "w", zipfile.ZIP_STORED) as z:
            for x in outs:
                z.write(x, x.name)
        first = outs[0]
    thumb = d / "thumb.jpg"
    try:
        with Image.open(first) as t:
            t.draft("RGB", (960, 960))
            t = t.convert("RGB")
            t.thumbnail((480, 480), Image.LANCZOS)
            t.save(thumb, "JPEG", quality=85)
    except Exception:
        pass
    for p in d.iterdir():
        if p not in (final, thumb) and p.name != "names.json":
            shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
    dl_set(jid, save=True, state="done", progress=1, file=final.name, size=final.stat().st_size, thumb=thumb.exists(),
           outputs=len(outs), dims=sizes[0] if sizes else "", finished=int(time.time() * 1000))
    log(f"upscale {jid} done: {final.name} ({sizes[0] if sizes else ''})")


def dl_worker() -> None:
    while True:
        jid = _dl_q.get()
        try:
            with _dl_lock:
                kind = (_dl_jobs.get(jid) or {}).get("kind")
            {"compress": run_compress, "convert": run_convert, "cutout": run_cutout, "upscale": run_upscale}.get(kind, run_download)(jid)
        except Exception as e:
            log(f"download {jid} failed: {e}")
            dl_set(jid, save=True, state="failed", error=str(e)[:220], speed="", eta="")
            with _dl_lock:
                still = jid in _dl_jobs
            if not still:
                shutil.rmtree(DL / jid, ignore_errors=True)


def delete_download(jid: str) -> None:
    with _dl_lock:
        if _dl_jobs.pop(jid, None) is None:
            raise LookupError("Download not found.")
    p = _dl_procs.get(jid)
    if p and p.poll() is None:
        p.kill()
    shutil.rmtree(DL / jid, ignore_errors=True)
    dl_save()


def dl_cleanup() -> None:
    now = time.time() * 1000
    with _dl_lock:
        jobs = sorted(_dl_jobs.values(), key=lambda j: j["created"], reverse=True)
    total, drop = 0, []
    for j in jobs:
        if j["state"] not in ("done", "failed"):
            continue
        total += j.get("size") or 0
        if now - j["created"] > DL_KEEP * 1000 or total > DL_MAX_TOTAL:
            drop.append(j["id"])
    for jid in drop:
        try:
            delete_download(jid)
        except LookupError:
            pass
    # Uploads that never became a job (tab closed mid-batch) go after a day.
    with _dl_lock:
        known = set(_dl_jobs)
    for p in DL.iterdir() if DL.is_dir() else []:
        if p.is_dir() and ID_RE.match(p.name) and p.name not in known and time.time() - p.stat().st_mtime > 86400:
            shutil.rmtree(p, ignore_errors=True)


def dl_list(kind: str = "download") -> dict:
    with _dl_lock:
        jobs = sorted((dict(j) for j in _dl_jobs.values() if j.get("kind", "download") == kind),
                      key=lambda j: j["created"], reverse=True)
    return {"jobs": jobs, "total": sum(j.get("size") or 0 for j in jobs), "keep_days": DL_KEEP // 86400}


# =========================== office file preview ===========================
# PowerPoint, Word and Excel files shared from the File Host get a preview:
# LibreOffice turns them into a PDF, poppler turns the pages into JPEGs.
# The pictures sit next to the file in /files/<id>/.preview/, so they're
# deleted together with it and nginx serves them under /f/<id>/.preview/.
PV_EXT = re.compile(r"\.(pptx|ppt|pptm|ppsx|pps|odp|docx|doc|docm|odt|rtf|xlsx|xls|xlsm|ods|pdf|heic|heif)$", re.I)
PV_ID = re.compile(r"^[A-Za-z0-9_-]{12}$")
PV_MAX_PAGES = 40
PV_MAX_SIZE = 200 * 1024 * 1024
_pv_lock = threading.Lock()
_pv_busy: set[str] = set()
_pv_one = threading.Semaphore(1)  # one LibreOffice at a time (shared profile, RAM)


def _pv_sig(src: Path) -> str:
    st = src.stat()
    return f"{st.st_size}-{int(st.st_mtime)}"


def _pv_meta(fid: str) -> dict:
    try:
        return json.loads((FILES / fid / ".preview" / "meta.json").read_text())
    except (OSError, ValueError):
        return {}


def office_preview(body: dict) -> dict:
    fid, name = str(body.get("id", "")), str(body.get("name", ""))
    if not PV_ID.match(fid) or not name or "/" in name or "\\" in name or name in (".", "..") or name.startswith("."):
        raise ValueError("Invalid file.")
    if not PV_EXT.search(name):
        return {"state": "none"}
    src = FILES / fid / name
    if not src.is_file():
        raise LookupError("File not found.")
    if src.stat().st_size > PV_MAX_SIZE:
        return {"state": "error"}
    sig = _pv_sig(src)
    meta = _pv_meta(fid)
    if meta.get("sig") == sig and meta.get("state") in ("ready", "error"):
        return meta
    with _pv_lock:
        if fid not in _pv_busy:
            _pv_busy.add(fid)
            threading.Thread(target=_pv_run, args=(fid, src, sig), daemon=True).start()
    return {"state": "working"}


def _pv_write(fid: str, meta: dict, pages: list[Path]) -> None:
    out, tmp = FILES / fid / ".preview", FILES / fid / ".preview.tmp"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    for i, p in enumerate(pages, 1):
        shutil.move(str(p), tmp / f"{i}.jpg")
    (tmp / "meta.json").write_text(json.dumps(meta))
    shutil.rmtree(out, ignore_errors=True)
    tmp.rename(out)


def _pv_run(fid: str, src: Path, sig: str) -> None:
    work = Path(f"/tmp/pv-{fid}")
    try:
        with _pv_one:
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir()
            inp = work / ("in" + src.suffix.lower())
            shutil.copyfile(src, inp)
            if inp.suffix in (".heic", ".heif"):
                # iPhone photos: one picture, turned upright, as JPEG
                from PIL import Image, ImageOps
                import pillow_heif
                pillow_heif.register_heif_opener()
                with Image.open(inp) as im:
                    im = ImageOps.exif_transpose(im).convert("RGB")
                    im.thumbnail((2000, 2000))
                    im.save(work / "p-1.jpg", "JPEG", quality=85)
                _pv_write(fid, {"state": "ready", "sig": sig, "pages": 1, "total": 1}, [work / "p-1.jpg"])
                return
            if inp.suffix == ".pdf":
                inp.rename(work / "in.pdf")
            else:
                subprocess.run(["nice", "-n", "15", "soffice", "--headless", "--norestore", "--nolockcheck",
                                "-env:UserInstallation=file:///tmp/lo-profile", "--convert-to", "pdf",
                                "--outdir", str(work), str(inp)], capture_output=True, timeout=300)
            pdf = work / "in.pdf"
            if not pdf.is_file():
                raise RuntimeError("LibreOffice made no PDF")
            subprocess.run(["nice", "-n", "15", "pdftoppm", "-jpeg", "-jpegopt", "quality=82", "-scale-to", "1400",
                            "-l", str(PV_MAX_PAGES), str(pdf), str(work / "p")], capture_output=True, timeout=300, check=True)
            pages = sorted(work.glob("p-*.jpg"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
            if not pages:
                raise RuntimeError("no pages")
            total = len(pages)
            try:
                from pypdf import PdfReader
                total = len(PdfReader(str(pdf)).pages)
            except Exception:
                pass
            _pv_write(fid, {"state": "ready", "sig": sig, "pages": len(pages), "total": total}, pages)
    except Exception as e:  # noqa: BLE001 - any failure just means "no preview"
        print(f"preview {fid}: {e}", file=sys.stderr)
        try:
            if (FILES / fid).is_dir():
                _pv_write(fid, {"state": "error", "sig": sig}, [])
        except OSError:
            pass
    finally:
        shutil.rmtree(work, ignore_errors=True)
        with _pv_lock:
            _pv_busy.discard(fid)


# ================================= http =================================
live_slots = threading.BoundedSemaphore(2)   # live camera viewers at the same time


class Handler(BaseHTTPRequestHandler):
    server_version = "media"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def send_json(self, status: int, data) -> None:
        body = json.dumps(data).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        if n > 8192:
            raise ValueError("Request is too large.")
        try:
            return json.loads(self.rfile.read(n) or b"{}")
        except json.JSONDecodeError:
            raise ValueError("Invalid JSON.")

    def handle_any(self, method: str) -> None:
        u = urlparse(self.path)
        parts = [p for p in u.path.split("/") if p]
        try:
            if parts[:1] == ["internal"]:
                if not TOKEN or not hmac.compare_digest(self.headers.get("X-Media-Token", ""), TOKEN):
                    return self.send_json(403, {"error": "Forbidden."})
                return self.internal(method, parts[1:], parse_qs(u.query))
            if parts[:1] == ["radio"] and len(parts) == 3 and method in ("GET", "HEAD"):
                return self.public(parts[1], parts[2], method)
            self.send_json(404, {"error": "Not found."})
        except LookupError as e:
            self.send_json(404, {"error": str(e)})
        except ValueError as e:
            self.send_json(400, {"error": str(e)})
        except RuntimeError as e:
            self.send_json(422, {"error": str(e)})
        except (BrokenPipeError, ConnectionResetError):
            pass

    do_GET = lambda self: self.handle_any("GET")
    do_HEAD = lambda self: self.handle_any("HEAD")
    do_POST = lambda self: self.handle_any("POST")
    do_PATCH = lambda self: self.handle_any("PATCH")
    do_DELETE = lambda self: self.handle_any("DELETE")

    # ---- live camera: pictures in (chunked body), H.264 out, nothing on disk ----
    def live(self, qs: dict) -> None:
        self.close_connection = True
        if not live_slots.acquire(blocking=False):
            return self.send_json(503, {"error": "Too many people are watching."})
        proc = None
        try:
            rate = 3000 if qs.get("hd") == ["1"] else 1400
            proc = subprocess.Popen(
                ["ffmpeg", "-hide_banner", "-loglevel", "error", "-probesize", "32", "-analyzeduration", "0", "-fflags", "nobuffer", "-flags", "low_delay",
                 "-use_wallclock_as_timestamps", "1", "-f", "mjpeg", "-i", "pipe:0", "-an", "-fps_mode", "passthrough",
                 "-vf", "scale=in_range=pc:out_range=tv,format=yuv420p", "-c:v", "libx264", "-preset", "superfast", "-tune", "zerolatency",
                 "-profile:v", "baseline", "-level", "3.1", "-b:v", f"{rate}k", "-maxrate", f"{rate + 400}k", "-bufsize", f"{rate // 2}k",
                 "-g", "30", "-x264-params", "aud=1:repeat-headers=1:scenecut=0", "-flush_packets", "1", "-f", "h264", "pipe:1"],
                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0)

            def feed() -> None:
                try:
                    while True:
                        size = int(self.rfile.readline(32).split(b";")[0] or b"0", 16)
                        if not 0 < size <= 8_000_000:
                            break
                        left = size
                        while left:
                            part = self.rfile.read(min(left, 65536))
                            if not part:
                                return
                            proc.stdin.write(part)
                            left -= len(part)
                        self.rfile.readline(8)
                except (OSError, ValueError):
                    pass
                finally:
                    try:
                        proc.stdin.close()
                    except OSError:
                        pass

            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Connection", "close")
            self.end_headers()
            threading.Thread(target=feed, daemon=True).start()
            while True:
                part = proc.stdout.read(65536)
                if not part:
                    break
                self.wfile.write(part)
        finally:
            if proc:
                proc.kill()
                proc.wait()
            live_slots.release()

    # ---- panel API ----
    def internal(self, m: str, p: list[str], qs: dict) -> None:
        if p == ["stations"] and m == "GET":
            d = load_db()
            return self.send_json(200, {"key": d["key"], "stations": [station_view(s, True) for s in d["stations"]]})
        if p == ["stations"] and m == "POST":
            return self.send_json(200, station_view(add_station(str(self.read_json().get("url", ""))), True))
        if p == ["key"] and m == "POST":
            return self.send_json(200, {"key": rotate_key()})
        if len(p) >= 2 and p[0] == "stations" and ID_RE.match(p[1]):
            if len(p) == 2 and m == "PATCH":
                return self.send_json(200, station_view(update_station(p[1], str(self.read_json().get("title", "")).strip()), True))
            if len(p) == 2 and m == "DELETE":
                delete_station(p[1])
                return self.send_json(200, {"ok": True})
            if p[2:] == ["move"] and m == "POST":
                move_station(p[1], self.read_json().get("dir") == "up")
                return self.send_json(200, {"ok": True})
        if p == ["live"] and m == "POST":
            return self.live(qs)
        if p == ["downloads"] and m == "GET":
            kind = (qs.get("kind") or ["download"])[0]
            return self.send_json(200, dl_list(kind if kind in ("compress", "convert", "cutout", "upscale") else "download"))
        if p == ["cutout"] and m == "POST":
            return self.send_json(200, new_cutout(self.read_json()))
        if p == ["upscale"] and m == "POST":
            return self.send_json(200, new_upscale(self.read_json()))
        if p == ["compress"] and m == "POST":
            return self.send_json(200, new_compress(self.read_json()))
        if p == ["convert"] and m == "POST":
            return self.send_json(200, new_convert(self.read_json()))
        if p == ["preview"] and m == "POST":
            return self.send_json(200, office_preview(self.read_json()))
        if p == ["downloads"] and m == "POST":
            return self.send_json(200, new_download(self.read_json()))
        if len(p) == 2 and p[0] == "downloads" and ID_RE.match(p[1]):
            if m == "DELETE":
                delete_download(p[1])
                return self.send_json(200, {"ok": True})
            if m == "GET":
                with _dl_lock:
                    j = _dl_jobs.get(p[1])
                if not j:
                    raise LookupError("Download not found.")
                return self.send_json(200, dict(j))
        if len(p) == 3 and p[0] == "clips" and ID_RE.match(p[1]) and p[2] == "process" and m == "POST":
            if not enqueue_clip(p[1], force=qs.get("force") == ["1"]):
                raise LookupError("Clip not found.")
            return self.send_json(200, {"ok": True, "queued": True})
        self.send_json(404, {"error": "Not found."})

    # ---- public radio ----
    def public(self, key: str, name: str, method: str) -> None:
        d = load_db()
        if not KEY_RE.match(key) or not hmac.compare_digest(key, d["key"]):
            return self.send_json(404, {"error": "This radio link doesn't exist (anymore)."})
        if name == "stations.json":
            return self.send_json(200, {"stations": [station_view(s, False) for s in d["stations"]]})
        m = re.match(r"^([A-Za-z0-9_-]{10})\.(mp3|jpg)$", name)
        if not m or not any(s["id"] == m.group(1) for s in d["stations"]):
            return self.send_json(404, {"error": "Not found."})
        sid, kind = m.groups()
        if kind == "jpg":
            f = MUSIC / f"{sid}.jpg"
            if not f.exists():
                return self.send_json(404, {"error": "No cover."})
            data = f.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "image/jpeg")
            self.send_header("Cache-Control", "public, max-age=86400")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if method == "GET":
                self.wfile.write(data)
            return
        if method == "HEAD":
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.end_headers()
            return
        self.stream(sid)

    def stream(self, sid: str) -> None:
        try:
            r, q = attach(sid)
        except Busy:
            return self.send_json(503, {"error": "Too many listeners right now."})
        self.close_connection = True
        try:
            self.send_response(200)
            self.send_header("Content-Type", "audio/mpeg")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("icy-name", r.st["title"][:60].encode("ascii", "ignore").decode())
            self.end_headers()
            first = True
            while True:
                try:
                    # Starting a station can take a few seconds (YouTube lookup).
                    chunk = q.get(timeout=60 if first else 30)
                except queue.Empty:
                    break
                if chunk is None:
                    break
                first = False
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, OSError):
            pass
        finally:
            detach(r, q)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


def main() -> None:
    if not TOKEN:
        log("MEDIA_TOKEN is not set; the panel can't manage anything.")
    MUSIC.mkdir(parents=True, exist_ok=True)
    with _db_lock:
        save_db(load_db())         # creates the radio key on first start
    threading.Thread(target=update_ytdlp, daemon=True).start()
    threading.Thread(target=reaper, daemon=True).start()
    threading.Thread(target=clip_worker, daemon=True).start()
    threading.Thread(target=backfill_clips, daemon=True).start()
    dl_load()
    for _ in range(2):
        threading.Thread(target=dl_worker, daemon=True).start()
    log("ffmpeg", "ok" if shutil.which("ffmpeg") else "MISSING")
    log("listening on :8000")
    Server(("0.0.0.0", 8000), Handler).serve_forever()


if __name__ == "__main__":
    main()
