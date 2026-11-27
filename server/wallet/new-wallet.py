#!/usr/bin/env python3
"""__BRAND__ - create a new BTC + LTC wallet.

Makes a brand-new 12-word recovery phrase (BIP39), shows it ONCE so you can
write it on paper, and saves only the PUBLIC receive addresses for the admin
panel. The words are never written to disk, never sent anywhere.

Addresses are standard native SegWit (BIP84):
  Bitcoin   m/84'/0'/0'  -> bc1q...
  Litecoin  m/84'/2'/0'  -> ltc1q...
To spend later, type the 12 words into Electrum (Bitcoin) / Electrum-LTC
(Litecoin) and pick "native segwit (p2wpkh)" - or any wallet that supports BIP84.

Only Python's standard library is used. Run:  python3 new-wallet.py
"""
import hashlib
import hmac
import json
import os
import secrets
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORDS_FILE = HERE / "english.txt"
WORDS_SHA256 = "2f5eed53a4727b4bf8880d8f3f199efc90e58503646d9ff8eff3a2ed3b24dbda"
OUT = Path(os.environ.get("WALLET_OUT", "__HOME__/files/.panel/wallet.json"))
GAP = 20   # addresses per chain (receive + change)

# ------------------------------------------------------------------ secp256k1
P = 2**256 - 2**32 - 977
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(a, b):
    if a is None:
        return b
    if b is None:
        return a
    if a[0] == b[0] and (a[1] + b[1]) % P == 0:
        return None
    if a == b:
        lam = 3 * a[0] * a[0] * pow(2 * a[1], -1, P) % P
    else:
        lam = (b[1] - a[1]) * pow(b[0] - a[0], -1, P) % P
    x = (lam * lam - a[0] - b[0]) % P
    return x, (lam * (a[0] - x) - a[1]) % P


def _mul(k, pt=G):
    r = None
    while k:
        if k & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
        k >>= 1
    return r


def pubkey(priv: int) -> bytes:
    x, y = _mul(priv)
    return bytes([2 + (y & 1)]) + x.to_bytes(32, "big")


# ------------------------------------------------------------------ hashes
def h160(b: bytes) -> bytes:
    try:
        r = hashlib.new("ripemd160")
    except ValueError:
        sys.exit("This Python has no RIPEMD-160. Install python3 with OpenSSL legacy support.")
    r.update(hashlib.sha256(b).digest())
    return r.digest()


def b58check(payload: bytes) -> str:
    data = payload + hashlib.sha256(hashlib.sha256(payload).digest()).digest()[:4]
    alpha = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    n, s = int.from_bytes(data, "big"), ""
    while n:
        n, r = divmod(n, 58)
        s = alpha[r] + s
    return "1" * (len(data) - len(data.lstrip(b"\0"))) + s


# ------------------------------------------------------------------ bech32 (BIP173, witness v0)
CHARSET = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _polymod(values):
    gen = [0x3B6A57B2, 0x26508E6D, 0x1EA119FA, 0x3D4233DD, 0x2A1462B3]
    chk = 1
    for v in values:
        b = chk >> 25
        chk = (chk & 0x1FFFFFF) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if ((b >> i) & 1) else 0
    return chk


def segwit_addr(hrp: str, prog: bytes) -> str:
    acc, bits, data = 0, 0, [0]
    for b in prog:
        acc = (acc << 8) | b
        bits += 8
        while bits >= 5:
            bits -= 5
            data.append((acc >> bits) & 31)
    if bits:
        data.append((acc << (5 - bits)) & 31)
    exp = [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp]
    pm = _polymod(exp + data + [0] * 6) ^ 1
    data += [(pm >> 5 * (5 - i)) & 31 for i in range(6)]
    return hrp + "1" + "".join(CHARSET[d] for d in data)


# ------------------------------------------------------------------ BIP39 / BIP32
def load_words():
    raw = WORDS_FILE.read_bytes()
    if hashlib.sha256(raw).hexdigest() != WORDS_SHA256:
        sys.exit("english.txt is damaged or was changed. Stopping.")
    words = raw.decode().split()
    assert len(words) == 2048
    return words


def mnemonic_from_entropy(ent: bytes, words) -> list:
    bits = int.from_bytes(ent, "big")
    cs_len = len(ent) * 8 // 32
    bits = (bits << cs_len) | (hashlib.sha256(ent).digest()[0] >> (8 - cs_len))
    n = (len(ent) * 8 + cs_len) // 11
    return [words[(bits >> (11 * (n - 1 - i))) & 2047] for i in range(n)]


def seed_from_mnemonic(mnemonic: str, passphrase: str = "") -> bytes:
    return hashlib.pbkdf2_hmac("sha512", mnemonic.encode(), ("mnemonic" + passphrase).encode(), 2048)


def master(seed: bytes):
    i = hmac.new(b"Bitcoin seed", seed, hashlib.sha512).digest()
    return int.from_bytes(i[:32], "big"), i[32:]


def ckd(k: int, c: bytes, idx: int):
    if idx >= 0x80000000:
        data = b"\0" + k.to_bytes(32, "big") + idx.to_bytes(4, "big")
    else:
        data = pubkey(k) + idx.to_bytes(4, "big")
    i = hmac.new(c, data, hashlib.sha512).digest()
    child = (int.from_bytes(i[:32], "big") + k) % N
    if child == 0 or int.from_bytes(i[:32], "big") >= N:
        raise ValueError("invalid child, use next index")   # astronomically unlikely
    return child, i[32:]


def derive(seed: bytes, path):
    k, c = master(seed)
    parent = None
    for idx in path:
        parent = k
        k, c = ckd(k, c, idx)
    return k, c, parent


H = 0x80000000


def account(seed: bytes, coin: int, hrp: str):
    path = [84 + H, coin + H, 0 + H]
    k, c, parent = derive(seed, path)
    fp = h160(pubkey(parent))[:4]
    zpub = b58check(bytes.fromhex("04b24746") + bytes([3]) + fp + path[-1].to_bytes(4, "big") + c + pubkey(k))
    chains = {}
    for chain, name in ((0, "receive"), (1, "change")):
        ck, cc = ckd(k, c, chain)
        chains[name] = [segwit_addr(hrp, h160(pubkey(ckd(ck, cc, i)[0]))) for i in range(GAP)]
    return zpub, chains


# ------------------------------------------------------------------ self-test
def self_test(words):
    # official BIP84 test vector
    m = " ".join(["abandon"] * 11 + ["about"])
    assert mnemonic_from_entropy(bytes(16), words) == m.split(), "BIP39 words wrong"
    seed = seed_from_mnemonic(m)
    zpub, ch = account(seed, 0, "bc")
    assert zpub == "zpub6rFR7y4Q2AijBEqTUquhVz398htDFrtymD9xYYfG1m4wAcvPhXNfE3EfH1r1ADqtfSdVCToUG868RvUUkgDKf31mGDtKsAYz2oz2AGutZYs", "zpub wrong"
    assert ch["receive"][0] == "bc1qcr8te4kr609gcawutmrza0j4xv80jy8z306fyu", "receive 0 wrong"
    assert ch["receive"][1] == "bc1qnjg0jd8228aq7egyzacy8cys3knf9xvrerkf9g", "receive 1 wrong"
    assert ch["change"][0] == "bc1q8c6fshw2dlwun7ekn9qwf37cu2rn755upcp6el", "change 0 wrong"
    assert ch["receive"][19] == "bc1q27yd7vz8m5kz230wuyncfe3pyazez6ah58yzy0", "receive 19 wrong"
    assert ch["change"][19] == "bc1q5w6rj60jh35g859xfq4harfxf6ta3mq6nxekfl", "change 19 wrong"
    # Litecoin, cross-checked against the independent "embit" library
    _, lch = account(seed, 2, "ltc")
    assert lch["receive"][0] == "ltc1qjmxnz78nmc8nq77wuxh25n2es7rzm5c2rkk4wh", "ltc receive 0 wrong"
    assert lch["receive"][19] == "ltc1q7azc8dpce9uwqddl5pr8wwkt9m7799sg7wt9fp", "ltc receive 19 wrong"
    assert lch["change"][1] == "ltc1qzenvkhkqqazfanrjs6htluwlm2sdwt0sgc3jh8", "ltc change 1 wrong"
    # BIP39 vector with passphrase TREZOR (checks PBKDF2)
    s = seed_from_mnemonic("legal winner thank year wave sausage worth useful legal winner thank yellow", "TREZOR")
    assert s.hex().startswith("2e8905819b8723fe2c1d161860e5ee1830318dbf49a83bd451cfb8440c28bd6f"), "seed wrong"


# ------------------------------------------------------------------ UI
def clear():
    print("\033[2J\033[3J\033[H", end="", flush=True)


def main():
    words = load_words()
    self_test(words)
    if "--test" in sys.argv:
        print("self-test ok")
        return
    if OUT.exists():
        sys.exit(f"There is already a wallet ({OUT}).\nI won't replace it - coins sent to those addresses would be lost track of.")

    clear()
    print("=" * 60)
    print("  __BRAND__  -  NEW WALLET (Bitcoin + Litecoin)")
    print("=" * 60)
    print("""
12 words come next. They ARE your money:
  - Write them down with a pen on paper, in the right order.
  - No photo, no screenshot, not in Discord/chat/a notes app.
  - Whoever has the words has the money. Without the words it's gone.
  - They are NOT saved. Only now, once.
""")
    if input("Pen and paper ready? (yes/no) ").strip().lower() not in ("ja", "j", "yes", "y"):
        print("Ok, start again later then.")
        return

    mnemonic = mnemonic_from_entropy(secrets.token_bytes(16), words)
    clear()
    print("\n  YOUR 12 WORDS  (write them down!)\n")
    for i in range(0, 12, 3):
        print("   " + "".join(f"{i + j + 1:>2}. {mnemonic[i + j]:<12}" for j in range(3)))
    print()
    input("Written down and compared once more? Then press Enter...")

    # check that they were written down correctly
    clear()
    print("\nQuick test that everything on your paper is right:\n")
    for n in sorted(secrets.SystemRandom().sample(range(12), 3)):
        for tries in range(3):
            if input(f"  Word no. {n + 1}: ").strip().lower() == mnemonic[n]:
                break
            print("  That's not it - check your paper again.")
        else:
            clear()
            print("\nWrong too often. Nothing was saved - please start again and copy them carefully.")
            return

    clear()
    print("\nCorrect! Creating addresses ...")
    seed = seed_from_mnemonic(" ".join(mnemonic))
    btc_zpub, btc = account(seed, 0, "bc")
    ltc_zpub, ltc = account(seed, 2, "ltc")
    del seed, mnemonic

    data = {
        "created": int(time.time()),
        "type": "bip84",
        "btc": {"path": "m/84'/0'/0'", "zpub": btc_zpub, **btc},
        "ltc": {"path": "m/84'/2'/0'", "zpub": ltc_zpub, **ltc},
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    tmp = OUT.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1))
    os.chmod(tmp, 0o644)
    os.replace(tmp, OUT)

    print(f"""
Done! ONLY the public addresses were saved.

  Bitcoin:   {btc['receive'][0]}
  Litecoin:  {ltc['receive'][0]}

You now see the addresses + QR codes in the admin under "Wallet".
Tip: send a small amount first and check that it arrives.
Keep the paper somewhere safe (not next to the laptop).
""")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        clear()
        print("\nCancelled. Nothing saved.")
