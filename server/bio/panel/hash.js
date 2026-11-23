"use strict";
// Liest ein Passwort von stdin und gibt den scrypt-Hash als JSON aus.
const crypto = require("crypto");
const chunks = [];
process.stdin.on("data", (c) => chunks.push(c));
process.stdin.on("end", () => {
  const salt = crypto.randomBytes(16);
  const hash = crypto.scryptSync(Buffer.concat(chunks).toString("utf8"), salt, 64, {
    N: 32768, r: 8, p: 1, maxmem: 128 * 1024 * 1024,
  });
  process.stdout.write(JSON.stringify({ salt: salt.toString("hex"), hash: hash.toString("hex") }));
});
