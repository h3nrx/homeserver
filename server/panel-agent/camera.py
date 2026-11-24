#!/usr/bin/env python3
"""
Live camera for the admin panel. Off by default: panel-agent starts it
(without root rights) only while the file ~/panel-agent/camera.on exists.

Talks HTTP on a unix socket that only the panel can reach:
  GET /scan                                  list of cameras
  GET /frame?dev=video0&hd=0&after=<seq>     newest JPEG (waits for a new one)
  GET /controls?dev=video0                   picture controls and their ranges
  GET /set?dev=video0&name=sharpness&value=9 change one of them

The camera is open only while frames are being asked for and is closed a few
seconds after the last request. Nothing is recorded, there is no sound.
Pictures are passed on exactly as the camera makes them (MJPEG).
"""
from __future__ import annotations

import ctypes
import errno
import fcntl
import json
import mmap
import os
import re
import select
import socketserver
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

SOCK = os.environ.get("CAMERA_SOCK", "/var/lib/panel/camera/camera.sock")
SYS = Path("/sys/class/video4linux")
DEV = re.compile(r"^video\d{1,3}$")
IDLE = 4.0          # seconds without a viewer until the camera goes off
SIZES = {False: (640, 480), True: (1280, 720)}
FPS = 30

# ---- V4L2 (64-bit layout) ----
MJPG = 0x47504A4D


def _ioc(d: int, nr: int, size: int) -> int:
    return (d << 30) | (size << 16) | (ord("V") << 8) | nr


QUERYCAP = _ioc(2, 0, 104)
ENUM_FMT = _ioc(3, 2, 64)
S_FMT = _ioc(3, 5, 208)
REQBUFS = _ioc(3, 8, 20)
QUERYBUF = _ioc(3, 9, 88)
QBUF = _ioc(3, 15, 88)
DQBUF = _ioc(3, 17, 88)
STREAMON = _ioc(1, 18, 4)
STREAMOFF = _ioc(1, 19, 4)
S_PARM = _ioc(3, 22, 204)
QUERYCTRL = _ioc(3, 36, 68)
G_CTRL = _ioc(3, 27, 8)
S_CTRL = _ioc(3, 28, 8)
# set on every start: 50 Hz light (cameras sold for the US flicker here) and
# no halving of the frame rate in dim light
PRESET = ((0x980918, 1), (0x9A0903, 0))
CONTROLS = {"brightness": 0x980900, "contrast": 0x980901, "saturation": 0x980902, "sharpness": 0x98091B,
            "backlight": 0x98091C, "zoom": 0x9A090D}
CAPTURE = 1
MMAP = 1
BUF_ERROR = 0x40

# Many webcams leave out the standard Huffman tables; browsers need them.
DHT = bytes.fromhex(
    "ffc4001f0000010501010101010100000000000000000102030405060708090a0bffc400b510000201030302040305050404"
    "0000017d01020300041105122131410613516107227114328191a1082342b1c11552d1f02433627282090a161718191a2526"
    "2728292a3435363738393a434445464748494a535455565758595a636465666768696a737475767778797a83848586878889"
    "8a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5c6c7c8c9cad2d3d4d5d6d7d8d9dae1e2e3e4"
    "e5e6e7e8e9eaf1f2f3f4f5f6f7f8f9faffc4001f0100030101010101010101010000000000000102030405060708090a0bff"
    "c400b51100020102040403040705040400010277000102031104052131061241510761711322328108144291a1b1c1092333"
    "52f0156272d10a162434e125f11718191a262728292a35363738393a434445464748494a535455565758595a636465666768"
    "696a737475767778797a82838485868788898a92939495969798999aa2a3a4a5a6a7a8a9aab2b3b4b5b6b7b8b9bac2c3c4c5"
    "c6c7c8c9cad2d3d4d5d6d7d8d9dae2e3e4e5e6e7e8e9eaf2f3f4f5f6f7f8f9fa")


def complete_jpeg(b: bytes) -> bytes | None:
    if len(b) < 200 or b[:2] != b"\xff\xd8":
        return None
    end = b.rfind(b"\xff\xd9")
    if end < 0:
        return None
    b = b[: end + 2]
    i = 2
    while i + 4 <= len(b) and b[i] == 0xFF:
        mark = b[i + 1]
        if mark == 0xC4:
            return b
        if mark == 0xDA:
            return b[:i] + DHT + b[i:]
        i += 2 + struct.unpack(">H", b[i + 2:i + 4])[0]
    return None


def can_capture(dev: str) -> tuple[bool, bool, str]:
    """(is a camera, has MJPEG, bus) without starting it."""
    fd = os.open(f"/dev/{dev}", os.O_RDWR | os.O_NONBLOCK)
    try:
        cap = bytearray(104)
        fcntl.ioctl(fd, QUERYCAP, cap)
        caps, dcaps = struct.unpack_from("<II", cap, 84)
        have = dcaps if caps & 0x80000000 else caps
        if not (have & 0x1 and have & 0x04000000):
            return False, False, ""
        fmts = []
        for n in range(32):
            f = bytearray(64)
            struct.pack_into("<II", f, 0, n, CAPTURE)
            try:
                fcntl.ioctl(fd, ENUM_FMT, f)
            except OSError:
                break
            fmts.append(struct.unpack_from("<I", f, 44)[0])
        return bool(fmts), MJPG in fmts, cap[48:80].split(b"\0")[0].decode("ascii", "replace")
    finally:
        os.close(fd)


def scan() -> list[dict]:
    out = []
    if not SYS.is_dir():
        return out
    for p in sorted((x for x in SYS.iterdir() if DEV.match(x.name)), key=lambda x: int(x.name[5:])):
        try:
            name = (p / "name").read_text(errors="replace").strip()
            cam, mjpg, _ = can_capture(p.name)
        except OSError as e:
            if e.errno in (errno.EACCES, errno.EPERM):
                out.append({"id": p.name, "name": p.name, "where": "", "ok": False})
            continue
        if not cam:
            continue
        # "HP HD Webcam: HP HD Webcam" -> "HP HD Webcam"
        head, _, rest = name.partition(": ")
        if rest and (head.startswith(rest) or rest.startswith(head)):
            name = head
        removable = ""
        try:
            removable = (p / "device").resolve().parent.joinpath("removable").read_text().strip()
        except OSError:
            pass
        out.append({"id": p.name, "name": name[:60], "where": {"fixed": "built-in", "removable": "usb"}.get(removable, ""), "ok": mjpg})
    return out


def ctrl_range(fd: int, cid: int) -> tuple[int, int, int, int] | None:
    """(min, max, step, default), or None when the camera doesn't have it."""
    b = bytearray(68)
    struct.pack_into("<I", b, 0, cid)
    try:
        fcntl.ioctl(fd, QUERYCTRL, b)
    except OSError:
        return None
    lo, hi, step, std, flags = struct.unpack_from("<iiiiI", b, 40)
    return None if flags & 0x1 or lo >= hi else (lo, hi, max(step, 1), std)


def controls(dev: str, name: str = "", value: int = 0) -> list[dict]:
    fd = os.open(f"/dev/{dev}", os.O_RDWR | os.O_NONBLOCK)
    try:
        out = []
        for key, cid in CONTROLS.items():
            r = ctrl_range(fd, cid)
            if not r:
                continue
            try:
                if key == name:
                    fcntl.ioctl(fd, S_CTRL, bytearray(struct.pack("<Ii", cid, min(max(value, r[0]), r[1]))))
                b = bytearray(struct.pack("<Ii", cid, 0))
                fcntl.ioctl(fd, G_CTRL, b)
            except OSError:
                continue
            out.append({"name": key, "min": r[0], "max": r[1], "step": r[2], "default": r[3], "value": struct.unpack("<Ii", b)[1]})
        return out
    finally:
        os.close(fd)


class Cam:
    def __init__(self, dev: str):
        self.dev = dev
        self.cond = threading.Condition()
        self.seq = 0
        self.jpeg = b""
        self.err = ""
        self.last = 0.0
        self.hd_until = 0.0
        self.running = False

    def frame(self, after: int, hd: bool) -> tuple[int, bytes]:
        with self.cond:
            now = time.monotonic()
            self.last = now
            if hd:
                self.hd_until = now + IDLE
            if not self.running:
                self.running, self.err, self.jpeg = True, "", b""
                threading.Thread(target=self.run, daemon=True).start()
                after = self.seq
            if after > self.seq:
                after = self.seq
            end = now + 6
            while not (self.jpeg and self.seq != after) and not self.err:
                left = end - time.monotonic()
                if left <= 0:
                    raise TimeoutError("The camera isn't sending a picture.")
                self.cond.wait(left)
            if self.err:
                raise RuntimeError(self.err)
            return self.seq, self.jpeg

    def run(self) -> None:
        err = ""
        try:
            while self.wanted():
                self.capture(time.monotonic() < self.hd_until)
        except OSError as e:
            err = {errno.EBUSY: "Another program is using this camera.",
                   errno.ENODEV: "The camera was unplugged.", errno.ENOENT: "The camera was unplugged.",
                   errno.EACCES: "No permission to open this camera."}.get(e.errno, "The camera stopped working.")
            print(f"camera {self.dev}: {e}", file=sys.stderr, flush=True)
        except Exception as e:  # noqa: BLE001
            err = "The camera stopped working."
            print(f"camera {self.dev}: {e!r}", file=sys.stderr, flush=True)
        with self.cond:
            self.running, self.err, self.jpeg = False, err, b""
            self.cond.notify_all()

    def wanted(self) -> bool:
        return time.monotonic() - self.last < IDLE

    def capture(self, hd: bool) -> None:
        fd = os.open(f"/dev/{self.dev}", os.O_RDWR | os.O_NONBLOCK)
        maps: list[mmap.mmap] = []
        on = False
        kind = bytearray(struct.pack("<i", CAPTURE))
        try:
            for cid, value in PRESET:
                try:
                    fcntl.ioctl(fd, S_CTRL, bytearray(struct.pack("<Ii", cid, value)))
                except OSError:
                    pass
            w, h = SIZES[hd]
            fmt = bytearray(208)
            struct.pack_into("<I", fmt, 0, CAPTURE)
            struct.pack_into("<IIII", fmt, 8, w, h, MJPG, 1)
            fcntl.ioctl(fd, S_FMT, fmt)
            if struct.unpack_from("<I", fmt, 16)[0] != MJPG:
                raise OSError(errno.EINVAL, "no MJPEG")
            parm = bytearray(204)
            struct.pack_into("<IIIII", parm, 0, CAPTURE, 0, 0, 1, FPS)
            try:
                fcntl.ioctl(fd, S_PARM, parm)
            except OSError:
                pass
            req = bytearray(struct.pack("<IIIII", 4, CAPTURE, MMAP, 0, 0))
            fcntl.ioctl(fd, REQBUFS, req)
            for n in range(struct.unpack_from("<I", req, 0)[0]):
                b = self.buf(n)
                fcntl.ioctl(fd, QUERYBUF, b)
                off, = struct.unpack_from("<I", b, 64)
                length, = struct.unpack_from("<I", b, 72)
                maps.append(mmap.mmap(fd, length, mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE, offset=off))
                fcntl.ioctl(fd, QBUF, self.buf(n))
            fcntl.ioctl(fd, STREAMON, kind)
            on = True
            misses = 0
            poll = select.poll()
            poll.register(fd, select.POLLIN)
            while self.wanted() and (time.monotonic() < self.hd_until) == hd:
                got = poll.poll(1000)
                if not got:
                    misses += 1
                    if misses >= 5:
                        raise OSError(errno.EIO, "no frames")
                    continue
                b = self.buf(0)
                try:
                    fcntl.ioctl(fd, DQBUF, b)
                except BlockingIOError:
                    if got[0][1] & (select.POLLERR | select.POLLHUP):
                        raise OSError(errno.ENODEV, "gone") from None
                    continue
                n, _, used, flags = struct.unpack_from("<IIII", b, 0)
                misses = 0
                pic = complete_jpeg(bytes(maps[n][:used])) if not flags & BUF_ERROR else None
                fcntl.ioctl(fd, QBUF, self.buf(n))
                if pic:
                    with self.cond:
                        self.seq += 1
                        self.jpeg = pic
                        self.cond.notify_all()
        finally:
            if on:
                try:
                    fcntl.ioctl(fd, STREAMOFF, kind)
                except OSError:
                    pass
            for m in maps:
                m.close()
            os.close(fd)

    @staticmethod
    def buf(n: int) -> bytearray:
        b = bytearray(88)
        struct.pack_into("<II", b, 0, n, CAPTURE)
        struct.pack_into("<I", b, 60, MMAP)
        return b


cams: dict[str, Cam] = {}
cams_lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    timeout = 30

    def address_string(self) -> str:
        return "panel"

    def log_message(self, *a) -> None:
        pass

    def reply(self, status: int, body: bytes, ctype: str = "application/json", extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/scan":
                return self.reply(200, json.dumps({"cameras": scan()}).encode())
            if u.path in ("/controls", "/set"):
                dev, name, value = q.get("dev", ""), q.get("name", ""), q.get("value", "0")
                if not DEV.match(dev) or not re.fullmatch(r"-?\d{1,6}", value):
                    return self.reply(404, b'{"error": "The camera was unplugged."}')
                try:
                    got = controls(dev, name if u.path == "/set" else "", int(value))
                except OSError:
                    return self.reply(503, b'{"error": "The camera was unplugged."}')
                return self.reply(200, json.dumps({"controls": got}).encode())
            if u.path == "/frame":
                dev, after = q.get("dev", ""), q.get("after", "0")
                if not DEV.match(dev) or not after.isdigit() or not (SYS / dev).exists():
                    return self.reply(404, b'{"error": "The camera was unplugged."}')
                with cams_lock:
                    cam = cams.setdefault(dev, Cam(dev))
                seq, pic = cam.frame(int(after[:12]), q.get("hd") == "1")
                return self.reply(200, pic, "image/jpeg", {"X-Frame": str(seq)})
            self.reply(404, b'{"error": "Not found."}')
        except (TimeoutError, RuntimeError) as e:
            self.reply(503, json.dumps({"error": str(e)}).encode())
        except (BrokenPipeError, ConnectionResetError):
            pass


class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 32


def main() -> None:
    if ctypes.sizeof(ctypes.c_void_p) != 8:
        sys.exit("camera: 64-bit only")
    try:
        os.unlink(SOCK)
    except FileNotFoundError:
        pass
    os.umask(0o077)
    with Server(SOCK, Handler) as s:
        print("camera: ready", flush=True)
        s.serve_forever()


if __name__ == "__main__":
    main()
