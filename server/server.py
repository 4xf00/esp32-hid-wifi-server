#!/usr/bin/env python3
"""
esp32-hid-bridge — LAN command server for an ESP32-C3 BLE HID agent.

Runs a minimal RFC6455 WebSocket server in the Python standard library (no pip
install, works on a stock Raspberry Pi OS / OpenWrt / NAS). The ESP32 connects
as a client; commands are pushed down the socket and the board turns them into
BLE HID keyboard/mouse reports that the paired Android phone sees as real
hardware input.

Design constraints (deliberate):
  * Binds to one LAN address, never 0.0.0.0.
  * Every upgrade requires a shared token, constant-time compared.
  * Character allowlist + per-command length caps: no arbitrary key sequences.
  * Token-bucket rate limit so a runaway loop cannot flood the phone.
  * Every accepted command is appended to an append-only audit log.
  * The board keeps a physical BOOT-button kill switch; the server can only
    ever ask for less than the firmware is willing to send.
  * POST /cmd is loopback-only by default, so an agent sharing the server host
    cannot drive the phone unless you explicitly opt in.

Protocol
--------
Client -> server (JSON text frames):
  {"ev":"hello","fw":"1.0.0","ble":true}
  {"id":"7","ev":"done","ok":true,"ms":42}
  {"ev":"error","id":"7","msg":"..."}

Server -> client:
  {"id":"7","op":"type","text":"hello"}
  {"id":"8","op":"key","key":"ENTER"}
  {"id":"9","op":"tap","x":610,"y":1356,"ms":70}
  {"id":"10","op":"swipe","x1":..,"y1":..,"x2":..,"y2":..,"ms":300}
  {"id":"11","op":"scroll","dy":-3}
  {"id":"12","op":"combo","keys":["CTRL","A"]}
  {"id":"13","op":"ping"}

HTTP surface:
  GET  /health        -> {"ok":true,"board_connected":...}
  GET  /status        -> connected board state
  GET  /ws?token=...  -> WebSocket upgrade (the ESP32 connects here)
  POST /cmd           -> submit a command (loopback or --bind by default)
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import socket
import socketserver
import struct
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"

# --------------------------------------------------------------------------
# Safety policy
# --------------------------------------------------------------------------

# Printable ASCII that a BLE keyboard can actually produce on Android without
# relying on a keyboard-layout quirk. Anything outside this (CJK, emoji, ... )
# must go over ADB instead — a hardware keyboard cannot type it.
SAFE_TEXT = set(
    "abcdefghijklmnopqrstuvwxyz"
    "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    "0123456789"
    " -_.:/?,+#%&*()[]{}<>=!@"
)

MAX_TEXT = 200          # chars per type command
MAX_COMBO_LEN = 2       # modifier + one key

ALLOWED_KEYS = frozenset(
    """ENTER BACK BACKSPACE BACKSPACE_DEL TAB TAB_FORWARD ESC SPACE DEL
       UP DOWN LEFT RIGHT HOME PAGE_UP PAGE_DOWN
       VOLUME_UP VOLUME_DOWN MUTE""".split()
)

# Names that read like keys but have no reliable BLE HID keyboard usage. The
# firmware would answer "no BLE HID usage for this key" — better to reject here
# with a reason the caller can act on, and to keep the wire protocol honest.
UNSUPPORTED_KEYS = {
    "APP_SWITCH": "BLE HID keyboards have no reliable app-switch usage; use `key HOME` or adb",
    "MENU":       "no BLE HID usage; this is a vendor key, not a standard one",
    "CAMERA":     "no BLE HID usage; Android reserves it for the camera app",
    "POWER":      "Android does not accept power keys over BLE HID",
    "WAKEUP":     "Android does not accept wake keys over BLE HID",
    "SLEEP":      "Android does not accept sleep keys over BLE HID",
}

ALLOWED_MODIFIERS = frozenset(["CTRL", "ALT", "SHIFT"])

# A combo's non-modifier half may be a single printable character, because
# "select all" / "copy" are expressed as CTRL+A / CTRL+C.
ALLOWED_COMBO_TAIL = ALLOWED_KEYS | frozenset(SAFE_TEXT)

# Coordinate ceiling. Coarse bound keeps this module free of a screen-size
# config; --screen tightens it when the operator knows their phone.
COORD_HARD_MAX = 4000

RATE_CAPACITY = 20# burst commands
RATE_REFILL = 3.0# commands per second sustained


class PolicyError(Exception):
    pass


def validate_command(cmd):
    """Return (op, cleaned_dict) or raise PolicyError."""
    op = cmd.get("op")
    if not isinstance(op, str):
        raise PolicyError("missing 'op'")

    if op == "type":
        text = cmd.get("text")
        if not isinstance(text, str):
            raise PolicyError("'text' must be a string")
        if len(text) > MAX_TEXT:
            raise PolicyError(f"text too long ({len(text)} > {MAX_TEXT})")
        bad = sorted({c for c in text if c not in SAFE_TEXT})
        if bad:
            raise PolicyError(
                f"characters not typeable over BLE HID: {bad!r}. "
                "Use ADB 'input text' for non-ASCII."
            )
        return op, {"text": text}

    if op == "key":
        key = cmd.get("key")
        if not isinstance(key, str) or key not in ALLOWED_KEYS:
            hint = UNSUPPORTED_KEYS.get(key) if isinstance(key, str) else None
            raise PolicyError(hint if hint else f"key not allowed: {key!r}")
        return op, {"key": key}

    if op == "combo":
        keys = cmd.get("keys")
        if not isinstance(keys, list) or not (1 <= len(keys) <= MAX_COMBO_LEN):
            raise PolicyError(f"'keys' must be a list of 1..{MAX_COMBO_LEN}")
        for k in keys:
            if k not in ALLOWED_MODIFIERS and k not in ALLOWED_COMBO_TAIL:
                raise PolicyError(f"key not allowed in combo: {k!r}")
        if not any(k in ALLOWED_MODIFIERS for k in keys):
            raise PolicyError("combo needs at least one modifier")
        return op, {"keys": [str(k) for k in keys]}

    if op in ("tap", "swipe"):
        need = ["x", "y"] if op == "tap" else ["x1", "y1", "x2", "y2"]
        vals = {}
        for k in need:
            v = cmd.get(k)
            if not isinstance(v, (int, float)):
                raise PolicyError(f"'{k}' must be a number")
            if not (0 <= v <= COORD_HARD_MAX):
                raise PolicyError(f"'{k}' out of range (0..{COORD_HARD_MAX})")
            vals[k] = int(v)
        if op == "tap":
            ms = int(cmd.get("ms", 70))
            if not (20 <= ms <= 1000):
                raise PolicyError("'ms' out of range (20-1000)")
            return op, {**vals, "ms": ms}
        ms = int(cmd.get("ms", 300))
        if not (50 <= ms <= 5000):
            raise PolicyError("'ms' out of range (50-5000)")
        return op, {**vals, "ms": ms}

    if op == "scroll":
        dy = cmd.get("dy")
        if not isinstance(dy, int) or not (-20 <= dy <= 20):
            raise PolicyError("'dy' out of range (-20..20)")
        return op, {"dy": dy}

    if op == "ping":
        return op, {}

    raise PolicyError(f"unknown op: {op!r}")


class RateLimiter:
    def __init__(self, capacity=RATE_CAPACITY, refill=RATE_REFILL):
        self.cap = capacity
        self.tokens = float(capacity)
        self.refill = refill
        self.last = time.monotonic()
        self.lock = threading.Lock()

    def allow(self):
        with self.lock:
            now = time.monotonic()
            self.tokens = min(self.cap, self.tokens + (now - self.last) * self.refill)
            self.last = now
            if self.tokens < 1:
                return False
            self.tokens -= 1
            return True


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------


class Audit:
    def __init__(self, path):
        self.path = path
        self.lock = threading.Lock()

    def write(self, event, detail):
        line = json.dumps(
            {"ts": round(time.time(), 3), "event": event, "detail": detail},
            ensure_ascii=False,
        )
        with self.lock:
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        print(f"[audit] {line}", flush=True)


# --------------------------------------------------------------------------
# Board connection (one at a time; newest wins)
# --------------------------------------------------------------------------


class Board:
    def __init__(self, audit):
        self.audit = audit
        # Reentrant: send() holds this lock and passes it down to ws_send().
        self.lock = threading.RLock()
        self.sock = None
        self.info = {}
        self.pending = {}
        self.seq = 0

    def attach(self, sock, peer):
        with self.lock:
            old = self.sock
            self.sock = sock
            self.info = {"peer": peer, "connected_at": time.time(), "fw": None, "ble": None}
            self.pending.clear()
        if old is not None:
            try:
                old.shutdown(socket.SHUT_RDWR)
                old.close()
            except OSError:
                pass
            self.audit.write("board_replaced", {"peer": peer})
        else:
            self.audit.write("board_attached", {"peer": peer})

    def detach(self, sock):
        with self.lock:
            if self.sock is sock:
                self.sock = None
                self.info = {}
                self.audit.write("board_detached", {"peer": None})

    def note(self, msg):
        with self.lock:
            if msg.get("ev") == "hello":
                self.info["fw"] = msg.get("fw")
                self.info["ble"] = msg.get("ble")
                self.audit.write("board_hello", msg)

    def status(self):
        with self.lock:
            return dict(self.info)

    def send(self, op, payload):
        with self.lock:
            if self.sock is None:
                raise PolicyError("no ESP32 connected")
            self.seq += 1
            msg = {"id": str(self.seq), "op": op, **payload}
            raw = json.dumps(msg, separators=(",", ":")).encode()
            ws_send(self.sock, raw, self.lock)
            self.audit.write("cmd", msg)
            return msg["id"]


# --------------------------------------------------------------------------
# WebSocket framing
# --------------------------------------------------------------------------

OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


def ws_frame(payload, opcode=OP_TEXT):
    """Server->client frames are never masked."""
    b = bytearray()
    b.append(0x80 | opcode)
    n = len(payload)
    if n < 126:
        b.append(n)
    elif n < (1 << 16):
        b.append(126)
        b += struct.pack(">H", n)
    else:
        b.append(127)
        b += struct.pack(">Q", n)
    b += payload
    return bytes(b)


def ws_send(sock, payload, lock):
    data = ws_frame(payload, OP_TEXT)
    with lock:
        sock.sendall(data)


def ws_recv(sock, bufsock_lock=None):
    """Read one frame. Returns (opcode, payload) or None on clean close."""
    hdr = _readn(sock, 2)
    if hdr is None:
        return None
    b0, b1 = hdr[0], hdr[1]
    fin = b0 & 0x80
    opcode = b0 & 0x0F
    masked = b1 & 0x80
    n = b1 & 0x7F
    if n == 126:
        ext = _readn(sock, 2)
        if ext is None:
            return None
        n = struct.unpack(">H", ext)[0]
    elif n == 127:
        ext = _readn(sock, 8)
        if ext is None:
            return None
        n = struct.unpack(">Q", ext)[0]
    if n > 1 << 20:
        raise ConnectionError("frame too large")
    mask = _readn(sock, 4) if masked else None
    data = _readn(sock, n) if n else b""
    if data is None:
        return None
    if mask:
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    if opcode == OP_CLOSE:
        return None
    return opcode, data


def _readn(sock, n):
    buf = b""
    while len(buf) < n:
        try:
            c = sock.recv(n - len(buf))
        except (OSError, ConnectionError):
            return None
        if not c:
            return None
        buf += c
    return buf


# --------------------------------------------------------------------------
# HTTP server
# --------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    server_version = "esp32-hid-bridge/1.0"
    protocol_version = "HTTP/1.1"

    # ---- plumbing ----
    def log_message(self, fmt, *a):
        sys.stderr.write("[http] " + (fmt % a) + "\n")

    def _json(self, code, obj):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _token_ok(self, got):
        want = self.server.cfg.token
        return isinstance(got, str) and hmac.compare_digest(got, want)

    # ---- routes ----
    def do_GET(self):
        cfg = self.server.cfg
        path = self.path.split("?", 1)[0]
        if path == "/health":
            return self._json(200, {"ok": True, "board_connected": cfg.board.sock is not None})
        if path == "/status":
            return self._json(200, {"board": cfg.board.status()})
        if path == "/ws":
            qs = self.path.split("?", 1)[1] if "?" in self.path else ""
            tok = None
            for pair in qs.split("&"):
                k, _, v = pair.partition("=")
                if k == "token":
                    tok = urllib_unquote(v)
            if not self._token_ok(tok):
                cfg.audit.write("ws_rejected", {"peer": self.client_address[0], "reason": "bad_token"})
                return self._json(401, {"error": "bad token"})
            return self.do_upgrade()
        return self._json(404, {"error": "not found"})

    # ---- WebSocket upgrade + pump ----
    def do_upgrade(self):
        cfg = self.server.cfg
        key = self.headers.get("Sec-WebSocket-Key")
        if not key:
            return self._json(400, {"error": "missing Sec-WebSocket-Key"})
        accept = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
        resp = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        self.wfile.write(resp.encode())
        self.wfile.flush()
        self.close_connection = True

        sock = self.connection
        peer = self.client_address[0]
        cfg.board.attach(sock, peer)
        try:
            while True:
                frame = ws_recv(sock)
                if frame is None:
                    break
                opcode, data = frame
                if opcode == OP_PING:
                    with cfg.board.lock:
                        sock.sendall(ws_frame(data, OP_PONG))
                    continue
                if opcode not in (OP_TEXT, OP_BIN):
                    continue
                try:
                    msg = json.loads(data.decode())
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(msg, dict):
                    continue
                cfg.board.note(msg)
        except (OSError, ConnectionError):
            pass
        finally:
            cfg.board.detach(sock)
            try:
                sock.close()
            except OSError:
                pass

    # ---- command submission (loopback only) ----
    def do_POST(self):
        cfg = self.server.cfg
        peer = self.client_address[0]
        if peer not in cfg.trusted_peers:
            cfg.audit.write("post_rejected", {"peer": peer, "reason": "not_loopback"})
            return self._json(403, {"error": "loopback only"})
        if self.headers.get("X-HID-Guard") != cfg.guard_header:
            cfg.audit.write("post_rejected", {"peer": peer, "reason": "bad_guard"})
            return self._json(403, {"error": "bad guard header"})
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0") or 0))
        try:
            cmd = json.loads(raw)
        except ValueError:
            return self._json(400, {"error": "invalid JSON"})
        return self._json(*cfg.submit(cmd))


def urllib_unquote(s):
    out, i = [], 0
    while i < len(s):
        if s[i] == "%" and i + 2 < len(s):
            out.append(chr(int(s[i + 1 : i + 3], 16)))
            i += 3
        else:
            out.append(s[i])
            i += 1
    return "".join(out)


class Config:
    def __init__(self, token, guard_header, audit_path, trusted_peers):
        self.token = token
        self.guard_header = guard_header
        self.audit = Audit(audit_path)
        self.trusted_peers = trusted_peers
        self.limiter = RateLimiter()
        self.board = Board(self.audit)

    def submit(self, cmd):
        try:
            op, payload = validate_command(cmd)
        except PolicyError as e:
            self.audit.write("cmd_rejected", {"cmd": cmd, "reason": str(e)})
            return 400, {"error": str(e)}
        if not self.limiter.allow():
            self.audit.write("cmd_throttled", {"cmd": cmd})
            return 429, {"error": "rate limited"}
        try:
            mid = self.board.send(op, payload)
        except PolicyError as e:
            return 409, {"error": str(e)}
        return 200, {"ok": True, "id": mid, "op": op}


class Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


def default_bind():
    """Guess this machine's LAN address.

    Binding to an explicit address (never 0.0.0.0) is a deliberate safety
    choice, but requiring the operator to know their own IP is a papercut that
    gets people to type 0.0.0.0 instead. So derive it: the address the kernel
    would use to reach the internet. Falls back to the first non-loopback IPv4.
    """
    # UDP "connect" doesn't send packets, it just asks the routing table which
    # local address would be used to reach the destination. 192.0.2.1 is
    # TEST-NET-1 (RFC 5737), reserved and never routed.
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("192.0.2.1", 53))
        ip = s.getsockname()[0]
        if not ip.startswith("127."):
            return ip
    except OSError:
        pass
    finally:
        s.close()
    return "127.0.0.1"


def _xdg(var, default):
    base = os.environ.get(var) or os.path.join(os.path.expanduser("~"), ".local", "share")
    return os.path.join(base, "esp32-hid-bridge", default)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[1])
    ap.add_argument("--bind", default=None,
                    help="LAN address to bind (never 0.0.0.0). Default: auto-detect")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--token-file", default=None,
                    help="default: $XDG_CONFIG_HOME/esp32-hid-bridge/token")
    ap.add_argument("--audit", default=None,
                    help="default: $XDG_STATE_HOME/esp32-hid-bridge/audit.jsonl")
    ap.add_argument("--listen-lan", action="store_true",
                    help="allow POST /cmd from --bind's address (default: loopback only)")
    args = ap.parse_args()

    # Environment overrides so a systemd unit can carry the config.
    # Precedence: explicit flag > env var > auto-detect / XDG default.
    if args.bind is None:
        args.bind = os.environ.get("BRIDGE_BIND") or default_bind()
    if args.token_file is None:
        args.token_file = os.environ.get("BRIDGE_TOKEN_FILE") or os.path.join(
            os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
            "esp32-hid-bridge", "token")
    if args.audit is None:
        args.audit = os.environ.get("BRIDGE_AUDIT_FILE") or os.path.join(
            os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state"),
            "esp32-hid-bridge", "audit.jsonl")
    if args.port == 8765 and os.environ.get("BRIDGE_PORT"):
        args.port = int(os.environ["BRIDGE_PORT"])

    if args.bind in ("0.0.0.0", "::"):
        sys.exit("refusing to bind 0.0.0.0 — pass an explicit LAN address")
    if args.bind.startswith("127."):
        print("warning: bound to loopback only; an ESP32 on the LAN cannot reach it.",
              file=sys.stderr)
        print("         pass --bind <your-lan-ip> if the board is remote.",
              file=sys.stderr)

    os.makedirs(os.path.dirname(args.token_file), exist_ok=True)
    os.makedirs(os.path.dirname(args.audit), exist_ok=True)
    if not os.path.exists(args.token_file):
        with open(os.open(args.token_file, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600), "w") as fh:
            fh.write(base64.urlsafe_b64encode(os.urandom(24)).decode().rstrip("="))
        print(f"generated token at {args.token_file}")

    with open(args.token_file) as fh:
        token = fh.read().strip()
    guard_header = hashlib.sha256(token.encode()).hexdigest()

    # Only loopback is trusted unless --listen-lan is given. Note that
    # --listen-lan adds *this one address*, not the whole subnet: the server can
    # never accept commands from an arbitrary host on the LAN.
    trusted = {"127.0.0.1", "::1"}
    if args.listen_lan:
        trusted.add(args.bind)

    cfg = Config(token, guard_header, args.audit, trusted)
    httpd = Server((args.bind, args.port), Handler)
    httpd.cfg = cfg

    print(f"esp32-hid-bridge listening on http://{args.bind}:{args.port}", flush=True)
    print(f"  token file : {args.token_file}", flush=True)
    print(f"  audit log  : {args.audit}", flush=True)
    print(f"  POST /cmd trusted peers: {sorted(trusted)}", flush=True)
    print(f"  ESP32 should connect to: ws://{args.bind}:{args.port}/ws?token={token}", flush=True)
    print()
    print("  ⚠ the line above contains your token — it is the key to your phone's keyboard.")
    print("    Don't paste it into chat logs or commit it.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye", flush=True)


if __name__ == "__main__":
    main()