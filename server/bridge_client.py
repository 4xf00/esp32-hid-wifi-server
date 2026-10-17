#!/usr/bin/env python3
"""
bridge_client.py — Python client for the ESP32 HID bridge.

Three ways to configure, in priority order:

1. Constructor arguments
       Bridge(host="192.168.1.10", token="....")
2. Environment variables
       export BRIDGE_URL=http://192.168.1.10:8765
       export BRIDGE_TOKEN=...        # or BRIDGE_TOKEN_FILE=/path/to/token
3. Config file
       ~/.config/esp32-hid-bridge/client.conf   (KEY=VALUE lines)

Quick use::

    from bridge_client import Bridge

    b = Bridge()
    print(b.status())
    b.type_text("hello")          # ASCII only — CJK goes over ADB
    b.key("ENTER")
    b.tap(610, 1356)              # absolute pixels, phone screen size
    b.swipe(610, 2000, 610, 800)
    b.combo("CTRL", "A")
    b.scroll(-3)
    b.ping()

CLI::

    python3 bridge_client.py status
    python3 bridge_client.py key VOLUME_UP
    python3 bridge_client.py type "hello world"
    python3 bridge_client.py tap 610 1356

Note on `trusted_peers`: the server only accepts POST /cmd from loopback or
from its own --bind address. So this client has to run *on the server host*.
Put it behind an SSH tunnel or a local agent if you need remote submission --
see README.
"""

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request

CONFIG_PATH = os.path.join(
    os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
    "esp32-hid-bridge", "client.conf")


class BridgeError(RuntimeError):
    pass


class BridgeConfigError(BridgeError):
    pass


def _load_config_file(path):
    """Parse KEY=VALUE lines. A missing file is fine — env or args may cover it."""
    out = {}
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, _, v = line.partition("=")
            out[k.strip().upper()] = v.strip().strip('"').strip("'")
    return out


def _read_token_file(path):
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError as e:
        raise BridgeConfigError(f"cannot read token file {path}: {e}") from None


def _resolve_token(explicit=None):
    if explicit and explicit.strip():
        return explicit.strip()
    if os.environ.get("BRIDGE_TOKEN"):
        return os.environ["BRIDGE_TOKEN"].strip()
    if os.environ.get("BRIDGE_TOKEN_FILE"):
        return _read_token_file(os.environ["BRIDGE_TOKEN_FILE"])
    cfg = _load_config_file(CONFIG_PATH)
    if cfg.get("TOKEN"):
        return cfg["TOKEN"]
    if cfg.get("TOKEN_FILE"):
        return _read_token_file(cfg["TOKEN_FILE"])
    # Last resort: the server's own token file — this client normally runs on
    # the same host, and that file is exactly what the server generated.
    default_token = os.path.join(
        os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config"),
        "esp32-hid-bridge", "token")
    if os.path.exists(default_token):
        return _read_token_file(default_token)
    raise BridgeConfigError(
        "no token found. Set BRIDGE_TOKEN or BRIDGE_TOKEN_FILE, or run "
        "scripts/gen_token.sh")


class Bridge:
    def __init__(self, url=None, host=None, port=None, token=None,
                 token_file=None, timeout=10):
        cfg = _load_config_file(CONFIG_PATH)

        if url is None:
            url = os.environ.get("BRIDGE_URL") or cfg.get("URL")
        if url is None:
            host = host or os.environ.get("BRIDGE_HOST") or cfg.get("HOST") or "127.0.0.1"
            port = port or os.environ.get("BRIDGE_PORT") or cfg.get("PORT") or 8765
            url = f"http://{host}:{port}"
        self.base = url.rstrip("/")
        self.timeout = timeout

        tok = None
        if token:
            tok = token
        elif token_file:
            tok = _read_token_file(token_file)
        elif cfg.get("TOKEN_FILE"):
            tok = _read_token_file(cfg["TOKEN_FILE"])
        self.token = _resolve_token(tok)
        self.guard = hashlib.sha256(self.token.encode()).hexdigest()

    # ---- transport ----------------------------------------------------
    def _post(self, cmd):
        body = json.dumps(cmd).encode()
        req = urllib.request.Request(
            self.base + "/cmd", data=body, method="POST",
            headers={"X-HID-Guard": self.guard, "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                out = json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                detail = json.loads(e.read()).get("error", str(e))
            except Exception:
                detail = str(e)
            if e.code == 403 and "loopback" in str(detail):
                detail += ("  — the server accepts POST /cmd only from loopback or its own "
                           "--bind address. Run this client on the server host, or tunnel: "
                           "`ssh -L 8765:127.0.0.1:8765 user@server` then "
                           "BRIDGE_URL=http://127.0.0.1:8765")
            raise BridgeError(f"HTTP {e.code}: {detail}") from None
        except urllib.error.URLError as e:
            raise BridgeError(f"cannot reach {self.base}: {e.reason}") from None
        if isinstance(out, dict) and "error" in out:
            raise BridgeError(out["error"])
        return out

    def _get(self, path):
        try:
            with urllib.request.urlopen(self.base + path, timeout=self.timeout) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            raise BridgeError(f"HTTP {e.code} on {path}") from None
        except urllib.error.URLError as e:
            raise BridgeError(f"cannot reach {self.base}: {e.reason}") from None

    # ---- ops ----------------------------------------------------------
    def ping(self):
        return self._post({"op": "ping"})

    def type_text(self, text):
        return self._post({"op": "type", "text": text})

    def key(self, name):
        return self._post({"op": "key", "key": name})

    def tap(self, x, y, ms=70):
        return self._post({"op": "tap", "x": x, "y": y, "ms": ms})

    def swipe(self, x1, y1, x2, y2, ms=300):
        return self._post({"op": "swipe", "x1": x1, "y1": y1, "x2": x2, "y2": y2, "ms": ms})

    def scroll(self, dy):
        return self._post({"op": "scroll", "dy": dy})

    def combo(self, modifier, key):
        return self._post({"op": "combo", "keys": [modifier, key]})

    # ---- introspection -------------------------------------------------
    def health(self):
        return self._get("/health")

    def status(self):
        return self._get("/status")

    def is_connected(self):
        """Cheap 'is the board there' check. Never raises."""
        try:
            return bool(self.health().get("board_connected"))
        except BridgeError:
            return False


# ---- CLI ---------------------------------------------------------------
CLI_METHODS = {
    "ping": "ping", "status": "status", "health": "health",
    "type": "type_text", "key": "key", "tap": "tap",
    "swipe": "swipe", "scroll": "scroll", "combo": "combo",
}


def _coerce(a):
    try:
        return int(a)
    except ValueError:
        return a


def _main(argv):
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    op = argv[0]
    if op not in CLI_METHODS:
        print(f"unknown op {op!r}. try: {', '.join(sorted(CLI_METHODS))}", file=sys.stderr)
        return 2

    b = Bridge()
    try:
        out = getattr(b, CLI_METHODS[op])(*[_coerce(a) for a in argv[1:]])
    except BridgeError as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    print(json.dumps(out, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))