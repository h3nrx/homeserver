"use strict";
/*
 * __BRAND__ admin panel API.
 * Talks to Minecraft over RCON and to the host-side panel-agent through a
 * small file mailbox (system stats, systemd units, Discord bots).
 */
const http = require("http");
const crypto = require("crypto");
const fs = require("fs");
const fsp = fs.promises;
const path = require("path");
const net = require("net");
const zlib = require("zlib");
const { Transform } = require("stream");
const { pipeline } = require("stream/promises");

const { RCON_PASSWORD, SERVER_ADDRESS = "", LOCAL_ADDRESS = "" } = process.env;
const RCON_HOST = process.env.RCON_HOST || "minecraft";
const RCON_PORT = +(process.env.RCON_PORT || 25575);
const AUTH_FILE = process.env.AUTH_FILE || "/data/auth.json";

const IPC_DIR = process.env.IPC_DIR || "/ipc";
const IPC_CMD = path.join(IPC_DIR, "commands");
const IPC_RESP = path.join(IPC_DIR, "responses");
const IPC_STATS = path.join(IPC_DIR, "stats.json");
const IPC_POWER = path.join(IPC_DIR, "power.json");

const BOTS_DIR = process.env.BOTS_DIR || "/bots";
const FILES_DIR = process.env.FILES_DIR || "/files";
const FILES_INDEX = path.join(FILES_DIR, "index.json");

const LOG_FILE = process.env.LOG_FILE || "/mc/logs/latest.log";
const RESET_FILE = process.env.RESET_FILE || "/control/reset-seed";

const SESSION_MS = 12 * 60 * 60 * 1000;
const WINDOW_MS = 15 * 60 * 1000;
const MAX_FAILS = 5;            // wrong passwords per visitor per window
const MAX_FAILS_GLOBAL = 40;    // all visitors together, against spread-out attacks

const NAME = /^[A-Za-z0-9_]{3,16}$/;
const REASON = /^[^\x00-\x1f\x7f§]{0,100}$/;
const SEED = /^[A-Za-z0-9_-]{1,32}$/;
const CONTROL = /[\x00-\x1f\x7f]/;
const BOT_ID = /^[a-z0-9][a-z0-9_-]{1,31}$/;
const SEGMENT = /^[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}$/;
const ENV_KEY = /^[A-Za-z_][A-Za-z0-9_]{0,63}$/;
const FILE_ID = /^[A-Za-z0-9_-]{12}$/;

const LOG_BYTES = 96 * 1024;
const LOG_LINES = 300;
const LOG_NOISE = /RCON (Client|Listener)|Rcon issued server command: \/minecraft:/;

const MAX_EDIT_BYTES = 256 * 1024;
const MAX_BOT_UPLOAD = 25 * 1024 * 1024;
const MAX_ZIP_ENTRIES = 2000;
const MAX_ZIP_TOTAL = 100 * 1024 * 1024;
const MAX_UPLOAD_BYTES = 2 * 1024 * 1024 * 1024;
const SKIP_DIRS = new Set([".venv", "__pycache__", ".git", "node_modules"]);
const RESERVED_TOP = new Set(["bot.log", "meta.json"]);
const MAIN_CANDIDATES = ["main.py", "bot.py", "app.py", "index.py", "run.py"];

const sessions = new Map();
let fails = [];
const failsByIp = new Map();
// nginx sets X-Real-IP from the Funnel's X-Forwarded-For; the panel port is not published.
const clientIp = (req) => String(req.headers["x-real-ip"] || req.socket.remoteAddress || "?").slice(0, 64);

class HttpError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}
const fail = (status, message) => { throw new HttpError(status, message); };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const pct = (a, b) => (b ? Math.round((a / b) * 100) : 0);

/* =============================== RCON =============================== */
function rcon(command) {
  return new Promise((resolve, reject) => {
    const sock = net.connect({ host: RCON_HOST, port: RCON_PORT });
    let buf = Buffer.alloc(0), authed = false, out = "", idle = null, finished = false;
    const timer = setTimeout(() => done(new Error("rcon timeout")), 6000);
    const clean = () => out.replace(/§./g, "").trim();
    function done(err, value) {
      if (finished) return;
      finished = true;
      clearTimeout(timer);
      clearTimeout(idle);
      sock.destroy();
      if (err) { err.rcon = true; reject(err); } else resolve(value);
    }
    function sendPacket(id, type, body) {
      const b = Buffer.from(body, "utf8");
      const p = Buffer.alloc(14 + b.length);
      p.writeInt32LE(10 + b.length, 0);
      p.writeInt32LE(id, 4);
      p.writeInt32LE(type, 8);
      b.copy(p, 12);
      sock.write(p);
    }
    sock.on("connect", () => sendPacket(1, 3, RCON_PASSWORD || ""));
    sock.on("error", (e) => done(e));
    sock.on("close", () => (authed ? done(null, clean()) : done(new Error("rcon closed"))));
    sock.on("data", (d) => {
      buf = Buffer.concat([buf, d]);
      while (buf.length >= 4) {
        const len = buf.readInt32LE(0);
        if (buf.length < len + 4) return;
        const id = buf.readInt32LE(4);
        const body = buf.toString("utf8", 12, len + 2);
        buf = buf.subarray(len + 4);
        if (!authed) {
          if (id === -1) return done(new Error("rcon auth failed"));
          authed = true;
          sendPacket(2, 2, command);
        } else if (id === 2) {
          out += body;
          clearTimeout(idle);
          idle = setTimeout(() => done(null, clean()), 60);
        }
      }
    });
  });
}

/* ============================= Agent IPC ============================= */
async function agent(action, params = {}, { timeout = 10000 } = {}) {
  const id = crypto.randomBytes(16).toString("hex") + ".json";
  const cmdPath = path.join(IPC_CMD, id);
  const respPath = path.join(IPC_RESP, id);
  await fsp.writeFile(cmdPath + ".tmp", JSON.stringify({ action, ...params }));
  await fsp.rename(cmdPath + ".tmp", cmdPath);
  const deadline = Date.now() + timeout;
  while (Date.now() < deadline) {
    let text;
    try { text = await fsp.readFile(respPath, "utf8"); }
    catch (e) {
      if (e.code === "ENOENT") { await sleep(100); continue; }
      throw e;
    }
    fsp.unlink(respPath).catch(() => {});
    const data = JSON.parse(text);
    if (!data.ok) fail(400, data.error || "The host agent reported an error.");
    return data.data;
  }
  fsp.unlink(cmdPath).catch(() => {});
  fail(504, "The host agent did not respond.");
}

/* ============================== Password ============================= */
function scrypt(password, salt) {
  return new Promise((resolve, reject) => {
    crypto.scrypt(password, salt, 64, { N: 32768, r: 8, p: 1, maxmem: 128 * 1024 * 1024 },
      (e, key) => (e ? reject(e) : resolve(key)));
  });
}
async function checkPassword(password) {
  const { salt, hash } = JSON.parse(fs.readFileSync(AUTH_FILE, "utf8"));
  const actual = await scrypt(password, Buffer.from(salt, "hex"));
  return crypto.timingSafeEqual(actual, Buffer.from(hash, "hex"));
}

/* =============================== Helpers ============================= */
function send(res, status, data, headers = {}) {
  res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store", ...headers });
  res.end(JSON.stringify(data));
}
function cookieToken(req) {
  const m = (req.headers.cookie || "").match(/(?:^|;\s*)sid=([a-f0-9]{64})/);
  return m ? m[1] : null;
}
function isAuthed(req) {
  const t = cookieToken(req);
  const exp = t && sessions.get(t);
  if (!exp) return false;
  if (exp < Date.now()) { sessions.delete(t); return false; }
  return true;
}
function sameOrigin(req) {
  try { return new URL(req.headers.origin).host === req.headers.host; }
  catch { return false; }
}
function readBody(req, max) {
  return new Promise((resolve, reject) => {
    let size = 0;
    const parts = [];
    req.on("data", (c) => {
      size += c.length;
      if (size > max) { reject(new HttpError(413, "Request is too large.")); req.destroy(); return; }
      parts.push(c);
    });
    req.on("end", () => resolve(Buffer.concat(parts)));
    req.on("error", reject);
  });
}
async function readJson(req, max = 4096) {
  const buf = await readBody(req, max);
  try { return JSON.parse(buf.toString("utf8") || "{}"); }
  catch { fail(400, "Invalid JSON."); }
}
function limiter(max) {
  let n = 0;
  return new Transform({
    transform(chunk, _enc, cb) {
      n += chunk.length;
      if (n > max) cb(new HttpError(413, "File is too large."));
      else cb(null, chunk);
    },
  });
}
async function streamTo(req, dest, max) {
  const tmp = `${dest}.part-${crypto.randomBytes(4).toString("hex")}`;
  try {
    await pipeline(req, limiter(max), fs.createWriteStream(tmp, { mode: 0o644 }));
    const { size } = await fsp.stat(tmp);
    await fsp.rename(tmp, dest);
    return size;
  } catch (e) {
    await fsp.rm(tmp, { force: true }).catch(() => {});
    if (e instanceof HttpError) throw e;
    fail(400, "Upload was interrupted.");
  }
}

/* ============================== Stats =============================== */
async function readStats() {
  let s;
  try { s = JSON.parse(await fsp.readFile(IPC_STATS, "utf8")); }
  catch { fail(503, "No stats yet. Is the panel agent running?"); }
  s.stale = Date.now() - (s.ts || 0) > 15000;
  return s;
}

/* ============================== Minecraft ============================ */
async function mcStatus() {
  const s = await agent("mc_status", {}, { timeout: 5000 });
  let state;
  if (s.unit === "activating") state = "starting";
  else if (s.unit === "deactivating") state = "stopping";
  else if (s.unit === "failed") state = "error";
  else if (s.unit !== "active") state = "offline";
  else if (s.container === "running") state = "online";
  else if (s.container === "restarting" || s.container === "created") state = "starting";
  else state = "offline";
  return { state, unit: s.unit, container: s.container };
}
function parseList(out) {
  const m = out.match(/(\d+)\D+(\d+)[^:]*:\s*(.*)$/s);
  const names = m && m[3] ? m[3].split(",").map((s) => s.trim()).filter(Boolean) : [];
  return { online: names, max: m ? +m[2] : 0 };
}
async function listWhitelist() {
  const out = await rcon("minecraft:whitelist list");
  const i = out.indexOf(":");
  if (i < 0) return [];
  return out.slice(i + 1).split(",").map((s) => s.trim()).filter(Boolean).sort((a, b) => a.localeCompare(b));
}
async function listBanned() {
  const out = await rcon("minecraft:banlist players");
  return [...out.matchAll(/([A-Za-z0-9_]{3,16}) was banned by/g)].map((m) => m[1]);
}
async function mcLight() {
  const status = await mcStatus();
  if (status.state !== "online") return { ...status, online: [], max: 0 };
  try { return { ...status, ...parseList(await rcon("minecraft:list")) }; }
  catch { return { ...status, state: "starting", online: [], max: 0 }; }
}
async function mcSnapshot() {
  const status = await mcStatus();
  const base = { ...status, address: SERVER_ADDRESS, localAddress: LOCAL_ADDRESS, online: [], max: 0, whitelist: [], banned: [] };
  if (status.state !== "online") return base;
  try {
    const list = parseList(await rcon("minecraft:list"));
    const [whitelist, banned] = await Promise.all([listWhitelist(), listBanned()]);
    return { ...base, ...list, whitelist, banned };
  } catch {
    return { ...base, state: "starting" };
  }
}
function readMcLog() {
  let fd;
  try {
    fd = fs.openSync(LOG_FILE, "r");
    const { size } = fs.fstatSync(fd);
    const length = Math.min(size, LOG_BYTES);
    const buf = Buffer.alloc(length);
    fs.readSync(fd, buf, 0, length, size - length);
    let text = buf.toString("utf8");
    if (size > length) text = text.slice(text.indexOf("\n") + 1);
    return text.replace(/\x1b\[[0-9;]*[A-Za-z]/g, "").split("\n")
      .filter((line) => line && !LOG_NOISE.test(line)).slice(-LOG_LINES);
  } catch { return []; }
  finally { if (fd !== undefined) fs.closeSync(fd); }
}
async function requireOnline() {
  const s = await mcStatus();
  if (s.state !== "online") fail(409, "The Minecraft server is not running.");
}

async function mcRoutes(req, res, p, m) {
  if (p === "/api/mc" && m === "GET") return send(res, 200, await mcSnapshot());
  if (p === "/api/mc/start" && m === "POST") { const r = await agent("mc_start"); await pluginsApplied(); return send(res, 200, r); }
  if (p === "/api/mc/stop" && m === "POST") return send(res, 200, await agent("mc_stop"));
  if (p === "/api/mc/restart" && m === "POST") { const r = await agent("mc_restart"); await pluginsApplied(); return send(res, 200, r); }
  if (p === "/api/mc/logs" && m === "GET") return send(res, 200, { lines: readMcLog() });

  if (p === "/api/mc/player" && m === "POST") {
    const { action, name, reason = "" } = await readJson(req);
    const commands = {
      "whitelist-add": () => `minecraft:whitelist add ${name}`,
      "whitelist-remove": () => `minecraft:whitelist remove ${name}`,
      kick: () => `minecraft:kick ${name}${reason ? ` ${reason}` : ""}`,
      ban: () => `minecraft:ban ${name}${reason ? ` ${reason}` : ""}`,
      pardon: () => `minecraft:pardon ${name}`,
    };
    if (!commands[action]) fail(400, "Unknown action.");
    if (typeof name !== "string" || !NAME.test(name)) fail(400, "Minecraft names are 3–16 letters, numbers or underscores.");
    if (typeof reason !== "string" || !REASON.test(reason)) fail(400, "That reason can't be used.");
    await requireOnline();
    const message = await rcon(commands[action]());
    return send(res, 200, { message, ...(await mcSnapshot()) });
  }
  if (p === "/api/mc/console" && m === "POST") {
    const body = await readJson(req);
    const command = typeof body.command === "string" ? body.command.trim().replace(/^\//, "") : "";
    if (!command || command.length > 1000 || CONTROL.test(command)) fail(400, "That command can't be sent.");
    await requireOnline();
    console.log(`console: ${command.slice(0, 200)}`);
    return send(res, 200, { output: (await rcon(command)).slice(0, 8000) });
  }
  if (p === "/api/mc/seed" && m === "GET") {
    await requireOnline();
    const match = (await rcon("minecraft:seed")).match(/\[(-?\d+)\]/);
    return send(res, 200, { seed: match ? match[1] : "" });
  }
  if (p === "/api/mc/reset" && m === "POST") {
    const { seed = "", confirm } = await readJson(req);
    if (confirm !== "RESET") fail(400, "Type RESET to confirm.");
    if (typeof seed !== "string" || (seed && !SEED.test(seed))) fail(400, "Seeds are up to 32 letters, numbers, - or _.");
    const next = seed || String(crypto.randomInt(1, 2 ** 47));
    console.log(`world reset requested: ${next}`);
    fs.writeFileSync(`${RESET_FILE}.tmp`, next);
    fs.renameSync(`${RESET_FILE}.tmp`, RESET_FILE);
    const { state } = await mcStatus();
    const running = ["online", "starting"].includes(state);
    // The boot script applies the reset the next time the container starts.
    if (running) await agent("mc_restart");
    return send(res, 200, { ok: true, seed: next, restarted: running });
  }
  fail(404, "Not found.");
}

/* ================================ Bots ================================ */
function botDir(id) {
  if (!BOT_ID.test(id)) fail(400, "Invalid bot id.");
  return path.join(BOTS_DIR, id);
}
function botPath(rel) {
  if (typeof rel !== "string") return null;
  const parts = rel.replace(/\\/g, "/").replace(/^\/+/, "").split("/");
  if (!parts.length || parts.length > 6 || !parts.every((s) => SEGMENT.test(s))) return null;
  if (parts.length === 1 && RESERVED_TOP.has(parts[0])) return null;
  if (parts.some((s) => SKIP_DIRS.has(s))) return null;
  return parts.join("/");
}
function resolveIn(dir, rel) {
  const full = path.resolve(dir, rel);
  if (!full.startsWith(dir + path.sep)) fail(400, "Invalid path.");
  return full;
}
async function walkBot(dir) {
  const out = [];
  async function walk(abs, rel, depth) {
    if (depth > 6 || out.length >= 1000) return;
    let entries;
    try { entries = await fsp.readdir(abs, { withFileTypes: true }); } catch { return; }
    for (const e of entries) {
      if (e.name.startsWith(".") || SKIP_DIRS.has(e.name)) continue;
      const r = rel ? `${rel}/${e.name}` : e.name;
      if (!rel && RESERVED_TOP.has(e.name)) continue;
      if (e.isDirectory()) await walk(path.join(abs, e.name), r, depth + 1);
      else if (e.isFile()) {
        const st = await fsp.stat(path.join(abs, e.name));
        out.push({ path: r, size: st.size, mtime: Math.round(st.mtimeMs) });
      }
    }
  }
  await walk(dir, "", 0);
  return out.sort((a, b) => {
    const da = a.path.includes("/"), db = b.path.includes("/");
    if (da !== db) return da ? 1 : -1;
    return a.path.localeCompare(b.path);
  });
}
async function writeBotFile(dir, rel, data) {
  const full = resolveIn(dir, rel);
  await fsp.mkdir(path.dirname(full), { recursive: true });
  try {
    const st = await fsp.lstat(full);
    if (st.isSymbolicLink() || st.isDirectory()) fail(400, "That path can't be written.");
  } catch (e) { if (e instanceof HttpError) throw e; }
  const tmp = `${full}.tmp-${crypto.randomBytes(4).toString("hex")}`;
  await fsp.writeFile(tmp, data, { mode: 0o644 });
  await fsp.rename(tmp, full);
}

function parseEnv(text) {
  const vars = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const m = line.match(/^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
    if (!m) continue;
    let v = m[2];
    if (v.length >= 2 && v.startsWith('"') && v.endsWith('"')) v = v.slice(1, -1).replace(/\\(["\\])/g, "$1");
    else if (v.length >= 2 && v.startsWith("'") && v.endsWith("'")) v = v.slice(1, -1);
    vars.push({ key: m[1], value: v });
  }
  return vars;
}
function serializeEnv(vars) {
  const lines = vars.map(({ key, value }) => `${key}="${value.replace(/\\/g, "\\\\").replace(/"/g, '\\"')}"`);
  return `# Managed by the __BRAND__ panel. Restart the bot after changes.\n${lines.join("\n")}\n`;
}
async function readEnv(dir) {
  try { return parseEnv(await fsp.readFile(path.join(dir, ".env"), "utf8")); }
  catch { return []; }
}

/* Minimal ZIP reader (stored + deflate), enough for bot uploads. */
function readZip(buf) {
  let eocd = -1;
  for (let i = buf.length - 22; i >= Math.max(0, buf.length - 65557); i--) {
    if (buf.readUInt32LE(i) === 0x06054b50) { eocd = i; break; }
  }
  if (eocd < 0) fail(400, "That doesn't look like a zip file.");
  const count = buf.readUInt16LE(eocd + 10);
  const cdOffset = buf.readUInt32LE(eocd + 16);
  if (count === 0xffff || cdOffset === 0xffffffff) fail(400, "ZIP64 archives aren't supported.");
  if (count > MAX_ZIP_ENTRIES) fail(400, `Zip has more than ${MAX_ZIP_ENTRIES} entries.`);
  const entries = [];
  let p = cdOffset;
  for (let i = 0; i < count; i++) {
    if (p + 46 > buf.length || buf.readUInt32LE(p) !== 0x02014b50) fail(400, "The zip file is damaged.");
    const flags = buf.readUInt16LE(p + 8);
    const method = buf.readUInt16LE(p + 10);
    const csize = buf.readUInt32LE(p + 20);
    const usize = buf.readUInt32LE(p + 24);
    const nlen = buf.readUInt16LE(p + 28);
    const xlen = buf.readUInt16LE(p + 30);
    const clen = buf.readUInt16LE(p + 32);
    const mode = buf.readUInt32LE(p + 38) >>> 16;
    const lho = buf.readUInt32LE(p + 42);
    const name = buf.toString(flags & 0x800 ? "utf8" : "latin1", p + 46, p + 46 + nlen);
    entries.push({ name, flags, method, csize, usize, mode, lho });
    p += 46 + nlen + xlen + clen;
  }
  return entries;
}
function zipEntryData(buf, e) {
  if (e.flags & 1) fail(400, "Encrypted zips aren't supported.");
  const p = e.lho;
  if (p + 30 > buf.length || buf.readUInt32LE(p) !== 0x04034b50) fail(400, "The zip file is damaged.");
  const start = p + 30 + buf.readUInt16LE(p + 26) + buf.readUInt16LE(p + 28);
  const data = buf.subarray(start, start + e.csize);
  if (e.method === 0) return data;
  if (e.method === 8) {
    try { return zlib.inflateRawSync(data, { maxOutputLength: Math.max(1, e.usize) }); }
    catch { fail(400, `Couldn't unpack ${e.name}.`); }
  }
  fail(400, `${e.name} uses an unsupported compression method.`);
}
async function extractZip(dir, buf) {
  const entries = readZip(buf).filter((e) => !e.name.endsWith("/") && (e.mode & 0o170000) !== 0o120000);
  const total = entries.reduce((s, e) => s + e.usize, 0);
  if (total > MAX_ZIP_TOTAL) fail(400, "The zip unpacks to more than 100 MB.");
  // Strip a shared top folder ("mybot/bot.py" -> "bot.py").
  const names = entries.map((e) => e.name.replace(/\\/g, "/").replace(/^\/+/, ""));
  const firsts = new Set(names.map((n) => (n.includes("/") ? n.split("/")[0] : "")));
  const strip = firsts.size === 1 && !firsts.has("") ? [...firsts][0].length + 1 : 0;
  let written = 0, skipped = 0;
  for (let i = 0; i < entries.length; i++) {
    const rel = names[i].slice(strip);
    const target = rel === ".env" ? ".env" : botPath(rel);
    if (!target) { skipped++; continue; }
    await writeBotFile(dir, target, zipEntryData(buf, entries[i]));
    if (target === ".env") await fsp.chmod(path.join(dir, ".env"), 0o600);
    written++;
  }
  return { written, skipped };
}

async function botRoutes(req, res, p, m, url) {
  if (p === "/api/bots" && m === "GET") return send(res, 200, await agent("bot_list"));
  if (p === "/api/bots" && m === "POST") {
    const { id, name = "" } = await readJson(req);
    if (typeof id !== "string" || !BOT_ID.test(id)) fail(400, "IDs are 2–32 characters: a–z, 0–9, - or _, starting with a letter or number.");
    const clean = String(name).replace(/[\x00-\x1f\x7f]/g, "").trim().slice(0, 64);
    return send(res, 200, await agent("bot_create", { id, name: clean || id }));
  }
  const match = p.match(/^\/api\/bots\/([^/]+)(?:\/(.*))?$/);
  if (!match) fail(404, "Not found.");
  const id = match[1];
  const rest = match[2] || "";
  const dir = botDir(id);

  if (rest === "" && m === "GET") {
    const info = await agent("bot_info", { id });
    const env = await readEnv(dir);
    const token = env.find((v) => v.key === "DISCORD_TOKEN");
    return send(res, 200, { ...info, tokenMissing: !!token && !token.value.trim() });
  }
  if (rest === "" && m === "DELETE") return send(res, 200, await agent("bot_delete", { id }, { timeout: 40000 }));
  if (rest === "start" && m === "POST") return send(res, 200, await agent("bot_start", { id }));
  if (rest === "stop" && m === "POST") return send(res, 200, await agent("bot_stop", { id }));
  if (rest === "restart" && m === "POST") return send(res, 200, await agent("bot_restart", { id }));
  if (rest === "logs" && m === "GET") {
    const lines = Math.max(10, Math.min(1000, +(url.searchParams.get("lines") || 400)));
    return send(res, 200, await agent("bot_logs", { id, lines }));
  }
  if (rest === "logs" && m === "DELETE") return send(res, 200, await agent("bot_clear_logs", { id }));
  if (rest === "meta" && m === "PUT") {
    const { name, main } = await readJson(req);
    const params = { id };
    if (name !== undefined) params.name = String(name).replace(/[\x00-\x1f\x7f]/g, "").trim().slice(0, 64);
    if (main !== undefined) {
      if (!botPath(main) || !String(main).endsWith(".py")) fail(400, "The main file must be a .py file in the bot folder.");
      params.main = botPath(main);
    }
    return send(res, 200, await agent("bot_set_meta", params));
  }
  if (rest === "env" && m === "GET") return send(res, 200, { vars: await readEnv(dir) });
  if (rest === "env" && m === "PUT") {
    const { vars } = await readJson(req, 64 * 1024);
    if (!Array.isArray(vars) || vars.length > 100) fail(400, "Invalid variables.");
    const seen = new Set();
    for (const v of vars) {
      if (!v || typeof v.key !== "string" || !ENV_KEY.test(v.key)) fail(400, `"${v?.key ?? ""}" is not a valid variable name.`);
      if (typeof v.value !== "string" || v.value.length > 4096 || /[\r\n\0]/.test(v.value)) fail(400, `The value for ${v.key} can't be used.`);
      if (seen.has(v.key)) fail(400, `${v.key} is listed twice.`);
      seen.add(v.key);
    }
    await writeBotFile(dir, ".env", serializeEnv(vars));
    await fsp.chmod(path.join(dir, ".env"), 0o600);
    return send(res, 200, { ok: true, vars });
  }
  if (rest === "files" && m === "GET") return send(res, 200, { files: await walkBot(dir) });

  if (rest.startsWith("files/")) {
    let rel;
    try { rel = botPath(rest.slice(6).split("/").map(decodeURIComponent).join("/")); } catch { rel = null; }
    if (!rel) fail(400, "That file name isn't allowed.");
    const full = resolveIn(dir, rel);
    if (m === "GET") {
      let st;
      try { st = await fsp.lstat(full); } catch { fail(404, "File not found."); }
      if (!st.isFile()) fail(404, "File not found.");
      if (st.size > MAX_EDIT_BYTES) return send(res, 200, { path: rel, size: st.size, editable: false, reason: "large" });
      const buf = await fsp.readFile(full);
      if (buf.subarray(0, 8192).includes(0)) return send(res, 200, { path: rel, size: st.size, editable: false, reason: "binary" });
      return send(res, 200, { path: rel, size: st.size, editable: true, content: buf.toString("utf8") });
    }
    if (m === "PUT") {
      const { content } = await readJson(req, MAX_EDIT_BYTES * 3);
      if (typeof content !== "string") fail(400, "Content must be text.");
      if (Buffer.byteLength(content) > MAX_EDIT_BYTES) fail(413, "Files edited here can be up to 256 KB.");
      await writeBotFile(dir, rel, content);
      return send(res, 200, { ok: true, path: rel });
    }
    if (m === "DELETE") {
      try { await fsp.unlink(full); } catch { fail(404, "File not found."); }
      // Remove folders that became empty.
      let parent = path.dirname(full);
      while (parent.startsWith(dir + path.sep)) {
        try { await fsp.rmdir(parent); } catch { break; }
        parent = path.dirname(parent);
      }
      return send(res, 200, { ok: true });
    }
  }

  if (rest === "upload" && m === "POST") {
    try { await fsp.access(dir); } catch { fail(404, "Bot not found."); }
    const name = url.searchParams.get("path") || "";
    if (url.searchParams.get("extract") === "1") {
      const buf = await readBody(req, MAX_BOT_UPLOAD);
      const result = await extractZip(dir, buf);
      const info = await agent("bot_info", { id });
      if (!info.main_exists) {
        const files = await walkBot(dir);
        const candidate = MAIN_CANDIDATES.find((c) => files.some((f) => f.path === c));
        if (candidate) await agent("bot_set_meta", { id, main: candidate });
      }
      return send(res, 200, { ok: true, ...result });
    }
    const rel = botPath(name);
    if (!rel) fail(400, "That file name isn't allowed.");
    const full = resolveIn(dir, rel);
    await fsp.mkdir(path.dirname(full), { recursive: true });
    const size = await streamTo(req, full, MAX_BOT_UPLOAD);
    return send(res, 200, { ok: true, path: rel, size });
  }
  fail(404, "Not found.");
}

/* ============================ Plugin shop ============================ */
// Paper plugins from Modrinth. Jars go straight into the server's plugins
// folder (mounted at /mc/plugins); Paper loads them on the next (re)start.
// Jars that were added by hand are recognised by their hash, so they get
// names, icons and update checks too.
const PLUGINS_DIR = process.env.PLUGINS_DIR || "/mc/plugins";
const PLUGINS_META = path.join(PLUGINS_DIR, ".__SLUG__-shop.json");
const MODRINTH = "https://api.modrinth.com/v2";
const MR_UA = "__SLUG__-panel/1.0 (self-hosted admin panel)";
const MR_ID = /^[A-Za-z0-9]{8}$/;
const JAR = /^[A-Za-z0-9][A-Za-z0-9 _.+()\[\]-]{0,119}\.jar$/;
const PLUGIN_LOADERS = ["paper", "spigot", "bukkit", "purpur"];
const MR_CATS = new Set(["adventure", "cursed", "decoration", "economy", "equipment", "food", "game-mechanics", "library", "magic",
  "management", "minigame", "mobs", "optimization", "social", "storage", "technology", "transportation", "utility", "worldgen"]);
const MAX_PLUGIN_BYTES = 100 * 1024 * 1024;
let mcVersionCache = process.env.MC_VERSION || "26.3";

function mcVersion() {
  // Paper logs "Starting minecraft server version 26.3" right at the top.
  let fd;
  try {
    fd = fs.openSync(LOG_FILE, "r");
    const buf = Buffer.alloc(16384);
    const n = fs.readSync(fd, buf, 0, buf.length, 0);
    const m = buf.toString("utf8", 0, n).match(/server version (\d+(?:\.\d+){1,2})\b/);
    if (m) mcVersionCache = m[1];
  } catch {}
  finally { if (fd !== undefined) fs.closeSync(fd); }
  return mcVersionCache;
}

const mrCache = new Map();
async function mr(p, { method = "GET", body, cache = 0, timeout = 10000 } = {}) {
  const key = `${method} ${p} ${body ? JSON.stringify(body) : ""}`;
  const hit = cache && mrCache.get(key);
  if (hit && hit.until > Date.now()) return hit.data;
  let r;
  try {
    r = await fetch(MODRINTH + p, {
      method, signal: AbortSignal.timeout(timeout),
      headers: { "User-Agent": MR_UA, ...(body ? { "Content-Type": "application/json" } : {}) },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch { fail(502, "Modrinth isn't reachable right now. Try again in a moment."); }
  if (r.status === 404) fail(404, "That plugin doesn't exist on Modrinth.");
  if (r.status === 429) fail(429, "Modrinth says slow down. Try again in a minute.");
  if (!r.ok) fail(502, `Modrinth answered with an error (${r.status}).`);
  const data = await r.json();
  if (cache) {
    if (mrCache.size > 300) mrCache.clear();
    mrCache.set(key, { until: Date.now() + cache, data });
  }
  return data;
}

let pluginLock = Promise.resolve();
function withPlugins(fn) {
  const run = pluginLock.then(async () => {
    let meta = { files: {}, pending: false };
    try { meta = { ...meta, ...JSON.parse(await fsp.readFile(PLUGINS_META, "utf8")) }; } catch {}
    return fn(meta, async () => {
      await fsp.writeFile(`${PLUGINS_META}.tmp`, JSON.stringify(meta, null, 1));
      await fsp.rename(`${PLUGINS_META}.tmp`, PLUGINS_META);
    });
  });
  pluginLock = run.catch(() => {});
  return run;
}
async function pluginsApplied() {
  try { await withPlugins(async (meta, save) => { if (meta.pending) { meta.pending = false; await save(); } }); } catch {}
}

const jarHashes = new Map();
async function jarSha1(file) {
  const st = await fsp.stat(file);
  const key = `${st.size}:${st.mtimeMs}`;
  const c = jarHashes.get(file);
  if (c && c.key === key) return c.sha1;
  const sha1 = crypto.createHash("sha1").update(await fsp.readFile(file)).digest("hex");
  jarHashes.set(file, { key, sha1 });
  return sha1;
}

async function pluginsList(lockedMeta) {
  let names;
  try {
    names = (await fsp.readdir(PLUGINS_DIR, { withFileTypes: true }))
      .filter((d) => d.isFile() && /\.jar$/i.test(d.name)).map((d) => d.name);
  } catch { fail(503, "The plugins folder isn't available."); }
  const meta = lockedMeta || await withPlugins((m) => m);   // inside withPlugins the caller passes its copy
  const version = mcVersion();
  const items = await Promise.all(names.map(async (file) => {
    const full = path.join(PLUGINS_DIR, file);
    const st = await fsp.stat(full);
    const known = meta.files[file] || {};
    return { file, size: st.size, added: known.installed || st.mtimeMs, shop: !!meta.files[file],
             title: known.title || "", icon: known.icon || "", slug: known.slug || "", project: known.project || "",
             number: known.number || "", sha1: await jarSha1(full) };
  }));
  let online = true;
  const hashes = items.map((i) => i.sha1);
  if (hashes.length) {
    try {
      const [known, latest] = await Promise.all([
        mr("/version_files", { method: "POST", body: { hashes, algorithm: "sha1" }, cache: 30 * 60e3 }),
        mr("/version_files/update", { method: "POST", body: { hashes, algorithm: "sha1", loaders: PLUGIN_LOADERS, game_versions: [version] }, cache: 10 * 60e3 }),
      ]);
      const ids = [...new Set(Object.values(known).map((v) => v.project_id))];
      const projects = ids.length ? await mr(`/projects?ids=${encodeURIComponent(JSON.stringify(ids))}`, { cache: 60 * 60e3 }) : [];
      const byId = Object.fromEntries(projects.map((x) => [x.id, x]));
      for (const i of items) {
        const v = known[i.sha1];
        if (!v) continue;
        const pr = byId[v.project_id] || {};
        Object.assign(i, { project: v.project_id, number: v.version_number, title: pr.title || i.title, icon: pr.icon_url || i.icon, slug: pr.slug || i.slug });
        const up = latest[i.sha1];
        if (up && up.id !== v.id && Date.parse(up.date_published) > Date.parse(v.date_published)) i.update = up.version_number;
      }
    } catch { online = false; }
  }
  for (const i of items) {
    delete i.sha1;
    if (!i.title) i.title = i.file.replace(/\.jar$/i, "");
  }
  items.sort((a, b) => a.title.localeCompare(b.title));
  return { version, plugins: items, pending: !!meta.pending, online };
}

function jarName(raw, taken) {
  let name = String(raw).replace(/[^A-Za-z0-9 _.+()\[\]-]/g, "_").replace(/^[^A-Za-z0-9]+/, "").slice(0, 110);
  if (!/\.jar$/i.test(name)) name += ".jar";
  if (!JAR.test(name)) name = `plugin-${crypto.randomBytes(3).toString("hex")}.jar`;
  return name;
}
async function fetchJar(f, dest) {
  const u = new URL(f.url);
  if (u.protocol !== "https:" || u.hostname !== "cdn.modrinth.com") fail(502, "Modrinth pointed to an unexpected download location.");
  if (f.size > MAX_PLUGIN_BYTES) fail(413, "That plugin is over 100 MB.");
  let r;
  try { r = await fetch(u, { headers: { "User-Agent": MR_UA }, signal: AbortSignal.timeout(120000) }); }
  catch { fail(502, "The download from Modrinth failed. Try again."); }
  if (!r.ok || new URL(r.url).hostname !== "cdn.modrinth.com") fail(502, "The download from Modrinth failed. Try again.");
  const buf = Buffer.from(await r.arrayBuffer());
  if (buf.length > MAX_PLUGIN_BYTES) fail(413, "That plugin is over 100 MB.");
  const want = f.hashes && f.hashes.sha512;
  if (want && crypto.createHash("sha512").update(buf).digest("hex") !== want) fail(502, "The download was damaged. Try again.");
  await fsp.writeFile(`${dest}.part`, buf, { mode: 0o664 });
  await fsp.rename(`${dest}.part`, dest);
}

// Installs the newest release that runs on this server, plus required dependencies.
async function installPlugin(projectId, ctx, depth = 0) {
  if (ctx.seen.has(projectId)) return;
  ctx.seen.add(projectId);
  const q = `loaders=${encodeURIComponent(JSON.stringify(PLUGIN_LOADERS))}&game_versions=${encodeURIComponent(JSON.stringify([ctx.version]))}`;
  const [versions, project] = await Promise.all([mr(`/project/${projectId}/version?${q}`), mr(`/project/${projectId}`, { cache: 60 * 60e3 })]);
  const v = versions.find((x) => x.version_type === "release") || versions[0];
  if (!v) fail(409, `${project.title} doesn't have a version for Minecraft ${ctx.version} yet.`);
  const f = v.files.find((x) => x.primary && /\.jar$/i.test(x.filename)) || v.files.find((x) => /\.jar$/i.test(x.filename));
  if (!f) fail(409, `${project.title} has no plugin file to install.`);
  if (depth < 2) {
    for (const d of v.dependencies || []) {
      if (d.dependency_type !== "required" || !d.project_id || ctx.have.has(d.project_id)) continue;
      await installPlugin(d.project_id, ctx, depth + 1);
    }
  }
  // Old copies of the same plugin go away so Paper doesn't load it twice.
  const old = ctx.current.filter((x) => x.project === projectId).map((x) => x.file);
  let file = jarName(f.filename);
  if (ctx.current.some((x) => x.file === file && x.project !== projectId)) file = file.replace(/\.jar$/i, `-${projectId}.jar`);
  await fetchJar(f, path.join(PLUGINS_DIR, file));
  for (const o of old) {
    if (o === file) continue;
    await fsp.rm(path.join(PLUGINS_DIR, o), { force: true });
    delete ctx.meta.files[o];
  }
  ctx.meta.files[file] = { project: projectId, version: v.id, number: v.version_number, title: project.title,
                           icon: project.icon_url || "", slug: project.slug, installed: Date.now() };
  ctx.have.add(projectId);
  ctx.done.push(depth ? `${project.title} (needed)` : project.title);
}

async function pluginRoutes(req, res, p, m, url) {
  if (p === "/api/plugins" && m === "GET") return send(res, 200, await pluginsList());
  if (p === "/api/plugins/search" && m === "GET") {
    const q = String(url.searchParams.get("q") || "").replace(/[\x00-\x1f\x7f]/g, "").trim().slice(0, 80);
    const cat = String(url.searchParams.get("cat") || "");
    const offset = Math.max(0, Math.min(1000, +(url.searchParams.get("offset") || 0) || 0));
    const version = mcVersion();
    const facets = [["project_type:plugin"], PLUGIN_LOADERS.map((l) => `categories:${l}`)];
    if (url.searchParams.get("all") !== "1") facets.push([`versions:${version}`]);
    if (MR_CATS.has(cat)) facets.push([`categories:${cat}`]);
    const qs = new URLSearchParams({ query: q, limit: "24", offset: String(offset), index: q ? "relevance" : "downloads", facets: JSON.stringify(facets) });
    const d = await mr(`/search?${qs}`, { cache: 5 * 60e3 });
    return send(res, 200, {
      version, total: d.total_hits, offset,
      hits: d.hits.map((h) => ({ id: h.project_id, slug: h.slug, title: h.title, author: h.author, description: h.description,
        downloads: h.downloads, icon: h.icon_url || "", updated: h.date_modified,
        categories: (h.display_categories || h.categories || []).filter((c) => MR_CATS.has(c)).slice(0, 3),
        fits: (h.versions || []).includes(version) })),
    });
  }
  if ((p === "/api/plugins/install" || p === "/api/plugins/update") && m === "POST") {
    const b = await readJson(req);
    const result = await withPlugins(async (meta, save) => {
      const list = await pluginsList(meta);
      let project = String(b.project || "");
      if (p.endsWith("/update")) {
        const it = list.plugins.find((x) => x.file === b.file);
        if (!it || !it.project) fail(404, "That plugin can't be updated from here.");
        project = it.project;
      }
      if (!MR_ID.test(project)) fail(400, "Unknown plugin.");
      if (p.endsWith("/install") && list.plugins.some((x) => x.project === project)) fail(409, "That plugin is already installed.");
      const ctx = { version: list.version, meta, current: list.plugins, seen: new Set(), done: [],
                    have: new Set(list.plugins.map((x) => x.project).filter(Boolean)) };
      ctx.have.delete(project);
      try { await installPlugin(project, ctx); }
      finally { if (ctx.done.length) { meta.pending = true; await save(); } }
      console.log(`plugins: ${p.endsWith("/update") ? "updated" : "installed"} ${ctx.done.join(", ")}`);
      return { ok: true, installed: ctx.done };
    });
    return send(res, 200, { ...result, ...(await pluginsList()) });
  }
  if (p === "/api/plugins/remove" && m === "POST") {
    const { file } = await readJson(req);
    if (typeof file !== "string" || !JAR.test(file)) fail(400, "Unknown plugin file.");
    await withPlugins(async (meta, save) => {
      try { await fsp.rm(path.join(PLUGINS_DIR, file)); }
      catch { fail(404, "That plugin isn't installed."); }
      delete meta.files[file];
      meta.pending = true;
      await save();
    });
    console.log(`plugins: removed ${file}`);
    return send(res, 200, { ok: true, ...(await pluginsList()) });
  }
  fail(404, "Not found.");
}

/* ============================ Global search ============================ */
// One query over everything the panel knows about. Each source is optional:
// if the media service or Minecraft is down, the rest still answers.
const within = (ms, p) => Promise.race([p, sleep(ms).then(() => { throw new Error("timeout"); })]);
async function globalSearch(raw) {
  const q = String(raw || "").replace(/[\x00-\x1f\x7f]/g, "").trim().toLowerCase().slice(0, 80);
  if (q.length < 2) return { q, groups: [] };
  const words = q.split(/\s+/).filter(Boolean);
  const hit = (...fields) => {
    const text = fields.filter(Boolean).join(" ").toLowerCase();
    return words.every((w) => text.includes(w));
  };
  const take = (list, n = 6) => list.slice(0, n);
  const DL_VIEWS = { download: ["File Downloader", "#downloader"], compress: ["Discord Compressor", "#compressor"], convert: ["File Converter", "#convert"],
                     cutout: ["Background Remover", "#cutout"], upscale: ["Image Upscaler", "#upscale"] };
  const dlLink = (j) => `/d/${j.id}/${encodeURIComponent(j.file)}`;
  const sources = {
    files: async () => (await filesList()).filter((f) => hit(f.name)).map((f) => ({
      title: f.name, sub: `File Host · ${bytesStr(f.size)}`, href: f.folder ? `#files/${f.folder}` : "#files", open: `/f/${f.id}/${encodeURIComponent(f.name)}`, icon: "folder" })),
    clips: async () => (await clipsList()).filter((c) => hit(c.title)).map((c) => ({
      title: c.title, sub: `Clip · ${c.views || 0} views`, href: "#clips", open: `/c/${c.id}`, icon: "film" })),
    pastes: async () => (await pastesList()).filter((x) => hit(x.title, x.preview)).map((x) => ({
      title: x.title || "Untitled paste", sub: `Paste · ${x.lines} line${x.lines === 1 ? "" : "s"}`, href: "#paste", open: `/p/${x.id}`, icon: "text" })),
    music: async () => (await media("/internal/stations", { timeout: 2500 })).stations.filter((s) => hit(s.title)).map((s) => ({
      title: s.title, sub: "Music Radio", href: "#music", icon: "music" })),
    jobs: async () => {
      const all = await Promise.all(Object.keys(DL_VIEWS).map(async (k) => (await media(`/internal/downloads?kind=${k}`, { timeout: 2500 })).jobs.map((j) => ({ ...j, k }))));
      return all.flat().filter((j) => j.state === "done" && hit(j.title, j.file)).sort((a, b) => b.created - a.created).map((j) => ({
        title: j.title, sub: `${DL_VIEWS[j.k][0]} · ${bytesStr(j.size)}`, href: DL_VIEWS[j.k][1], open: dlLink(j), icon: "download" }));
    },
    bots: async () => (await agent("bot_list", {}, { timeout: 3000 })).bots.filter((b) => hit(b.name, b.id)).map((b) => ({
      title: b.name, sub: `Discord Bot · ${b.id}`, href: `#bots/${b.id}`, icon: "bot" })),
    players: async () => {
      const s = await mcStatus();
      if (s.state !== "online") return [];
      const [list, white] = await Promise.all([rcon("minecraft:list").then(parseList), listWhitelist()]);
      const names = [...new Set([...list.online, ...white])];
      return names.filter((n) => hit(n)).map((n) => ({
        title: n, sub: list.online.includes(n) ? "Minecraft · online now" : "Minecraft · whitelisted", href: "#minecraft", icon: "player", face: n }));
    },
    plugins: async () => {
      const names = (await fsp.readdir(PLUGINS_DIR)).filter((n) => /\.jar$/i.test(n));
      const meta = await withPlugins((m) => m);
      return names.map((f) => ({ f, t: (meta.files[f] || {}).title || f.replace(/\.jar$/i, "") })).filter((x) => hit(x.t, x.f)).map((x) => ({
        title: x.t, sub: "Minecraft plugin", href: "#minecraft/plugins", icon: "plug" }));
    },
  };
  const LABELS = { files: "File Host", clips: "Clips", pastes: "Pastebin", music: "Music Radio", jobs: "Made with File & Discord Tools",
                   bots: "Discord Bots", players: "Minecraft players", plugins: "Minecraft plugins" };
  const results = await Promise.allSettled(Object.entries(sources).map(async ([k, fn]) => [k, await within(4000, fn())]));
  const groups = results.filter((r) => r.status === "fulfilled" && r.value[1].length)
    .map((r) => ({ id: r.value[0], label: LABELS[r.value[0]], total: r.value[1].length, items: take(r.value[1]) }));
  return { q, groups };
}

/* ============================== Preferences ============================== */
// Layout of the tile pages (order + hidden tiles), shared by every device.
const PREFS_FILE = path.join(FILES_DIR, ".panel", "prefs.json");
const PREF_HUBS = new Set(["home", "filetools", "discord"]);
const TILE_ID = /^[a-z0-9-]{1,32}$/;
async function readPrefs() {
  try { return JSON.parse(await fsp.readFile(PREFS_FILE, "utf8")); }
  catch { return { tiles: {} }; }
}
async function writePrefs(b) {
  const prefs = await readPrefs();
  prefs.tiles = prefs.tiles || {};
  for (const [hub, v] of Object.entries((b && b.tiles) || {})) {
    if (!PREF_HUBS.has(hub)) fail(400, "Unknown page.");
    if (v === null) { delete prefs.tiles[hub]; continue; }
    const clean = (a) => (Array.isArray(a) ? a.filter((x) => typeof x === "string" && TILE_ID.test(x)).slice(0, 40) : []);
    prefs.tiles[hub] = { order: clean(v.order), hidden: clean(v.hidden) };
  }
  // Design page: theme, accent color, background, corners, font
  if (b && "design" in b) {
    const d = b.design;
    if (d === null) delete prefs.design;
    else if (d && typeof d === "object") {
      const pick = (v, ok, def) => (ok.includes(v) ? v : def);
      prefs.design = {
        theme: typeof d.theme === "string" && /^[a-z0-9-]{1,24}$/.test(d.theme) ? d.theme : "aurora",
        accent: typeof d.accent === "string" && /^#[0-9a-f]{6}$/i.test(d.accent) ? d.accent.toLowerCase() : null,
        bg: pick(d.bg, ["sky", "aurora", "mesh", "grid", "stars", "solid",
          "p-lake", "p-northern", "p-nebula", "p-tokyo", "p-forest", "p-ocean", "p-dunes", "p-cabin", "p-waves", "p-voxel"], null),
        radius: pick(d.radius, ["round", "soft", "sharp"], "round"),
        font: pick(d.font, ["geist", "inter", "grotesk", "outfit", "sora", "manrope", "syne", "serif", "rounded", "mono", "pixel"], null),
      };
    } else fail(400, "Bad design.");
  }
  await fsp.mkdir(path.dirname(PREFS_FILE), { recursive: true, mode: 0o755 });
  await fsp.writeFile(`${PREFS_FILE}.tmp`, JSON.stringify(prefs));
  await fsp.rename(`${PREFS_FILE}.tmp`, PREFS_FILE);
  return prefs;
}

/* ============================= File hoster ============================ */
// Files live in /files/<id>/<name>; index.json maps id -> { name, size, uploaded, folder }.
// Folders only exist in folders.json, so moving a file never changes its share link.
const FOLDERS_FILE = path.join(FILES_DIR, ".panel", "folders.json");
const FOLDER_ID = /^(f-[A-Za-z0-9_-]{10}|sys-[a-z]{2,16})$/;
const SYS_FOLDERS = {
  "sys-uploads": "Uploads", "sys-converter": "File Converter", "sys-downloader": "File Downloader",
  "sys-compressor": "Discord Compressor", "sys-cutout": "Background Remover", "sys-upscale": "Image Upscaler",
};
const KIND_FOLDER = { download: "sys-downloader", convert: "sys-converter", compress: "sys-compressor", cutout: "sys-cutout", upscale: "sys-upscale" };
const GB = 1024 ** 3;
let indexLock = Promise.resolve();
// fn(idx, save, folders): idx = files, folders = user folders { id: { name, parent, created } }.
function withIndex(fn) {
  const run = indexLock.then(async () => {
    let idx = {}, folders = {};
    try { idx = JSON.parse(await fsp.readFile(FILES_INDEX, "utf8")); }
    catch (e) { if (e.code !== "ENOENT") throw e; }
    try { folders = JSON.parse(await fsp.readFile(FOLDERS_FILE, "utf8")); } catch {}
    return fn(idx, async () => {
      await fsp.writeFile(`${FILES_INDEX}.tmp`, JSON.stringify(idx));
      await fsp.rename(`${FILES_INDEX}.tmp`, FILES_INDEX);
      await fsp.mkdir(path.dirname(FOLDERS_FILE), { recursive: true, mode: 0o755 });
      await fsp.writeFile(`${FOLDERS_FILE}.tmp`, JSON.stringify(folders));
      await fsp.rename(`${FOLDERS_FILE}.tmp`, FOLDERS_FILE);
    }, folders);
  });
  indexLock = run.catch(() => {});
  return run;
}
const allFolders = (folders) => ({
  ...Object.fromEntries(Object.entries(SYS_FOLDERS).map(([id, name]) => [id, { name, parent: null, sys: true }])),
  ...folders,
});
async function filesList() {
  return withIndex((idx) => Object.entries(idx).map(([id, e]) => ({ id, ...e }))
    .sort((a, b) => (b.uploaded || 0) - (a.uploaded || 0)));
}
async function filesSummary() {
  const files = await filesList();
  const size = files.reduce((s, f) => s + (f.size || 0), 0);
  let limit = 0;
  try { limit = (await storageLimit()).limit; } catch {}
  return { count: files.length, size, limit };
}
function cleanFileName(raw, fallback = "upload.bin") {
  const name = String(raw || "").replace(/\\/g, "/").split("/").pop().replace(/[\x00-\x1f\x7f]/g, "").replace(/[<>:"|?*]/g, "_").trim().slice(0, 200) || fallback;
  if (name === "." || name === "..") fail(400, "That file name isn't allowed.");
  return name;
}
function cleanFolderName(raw) {
  const name = String(raw || "").replace(/[\x00-\x1f\x7f/\\]/g, "").trim().slice(0, 60);
  if (!name || name === "." || name === "..") fail(400, "Give the folder a name.");
  return name;
}
const newFileId = () => crypto.randomBytes(9).toString("base64url").slice(0, 12);
function checkFolder(all, id) {
  if (id === null || id === undefined || id === "") return null;
  if (typeof id !== "string" || !FOLDER_ID.test(id) || !all[id]) fail(400, "That folder doesn't exist.");
  return id;
}
function siblingTaken(all, parent, name, except) {
  const n = name.toLowerCase();
  return Object.entries(all).some(([id, f]) => id !== except && (f.parent || null) === (parent || null) && f.name.toLowerCase() === n);
}
// All folder ids inside `root` (including itself).
function subtree(all, root) {
  const out = new Set([root]);
  let grew = true;
  while (grew) {
    grew = false;
    for (const [id, f] of Object.entries(all)) if (f.parent && out.has(f.parent) && !out.has(id)) { out.add(id); grew = true; }
  }
  return out;
}

/* ---- storage limit ---- */
// "auto": whatever fits on the disk while keeping a reserve free for the system.
async function diskInfo() {
  const s = await fsp.statfs(FILES_DIR);
  return { total: s.blocks * s.bsize, free: s.bavail * s.bsize };
}
async function storageLimit() {
  const [prefs, used, disk] = await Promise.all([readPrefs(), storageUsed(), diskInfo()]);
  const st = prefs.storage || {};
  const reserve = Math.max(15 * GB, disk.total * 0.1);
  const max = Math.max(0, Math.floor(used + disk.free - reserve));
  const custom = st.mode === "custom" && st.gb > 0;
  return { mode: custom ? "custom" : "auto", gb: custom ? st.gb : null, limit: custom ? Math.min(st.gb * GB, max) : max, used, max, reserve, disk };
}
const gbStr = (b) => `${Math.round(b / GB)} GB`;
async function ensureRoom(bytes) {
  const s = await storageLimit();
  if (s.used + bytes > s.limit) fail(507, `Storage is full (${gbStr(s.limit)}). Delete something or raise the limit in File Host settings.`);
}

/* ---- results of the tools land in the file host ---- */
// Finished converter/downloader/... jobs are hard-linked into their system
// folder: same data on disk, and the copy outlives the 7-day download cleanup.
const IMPORTED_FILE = path.join(FILES_DIR, ".panel", "imported.json");
let importing = null;
function importJobs() {
  if (!importing) importing = doImport().catch(() => {}).finally(() => { importing = null; });
  return importing;
}
async function doImport() {
  let st;
  try { st = JSON.parse(await fsp.readFile(IMPORTED_FILE, "utf8")); } catch { st = null; }
  if (!st || typeof st.since !== "number") st = { since: Date.now(), jobs: {} };
  let changed = false;
  for (const [kind, folder] of Object.entries(KIND_FOLDER)) {
    let d;
    try { d = await media(`/internal/downloads?kind=${kind}`, { timeout: 4000 }); } catch { continue; }
    for (const j of d.jobs || []) {
      if (j.state !== "done" || !j.file || !DL_ID.test(j.id || "") || st.jobs[j.id] || (j.created || 0) < st.since) continue;
      const src = path.join(DOWNLOADS_DIR, j.id, path.basename(j.file));
      let size;
      try { size = (await fsp.stat(src)).size; } catch { continue; }
      try { await ensureRoom(size); } catch { continue; }   // full: try again later
      const id = newFileId(), name = cleanFileName(j.file, "result.bin"), dir = path.join(FILES_DIR, id);
      await fsp.mkdir(dir, { mode: 0o755 });
      try { await fsp.link(src, path.join(dir, name)); }
      catch { try { await fsp.copyFile(src, path.join(dir, name)); } catch { await fsp.rm(dir, { recursive: true, force: true }); continue; } }
      await withIndex(async (idx, save) => { idx[id] = { name, size, uploaded: Date.now(), folder, from: kind }; await save(); });
      st.jobs[j.id] = Date.now();
      changed = true;
    }
  }
  const old = Date.now() - 30 * 86400000;
  for (const [k, t] of Object.entries(st.jobs)) if (t < old) { delete st.jobs[k]; changed = true; }
  if (changed || !(await fsp.stat(IMPORTED_FILE).catch(() => null))) {
    await fsp.mkdir(path.dirname(IMPORTED_FILE), { recursive: true, mode: 0o755 });
    await fsp.writeFile(`${IMPORTED_FILE}.tmp`, JSON.stringify(st));
    await fsp.rename(`${IMPORTED_FILE}.tmp`, IMPORTED_FILE);
  }
}
setInterval(() => importJobs(), 15000).unref();
setTimeout(() => importJobs(), 5000).unref();

/* ---- upload keys (iPad / iPhone shortcut) ---- */
// A key can only upload into "Uploads". Only its SHA-256 is stored.
const KEYS_FILE = path.join(FILES_DIR, ".panel", "upload-keys.json");
const KEY_ID = /^[A-Za-z0-9_-]{8}$/;
const sha256 = (s) => crypto.createHash("sha256").update(s).digest("hex");
async function readKeys() {
  try { const k = JSON.parse(await fsp.readFile(KEYS_FILE, "utf8")); return Array.isArray(k) ? k : []; }
  catch { return []; }
}
async function writeKeys(keys) {
  await fsp.mkdir(path.dirname(KEYS_FILE), { recursive: true, mode: 0o755 });
  await fsp.writeFile(`${KEYS_FILE}.tmp`, JSON.stringify(keys), { mode: 0o600 });
  await fsp.rename(`${KEYS_FILE}.tmp`, KEYS_FILE);
}
const TYPE_EXT = { "image/jpeg": "jpg", "image/png": "png", "image/heic": "heic", "image/heif": "heif", "image/gif": "gif", "image/webp": "webp",
  "video/mp4": "mp4", "video/quicktime": "mov", "audio/mpeg": "mp3", "audio/mp4": "m4a", "audio/x-m4a": "m4a", "audio/wav": "wav", "audio/x-wav": "wav",
  "application/pdf": "pdf", "application/zip": "zip", "text/plain": "txt", "application/json": "json", "text/csv": "csv" };
let keyFails = [];
async function keyUpload(req, res, url) {
  const now = Date.now();
  keyFails = keyFails.filter((t) => now - t < 15 * 60000);
  if (keyFails.length >= 30) fail(429, "Too many wrong keys. Try again later.");
  const m = String(req.headers.authorization || "").match(/^Bearer\s+(gk_[A-Za-z0-9_-]{20,80})$/);
  const keys = await readKeys();
  const hash = m ? sha256(m[1]) : "";
  const key = m && keys.find((k) => k.hash.length === hash.length && crypto.timingSafeEqual(Buffer.from(k.hash), Buffer.from(hash)));
  if (!key) {
    keyFails.push(now);
    await sleep(600);
    fail(401, "Wrong or revoked upload key.");
  }
  // name from ?name= or the X-Name header (Shortcuts sends UTF-8 bytes); the
  // extension comes from the Content-Type when the name has none (photos: "IMG_1234")
  let rawName = url.searchParams.get("name") || "";
  if (!rawName && req.headers["x-name"]) rawName = Buffer.from(String(req.headers["x-name"]), "latin1").toString("utf8");
  const type = String(req.headers["content-type"] || "").split(";")[0].trim().toLowerCase();
  const ext = TYPE_EXT[type] || "";
  let name = cleanFileName(rawName, `upload-${new Date().toISOString().slice(0, 16).replace(/[T:]/g, "-")}`);
  if (!/.[A-Za-z0-9]{1,8}$/.test(name) && ext) name += `.${ext}`;
  const declared = +(req.headers["content-length"] || 0);
  if (declared > MAX_UPLOAD_BYTES) fail(413, "Files can be up to 2 GB.");
  await ensureRoom(declared);
  const id = newFileId(), dir = path.join(FILES_DIR, id);
  await fsp.mkdir(dir, { mode: 0o755 });
  let size;
  try { size = await streamTo(req, path.join(dir, name), MAX_UPLOAD_BYTES); }
  catch (e) { await fsp.rm(dir, { recursive: true, force: true }); throw e; }
  if (!size) { await fsp.rm(dir, { recursive: true, force: true }); fail(400, "The file was empty."); }
  await withIndex(async (idx, save) => { idx[id] = { name, size, uploaded: Date.now(), folder: "sys-uploads", from: "key" }; await save(); });
  key.used = Date.now(); key.uses = (key.uses || 0) + 1;
  await writeKeys(keys).catch(() => {});
  const link = `https://${req.headers.host}/f/${id}/${encodeURIComponent(name)}`;
  res.writeHead(200, { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store" });
  res.end(link);
}

async function fileRoutes(req, res, p, m, url) {
  if (p === "/api/files" && m === "GET") {
    await importJobs();
    const [{ files, folders }, lim] = await Promise.all([
      withIndex((idx, _s, folders) => ({
        files: Object.entries(idx).map(([id, e]) => ({ id, ...e })).sort((a, b) => (b.uploaded || 0) - (a.uploaded || 0)),
        folders: Object.entries(allFolders(folders)).map(([id, f]) => ({ id, ...f })),
      })),
      storageLimit(),
    ]);
    const total = files.reduce((s, f) => s + (f.size || 0), 0);
    return send(res, 200, { files, folders, total, used: lim.used, limit: lim.limit, storage: { mode: lim.mode, gb: lim.gb, max: lim.max, reserve: lim.reserve, disk: lim.disk } });
  }
  if (p === "/api/files/upload" && m === "POST") {
    const name = cleanFileName(url.searchParams.get("name"));
    const declared = +(req.headers["content-length"] || 0);
    if (declared > MAX_UPLOAD_BYTES) fail(413, "Files can be up to 2 GB.");
    await ensureRoom(declared);
    const want = url.searchParams.get("folder") || null;
    await withIndex((_i, _s, folders) => checkFolder(allFolders(folders), want));
    const id = newFileId();
    const dir = path.join(FILES_DIR, id);
    await fsp.mkdir(dir, { mode: 0o755 });
    let size;
    try { size = await streamTo(req, path.join(dir, name), MAX_UPLOAD_BYTES); }
    catch (e) { await fsp.rm(dir, { recursive: true, force: true }); throw e; }
    await withIndex(async (idx, save, folders) => {
      // the folder may have been deleted while the upload ran
      const folder = want && allFolders(folders)[want] ? want : null;
      idx[id] = { name, size, uploaded: Date.now(), folder };
      await save();
    });
    return send(res, 200, { ok: true, id, name, size, link: `/f/${id}/${encodeURIComponent(name)}` });
  }

  /* settings: storage limit */
  if (p === "/api/files/settings" && m === "PUT") {
    const b = await readJson(req);
    const prefs = await readPrefs();
    if (b.mode === "custom") {
      const gb = Math.round(+b.gb);
      if (!Number.isFinite(gb) || gb < 1 || gb > 100000) fail(400, "Pick a size in GB.");
      prefs.storage = { mode: "custom", gb };
    } else prefs.storage = { mode: "auto" };
    await fsp.mkdir(path.dirname(PREFS_FILE), { recursive: true, mode: 0o755 });
    await fsp.writeFile(`${PREFS_FILE}.tmp`, JSON.stringify(prefs));
    await fsp.rename(`${PREFS_FILE}.tmp`, PREFS_FILE);
    const lim = await storageLimit();
    return send(res, 200, { ok: true, limit: lim.limit, max: lim.max, mode: lim.mode, gb: lim.gb });
  }

  /* upload keys */
  if (p === "/api/files/keys" && m === "GET") {
    return send(res, 200, { keys: (await readKeys()).map(({ id, name, created, used, uses }) => ({ id, name, created, used: used || null, uses: uses || 0 })) });
  }
  if (p === "/api/files/keys" && m === "POST") {
    const b = await readJson(req);
    const name = String(b.name || "").replace(/[\x00-\x1f\x7f]/g, "").trim().slice(0, 40) || "iPad";
    const keys = await readKeys();
    if (keys.length >= 20) fail(400, "That's enough keys. Delete an old one first.");
    const token = `gk_${crypto.randomBytes(24).toString("base64url")}`;
    const id = crypto.randomBytes(6).toString("base64url").slice(0, 8);
    keys.push({ id, name, hash: sha256(token), created: Date.now(), uses: 0 });
    await writeKeys(keys);
    return send(res, 200, { ok: true, id, name, token });
  }
  {
    const k = p.match(/^\/api\/files\/keys\/([^/]+)$/);
    if (k && m === "DELETE") {
      if (!KEY_ID.test(k[1])) fail(400, "Invalid key.");
      const keys = await readKeys();
      const left = keys.filter((x) => x.id !== k[1]);
      if (left.length === keys.length) fail(404, "Key not found.");
      await writeKeys(left);
      return send(res, 200, { ok: true });
    }
  }

  /* folders */
  if (p === "/api/files/folders" && m === "POST") {
    const b = await readJson(req);
    const name = cleanFolderName(b.name);
    return send(res, 200, await withIndex(async (_idx, save, folders) => {
      const all = allFolders(folders);
      const parent = checkFolder(all, b.parent);
      if (Object.keys(folders).length >= 500) fail(400, "That's a lot of folders. Delete a few first.");
      if (siblingTaken(all, parent, name)) fail(409, `There's already a folder called "${name}" here.`);
      const id = `f-${crypto.randomBytes(8).toString("base64url").slice(0, 10)}`;
      folders[id] = { name, parent, created: Date.now() };
      await save();
      return { ok: true, id };
    }));
  }
  {
    const f = p.match(/^\/api\/files\/folders\/([^/]+)$/);
    if (f && (m === "PATCH" || m === "DELETE")) {
      const id = f[1];
      if (!FOLDER_ID.test(id)) fail(400, "Invalid folder.");
      if (SYS_FOLDERS[id]) fail(400, "Built-in folders can't be changed.");
      if (m === "PATCH") {
        const b = await readJson(req);
        return send(res, 200, await withIndex(async (_idx, save, folders) => {
          if (!folders[id]) fail(404, "Folder not found.");
          const name = cleanFolderName(b.name);
          if (siblingTaken(allFolders(folders), folders[id].parent, name, id)) fail(409, `There's already a folder called "${name}" here.`);
          folders[id].name = name;
          await save();
          return { ok: true };
        }));
      }
      // DELETE: removes the folder with everything inside it
      const gone = await withIndex(async (idx, save, folders) => {
        if (!folders[id]) fail(404, "Folder not found.");
        const tree = subtree(allFolders(folders), id);
        const fileIds = Object.keys(idx).filter((fid) => idx[fid].folder && tree.has(idx[fid].folder));
        for (const fid of fileIds) delete idx[fid];
        for (const fid of tree) delete folders[fid];
        await save();
        return fileIds;
      });
      await Promise.all(gone.map((fid) => fsp.rm(path.join(FILES_DIR, fid), { recursive: true, force: true })));
      return send(res, 200, { ok: true, deleted: gone.length });
    }
  }

  /* move / delete many */
  if (p === "/api/files/move" && m === "POST") {
    const b = await readJson(req, 65536);
    const fileIds = Array.isArray(b.files) ? b.files.filter((x) => typeof x === "string" && FILE_ID.test(x)).slice(0, 2000) : [];
    const folderIds = Array.isArray(b.folders) ? b.folders.filter((x) => typeof x === "string" && FOLDER_ID.test(x)).slice(0, 500) : [];
    return send(res, 200, await withIndex(async (idx, save, folders) => {
      const all = allFolders(folders);
      const to = checkFolder(all, b.to);
      for (const fid of folderIds) {
        if (SYS_FOLDERS[fid]) fail(400, "Built-in folders can't be moved.");
        if (!folders[fid]) fail(404, "Folder not found.");
        if (to && subtree(all, fid).has(to)) fail(400, "A folder can't go inside itself.");
        if ((folders[fid].parent || null) !== to && siblingTaken(all, to, folders[fid].name, fid)) fail(409, `There's already a folder called "${folders[fid].name}" there.`);
      }
      for (const fid of folderIds) folders[fid].parent = to;
      let moved = 0;
      for (const fid of fileIds) if (idx[fid]) { idx[fid].folder = to; moved++; }
      await save();
      return { ok: true, moved: moved + folderIds.length };
    }));
  }
  if (p === "/api/files/delete" && m === "POST") {
    const b = await readJson(req, 65536);
    const ids = Array.isArray(b.files) ? b.files.filter((x) => typeof x === "string" && FILE_ID.test(x)).slice(0, 2000) : [];
    const gone = await withIndex(async (idx, save) => {
      const out = ids.filter((id) => idx[id]);
      for (const id of out) delete idx[id];
      await save();
      return out;
    });
    await Promise.all(gone.map((id) => fsp.rm(path.join(FILES_DIR, id), { recursive: true, force: true })));
    return send(res, 200, { ok: true, deleted: gone.length });
  }

  /* single file: rename / delete */
  const one = p.match(/^\/api\/files\/([^/]+)$/);
  if (one && m === "PATCH") {
    const id = one[1];
    if (!FILE_ID.test(id)) fail(400, "Invalid file id.");
    const b = await readJson(req);
    const name = cleanFileName(b.name, "");
    if (!name) fail(400, "Give the file a name.");
    return send(res, 200, await withIndex(async (idx, save) => {
      const e = idx[id];
      if (!e) fail(404, "File not found.");
      if (e.name !== name) {
        await fsp.rename(path.join(FILES_DIR, id, e.name), path.join(FILES_DIR, id, name));
        e.name = name;
        await save();
      }
      return { ok: true, link: `/f/${id}/${encodeURIComponent(name)}` };
    }));
  }
  if (one && m === "DELETE") {
    const id = one[1];
    if (!FILE_ID.test(id)) fail(400, "Invalid file id.");
    const found = await withIndex(async (idx, save) => {
      if (!idx[id]) return false;
      delete idx[id];
      await save();
      return true;
    });
    if (!found) fail(404, "File not found.");
    await fsp.rm(path.join(FILES_DIR, id), { recursive: true, force: true });
    return send(res, 200, { ok: true });
  }
  fail(404, "Not found.");
}

/* ============================ Media service ============================ */
// The "media" container (ffmpeg + yt-dlp) restreams YouTube audio and makes
// Discord-friendly copies of clips. Its /internal API is only reachable on the
// Docker network and additionally needs the shared token.
const MEDIA_TOKEN = process.env.MEDIA_TOKEN || "";
const MEDIA_HOST = process.env.MEDIA_HOST || "media";
const MEDIA_PORT = +(process.env.MEDIA_PORT || 8000);
function media(p, { method = "GET", body, timeout = 8000 } = {}) {
  return new Promise((resolve, reject) => {
    const data = body === undefined ? null : Buffer.from(JSON.stringify(body));
    const headers = { "X-Media-Token": MEDIA_TOKEN };
    if (data) { headers["Content-Type"] = "application/json"; headers["Content-Length"] = data.length; }
    const req = http.request({ host: MEDIA_HOST, port: MEDIA_PORT, path: p, method, timeout, headers }, (res) => {
      const parts = [];
      res.on("data", (c) => parts.push(c));
      res.on("end", () => {
        let j = {};
        try { j = JSON.parse(Buffer.concat(parts).toString("utf8") || "{}"); } catch {}
        if (res.statusCode >= 400) reject(new HttpError(res.statusCode >= 500 ? 502 : res.statusCode, j.error || "The media service failed."));
        else resolve(j);
      });
    });
    req.on("timeout", () => req.destroy(new HttpError(504, "The media service didn't answer in time.")));
    req.on("error", (e) => reject(e instanceof HttpError ? e : new HttpError(503, "The media service isn't running.")));
    if (data) req.write(data);
    req.end();
  });
}

/* ============================== Clip hoster ============================== */
// Videos live in /files/clips/<id>/clip.<ext> with a clip.jpg poster that the
// browser renders from the first second before upload. nginx serves both under
// /v/<id>.<ext>; the panel renders the public player page /c/<id> with the
// Open Graph tags Discord and WhatsApp need for an inline video embed.
const CLIPS_DIR = path.join(FILES_DIR, "clips");
const CLIPS_INDEX = path.join(CLIPS_DIR, "index.json");
const CLIP_ID = /^[A-Za-z0-9_-]{10}$/;
const CLIP_EXT = { mp4: "video/mp4", webm: "video/webm", mov: "video/quicktime", m4v: "video/mp4" };
const MAX_THUMB = 3 * 1024 * 1024;
const TITLE = /^[^\x00-\x1f\x7f]{1,100}$/;

let clipLock = Promise.resolve();
function withClips(fn) {
  const run = clipLock.then(async () => {
    let idx = {};
    try { idx = JSON.parse(await fsp.readFile(CLIPS_INDEX, "utf8")); }
    catch (e) { if (e.code !== "ENOENT") throw e; }
    return fn(idx, async () => {
      await fsp.mkdir(CLIPS_DIR, { recursive: true, mode: 0o755 });
      await fsp.writeFile(`${CLIPS_INDEX}.tmp`, JSON.stringify(idx));
      await fsp.rename(`${CLIPS_INDEX}.tmp`, CLIPS_INDEX);
    });
  });
  clipLock = run.catch(() => {});
  return run;
}
// The media service writes /files/clips/<id>/meta.json while it builds the
// Discord copy (embed.mp4) and, if needed, a poster.
async function clipMeta(id) {
  try { return JSON.parse(await fsp.readFile(path.join(CLIPS_DIR, id, "meta.json"), "utf8")); }
  catch { return null; }
}
function applyMeta(c, m) {
  if (!m) return c;
  c.state = m.state;
  if (m.state === "processing") c.progress = m.progress || 0;
  c.embed = m.state === "ready" && !!m.embed;
  c.stream = m.state === "ready" && !!m.stream;
  if (m.ew) { c.ew = m.ew; c.eh = m.eh; }
  if (m.poster) c.thumb = true;
  if (m.web_ok === false) c.web_ok = false;
  for (const k of ["duration", "w", "h"]) if (!c[k] && m[k]) c[k] = m[k];
  return c;
}
async function clipsList() {
  const list = await withClips((idx) => Object.entries(idx).map(([id, c]) => ({ id, ...c }))
    .sort((a, b) => (b.uploaded || 0) - (a.uploaded || 0)));
  await Promise.all(list.map(async (c) => applyMeta(c, await clipMeta(c.id))));
  return list;
}
async function clipsSummary() {
  const clips = await clipsList();
  return {
    count: clips.length,
    size: clips.reduce((s, c) => s + (c.size || 0), 0),
    views: clips.reduce((s, c) => s + (c.views || 0), 0),
    latest: clips.slice(0, 3).map((c) => ({ id: c.id, title: c.title, thumb: !!c.thumb })),
  };
}
async function filesSize() {
  return (await filesList()).reduce((s, f) => s + (f.size || 0), 0);
}
async function storageUsed() {
  const [f, c] = await Promise.all([filesSize(), clipsSummary()]);
  return f + c.size;
}

async function clipRoutes(req, res, p, m, url) {
  if (p === "/api/clips" && m === "GET") {
    const clips = await clipsList();
    return send(res, 200, { clips, total: clips.reduce((s, c) => s + (c.size || 0), 0), ...(await storageLimit().then((l) => ({ used: l.used, limit: l.limit }))) });
  }
  if (p === "/api/clips/upload" && m === "POST") {
    const raw = String(url.searchParams.get("name") || "");
    const ext = raw.includes(".") ? raw.split(".").pop().toLowerCase() : "";
    if (!CLIP_EXT[ext]) fail(400, "Clips must be MP4, WebM or MOV videos.");
    let title = String(url.searchParams.get("title") || raw.replace(/\.[^.]+$/, "")).replace(/[\x00-\x1f\x7f]/g, "").trim().slice(0, 100);
    if (!title) title = "Untitled clip";
    const declared = +(req.headers["content-length"] || 0);
    if (declared > MAX_UPLOAD_BYTES) fail(413, "Clips can be up to 2 GB.");
    await ensureRoom(declared);
    const id = crypto.randomBytes(8).toString("base64url").slice(0, 10);
    const dir = path.join(CLIPS_DIR, id);
    await fsp.mkdir(dir, { recursive: true, mode: 0o755 });
    let size;
    try { size = await streamTo(req, path.join(dir, `clip.${ext}`), MAX_UPLOAD_BYTES); }
    catch (e) { await fsp.rm(dir, { recursive: true, force: true }); throw e; }
    await withClips(async (idx, save) => { idx[id] = { title, ext, size, uploaded: Date.now(), views: 0, thumb: false }; await save(); });
    media(`/internal/clips/${id}/process`, { method: "POST" }).catch((e) => console.log(`clip ${id}: media service not reached (${e.message})`));
    return send(res, 200, { ok: true, id, link: `/c/${id}` });
  }
  const mm = p.match(/^\/api\/clips\/([^/]+)(\/thumb)?$/);
  if (!mm || !CLIP_ID.test(mm[1])) fail(404, "Not found.");
  const id = mm[1];
  if (mm[2] && m === "PUT") {
    const num = (k, max) => { const v = +url.searchParams.get(k); return Number.isFinite(v) && v > 0 && v <= max ? v : null; };
    const meta = { duration: num("duration", 86400), w: num("w", 8192), h: num("h", 8192) };
    const body = await readBody(req, MAX_THUMB);
    const isJpeg = body.length > 3 && body[0] === 0xff && body[1] === 0xd8 && body[2] === 0xff;
    if (body.length && !isJpeg) fail(400, "Thumbnails must be JPEG images.");
    const found = await withClips(async (idx, save) => {
      if (!idx[id]) return false;
      if (isJpeg) {
        await fsp.writeFile(path.join(CLIPS_DIR, id, "clip.jpg.tmp"), body, { mode: 0o644 });
        await fsp.rename(path.join(CLIPS_DIR, id, "clip.jpg.tmp"), path.join(CLIPS_DIR, id, "clip.jpg"));
        idx[id].thumb = true;
      }
      for (const [k, v] of Object.entries(meta)) if (v) idx[id][k] = k === "duration" ? Math.round(v * 10) / 10 : Math.round(v);
      await save();
      return true;
    });
    if (!found) fail(404, "Clip not found.");
    return send(res, 200, { ok: true });
  }
  if (!mm[2] && m === "POST" && url.searchParams.get("action") === "optimize") {
    if (!(await withClips(async (idx) => !!idx[id]))) fail(404, "Clip not found.");
    return send(res, 200, await media(`/internal/clips/${id}/process?force=1`, { method: "POST" }));
  }
  if (!mm[2] && m === "PATCH") {
    const { title } = await readJson(req);
    const t = String(title ?? "").trim();
    if (!TITLE.test(t)) fail(400, "Titles can be 1 to 100 characters.");
    const found = await withClips(async (idx, save) => { if (!idx[id]) return false; idx[id].title = t; await save(); return true; });
    if (!found) fail(404, "Clip not found.");
    return send(res, 200, { ok: true });
  }
  if (!mm[2] && m === "DELETE") {
    const found = await withClips(async (idx, save) => { if (!idx[id]) return false; delete idx[id]; await save(); return true; });
    if (!found) fail(404, "Clip not found.");
    await fsp.rm(path.join(CLIPS_DIR, id), { recursive: true, force: true });
    return send(res, 200, { ok: true });
  }
  fail(404, "Not found.");
}

// ---- public player page ----
const viewSeen = new Map();
const CRAWLER = /bot|crawl|spider|preview|discord|whatsapp|telegram|slack|facebookexternalhit|twitter|embed|curl|wget/i;
function publicOrigin(req) {
  if (/^https:\/\/[a-z0-9.-]+$/i.test(process.env.PUBLIC_URL || "")) return process.env.PUBLIC_URL;
  const host = String(req.headers.host || "");
  if (!/^[a-z0-9.-]+(:\d{1,5})?$/i.test(host)) return "";
  return `${/^(localhost|127\.)/.test(host) ? "http" : "https"}://${host}`;
}
const htmlEsc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
function clipTime(s) {
  if (!s) return "";
  s = Math.round(s);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}
async function clipPage(req, res, id) {
  const page = (status, html) => {
    res.writeHead(status, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-cache" });
    res.end(html);
  };
  const c = CLIP_ID.test(id) ? await withClips(async (idx, save) => {
    const clip = idx[id];
    if (!clip) return null;
    const ua = String(req.headers["user-agent"] || "");
    const key = `${clientIp(req)}|${id}`, now = Date.now();
    if (!CRAWLER.test(ua) && !(viewSeen.get(key) > now - 6 * 3600 * 1000)) {
      viewSeen.set(key, now);
      if (viewSeen.size > 5000) for (const [k, t] of viewSeen) if (t < now - 6 * 3600 * 1000) viewSeen.delete(k);
      clip.views = (clip.views || 0) + 1;
      await save();
    }
    return { ...clip };
  }) : null;
  if (c) applyMeta(c, await clipMeta(id));
  if (!c) return page(404, CLIP_HTML({ missing: true }));
  const origin = publicOrigin(req);
  // Discord fetches the video but won't show video embeds from this (.ts.net) address,
  // and then drops the whole preview. So links get an image card; ?e=vid keeps the
  // video tags for a future own domain.
  const imgOnly = !/[?&]e=vid(&|$)/.test(req.url || "");
  return page(200, CLIP_HTML({ id, c, origin, imgOnly }));
}
function CLIP_HTML({ id, c, origin, missing, imgOnly }) {
  const title = missing ? "Clip not found" : c.title;
  const original = missing ? "" : `/v/${id}.${c.ext}`;
  const embed = !missing && c.embed ? `/v/${id}/embed.mp4` : "";
  // Play the light stream copy when there is one (smooth over a home connection),
  // else the original unless the browser can't decode it (HEVC etc.).
  const stream = !missing && c.stream ? `/v/${id}/stream.mp4` : "";
  const canOriginal = !missing && c.web_ok !== false;
  const video = stream || (!missing && c.web_ok === false && embed ? embed : original);
  // Discord and friends get the small H.264 copy with the index at the front.
  const share = embed || original;
  const shareType = embed ? "video/mp4" : missing ? "" : CLIP_EXT[c.ext];
  const sw = missing ? 0 : (embed && c.ew) || c.w, sh = missing ? 0 : (embed && c.eh) || c.h;
  const poster = !missing && c.thumb ? `/v/${id}.jpg` : "";
  const meta = missing ? "" : [clipTime(c.duration), `${c.views || 0} view${c.views === 1 ? "" : "s"}`,
    new Date(c.uploaded).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })].filter(Boolean).join(" · ");
  const og = missing ? "" : `
<meta property="og:site_name" content="__BRAND__ clips">
<meta property="og:title" content="${htmlEsc(title)}">
<meta property="og:description" content="${htmlEsc(meta)}">
<meta property="og:type" content="${imgOnly ? "website" : "video.other"}">
<meta property="og:url" content="${origin}/c/${id}">
${poster ? `<meta property="og:image" content="${origin}${poster}">
<meta property="og:image:secure_url" content="${origin}${poster}">
<meta property="og:image:type" content="image/jpeg">` : ""}
${poster && sw ? `<meta property="og:image:width" content="${sw}">\n<meta property="og:image:height" content="${sh}">` : ""}
${imgOnly ? `<meta name="twitter:card" content="summary_large_image">
<meta name="twitter:title" content="${htmlEsc(title)}">
${poster ? `<meta name="twitter:image" content="${origin}${poster}">` : ""}` : `<meta property="og:video" content="${origin}${share}">
<meta property="og:video:secure_url" content="${origin}${share}">
<meta property="og:video:type" content="${shareType}">
${sw ? `<meta property="og:video:width" content="${sw}">\n<meta property="og:video:height" content="${sh}">` : ""}
<meta name="twitter:card" content="player">
<meta name="twitter:title" content="${htmlEsc(title)}">
<meta name="twitter:description" content="${htmlEsc(meta)}">
${poster ? `<meta name="twitter:image" content="${origin}${poster}">` : ""}
<meta name="twitter:player" content="${origin}/c/${id}">
<meta name="twitter:player:stream" content="${origin}${share}">
${sw ? `<meta name="twitter:player:width" content="${sw}">\n<meta name="twitter:player:height" content="${sh}">` : ""}`}`;
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>${htmlEsc(title)} · __BRAND__</title>
<meta name="theme-color" content="#6ea4ff">
<meta name="robots" content="noindex">
<link rel="icon" type="image/png" href="/img/__ICON__-logo-64.png">${og}
<style>
@font-face { font-family: "Geist"; src: url("/fonts/geist.woff2") format("woff2"); font-weight: 100 900; font-display: swap; }
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; }
body { background: #05070f; color: #eef1f7; font: 400 15px/1.5 "Geist", system-ui, sans-serif; letter-spacing: -.01em; -webkit-font-smoothing: antialiased; }
body::before { content: ""; position: fixed; inset: 0; z-index: -1; background: radial-gradient(1000px 520px at 50% -12%, rgba(110,164,255,.16), transparent 70%), url("/img/backdrop.jpg") center 75% / cover no-repeat; opacity: .35; }
a { color: inherit; text-decoration: none; }
.top { display: flex; align-items: center; justify-content: space-between; max-width: 1180px; margin: 0 auto; padding: 18px 20px; }
.brand { display: inline-flex; align-items: center; gap: 10px; font-weight: 600; letter-spacing: -.035em; }
.brand img { width: 26px; height: 26px; border-radius: 50%; box-shadow: 0 0 18px rgba(110,164,255,.4); }
main { max-width: 1180px; margin: 0 auto; padding: 8px 20px 60px; }
.player { position: relative; border-radius: 22px; overflow: hidden; background: #000; border: 1px solid rgba(255,255,255,.08); box-shadow: 0 30px 90px rgba(0,0,0,.55), 0 0 0 1px rgba(110,164,255,.06); }
video { display: block; width: 100%; max-height: 78vh; background: #000; }
.buf { position: absolute; inset: 0; display: grid; place-items: center; background: rgba(5,7,15,.55); -webkit-backdrop-filter: blur(6px); backdrop-filter: blur(6px); opacity: 0; pointer-events: none; transition: opacity .25s; }
.buf.on { opacity: 1; pointer-events: auto; }
.ring { position: relative; width: 84px; height: 84px; }
.ring svg { width: 100%; height: 100%; transform: rotate(-90deg); }
.ring circle { fill: none; stroke-width: 5; }
.ring .bg { stroke: rgba(255,255,255,.12); }
.ring .fg { stroke: #6ea4ff; stroke-linecap: round; stroke-dasharray: 226; stroke-dashoffset: 226; transition: stroke-dashoffset .4s ease; filter: drop-shadow(0 0 6px rgba(110,164,255,.6)); }
.ring b { position: absolute; inset: 0; display: grid; place-items: center; font-size: 17px; font-weight: 600; font-variant-numeric: tabular-nums; }
.skip { position: absolute; bottom: 16px; right: 16px; height: 32px; padding: 0 14px; border-radius: 999px; border: 1px solid rgba(255,255,255,.18); background: rgba(255,255,255,.06); color: rgba(238,241,247,.8); font: 500 13px "Geist", system-ui, sans-serif; cursor: pointer; }
.skip:hover { background: rgba(255,255,255,.12); color: #fff; }
.q { position: absolute; top: 14px; right: 14px; z-index: 2; height: 30px; padding: 0 12px; border-radius: 999px; border: 1px solid rgba(255,255,255,.18); background: rgba(5,7,15,.55); -webkit-backdrop-filter: blur(8px); backdrop-filter: blur(8px); color: rgba(238,241,247,.85); font: 600 12px "Geist", system-ui, sans-serif; letter-spacing: .04em; cursor: pointer; opacity: 0; transition: opacity .2s; }
.player:hover .q, .q:focus-visible, .q.hd { opacity: 1; }
.q.hd { background: #6ea4ff; border-color: transparent; color: #050b1a; }
@media (hover: none) { .q { opacity: 1; } }
h1 { margin: 22px 0 6px; font-size: clamp(22px, 3vw, 30px); font-weight: 600; letter-spacing: -.035em; line-height: 1.2; overflow-wrap: anywhere; }
.row { display: flex; align-items: center; justify-content: space-between; gap: 14px; flex-wrap: wrap; }
.meta { color: rgba(238,241,247,.6); font-variant-numeric: tabular-nums; }
.acts { display: flex; gap: 8px; }
.btn { display: inline-flex; align-items: center; gap: 8px; height: 40px; padding: 0 16px; border-radius: 999px; border: 1px solid rgba(255,255,255,.13); background: rgba(255,255,255,.05); color: #eef1f7; font: 500 14px "Geist", system-ui, sans-serif; cursor: pointer; }
.btn:hover { background: rgba(255,255,255,.09); }
.btn.primary { background: #6ea4ff; border-color: transparent; color: #050b1a; }
.btn.primary:hover { background: #8cb8ff; }
.btn svg { width: 17px; height: 17px; }
.missing { display: grid; place-items: center; min-height: 60vh; text-align: center; color: rgba(238,241,247,.6); }
.missing b { display: block; margin-bottom: 6px; color: #eef1f7; font-size: 22px; }
.toast { position: fixed; left: 50%; bottom: 24px; transform: translate(-50%, 20px); opacity: 0; padding: 10px 18px; border-radius: 999px; background: #10131d; border: 1px solid rgba(255,255,255,.13); transition: .25s; }
.toast.on { opacity: 1; transform: translate(-50%, 0); }
@media (max-width: 600px) { .player { border-radius: 14px; margin: 0 -8px; } .acts { width: 100%; } .btn { flex: 1; justify-content: center; } }
</style>
</head>
<body>
<header class="top"><a class="brand" href="/">__BRAND__</a></header>
<main>
${missing ? `<div class="missing"><div><b>Clip not found</b>It was deleted or the link is wrong.</div></div>` : `
  <div class="player"><video id="vid" src="${video}" ${poster ? `poster="${poster}"` : ""} controls playsinline preload="auto"></video>
    <div class="buf" id="buf"><div class="ring"><svg viewBox="0 0 84 84"><circle class="bg" cx="42" cy="42" r="36"/><circle class="fg" id="ring" cx="42" cy="42" r="36"/></svg><b id="pct">0%</b></div><button class="skip" id="skip" type="button">play now</button></div>${stream && canOriginal ? `<button class="q" id="q" type="button" data-hd="${original}" data-sd="${stream}" aria-pressed="false">HD</button>` : ""}</div>
  <h1>${htmlEsc(title)}</h1>
  <div class="row">
    <div class="meta">${htmlEsc(meta)}</div>
    <div class="acts">
      <button class="btn" id="copy" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>Copy link</button>
      <a class="btn primary" href="${original}" download="${htmlEsc(title.replace(/[\\/:*?"<>|]/g, "_"))}.${c.ext}"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><path d="m7 10 5 5 5-5"/><path d="M12 15V3"/></svg>Download</a>
    </div>
  </div>
  <div class="toast" id="toast">Link copied</div>
<script>
// Buffer first, then play without stopping: hold playback until enough of the
// video is downloaded that the rest arrives before it's needed.
(() => {
  const v = document.getElementById("vid"), buf = document.getElementById("buf"), ring = document.getElementById("ring"), pct = document.getElementById("pct");
  let want = false, ours = false, samples = [], lastGrow = Date.now(), lastEnd = 0, timer = 0, heldAt = 0, shown = 0;
  const ahead = () => { for (let i = 0; i < v.buffered.length; i++) if (v.buffered.start(i) <= v.currentTime + .3 && v.buffered.end(i) > v.currentTime) return v.buffered.end(i); return v.currentTime; };
  function rate() {   // seconds of video downloaded per second of waiting, over the last few seconds
    const now = Date.now(), end = ahead();
    if (end > lastEnd + .01) { lastGrow = now; lastEnd = end; }
    samples.push([now, end]); while (samples.length > 2 && now - samples[0][0] > 5000) samples.shift();
    const [t0, e0] = samples[0];
    return now - t0 > 900 ? (end - e0) / ((now - t0) / 1000) : 0;
  }
  function ready() {
    const d = v.duration, end = ahead(), left = d - v.currentTime, r = rate();
    if (!isFinite(d) || !d) return { ok: false, p: 0 };
    if (end >= d - .25) return { ok: true, p: 1 };
    // the part still missing must arrive faster than we watch what's already here (+15% margin)
    const need = r > 0 ? (d - end) / r * 1.15 : Infinity, have = end - v.currentTime;
    const ok = have >= 3 && need <= left;
    // stuck (browser stopped fetching while paused) but a decent chunk is ready: go anyway
    const stalled = Date.now() - lastGrow > 4000 && have >= 8;
    const p = Math.min(.99, have / 3, isFinite(need) ? left / Math.max(need, .001) : 0);
    return { ok: ok || stalled, p: Math.max(0, p) };
  }
  function show(on) { buf.classList.toggle("on", on); }
  function tick() {
    if (!want) return;
    const s = ready();
    shown = Math.max(shown, s.p);   // only ever goes up
    pct.textContent = Math.round(shown * 100) + "%"; ring.style.strokeDashoffset = String(226 * (1 - shown));
    if (s.ok) { start(); return; }
    timer = setTimeout(tick, 400);
  }
  function hold() {   // stop and wait for the buffer
    want = true; heldAt = Date.now(); shown = 0; v.pause();
    samples = []; lastGrow = Date.now(); lastEnd = ahead();
    show(true); clearTimeout(timer); tick();
  }
  function start() {
    want = false; clearTimeout(timer);
    pct.textContent = "100%"; ring.style.strokeDashoffset = "0";
    setTimeout(() => show(false), 150);
    ours = true; v.play().catch(() => {}).finally(() => { ours = false; });
  }
  v.addEventListener("play", () => { if (ours || want) return; if (!ready().ok) hold(); });
  v.addEventListener("waiting", () => { if (!v.paused && !want) hold(); });
  // you paused it yourself while it was waiting: stop waiting
  v.addEventListener("pause", () => { if (want && Date.now() - heldAt > 400) { want = false; clearTimeout(timer); show(false); } });
  v.addEventListener("seeking", () => { samples = []; lastEnd = ahead(); });
  document.getElementById("skip").addEventListener("click", start);
  // HD: switch to the original file, keeping the spot (it may need to buffer first)
  const q = document.getElementById("q");
  if (q) q.addEventListener("click", () => {
    const hd = q.getAttribute("aria-pressed") !== "true", t = v.currentTime, playing = !v.paused || want;
    q.setAttribute("aria-pressed", String(hd)); q.classList.toggle("hd", hd);
    want = false; clearTimeout(timer); show(false);
    v.src = hd ? q.dataset.hd : q.dataset.sd;
    v.addEventListener("loadedmetadata", () => { v.currentTime = t; if (playing) { if (ready().ok) start(); else hold(); } }, { once: true });
  });
})();
document.getElementById("copy").addEventListener("click", async () => {
  try { await navigator.clipboard.writeText(location.href); const t = document.getElementById("toast"); t.classList.add("on"); setTimeout(() => t.classList.remove("on"), 1800); } catch {}
});
</script>`}
</main>
</body>
</html>`;
}

/* ================================ Music ================================ */
// YouTube links become radio stations: the media service restreams their audio
// as MP3 under /radio/<key>/<id>.mp3, so a phone can play it with the screen off.
async function musicSummary() {
  const d = await media("/internal/stations", { timeout: 2500 });
  return {
    count: d.stations.length,
    listeners: d.stations.reduce((n, x) => n + (x.listeners || 0), 0),
    on_air: d.stations.filter((x) => x.on_air).length,
    key: d.key,
    latest: d.stations.slice(0, 4).map((x) => ({ id: x.id, title: x.title, thumb: x.thumb })),
  };
}
async function musicRoutes(req, res, p, m) {
  if (p === "/api/music" && m === "GET") return send(res, 200, await media("/internal/stations"));
  if (p === "/api/music" && m === "POST") {
    const { url } = await readJson(req);
    return send(res, 200, await media("/internal/stations", { method: "POST", body: { url: String(url || "").slice(0, 500) }, timeout: 110000 }));
  }
  if (p === "/api/music/key" && m === "POST") return send(res, 200, await media("/internal/key", { method: "POST" }));
  const mm = p.match(/^\/api\/music\/([A-Za-z0-9_-]{10})(\/move)?$/);
  if (mm && !mm[2] && m === "PATCH") {
    const { title } = await readJson(req);
    const t = String(title ?? "").trim();
    if (!TITLE.test(t)) fail(400, "Names can be 1 to 100 characters.");
    return send(res, 200, await media(`/internal/stations/${mm[1]}`, { method: "PATCH", body: { title: t } }));
  }
  if (mm && mm[2] && m === "POST") {
    const { dir } = await readJson(req);
    return send(res, 200, await media(`/internal/stations/${mm[1]}/move`, { method: "POST", body: { dir: dir === "up" ? "up" : "down" } }));
  }
  if (mm && !mm[2] && m === "DELETE") return send(res, 200, await media(`/internal/stations/${mm[1]}`, { method: "DELETE" }));
  fail(404, "Not found.");
}

/* ============================== Downloader ============================== */
// The media service fetches the link with yt-dlp; files appear under /d/<id>/.
const DOWNLOADS_DIR = path.join(FILES_DIR, "downloads");
const DL_ID = /^[A-Za-z0-9_-]{10}$/;
async function downloadsSummary() {
  const d = await media("/internal/downloads", { timeout: 2500 });
  return {
    count: d.jobs.filter((j) => j.state === "done").length,
    active: d.jobs.filter((j) => ["queued", "downloading", "converting"].includes(j.state)).length,
    size: d.total,
    latest: d.jobs.slice(0, 3).map((j) => ({ id: j.id, title: j.title, format: j.format, state: j.state, thumb: j.thumb })),
  };
}
async function downloadRoutes(req, res, p, m) {
  if (p === "/api/downloads" && m === "GET") return send(res, 200, await media("/internal/downloads"));
  if (p === "/api/downloads" && m === "POST") {
    const b = await readJson(req);
    const pick = ["url", "format", "quality", "width", "fps", "start", "end"];
    const body = Object.fromEntries(pick.filter((k) => b[k] !== undefined && b[k] !== null).map((k) => [k, String(b[k]).slice(0, 500)]));
    return send(res, 200, await media("/internal/downloads", { method: "POST", body }));
  }
  const mm = p.match(/^\/api\/downloads\/([^/]+)(\/clip)?$/);
  if (!mm || !DL_ID.test(mm[1])) fail(404, "Not found.");
  if (!mm[2] && m === "DELETE") return send(res, 200, await media(`/internal/downloads/${mm[1]}`, { method: "DELETE" }));
  if (mm[2] && m === "POST") {
    const j = await media(`/internal/downloads/${mm[1]}`);
    const ext = String(j.file || "").split(".").pop().toLowerCase();
    if (j.state !== "done" || !CLIP_EXT[ext]) fail(400, "Only finished MP4 downloads can become clips.");
    const src = path.join(DOWNLOADS_DIR, mm[1], path.basename(j.file));
    await ensureRoom(j.size || 0);
    const id = crypto.randomBytes(8).toString("base64url").slice(0, 10);
    const dir = path.join(CLIPS_DIR, id);
    await fsp.mkdir(dir, { recursive: true, mode: 0o755 });
    try {
      await fsp.copyFile(src, path.join(dir, `clip.${ext}`));
      if (j.thumb) await fsp.copyFile(path.join(DOWNLOADS_DIR, mm[1], "thumb.jpg"), path.join(dir, "clip.jpg")).catch(() => {});
    } catch (e) { await fsp.rm(dir, { recursive: true, force: true }); throw e; }
    const title = String(j.title || "Download").replace(/[\x00-\x1f\x7f]/g, "").trim().slice(0, 100) || "Download";
    const thumb = await fsp.stat(path.join(dir, "clip.jpg")).then(() => true, () => false);
    await withClips(async (idx, save) => { idx[id] = { title, ext, size: j.size, uploaded: Date.now(), views: 0, thumb }; await save(); });
    media(`/internal/clips/${id}/process`, { method: "POST" }).catch(() => {});
    return send(res, 200, { ok: true, id, link: `/c/${id}` });
  }
  fail(404, "Not found.");
}

/* ============================ File converter ============================ */
// A batch is uploaded file by file into /files/downloads/<id>/in-NN.<ext>,
// then one start call turns it into one result (or a zip).
const CV_EXT = new Set(["jpg", "jpeg", "png", "webp", "heic", "heif", "gif", "bmp", "tif", "tiff", "avif", "ico", "pdf",
  "mp4", "mov", "webm", "mkv", "m4v", "avi", "flv", "wmv", "ts", "3gp", "mp3", "wav", "m4a", "ogg", "flac", "opus", "aac", "wma"]);
const CV_MAX_FILES = 30;
async function convertRoutes(req, res, p, m, url) {
  if (p === "/api/convert" && m === "GET") return send(res, 200, await media("/internal/downloads?kind=convert"));
  if (p === "/api/convert/upload" && m === "POST") {
    const raw = String(url.searchParams.get("name") || "file").replace(/[\x00-\x1f\x7f]/g, "").slice(0, 120);
    const ext = raw.includes(".") ? raw.split(".").pop().toLowerCase() : "";
    if (!CV_EXT.has(ext)) fail(400, `${raw}: not a picture, PDF, video or audio file.`);
    if (+(req.headers["content-length"] || 0) > MAX_UPLOAD_BYTES) fail(413, "Files can be up to 2 GB.");
    let id = String(url.searchParams.get("id") || "");
    const fresh = !id;
    if (fresh) id = crypto.randomBytes(8).toString("base64url").slice(0, 10);
    else if (!DL_ID.test(id)) fail(400, "Bad batch.");
    const dir = path.join(DOWNLOADS_DIR, id);
    if (fresh) await fsp.mkdir(dir, { recursive: true, mode: 0o755 });
    else if (!(await fsp.stat(path.join(dir, "names.json")).catch(() => null))) fail(404, "Batch not found.");
    let names = {};
    try { names = JSON.parse(await fsp.readFile(path.join(dir, "names.json"), "utf8")); } catch {}
    const n = Object.keys(names).length + 1;
    if (n > CV_MAX_FILES) fail(400, `Up to ${CV_MAX_FILES} files at once.`);
    const file = `in-${String(n).padStart(2, "0")}.${ext === "jpeg" ? "jpg" : ext}`;
    try { await streamTo(req, path.join(dir, file), MAX_UPLOAD_BYTES); }
    catch (e) { if (fresh) await fsp.rm(dir, { recursive: true, force: true }); throw e; }
    names[file] = raw;
    await fsp.writeFile(path.join(dir, "names.json"), JSON.stringify(names), { mode: 0o644 });
    return send(res, 200, { ok: true, id, count: n });
  }
  const mm = p.match(/^\/api\/convert\/([^/]+)(\/start)?$/);
  if (!mm || !DL_ID.test(mm[1])) fail(404, "Not found.");
  if (mm[2] && m === "POST") {
    const { to, width } = await readJson(req);
    try { return send(res, 200, await media("/internal/convert", { method: "POST", body: { id: mm[1], to: String(to || ""), width: +width || 0 } })); }
    catch (e) { await fsp.rm(path.join(DOWNLOADS_DIR, mm[1]), { recursive: true, force: true }); throw e; }
  }
  if (!mm[2] && m === "DELETE") {
    try { await media(`/internal/downloads/${mm[1]}`, { method: "DELETE" }); }
    catch (e) { if (e.status !== 404) throw e; }
    await fsp.rm(path.join(DOWNLOADS_DIR, mm[1]), { recursive: true, force: true });
    return send(res, 200, { ok: true });
  }
  fail(404, "Not found.");
}

/* ===================== Background remover + upscaler ===================== */
// Pictures are uploaded with the converter's batch upload, then started here.
// kind is "cutout" or "upscale"; both share the same list/start/delete shape.
async function cutoutRoutes(req, res, p, m) {
  const kind = p.startsWith("/api/upscale") ? "upscale" : "cutout";
  if (p === `/api/${kind}` && m === "GET") return send(res, 200, await media(`/internal/downloads?kind=${kind}`));
  const mm = p.match(/^\/api\/(?:cutout|upscale)\/([^/]+)(\/start)?$/);
  if (!mm || !DL_ID.test(mm[1])) fail(404, "Not found.");
  if (mm[2] && m === "POST") {
    const b = await readJson(req);
    const body = kind === "cutout"
      ? { id: mm[1], bg: String(b.bg || "none"), crop: b.crop ? "1" : "" }
      : { id: mm[1], scale: String(b.scale) === "2" ? "2" : "4", to: b.to === "png" ? "png" : "jpg" };
    try { return send(res, 200, await media(`/internal/${kind}`, { method: "POST", body })); }
    catch (e) { await fsp.rm(path.join(DOWNLOADS_DIR, mm[1]), { recursive: true, force: true }); throw e; }
  }
  if (!mm[2] && m === "DELETE") {
    try { await media(`/internal/downloads/${mm[1]}`, { method: "DELETE" }); }
    catch (e) { if (e.status !== 404) throw e; }
    await fsp.rm(path.join(DOWNLOADS_DIR, mm[1]), { recursive: true, force: true });
    return send(res, 200, { ok: true });
  }
  fail(404, "Not found.");
}

/* =============================== Pastebin =============================== */
// Text and code snippets shared under /p/<id>. One JSON file per paste.
const PASTES_DIR = path.join(FILES_DIR, "pastes");
const PASTE_ID = /^[A-Za-z0-9_-]{10}$/;
const PASTE_MAX = 512 * 1024;
const PASTE_LANGS = { text: "Text", code: "Code", js: "JavaScript", py: "Python", json: "JSON", html: "HTML", css: "CSS", sh: "Shell", md: "Markdown", java: "Java", lua: "Lua", yaml: "YAML" };
const bytesStr = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${(n / 1024).toFixed(1)} KB` : `${(n / 1048576).toFixed(1)} MB`);
const PASTE_EXPIRY = { "1h": 3600e3, "1d": 86400e3, "7d": 7 * 86400e3, "30d": 30 * 86400e3, never: 0 };

async function pasteRead(id) {
  if (!PASTE_ID.test(id)) return null;
  let d;
  try { d = JSON.parse(await fsp.readFile(path.join(PASTES_DIR, `${id}.json`), "utf8")); }
  catch { return null; }
  if (d.expires && d.expires < Date.now()) { await fsp.rm(path.join(PASTES_DIR, `${id}.json`), { force: true }); return null; }
  return d;
}
async function pasteWrite(id, d) {
  await fsp.mkdir(PASTES_DIR, { recursive: true, mode: 0o755 });
  const f = path.join(PASTES_DIR, `${id}.json`);
  await fsp.writeFile(`${f}.tmp`, JSON.stringify(d), { mode: 0o644 });
  await fsp.rename(`${f}.tmp`, f);
}
async function pastesList() {
  let names = [];
  try { names = (await fsp.readdir(PASTES_DIR)).filter((n) => /^[A-Za-z0-9_-]{10}\.json$/.test(n)); } catch {}
  const all = await Promise.all(names.map(async (n) => {
    const id = n.slice(0, 10), d = await pasteRead(id);
    if (!d) return null;
    const { content, ...meta } = d;
    return { id, ...meta, lines: content.split("\n").length, preview: content.slice(0, 240) };
  }));
  return all.filter(Boolean).sort((a, b) => b.created - a.created);
}
async function pastesSummary() {
  const l = await pastesList();
  return { count: l.length, views: l.reduce((s, x) => s + (x.views || 0), 0) };
}
function pasteFields(b, partial) {
  const out = {};
  if (!partial || b.title !== undefined) out.title = String(b.title ?? "").replace(/[\x00-\x1f\x7f]/g, "").trim().slice(0, 100);
  if (!partial || b.content !== undefined) {
    const c = String(b.content ?? "").replace(/\r\n?/g, "\n");
    if (!c.trim()) fail(400, "The paste is empty.");
    if (Buffer.byteLength(c) > PASTE_MAX) fail(413, "Pastes can be up to 512 KB.");
    out.content = c;
  }
  if (!partial || b.lang !== undefined) out.lang = PASTE_LANGS[b.lang] ? b.lang : "text";
  return out;
}
async function pasteRoutes(req, res, p, m) {
  if (p === "/api/pastes" && m === "GET") return send(res, 200, { pastes: await pastesList() });
  if (p === "/api/pastes" && m === "POST") {
    const b = await readJson(req, PASTE_MAX * 2);
    const f = pasteFields(b, false);
    if (!(b.expiry in PASTE_EXPIRY)) fail(400, "Unknown expiry.");
    const id = crypto.randomBytes(8).toString("base64url").slice(0, 10);
    const now = Date.now();
    await pasteWrite(id, { ...f, title: f.title || "Untitled paste", created: now, expires: PASTE_EXPIRY[b.expiry] ? now + PASTE_EXPIRY[b.expiry] : null, burn: !!b.burn, views: 0, size: Buffer.byteLength(f.content) });
    return send(res, 200, { ok: true, id, link: `/p/${id}` });
  }
  const mm = p.match(/^\/api\/pastes\/([^/]+)$/);
  if (!mm || !PASTE_ID.test(mm[1])) fail(404, "Not found.");
  const d = await pasteRead(mm[1]);
  if (!d) fail(404, "Paste not found.");
  if (m === "GET") return send(res, 200, { id: mm[1], ...d });
  if (m === "PATCH") {
    const f = pasteFields(await readJson(req, PASTE_MAX * 2), true);
    if (f.title === "") f.title = "Untitled paste";
    const n = { ...d, ...f, edited: Date.now() };
    n.size = Buffer.byteLength(n.content);
    await pasteWrite(mm[1], n);
    return send(res, 200, { ok: true });
  }
  if (m === "DELETE") { await fsp.rm(path.join(PASTES_DIR, `${mm[1]}.json`), { force: true }); return send(res, 200, { ok: true }); }
  fail(404, "Not found.");
}
setInterval(() => { pastesList().catch(() => {}); }, 3600e3).unref();   // reading drops expired pastes

// Small highlighter: comments, strings, numbers, keywords. Works line by line.
const HL_KW = new Set("const let var function return if else for while do break continue switch case default new class extends import from export async await try catch finally throw typeof instanceof in of def elif lambda pass with as yield global nonlocal None True False null true false undefined this self public private protected static void int float double boolean string local then end nil not and or is echo fi done esac".split(" "));
function highlight(text, lang) {
  if (lang === "text" || lang === "md") return htmlEsc(text);
  const hash = ["py", "sh", "yaml", "code"].includes(lang), dash = lang === "lua";
  const rx = new RegExp(String.raw`(\/\/.*$${hash ? "|#.*$" : ""}${dash ? "|--.*$" : ""})|("(?:[^"\\\n]|\\.)*"?|'(?:[^'\\\n]|\\.)*'?|` + "`(?:[^`\\\\\\n]|\\\\.)*`?" + String.raw`)|(\b\d+(?:\.\d+)?\b)|([A-Za-z_$][\w$]*)`, "gm");
  let out = "", last = 0;
  for (const mt of text.matchAll(rx)) {
    out += htmlEsc(text.slice(last, mt.index));
    const [t, com, str, num, word] = mt;
    if (com) out += `<i class="c">${htmlEsc(t)}</i>`;
    else if (str) out += `<i class="s">${htmlEsc(t)}</i>`;
    else if (num) out += `<i class="n">${htmlEsc(t)}</i>`;
    else if (word && HL_KW.has(word)) out += `<i class="k">${htmlEsc(t)}</i>`;
    else out += htmlEsc(t);
    last = mt.index + t.length;
  }
  return out + htmlEsc(text.slice(last));
}

async function pastePage(req, res, id, raw) {
  const ua = String(req.headers["user-agent"] || "");
  const bot = CRAWLER.test(ua);
  const d = await pasteRead(id);
  const html = (status, body) => { res.writeHead(status, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store" }); res.end(body); };
  if (!d) return raw ? (res.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" }), res.end("Paste not found.")) : html(404, PASTE_HTML({ missing: true }));
  let burned = false;
  if (!bot && req.method === "GET") {
    if (d.burn) { burned = true; await fsp.rm(path.join(PASTES_DIR, `${id}.json`), { force: true }); }
    else {
      const key = `${clientIp(req)}|p${id}`, now = Date.now();
      if (!(viewSeen.get(key) > now - 6 * 3600e3)) { viewSeen.set(key, now); d.views = (d.views || 0) + 1; await pasteWrite(id, d).catch(() => {}); }
    }
  }
  if (raw) {
    if (bot && d.burn) { res.writeHead(403, { "Content-Type": "text/plain; charset=utf-8" }); return res.end("This paste can only be opened once."); }
    res.writeHead(200, { "Content-Type": "text/plain; charset=utf-8", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" });
    return res.end(d.content);
  }
  return html(200, PASTE_HTML({ id, d, burned, bot, origin: publicOrigin(req) }));
}
function PASTE_HTML({ id, d, burned, bot, origin, missing }) {
  const title = missing ? "Paste not found" : d.title;
  const hide = !missing && bot && d.burn;      // link previews must not use up a one-time paste
  const lines = missing || hide ? [] : d.content.split("\n");
  const body = missing || hide ? "" : highlight(d.content, d.lang).split("\n").map((l) => `<span class="l">${l || " "}</span>`).join("");
  const info = missing ? "" : [PASTE_LANGS[d.lang] || "Text", `${lines.length || "?"} line${lines.length === 1 ? "" : "s"}`, bytesStr(d.size),
    new Date(d.created).toLocaleDateString("en-GB", { day: "numeric", month: "short", year: "numeric" })].join(" · ");
  const desc = missing ? "" : hide ? "One-time paste. Opens once, then it's gone." : d.content.slice(0, 180).replace(/\s+/g, " ");
  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>${htmlEsc(title)} · __BRAND__</title>
<meta name="theme-color" content="#6ea4ff">
<meta name="robots" content="noindex">
<link rel="icon" type="image/png" href="/img/__ICON__-logo-64.png">
${missing ? "" : `<meta property="og:site_name" content="__BRAND__ paste">
<meta property="og:title" content="${htmlEsc(title)}">
<meta property="og:description" content="${htmlEsc(desc)}">
<meta property="og:url" content="${origin}/p/${id}">`}
<style>
@font-face { font-family: "Geist"; src: url("/fonts/geist.woff2") format("woff2"); font-weight: 100 900; font-display: swap; }
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; }
body { background: #05070f; color: #eef1f7; font: 400 15px/1.5 "Geist", system-ui, sans-serif; letter-spacing: -.01em; -webkit-font-smoothing: antialiased; }
body::before { content: ""; position: fixed; inset: 0; z-index: -1; background: radial-gradient(1000px 520px at 50% -12%, rgba(110,164,255,.16), transparent 70%), url("/img/backdrop.jpg") center 75% / cover no-repeat; opacity: .3; }
a { color: inherit; text-decoration: none; }
.top { display: flex; align-items: center; justify-content: space-between; max-width: 1180px; margin: 0 auto; padding: 18px 20px; }
.brand { display: inline-flex; align-items: center; gap: 10px; font-weight: 600; letter-spacing: -.035em; }
.brand img { width: 26px; height: 26px; border-radius: 50%; box-shadow: 0 0 18px rgba(110,164,255,.4); }
main { max-width: 1180px; margin: 0 auto; padding: 8px 20px 60px; }
h1 { margin: 6px 0 4px; font-size: clamp(22px, 3vw, 30px); font-weight: 600; letter-spacing: -.035em; line-height: 1.2; overflow-wrap: anywhere; }
.row { display: flex; align-items: center; justify-content: space-between; gap: 14px; flex-wrap: wrap; margin-bottom: 16px; }
.meta { color: rgba(238,241,247,.6); font-variant-numeric: tabular-nums; }
.acts { display: flex; gap: 8px; flex-wrap: wrap; }
.btn { display: inline-flex; align-items: center; gap: 8px; height: 38px; padding: 0 15px; border-radius: 999px; border: 1px solid rgba(255,255,255,.13); background: rgba(255,255,255,.05); color: #eef1f7; font: 500 14px "Geist", system-ui, sans-serif; cursor: pointer; }
.btn:hover { background: rgba(255,255,255,.09); }
.btn.primary { background: #6ea4ff; border-color: transparent; color: #050b1a; }
.btn svg { width: 16px; height: 16px; }
.code { border-radius: 18px; border: 1px solid rgba(255,255,255,.08); background: rgba(6,9,20,.82); backdrop-filter: blur(14px); overflow: auto; box-shadow: 0 30px 90px rgba(0,0,0,.5); }
pre { margin: 0; padding: 16px 0; font: 13.5px/1.65 ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace; counter-reset: ln; tab-size: 4; }
.l { display: block; padding: 0 20px 0 0; white-space: pre; }
.l::before { counter-increment: ln; content: counter(ln); display: inline-block; width: 52px; padding-right: 16px; margin-right: 4px; text-align: right; color: rgba(238,241,247,.25); user-select: none; -webkit-user-select: none; }
.l:hover { background: rgba(110,164,255,.05); }
.wrap .l { white-space: pre-wrap; overflow-wrap: anywhere; padding-left: 72px; text-indent: -72px; }
i { font-style: normal; } .c { color: #6b7894; } .s { color: #a5e3a0; } .n { color: #f6c177; } .k { color: #8cb8ff; }
.note { margin-bottom: 14px; padding: 12px 16px; border-radius: 14px; background: rgba(110,164,255,.1); border: 1px solid rgba(110,164,255,.3); color: #dbe8ff; font-size: 14px; }
.missing { display: grid; place-items: center; min-height: 60vh; text-align: center; color: rgba(238,241,247,.6); }
.missing b { display: block; margin-bottom: 6px; color: #eef1f7; font-size: 22px; }
.toast { position: fixed; left: 50%; bottom: 24px; transform: translate(-50%, 20px); opacity: 0; padding: 10px 18px; border-radius: 999px; background: #10131d; border: 1px solid rgba(255,255,255,.13); transition: .25s; }
.toast.on { opacity: 1; transform: translate(-50%, 0); }
@media (max-width: 600px) { .acts { width: 100%; } .btn { flex: 1; justify-content: center; } pre { font-size: 12.5px; } .l::before { width: 38px; padding-right: 10px; } }
</style>
</head>
<body>
<header class="top"><a class="brand" href="/">__BRAND__</a></header>
<main>
${missing ? `<div class="missing"><div><b>Paste not found</b>It expired, was deleted, or could only be opened once.</div></div>` : hide ? `<div class="missing"><div><b>${htmlEsc(title)}</b>One-time paste. Open the link in your browser to read it.</div></div>` : `
  ${burned ? `<div class="note">This was a one-time paste. It's deleted now, so copy what you need before you leave.</div>` : ""}
  <h1>${htmlEsc(title)}</h1>
  <div class="row">
    <div class="meta">${htmlEsc(info)}</div>
    <div class="acts">
      <button class="btn" id="wrap" type="button">Wrap lines</button>
      ${burned ? "" : `<a class="btn" href="/p/${id}/raw">Raw</a>`}
      <button class="btn primary" id="copy" type="button"><svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="9" y="9" width="13" height="13" rx="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/></svg>Copy</button>
    </div>
  </div>
  <div class="code"><pre id="code">${body}</pre></div>
  <div class="toast" id="toast">Copied</div>
<script>
const toast = (t) => { const e = document.getElementById("toast"); e.textContent = t; e.classList.add("on"); setTimeout(() => e.classList.remove("on"), 1600); };
document.getElementById("copy").addEventListener("click", async () => {
  const text = [...document.querySelectorAll("#code .l")].map((l) => l.textContent).join("\\n");
  try { await navigator.clipboard.writeText(text); toast("Copied"); } catch { toast("Couldn't copy"); }
});
document.getElementById("wrap").addEventListener("click", () => document.querySelector(".code").classList.toggle("wrap"));
</script>`}
</main>
</body>
</html>`;
}

/* ========================== File Host: preview page ========================== */
// When a share link /f/<id>/<name> is opened in a browser tab, nginx sends it
// here: the file is shown first, with a download button. Embeds, Discord,
// curl and ?dl=1 / ?raw=1 still get the file itself straight from nginx.
const FP_EXT = {
  image: /\.(jpe?g|png|gif|webp|avif|bmp|ico)$/i,
  video: /\.(mp4|webm|mov|m4v|ogv)$/i,
  audio: /\.(mp3|m4a|aac|wav|ogg|oga|opus|flac)$/i,
  pdf: /\.pdf$/i,
  office: /\.(pptx|ppt|pptm|ppsx|pps|odp|docx|doc|docm|odt|rtf|xlsx|xls|xlsm|ods)$/i,
  text: /\.(txt|md|log|json|csv|tsv|ya?ml|ini|cfg|conf|toml|properties|env\.example|py|js|mjs|ts|tsx|jsx|css|scss|html?|xml|svg|sh|bat|cmd|ps1|java|kt|c|cc|cpp|h|hpp|cs|go|rs|lua|rb|php|sql|srt|vtt|gitignore|dockerfile)$/i,
};
const FP_TEXT_MAX = 256 * 1024;
const FP_T = {
  en: { dl: "Download", open: "Open in browser", none: "No preview for this file type.", cut: "Preview shortened. Download the file to see all of it.",
        missing: "File not found", gone: "It was deleted or the link is wrong.", shared: "Shared file",
        making: "Making the preview…", first: "The preview shows the first {0} of {1} pages." },
  de: { dl: "Herunterladen", open: "Im Browser öffnen", none: "Für diesen Dateityp gibt es keine Vorschau.", cut: "Vorschau gekürzt. Lade die Datei herunter, um alles zu sehen.",
        missing: "Datei nicht gefunden", gone: "Sie wurde gelöscht oder der Link ist falsch.", shared: "Geteilte Datei",
        making: "Vorschau wird erstellt…", first: "Die Vorschau zeigt die ersten {0} von {1} Seiten." },
};
async function filePage(req, res, id, rawName) {
  const t = /^de\b/i.test(String(req.headers["accept-language"] || "")) ? FP_T.de : FP_T.en;
  const lang = t === FP_T.de ? "de" : "en";
  let name = "";
  try { name = decodeURIComponent(rawName); } catch {}
  const html = (status, body) => { res.writeHead(status, { "Content-Type": "text/html; charset=utf-8", "Cache-Control": "no-store", "Vary": "Accept-Language" }); res.end(req.method === "HEAD" ? "" : body); };
  let st = null;
  if (name && !/[\/\\]/.test(name) && name !== "." && name !== "..") {
    try { st = await fsp.stat(path.join(FILES_DIR, id, name)); } catch {}
  }
  if (!st || !st.isFile()) return html(404, FILE_HTML({ t, lang, missing: true }));
  let info = null;
  try { info = (JSON.parse(await fsp.readFile(FILES_INDEX, "utf8")))[id] || null; } catch {}
  const kind = Object.keys(FP_EXT).find((k) => FP_EXT[k].test(name)) || "other";
  let text = null, cut = false;
  if (kind === "text") {
    const fh = await fsp.open(path.join(FILES_DIR, id, name), "r");
    try {
      const buf = Buffer.alloc(Math.min(st.size, FP_TEXT_MAX));
      await fh.read(buf, 0, buf.length, 0);
      if (!buf.includes(0)) { text = buf.toString("utf8"); cut = st.size > FP_TEXT_MAX; }
    } finally { await fh.close(); }
  }
  let pv = null;
  if (kind === "office" || kind === "pdf") pv = await filePreview(id, name);
  const when = new Date((info && info.uploaded) || st.mtimeMs).toLocaleDateString(lang === "de" ? "de-DE" : "en-GB", { day: "numeric", month: "short", year: "numeric" });
  return html(200, FILE_HTML({ t, lang, id, name, size: st.size, when, kind, text, cut, pv, origin: publicOrigin(req) }));
}
// Office files and PDFs: page pictures made by the media service (see there).
async function filePreview(id, name) {
  try {
    const d = await media("/internal/preview", { method: "POST", body: { id, name }, timeout: 4000 });
    return { state: String(d.state || "error"), pages: +d.pages || 0, total: +d.total || 0, sig: String(d.sig || "") };
  } catch { return { state: "error" }; }
}
async function filePreviewRoute(res, id) {
  let info = null;
  try { info = (JSON.parse(await fsp.readFile(FILES_INDEX, "utf8")))[id] || null; } catch {}
  if (!info || !info.name) return send(res, 404, { error: "File not found." });
  const pv = await filePreview(id, info.name);
  return send(res, 200, { state: pv.state });
}
function FILE_HTML({ t, lang, id, name, size, when, kind, text, cut, pv, origin, missing }) {
  const src = missing ? "" : `/f/${id}/${encodeURIComponent(name)}`;
  const ext = missing ? "" : (/\.([A-Za-z0-9]{1,8})$/.exec(name) || [, "file"])[1].toUpperCase();
  const icon = (d) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${d}</svg>`;
  const view = missing ? "" : kind === "image" ? `<img src="${src}" alt="${htmlEsc(name)}">`
    : kind === "video" ? `<video src="${src}" controls playsinline preload="metadata"></video>`
    : kind === "audio" ? `<div class="big"><div class="ext">${htmlEsc(ext)}</div><audio src="${src}" controls preload="metadata"></audio></div>`
    : pv && pv.state === "ready" ? `<div class="pages">${Array.from({ length: pv.pages }, (_, i) =>
        `<img src="/f/${id}/.preview/${i + 1}.jpg?v=${encodeURIComponent(pv.sig)}" alt="${i + 1}" loading="${i < 2 ? "eager" : "lazy"}">`).join("")}</div>${
        pv.total > pv.pages ? `<div class="cut">${t.first.replace("{0}", pv.pages).replace("{1}", pv.total)}</div>` : ""}`
    : pv && pv.state === "working" ? `<div class="big" id="pv-wait"><div class="spin"></div><span>${t.making}</span></div>
<script>
(() => {
  let n = 0;
  const tick = async () => {
    n++;
    try {
      const d = await (await fetch("/api/fpreview/${id}", { cache: "no-store" })).json();
      if (d.state === "ready") return location.reload();
      if (d.state !== "working") { document.getElementById("pv-wait").innerHTML = '<div class="ext">${htmlEsc(ext)}</div><span>${t.none}</span>'; return; }
    } catch {}
    if (n < 90) setTimeout(tick, 2500);
  };
  setTimeout(tick, 2500);
})();
</script>`
    : text != null ? `<pre>${htmlEsc(text)}</pre>${cut ? `<div class="cut">${t.cut}</div>` : ""}`
    : `<div class="big"><div class="ext">${htmlEsc(ext)}</div><span>${kind === "pdf" ? "PDF" : t.none}</span></div>`;
  const ogMedia = missing || !origin ? "" : kind === "image" ? `<meta property="og:image" content="${origin}${src}">`
    : kind === "video" ? `<meta property="og:video" content="${origin}${src}">` : "";
  return `<!doctype html>
<html lang="${lang}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>${htmlEsc(missing ? t.missing : name)} · __BRAND__</title>
<meta name="theme-color" content="#05070f">
<meta name="robots" content="noindex">
<link rel="icon" type="image/png" href="/img/__ICON__-logo-64.png">
${missing ? "" : `<meta property="og:site_name" content="__BRAND__">
<meta property="og:title" content="${htmlEsc(name)}">
<meta property="og:description" content="${htmlEsc(`${t.shared} · ${bytesStr(size)}`)}">
${ogMedia}`}
<style>
@font-face { font-family: "Geist"; src: url("/fonts/geist.woff2") format("woff2"); font-weight: 100 900; font-display: swap; }
* { box-sizing: border-box; }
html, body { margin: 0; min-height: 100%; }
body { background: #05070f; color: #eef1f7; font: 400 15px/1.5 "Geist", system-ui, sans-serif; letter-spacing: -.01em; -webkit-font-smoothing: antialiased; }
body::before { content: ""; position: fixed; inset: 0; z-index: -1; background: radial-gradient(1000px 520px at 50% -12%, rgba(110,164,255,.16), transparent 70%), url("/img/backdrop.jpg") center 75% / cover no-repeat; opacity: .3; }
a { color: inherit; text-decoration: none; }
.top { max-width: 1000px; margin: 0 auto; padding: 18px 20px; }
.brand { font-weight: 600; letter-spacing: -.035em; }
main { max-width: 1000px; margin: 0 auto; padding: 4px 20px calc(110px + env(safe-area-inset-bottom)); }
h1 { margin: 6px 0 2px; font-size: clamp(20px, 3vw, 28px); font-weight: 600; letter-spacing: -.035em; line-height: 1.25; overflow-wrap: anywhere; }
.meta { color: rgba(238,241,247,.6); font-variant-numeric: tabular-nums; margin-bottom: 18px; }
.view { border-radius: 18px; border: 1px solid rgba(255,255,255,.08); background: rgba(6,9,20,.82); backdrop-filter: blur(14px); overflow: hidden; box-shadow: 0 30px 90px rgba(0,0,0,.5); }
.view img, .view video { display: block; width: 100%; max-height: 72vh; object-fit: contain; background: #000; }
.view pre { margin: 0; padding: 16px 18px; max-height: 70vh; overflow: auto; font: 13px/1.6 ui-monospace, "SF Mono", "Cascadia Code", Consolas, monospace; white-space: pre-wrap; overflow-wrap: anywhere; tab-size: 4; }
.cut { padding: 10px 18px; border-top: 1px solid rgba(255,255,255,.08); color: rgba(238,241,247,.6); font-size: 13px; }
.big { display: grid; place-items: center; gap: 14px; padding: 48px 20px; text-align: center; color: rgba(238,241,247,.6); }
.ext { display: grid; place-items: center; width: 86px; height: 104px; border-radius: 14px; background: rgba(110,164,255,.12); border: 1px solid rgba(110,164,255,.3); color: #cfe0ff; font-weight: 600; font-size: 15px; letter-spacing: .02em; overflow: hidden; }
.big audio { width: min(420px, 100%); }
.pages { display: grid; gap: 10px; padding: 10px; }
.view .pages img { max-height: none; height: auto; border-radius: 8px; background: #fff; }
.spin { width: 34px; height: 34px; border-radius: 50%; border: 3px solid rgba(110,164,255,.25); border-top-color: #6ea4ff; animation: spin .8s linear infinite; }
@keyframes spin { to { transform: rotate(360deg); } }
.acts { position: fixed; left: 0; right: 0; bottom: 0; display: flex; justify-content: center; gap: 10px; padding: 14px 20px calc(14px + env(safe-area-inset-bottom)); background: linear-gradient(transparent, rgba(5,7,15,.92) 40%); }
.btn { display: inline-flex; align-items: center; justify-content: center; gap: 8px; height: 48px; padding: 0 22px; border-radius: 999px; border: 1px solid rgba(255,255,255,.13); background: rgba(20,24,36,.9); color: #eef1f7; font: 500 15px "Geist", system-ui, sans-serif; }
.btn.primary { background: #6ea4ff; border-color: transparent; color: #050b1a; font-weight: 600; }
.btn svg { width: 18px; height: 18px; }
.missing { display: grid; place-items: center; min-height: 60vh; text-align: center; color: rgba(238,241,247,.6); }
.missing b { display: block; margin-bottom: 6px; color: #eef1f7; font-size: 22px; }
@media (max-width: 600px) { .btn { flex: 1; padding: 0 12px; } }
</style>
</head>
<body>
<header class="top"><a class="brand" href="/">__BRAND__</a></header>
<main>
${missing ? `<div class="missing"><div><b>${t.missing}</b>${t.gone}</div></div>` : `
  <h1>${htmlEsc(name)}</h1>
  <div class="meta">${htmlEsc(bytesStr(size))} · ${htmlEsc(when)}</div>
  <div class="view">${view}</div>
  <div class="acts">
    ${kind === "pdf" ?`<a class="btn" href="${src}?raw=1">${t.open}</a>` : ""}
    <a class="btn primary" href="${src}?dl=1" download="${htmlEsc(name)}">${icon('<path d="M12 3v12"/><path d="m7 10 5 5 5-5"/><path d="M5 21h14"/>')}${t.dl}</a>
  </div>`}
</main>
</body>
</html>`;
}

/* ========================== Discord compressor ========================== */
// Upload goes straight to /files/downloads/<id>/source.<ext>; media squeezes it
// under Discord's limit. Results share the downloader's storage and /d/ links.
const CMP_EXT = new Set(["mp4", "mov", "webm", "mkv", "m4v", "avi", "flv", "wmv", "ts", "gif", "mp3", "wav", "m4a", "ogg", "flac", "opus", "aac"]);
async function compressRoutes(req, res, p, m, url) {
  if (p === "/api/compress" && m === "GET") return send(res, 200, await media("/internal/downloads?kind=compress"));
  if (p === "/api/compress/upload" && m === "POST") {
    const raw = String(url.searchParams.get("name") || "video.mp4").replace(/[\x00-\x1f\x7f]/g, "").slice(0, 120);
    const ext = raw.includes(".") ? raw.split(".").pop().toLowerCase() : "";
    if (!CMP_EXT.has(ext)) fail(400, "Pick a video or audio file (MP4, MOV, WebM, MKV, MP3…).");
    const declared = +(req.headers["content-length"] || 0);
    if (declared > MAX_UPLOAD_BYTES) fail(413, "Files can be up to 2 GB.");
    const id = crypto.randomBytes(8).toString("base64url").slice(0, 10);
    const dir = path.join(DOWNLOADS_DIR, id);
    await fsp.mkdir(dir, { recursive: true, mode: 0o755 });
    try {
      await streamTo(req, path.join(dir, `source.${ext}`), MAX_UPLOAD_BYTES);
      const q = (k) => url.searchParams.get(k) || "";
      const job = await media("/internal/compress", { method: "POST", body: { id, src: `source.${ext}`, name: raw, target: q("target"), audio: q("audio"), start: q("start"), end: q("end") } });
      return send(res, 200, job);
    } catch (e) { await fsp.rm(dir, { recursive: true, force: true }); throw e; }
  }
  const mm = p.match(/^\/api\/compress\/([^/]+)$/);
  if (mm && DL_ID.test(mm[1]) && m === "DELETE") return send(res, 200, await media(`/internal/downloads/${mm[1]}`, { method: "DELETE" }));
  fail(404, "Not found.");
}

/* ================================ Power ================================ */
async function readPower() {
  try { return JSON.parse(await fsp.readFile(IPC_POWER, "utf8")); }
  catch { return { phase: "idle" }; }
}
async function powerRoutes(req, res, p, m) {
  if (m === "GET") return send(res, 200, await readPower());
  if (m === "POST") {
    const { action } = await readJson(req);
    const mode = { shutdown: "poweroff", restart: "reboot" }[action];
    if (!mode) fail(400, "Unknown power action.");
    // Let anyone in game know before the server goes away.
    try {
      if ((await mcStatus()).state === "online") {
        await rcon(`say The server is ${action === "shutdown" ? "shutting down" : "restarting"} now. The world is being saved.`);
      }
    } catch {}
    console.log(`power: ${action} requested`);
    return send(res, 200, await agent("power", { mode }));
  }
  fail(404, "Not found.");
}

/* ================================ Updates ================================ */
// New versions come from GitHub releases. The panel only checks and asks;
// the host agent hands the install to update.sh, which backs up, installs,
// health-checks and rolls back by itself if the new version doesn't come up.
const UPDATE_REPO = "__REPO__";
const REPO_RE = /^[A-Za-z0-9_.-]{1,64}\/[A-Za-z0-9_.-]{1,100}$/;
const TAG_RE = /^v\d{1,3}\.\d{1,3}\.\d{1,3}$/;
const IPC_UPDATE = path.join(IPC_DIR, "update.json");
const release = { at: 0, data: null, error: null, busy: null };

async function currentVersion() {
  try { return (await fsp.readFile(path.join(__dirname, "VERSION"), "utf8")).trim(); }
  catch { return null; }
}
function verParts(v) {
  const m = /^v?(\d+)\.(\d+)\.(\d+)$/.exec(String(v || "").trim());
  return m ? m.slice(1).map(Number) : null;
}
function newer(a, b) {
  const x = verParts(a), y = verParts(b);
  if (!x || !y) return false;
  for (let i = 0; i < 3; i++) if (x[i] !== y[i]) return x[i] > y[i];
  return false;
}
async function fetchRelease() {
  if (!REPO_RE.test(UPDATE_REPO)) throw new Error("No update source is set up for this server.");
  const r = await fetch(`https://api.github.com/repos/${UPDATE_REPO}/releases/latest`, {
    signal: AbortSignal.timeout(10000),
    headers: { "User-Agent": "__SLUG__-panel/1.0", Accept: "application/vnd.github+json" },
  });
  if (r.status === 404) return null;   // no release published yet
  if (r.status === 403 || r.status === 429) throw new Error("GitHub is rate-limiting update checks. Try again in a few minutes.");
  if (!r.ok) throw new Error(`GitHub answered ${r.status}.`);
  const j = await r.json();
  if (!TAG_RE.test(j.tag_name || "")) return null;
  return {
    tag: j.tag_name,
    name: String(j.name || j.tag_name).slice(0, 120),
    notes: String(j.body || "").slice(0, 20000),
    published: j.published_at || null,
    url: typeof j.html_url === "string" && j.html_url.startsWith("https://github.com/") ? j.html_url : null,
  };
}
async function latestRelease(force) {
  const age = Date.now() - release.at;
  if (release.at && age < (force ? 20e3 : 10 * 60e3)) return release;
  if (!release.busy) {
    release.busy = fetchRelease()
      .then((d) => { release.data = d; release.error = null; })
      .catch((e) => { release.error = e.name === "TimeoutError" ? "GitHub didn't answer in time." : e.message; })
      .finally(() => { release.at = Date.now(); release.busy = null; });
  }
  await release.busy;
  return release;
}
async function readUpdateStatus() {
  try { return JSON.parse(await fsp.readFile(IPC_UPDATE, "utf8")); }
  catch { return { state: "idle" }; }
}
async function updateInfo(force) {
  const [current, rel, status] = await Promise.all([currentVersion(), latestRelease(force), readUpdateStatus()]);
  return {
    current,
    repo: REPO_RE.test(UPDATE_REPO) ? UPDATE_REPO : null,
    latest: rel.data,
    available: !!(rel.data && newer(rel.data.tag, current)),
    checked: rel.at || null,
    error: rel.error,
    status,
  };
}
async function updateRoutes(req, res, p, m, url) {
  if (p === "/api/update" && m === "GET") return send(res, 200, await updateInfo(url.searchParams.get("refresh") === "1"));
  if (p === "/api/update/install" && m === "POST") {
    const { tag } = await readJson(req);
    if (typeof tag !== "string" || !TAG_RE.test(tag)) fail(400, "Invalid version.");
    const info = await updateInfo(false);
    if (!info.latest || info.latest.tag !== tag) fail(409, "That isn't the newest version. Check again.");
    if (!info.available) fail(409, "This server is already up to date.");
    if (["starting", "running"].includes(info.status.state) && Date.now() - (info.status.since || 0) < 30 * 60e3) fail(409, "An update is already running.");
    console.log(`update: ${info.current} -> ${tag} requested`);
    return send(res, 200, await agent("update_install", { tag }, { timeout: 20000 }));
  }
  fail(404, "Not found.");
}

/* ============================ Overview / public ============================ */
async function overview() {
  const [stats, mc, bots, files, clips, music, downloads, pastes, training, wallet] = await Promise.allSettled([readStats(), mcLight(), agent("bot_list"), filesSummary(), clipsSummary(), musicSummary(), downloadsSummary(), pastesSummary(), trainingSummary(), walletSummary()]);
  const val = (r) => (r.status === "fulfilled" ? r.value : null);
  const s = val(stats);
  const b = val(bots);
  return {
    system: s && {
      cpu: s.cpu?.total ?? 0,
      mem_pct: pct(s.mem_used, s.mem_total),
      disk_pct: s.disks?.[0] ? pct(s.disks[0].used, s.disks[0].total) : 0,
      uptime: s.uptime,
      temp_c: s.temp_c,
      history: s.history?.cpu || [],
      stale: s.stale,
    },
    minecraft: val(mc),
    bots: b && {
      total: b.bots.length,
      running: b.bots.filter((x) => x.state === "active").length,
      crashed: b.bots.filter((x) => x.state === "failed").length,
      items: b.bots.map((x) => ({ id: x.id, name: x.name, state: x.state, enabled: x.enabled })),
    },
    files: val(files),
    clips: val(clips),
    music: val(music),
    downloads: val(downloads),
    pastes: val(pastes),
    training: val(training),
    wallet: val(wallet),
  };
}

let publicCache = { t: 0, p: null };
async function computePublic() {
  let mc = { active: false, online: 0, max: 0 };
  try {
    const s = await mcLight();
    mc = { active: s.state === "online", online: s.online.length, max: s.max };
  } catch {}
  let stats = null;
  try {
    const s = await readStats();
    stats = { uptime: s.uptime, cpu: s.cpu?.total ?? 0, mem_pct: pct(s.mem_used, s.mem_total) };
  } catch {}
  return { mc, stats };
}
function publicInfo() {
  if (!publicCache.p || Date.now() - publicCache.t > 2000) {
    publicCache = { t: Date.now(), p: computePublic() };
  }
  return publicCache.p;
}

// Visitors of the public page: who is here right now (tabs that pinged in the
// last 45 s) and how many different visitors came today. Only salted hashes are
// kept (new salt every day), never IP addresses.
const VISITS_FILE = path.join(FILES_DIR, ".panel", "visits.json");
const VISIT_ID = /^[a-f0-9]{16}$/;
const presence = new Map();
let visits = null, visitsDirty = false;
const berlinDay = () => new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Berlin" }).format(new Date());
function loadVisits() {
  if (visits) return visits;
  let v = {};
  try { v = JSON.parse(fs.readFileSync(VISITS_FILE, "utf8")); } catch {}
  visits = { day: String(v.day || ""), salt: String(v.salt || ""), seen: Array.isArray(v.seen) ? v.seen : [], total: +v.total || 0 };
  return visits;
}
function countVisit(req, id) {
  const v = loadVisits(), now = Date.now(), day = berlinDay();
  if (v.day !== day) { Object.assign(v, { day, salt: crypto.randomBytes(16).toString("hex"), seen: [] }); visitsDirty = true; }
  // One person = one IP (hashed), no matter how many tabs they have open.
  const h = crypto.createHash("sha256").update(`${v.salt}|${clientIp(req)}`).digest("hex").slice(0, 16);
  if (!v.seen.includes(h)) {
    v.seen.push(h);
    v.total++;
    if (v.seen.length > 20000) v.seen.shift();
    visitsDirty = true;
  }
  presence.set(h, now);
  for (const [k, t] of presence) if (now - t > 45000) presence.delete(k);
  if (presence.size > 5000) presence.clear();
  return { here: presence.size, today: v.seen.length, total: v.total };
}
setInterval(async () => {
  if (!visitsDirty || !visits) return;
  visitsDirty = false;
  try {
    await fsp.mkdir(path.dirname(VISITS_FILE), { recursive: true, mode: 0o755 });
    await fsp.writeFile(`${VISITS_FILE}.tmp`, JSON.stringify(visits));
    await fsp.rename(`${VISITS_FILE}.tmp`, VISITS_FILE);
  } catch { visitsDirty = true; }
}, 20000).unref();

/* =============================== Training =============================== */
// A fixed daily plan (e.g. 3x5 pull-ups) you tick off set by set. Streak with one
// joker per week, XP + levels, and a nudge for more reps after a strong week.
const TRAIN_FILE = path.join(FILES_DIR, ".panel", "training.json");
const TRAIN_DEFAULT = [
  { id: "pullups", name: "Pull-ups", sets: 3, reps: 5, unit: "reps" },
  { id: "pushups", name: "Push-ups", sets: 3, reps: 10, unit: "reps" },
  { id: "squats", name: "Squats", sets: 3, reps: 10, unit: "reps" },
];
const EX_ID = /^[a-z0-9-]{1,24}$/;
const dayKey = (d) => new Intl.DateTimeFormat("en-CA", { timeZone: "Europe/Berlin" }).format(d);
const dayAdd = (key, n) => new Date(Date.parse(`${key}T12:00:00Z`) + n * 86400000).toISOString().slice(0, 10);
const weekOf = (key) => { const dow = (new Date(`${key}T12:00:00Z`).getUTCDay() + 6) % 7; return dayAdd(key, -dow); };   // Monday
const XP_SET = 10, XP_DAY = 50;
const levelAt = (xp) => { let l = 1; while (75 * (l + 1) * l <= xp) l++; return l; };   // level n starts at 75*n*(n-1)

let trainLock = Promise.resolve();
function withTraining(fn) {
  const run = trainLock.then(async () => {
    let t;
    try { t = JSON.parse(await fsp.readFile(TRAIN_FILE, "utf8")); } catch { t = null; }
    if (!t || !Array.isArray(t.plan)) {
      const today = dayKey(new Date());
      t = { start: today, plan: TRAIN_DEFAULT.map((e) => ({ ...e, since: today })), days: {} };
    }
    t.days = t.days || {};
    return fn(t, async () => {
      await fsp.mkdir(path.dirname(TRAIN_FILE), { recursive: true, mode: 0o755 });
      await fsp.writeFile(`${TRAIN_FILE}.tmp`, JSON.stringify(t));
      await fsp.rename(`${TRAIN_FILE}.tmp`, TRAIN_FILE);
    });
  });
  trainLock = run.catch(() => {});
  return run;
}
const dayDone = (rec) => !!rec && rec.n > 0 && Object.keys(rec.ex || {}).length >= rec.n && Object.values(rec.ex).every((e) => e.d >= e.s);
const daySets = (rec) => (rec ? Object.values(rec.ex || {}).reduce((s, e) => s + Math.min(e.d, e.s), 0) : 0);

function trainingState(t) {
  const today = dayKey(new Date());
  // walk every day since the start: streak with one joker per Mon-Sun week
  let cur = 0, best = 0, xp = 0, totalSets = 0, fullDays = 0;
  const jokers = new Set(), status = {};
  for (let d = t.start; d <= today; d = dayAdd(d, 1)) {
    const rec = t.days[d], done = dayDone(rec), sets = daySets(rec);
    xp += sets * XP_SET + (done ? XP_DAY : 0);
    totalSets += sets;
    if (done) { fullDays++; cur++; status[d] = 2; }
    else if (d === today) status[d] = sets ? 1 : 0;              // the day isn't over yet
    else if (!jokers.has(weekOf(d))) { jokers.add(weekOf(d)); status[d] = 3; }   // joker keeps the streak alive
    else { cur = 0; status[d] = sets ? 1 : 0; }
    best = Math.max(best, cur);
  }
  const level = levelAt(xp), lo = 75 * level * (level - 1), hi = 75 * (level + 1) * level;

  // calendar: the last 18 weeks, Monday first
  const first = dayAdd(weekOf(today), -7 * 17), cal = [];
  for (let d = first; d <= dayAdd(weekOf(today), 6); d = dayAdd(d, 1)) {
    cal.push(d > today ? -1 : d < t.start ? -2 : status[d] ?? 0);
  }

  // more reps? after 7 days on the current target with at most one miss
  const suggest = [];
  for (const e of t.plan) {
    if (dayAdd(e.since || t.start, 7) > today) continue;
    let hit = 0;
    for (let i = 1; i <= 7; i++) {
      const r = t.days[dayAdd(today, -i)]?.ex?.[e.id];
      if (r && r.d >= r.s && r.r >= e.reps && r.s >= e.sets) hit++;
    }
    if (hit >= 6) suggest.push({ id: e.id, name: e.name, from: e.reps, to: e.reps + (e.unit === "sec" ? 5 : e.reps < 10 ? 1 : 2), unit: e.unit });
  }

  const rec = t.days[today];
  return {
    today,
    plan: t.plan.map((e) => ({ ...e, done: Math.min(rec?.ex?.[e.id]?.d || 0, e.sets) })),
    done_today: dayDone(rec),
    streak: cur, best, joker_free: !jokers.has(weekOf(today)),
    xp, level, level_from: lo, level_to: hi,
    total_sets: totalSets, full_days: fullDays,
    calendar: { first, days: cal },
    suggest,
  };
}
function cleanPlan(raw) {
  if (!Array.isArray(raw) || !raw.length || raw.length > 12) fail(400, "Add between 1 and 12 exercises.");
  const seen = new Set();
  return raw.map((e) => {
    const name = String(e?.name ?? "").replace(/[\x00-\x1f\x7f]/g, "").trim().slice(0, 32);
    if (!name) fail(400, "Every exercise needs a name.");
    let id = typeof e.id === "string" && EX_ID.test(e.id) ? e.id : name.toLowerCase().normalize("NFKD").replace(/[^a-z0-9]+/g, "-").replace(/^-|-$/g, "").slice(0, 24) || "ex";
    while (seen.has(id)) id = `${id.slice(0, 20)}-${crypto.randomBytes(1).toString("hex")}`;
    seen.add(id);
    const sets = Math.round(+e.sets), reps = Math.round(+e.reps);
    if (!(sets >= 1 && sets <= 10)) fail(400, "Sets must be 1 to 10.");
    if (!(reps >= 1 && reps <= 600)) fail(400, "Reps must be 1 to 600.");
    return { id, name, sets, reps, unit: e.unit === "sec" ? "sec" : "reps" };
  });
}
async function trainingRoutes(req, res, p, m) {
  if (p === "/api/training" && m === "GET") return send(res, 200, await withTraining((t) => trainingState(t)));
  if (p === "/api/training/set" && m === "POST") {
    const b = await readJson(req);
    return send(res, 200, await withTraining(async (t, save) => {
      const e = t.plan.find((x) => x.id === b.id);
      if (!e) fail(404, "That exercise isn't in your plan.");
      const n = Math.round(+b.done);
      if (!(n >= 0 && n <= e.sets)) fail(400, "Bad set count.");
      const today = dayKey(new Date());
      const rec = t.days[today] || (t.days[today] = { n: 0, ex: {} });
      rec.ex[e.id] = { d: n, s: e.sets, r: e.reps };
      for (const id of Object.keys(rec.ex)) if (!t.plan.some((x) => x.id === id)) delete rec.ex[id];
      rec.n = t.plan.length;
      if (!Object.values(rec.ex).some((x) => x.d)) delete t.days[today];
      await save();
      return trainingState(t);
    }));
  }
  if (p === "/api/training/plan" && m === "PUT") {
    const b = await readJson(req, 8192);
    return send(res, 200, await withTraining(async (t, save) => {
      const today = dayKey(new Date()), old = new Map(t.plan.map((e) => [e.id, e]));
      t.plan = cleanPlan(b.plan).map((e) => {
        const o = old.get(e.id);
        return { ...e, since: o && o.reps === e.reps && o.sets === e.sets ? o.since || t.start : today };
      });
      const rec = t.days[today];
      if (rec) {   // keep today's ticks in line with the new plan
        for (const id of Object.keys(rec.ex)) {
          const e = t.plan.find((x) => x.id === id);
          if (!e) delete rec.ex[id];
          else rec.ex[id] = { d: Math.min(rec.ex[id].d, e.sets), s: e.sets, r: e.reps };
        }
        rec.n = t.plan.length;
        if (!Object.values(rec.ex).some((x) => x.d)) delete t.days[today];
      }
      await save();
      return trainingState(t);
    }));
  }
  if (p === "/api/training/bump" && m === "POST") {
    const b = await readJson(req);
    return send(res, 200, await withTraining(async (t, save) => {
      const s = trainingState(t).suggest.find((x) => x.id === b.id);
      if (!s) fail(409, "No level-up waiting for that exercise.");
      const e = t.plan.find((x) => x.id === b.id);
      e.reps = s.to; e.since = dayKey(new Date());
      await save();
      return trainingState(t);
    }));
  }
  fail(404, "Not found.");
}
async function trainingSummary() {
  return withTraining((t) => {
    const s = trainingState(t);
    return { streak: s.streak, level: s.level, done_today: s.done_today,
      sets_today: s.plan.reduce((a, e) => a + e.done, 0), sets_total: s.plan.reduce((a, e) => a + e.sets, 0),
      week: s.calendar.days.slice(-7) };
  });
}

/* ================================ Wallet ================================ */
// Watch-only: the panel only ever sees PUBLIC addresses (made by ~/wallet/new-wallet.py).
// The 12 words live on paper. Nothing here can spend.
const WALLET_FILE = path.join(FILES_DIR, ".panel", "wallet.json");
const WALLET_HIST = path.join(FILES_DIR, ".panel", "wallet-history.json");
const COINS = {
  btc: { name: "Bitcoin", api: "https://mempool.space/api", explorer: "https://mempool.space", gecko: "bitcoin", uri: "bitcoin" },
  ltc: { name: "Litecoin", api: "https://litecoinspace.org/api", explorer: "https://litecoinspace.org", gecko: "litecoin", uri: "litecoin" },
};
const SAT = 1e8;
let walletCache = { t: 0, data: null, running: null };
let priceCache = { t: 0, eur: null };
const qrlib = require("./qrcode.js");
function qrSvg(text) {
  const q = qrlib(0, "M");
  q.addData(text);
  q.make();
  const n = q.getModuleCount();
  let d = "";
  for (let r = 0; r < n; r++) for (let c = 0; c < n; c++) if (q.isDark(r, c)) d += `M${c} ${r}h1v1h-1z`;
  return `<svg xmlns="http://www.w3.org/2000/svg" viewBox="-4 -4 ${n + 8} ${n + 8}" shape-rendering="crispEdges"><rect x="-4" y="-4" width="${n + 8}" height="${n + 8}" fill="#fff"/><path d="${d}" fill="#000"/></svg>`;
}
async function readWalletFile() {
  try {
    const w = JSON.parse(await fsp.readFile(WALLET_FILE, "utf8"));
    const ok = (a, re) => Array.isArray(a) && a.length && a.every((x) => typeof x === "string" && re.test(x));
    if (!ok(w.btc?.receive, /^bc1q[02-9ac-hj-np-z]{38}$/) || !ok(w.ltc?.receive, /^ltc1q[02-9ac-hj-np-z]{38}$/)) return null;
    return w;
  } catch { return null; }
}
async function getJson(url, timeout = 12000) {
  const r = await fetch(url, { signal: AbortSignal.timeout(timeout), headers: { "User-Agent": "__SLUG__-panel/1.0" } });
  if (!r.ok) throw new Error(`${new URL(url).host} answered ${r.status}`);
  return r.json();
}
async function prices() {
  if (priceCache.eur && Date.now() - priceCache.t < 5 * 60 * 1000) return priceCache.eur;
  try {
    const j = await getJson("https://api.coingecko.com/api/v3/simple/price?ids=bitcoin,litecoin&vs_currencies=eur&include_24hr_change=true");
    priceCache = { t: Date.now(), eur: {
      btc: { eur: j.bitcoin.eur, change: j.bitcoin.eur_24h_change ?? null },
      ltc: { eur: j.litecoin.eur, change: j.litecoin.eur_24h_change ?? null },
    } };
  } catch (e) { if (!priceCache.eur) throw e; }
  return priceCache.eur;
}
async function scanCoin(key, w) {
  const C = COINS[key], mine = new Set([...w.receive, ...(w.change || [])]);
  let sats = 0, pending = 0;
  const used = [];
  // look at addresses until a few unused ones in a row (the usual "gap" rule)
  for (const [list, gap] of [[w.receive, 4], [w.change || [], 3]]) {
    let empty = 0;
    for (const a of list) {
      const j = await getJson(`${C.api}/address/${a}`);
      const cs = j.chain_stats, ms = j.mempool_stats;
      if (cs.tx_count + ms.tx_count === 0) { if (++empty >= gap) break; continue; }
      empty = 0;
      used.push(a);
      sats += cs.funded_txo_sum - cs.spent_txo_sum;
      pending += ms.funded_txo_sum - ms.spent_txo_sum;
    }
  }
  // recent transactions, as one net amount each (in minus out of our addresses)
  const txs = new Map();
  for (const a of used.slice(0, 8)) {
    for (const tx of await getJson(`${C.api}/address/${a}/txs`)) {
      if (txs.has(tx.txid)) continue;
      let net = 0;
      for (const o of tx.vout) if (mine.has(o.scriptpubkey_address)) net += o.value;
      for (const i of tx.vin) if (i.prevout && mine.has(i.prevout.scriptpubkey_address)) net -= i.prevout.value;
      txs.set(tx.txid, { txid: tx.txid, amount: net / SAT, time: tx.status.block_time || null, confirmed: !!tx.status.confirmed });
    }
  }
  const list = [...txs.values()].sort((a, b) => (b.time || Infinity) - (a.time || Infinity)).slice(0, 15);
  const addr = w.receive[0];
  return {
    name: C.name, address: addr, uri: `${C.uri}:${addr}`, qr: qrSvg(`${C.uri}:${addr}`),
    explorer: `${C.explorer}/address/${addr}`, tx_url: `${C.explorer}/tx/`,
    balance: sats / SAT, pending: pending / SAT, used: used.length, txs: list,
  };
}
async function walletHistory(point) {
  let h = [];
  try { h = JSON.parse(await fsp.readFile(WALLET_HIST, "utf8")); } catch {}
  if (point) {
    const last = h[h.length - 1];
    if (!last || point.t - last.t >= 60 * 60 * 1000) h.push(point);          // one point per hour
    else if (Math.abs(point.eur - last.eur) >= 0.005) h[h.length - 1] = { ...point, t: last.t };
    h = h.slice(-24 * 120);
    await fsp.writeFile(`${WALLET_HIST}.tmp`, JSON.stringify(h));
    await fsp.rename(`${WALLET_HIST}.tmp`, WALLET_HIST);
  }
  return h;
}
async function refreshWallet() {
  const w = await readWalletFile();
  if (!w) return { setup: false };
  const [p, btc, ltc] = await Promise.all([prices(), scanCoin("btc", w.btc), scanCoin("ltc", w.ltc)]);
  const coins = { btc, ltc };
  for (const k of Object.keys(coins)) {
    coins[k].price = p[k].eur; coins[k].change = p[k].change;
    coins[k].eur = (coins[k].balance + coins[k].pending) * p[k].eur;
  }
  const total = coins.btc.eur + coins.ltc.eur;
  const hist = await walletHistory({ t: Date.now(), eur: Math.round(total * 100) / 100, btc: btc.balance + btc.pending, ltc: ltc.balance + ltc.pending });
  return { setup: true, created: w.created, coins, total_eur: total, history: hist.map((x) => [x.t, x.eur]), updated: Date.now() };
}
async function walletState(force = false) {
  const fresh = walletCache.data && Date.now() - walletCache.t < (force ? 15000 : 90000);
  if (fresh) return walletCache.data;
  if (!walletCache.running) {
    walletCache.running = refreshWallet()
      .then((d) => { walletCache = { t: Date.now(), data: d, running: null }; return d; })
      .catch((e) => { walletCache.running = null; console.error("wallet:", e.message); throw e; });
  }
  try { return await walletCache.running; }
  catch (e) {
    if (walletCache.data) return { ...walletCache.data, stale: true };
    fail(503, `Couldn't load balances (${e.message}).`);
  }
}
async function walletRoutes(req, res, p, m, url) {
  if (p === "/api/wallet" && m === "GET") return send(res, 200, await walletState(url.searchParams.get("fresh") === "1"));
  fail(404, "Not found.");
}
async function walletSummary() {
  if (!walletCache.data) {
    if (!(await readWalletFile())) return { setup: false };
    walletState().catch(() => {});       // warm up in the background, the tile fills in on the next poll
    return { setup: true, loading: true };
  }
  if (Date.now() - walletCache.t > 90000) walletState().catch(() => {});
  const d = walletCache.data;
  if (!d.setup) return { setup: false };
  return { setup: true, total_eur: d.total_eur, btc: d.coins.btc.balance + d.coins.btc.pending, ltc: d.coins.ltc.balance + d.coins.ltc.pending,
    history: d.history.slice(-48).map((x) => x[1]) };
}

/* ================================ Router ================================ */
async function route(req, res) {
  const url = new URL(req.url, "http://x");
  const p = url.pathname;
  const m = req.method;
  // iPad/iPhone shortcut upload: authenticated by an upload key, not a cookie
  if (p === "/api/u" && m === "POST") return keyUpload(req, res, url);
  if (m !== "GET" && m !== "HEAD" && !sameOrigin(req)) fail(403, "Request blocked (wrong origin).");

  if (p === "/api/public" && m === "GET") {
    const base = await publicInfo(), id = url.searchParams.get("id") || "";
    return send(res, 200, VISIT_ID.test(id) ? { ...base, visitors: countVisit(req, id) } : base);
  }
  // the saved design is public, so the login screen looks the same for everyone
  if (p === "/api/look" && m === "GET") return send(res, 200, { design: (await readPrefs()).design || null });
  { const c = p.match(/^\/c\/([^/]+)$/); if (c && (m === "GET" || m === "HEAD")) return clipPage(req, res, c[1]); }
  { const c = p.match(/^\/p\/([A-Za-z0-9_-]{10})(\/raw)?$/); if (c && (m === "GET" || m === "HEAD")) return pastePage(req, res, c[1], !!c[2]); }
  { const c = p.match(/^\/f\/([A-Za-z0-9_-]{12})\/([^/]+)$/); if (c && (m === "GET" || m === "HEAD")) return filePage(req, res, c[1], c[2]); }
  { const c = p.match(/^\/api\/fpreview\/([A-Za-z0-9_-]{12})$/); if (c && m === "GET") return filePreviewRoute(res, c[1]); }
  if (p === "/api/session" && m === "GET") return send(res, 200, { authed: isAuthed(req) });
  // Progress only (no details), so the update screen can follow along while
  // the panel restarts and the old session is gone.
  if (p === "/api/update/state" && m === "GET") {
    const s = await readUpdateStatus();
    return send(res, 200, { state: s.state || "idle", step: s.step || null, to: s.to || null, version: await currentVersion() });
  }
  if (p === "/api/login" && m === "POST") {
    const now = Date.now(), ip = clientIp(req);
    fails = fails.filter((t) => now - t < WINDOW_MS);
    if (failsByIp.size > 2000) for (const [k, v] of failsByIp) if (!v.some((t) => now - t < WINDOW_MS)) failsByIp.delete(k);
    const mine = (failsByIp.get(ip) || []).filter((t) => now - t < WINDOW_MS);
    if (mine.length >= MAX_FAILS || fails.length >= MAX_FAILS_GLOBAL) {
      console.log(`login blocked for ${ip}`);
      fail(429, "Too many attempts. Try again in 15 minutes.");
    }
    const { password } = await readJson(req);
    let ok = false;
    if (typeof password === "string" && password.length <= 256) {
      try { ok = await checkPassword(password); }
      catch { fail(503, "No admin password has been set yet."); }
    }
    if (!ok) {
      fails.push(now); mine.push(now); failsByIp.set(ip, mine);
      console.log(`login failed from ${ip} (${mine.length}/${MAX_FAILS})`);
      await sleep(700 + Math.random() * 500);   // slows down guessing
      fail(401, "Wrong password.");
    }
    failsByIp.delete(ip);
    console.log(`login ok from ${ip}`);
    const token = crypto.randomBytes(32).toString("hex");
    sessions.set(token, now + SESSION_MS);
    return send(res, 200, { ok: true }, {
      "Set-Cookie": `sid=${token}; Path=/api; HttpOnly; Secure; SameSite=Strict; Max-Age=${SESSION_MS / 1000}`,
    });
  }

  if (!isAuthed(req)) fail(401, "Please sign in.");

  if (p === "/api/logout" && m === "POST") {
    sessions.delete(cookieToken(req));
    return send(res, 200, { ok: true }, { "Set-Cookie": "sid=; Path=/api; HttpOnly; Secure; SameSite=Strict; Max-Age=0" });
  }
  if (p === "/api/overview" && m === "GET") return send(res, 200, await overview());
  if (p === "/api/stats" && m === "GET") return send(res, 200, await readStats());
  if (p === "/api/mc" || p.startsWith("/api/mc/")) return mcRoutes(req, res, p, m, url);
  if (p === "/api/plugins" || p.startsWith("/api/plugins/")) return pluginRoutes(req, res, p, m, url);
  if (p === "/api/search" && m === "GET") return send(res, 200, await globalSearch(url.searchParams.get("q")));
  if (p === "/api/prefs" && m === "GET") return send(res, 200, await readPrefs());
  if (p === "/api/prefs" && m === "PUT") return send(res, 200, await writePrefs(await readJson(req, 16384)));
  if (p === "/api/bots" || p.startsWith("/api/bots/")) return botRoutes(req, res, p, m, url);
  if (p === "/api/files" || p.startsWith("/api/files/")) return fileRoutes(req, res, p, m, url);
  if (p === "/api/power") return powerRoutes(req, res, p, m);
  if (p === "/api/update" || p === "/api/update/install") return updateRoutes(req, res, p, m, url);
  if (p === "/api/clips" || p.startsWith("/api/clips/")) return clipRoutes(req, res, p, m, url);
  if (p === "/api/music" || p.startsWith("/api/music/")) return musicRoutes(req, res, p, m);
  if (p === "/api/downloads" || p.startsWith("/api/downloads/")) return downloadRoutes(req, res, p, m);
  if (p === "/api/compress" || p.startsWith("/api/compress/")) return compressRoutes(req, res, p, m, url);
  if (p === "/api/convert" || p.startsWith("/api/convert/")) return convertRoutes(req, res, p, m, url);
  if (p === "/api/pastes" || p.startsWith("/api/pastes/")) return pasteRoutes(req, res, p, m);
  if (p === "/api/training" || p.startsWith("/api/training/")) return trainingRoutes(req, res, p, m);
  if (p === "/api/wallet" || p.startsWith("/api/wallet/")) return walletRoutes(req, res, p, m, url);
  if (p === "/api/cutout" || p.startsWith("/api/cutout/") || p === "/api/upscale" || p.startsWith("/api/upscale/")) return cutoutRoutes(req, res, p, m);
  fail(404, "Not found.");
}

const server = http.createServer((req, res) => {
  route(req, res).catch((e) => {
    if (res.headersSent) return res.destroy();
    if (e instanceof HttpError) return send(res, e.status, { error: e.message });
    if (e.rcon || e.code === "ECONNREFUSED" || e.code === "ENOTFOUND" || e.code === "EAI_AGAIN") {
      return send(res, 503, { error: "The Minecraft server isn't reachable right now." });
    }
    console.error("route error:", e);
    send(res, 500, { error: "Something went wrong on the server." });
  });
});
// Node cuts every request off after 5 minutes by default. Big uploads over a
// normal home connection take longer than that, so give them up to 2 hours.
server.requestTimeout = 2 * 60 * 60 * 1000;
server.listen(3000, () => console.log("panel listening on 3000"));

setInterval(() => {
  const now = Date.now();
  for (const [t, exp] of sessions) if (exp < now) sessions.delete(t);
}, 60 * 60 * 1000).unref();
