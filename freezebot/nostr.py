"""Minimal, stdlib-only Nostr publishing: NIP-01 kind-1 notes, BIP-340 Schnorr signatures, NIP-19 nsec
decoding, and a tiny RFC 6455 websocket client (client->server frames masked, text only).

Pure-Python secp256k1 is slow-ish (~10 ms per signature) but the bot posts a few notes a day.
"""
import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
import urllib.parse

P = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEFFFFFC2F
N = 0xFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFFEBAAEDCE6AF48A03BBFD25E8CD0364141
G = (0x79BE667EF9DCBBAC55A06295CE870B07029BFCDB2DCE28D959F2815B16F81798,
     0x483ADA7726A3C4655DA4FBFC0E1108A8FD17B448A68554199C47D08FFB10D4B8)


def _add(p1, p2):
    if p1 is None:
        return p2
    if p2 is None:
        return p1
    if p1[0] == p2[0] and p1[1] != p2[1]:
        return None
    if p1 == p2:
        lam = 3 * p1[0] * p1[0] * pow(2 * p1[1], P - 2, P) % P
    else:
        lam = (p2[1] - p1[1]) * pow(p2[0] - p1[0], P - 2, P) % P
    x3 = (lam * lam - p1[0] - p2[0]) % P
    return x3, (lam * (p1[0] - x3) - p1[1]) % P


def _mul(pt, n):
    r = None
    for i in range(256):
        if (n >> i) & 1:
            r = _add(r, pt)
        pt = _add(pt, pt)
    return r


def _tagged(tag, msg):
    t = hashlib.sha256(tag.encode()).digest()
    return hashlib.sha256(t + t + msg).digest()


def _i(b):
    return int.from_bytes(b, "big")


def _b(x):
    return x.to_bytes(32, "big")


def pubkey(seckey: bytes) -> bytes:
    d = _i(seckey)
    if not 1 <= d < N:
        raise ValueError("invalid secret key")
    return _b(_mul(G, d)[0])


def schnorr_sign(msg: bytes, seckey: bytes, aux: bytes = None) -> bytes:
    """BIP-340 signature over a 32-byte message."""
    aux = os.urandom(32) if aux is None else aux
    d0 = _i(seckey)
    if not 1 <= d0 < N:
        raise ValueError("invalid secret key")
    pt = _mul(G, d0)
    d = d0 if pt[1] % 2 == 0 else N - d0
    t = _b(d ^ _i(_tagged("BIP0340/aux", aux)))
    k0 = _i(_tagged("BIP0340/nonce", t + _b(pt[0]) + msg)) % N
    if k0 == 0:
        raise RuntimeError("bad nonce")
    r = _mul(G, k0)
    k = k0 if r[1] % 2 == 0 else N - k0
    e = _i(_tagged("BIP0340/challenge", _b(r[0]) + _b(pt[0]) + msg)) % N
    return _b(r[0]) + _b((k + e * d) % N)


def schnorr_verify(msg: bytes, pub: bytes, sig: bytes) -> bool:
    x = _i(pub)
    if x >= P:
        return False
    y = pow((pow(x, 3, P) + 7) % P, (P + 1) // 4, P)
    if (y * y - pow(x, 3, P) - 7) % P:
        return False
    pt = (x, y if y % 2 == 0 else P - y)
    r, s = _i(sig[:32]), _i(sig[32:])
    if r >= P or s >= N:
        return False
    e = _i(_tagged("BIP0340/challenge", sig[:32] + pub + msg)) % N
    rr = _add(_mul(G, s), _mul(pt, N - e))
    return rr is not None and rr[1] % 2 == 0 and rr[0] == r


_B32 = "qpzry9x8gf2tvdw0s3jn54khce6mua7l"


def _bech32_decode(s):
    s = s.lower()
    hrp, data = s[:s.rfind("1")], [_B32.index(c) for c in s[s.rfind("1") + 1:]]
    gen = [0x3b6a57b2, 0x26508e6d, 0x1ea119fa, 0x3d4233dd, 0x2a1462b3]
    chk = 1
    for v in [ord(c) >> 5 for c in hrp] + [0] + [ord(c) & 31 for c in hrp] + data:
        b = chk >> 25
        chk = (chk & 0x1ffffff) << 5 ^ v
        for i in range(5):
            chk ^= gen[i] if (b >> i) & 1 else 0
    if chk != 1:
        raise ValueError("bad bech32 checksum")
    acc, bits, out = 0, 0, bytearray()
    for v in data[:-6]:
        acc, bits = (acc << 5) | v, bits + 5
        while bits >= 8:
            bits -= 8
            out.append((acc >> bits) & 0xff)
    return hrp, bytes(out)


def parse_seckey(value: str) -> bytes:
    """Accept an nsec1… (NIP-19) or 64-char hex secret key."""
    value = value.strip()
    if value.startswith("nsec1"):
        hrp, raw = _bech32_decode(value)
        if hrp != "nsec" or len(raw) != 32:
            raise ValueError("not an nsec key")
        return raw
    raw = bytes.fromhex(value)
    if len(raw) != 32:
        raise ValueError("secret key must be 32 bytes")
    return raw


def make_event(content, seckey: bytes, tags=None, created_at=None, kind=1, aux=None):
    pub = pubkey(seckey).hex()
    created_at = int(time.time()) if created_at is None else created_at
    tags = tags or []
    ser = json.dumps([0, pub, created_at, kind, tags, content], separators=(",", ":"), ensure_ascii=False)
    eid = hashlib.sha256(ser.encode()).digest()
    return {"id": eid.hex(), "pubkey": pub, "created_at": created_at, "kind": kind, "tags": tags,
            "content": content, "sig": schnorr_sign(eid, seckey, aux).hex()}


class WebSocket:
    def __init__(self, url, timeout=15):
        u = urllib.parse.urlparse(url)
        port = u.port or (443 if u.scheme == "wss" else 80)
        sock = socket.create_connection((u.hostname, port), timeout=timeout)
        if u.scheme == "wss":
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=u.hostname)
        key = base64.b64encode(os.urandom(16)).decode()
        path = (u.path or "/") + (("?" + u.query) if u.query else "")
        sock.sendall((f"GET {path} HTTP/1.1\r\nHost: {u.hostname}\r\nUpgrade: websocket\r\n"
                      f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n"
                      "User-Agent: xmr-freeze-bot\r\n\r\n").encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = sock.recv(4096)
            if not chunk:
                raise ConnectionError("websocket handshake: connection closed")
            resp += chunk
        status_line = resp.split(b"\r\n", 1)[0]
        if b" 101 " not in status_line:
            raise ConnectionError("websocket handshake failed: %r" % status_line[:80])
        self.sock, self._buf = sock, resp.split(b"\r\n\r\n", 1)[1]

    def send_text(self, text):
        data = text.encode()
        hdr = bytearray([0x81])
        n = len(data)
        if n < 126:
            hdr.append(0x80 | n)
        elif n < 65536:
            hdr += bytes([0x80 | 126]) + struct.pack(">H", n)
        else:
            hdr += bytes([0x80 | 127]) + struct.pack(">Q", n)
        mask = os.urandom(4)
        self.sock.sendall(bytes(hdr) + mask + bytes(b ^ mask[i % 4] for i, b in enumerate(data)))

    def _read(self, n):
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("websocket closed")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def recv_text(self):
        while True:
            b0, b1 = self._read(2)
            n = b1 & 0x7F
            if n == 126:
                n = struct.unpack(">H", self._read(2))[0]
            elif n == 127:
                n = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(n)
            op = b0 & 0x0F
            if op == 1:
                return payload.decode(errors="replace")
            if op == 8:
                raise ConnectionError("websocket closed by relay")
            # ping/pong/binary frames are ignored

    def close(self):
        try:
            self.sock.close()
        except OSError:
            pass


def publish(relay_url, event, timeout=15):
    """Send ["EVENT", ev] and wait for the relay's ["OK", id, accepted, msg]. Returns (accepted, msg)."""
    ws = WebSocket(relay_url, timeout=timeout)
    try:
        ws.send_text(json.dumps(["EVENT", event]))
        deadline = time.time() + timeout
        while time.time() < deadline:
            msg = json.loads(ws.recv_text())
            if msg and msg[0] == "OK" and msg[1] == event["id"]:
                return bool(msg[2]), msg[3] if len(msg) > 3 else ""
        return False, "timeout waiting for OK"
    finally:
        ws.close()
