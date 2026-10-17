#!/usr/bin/env python3
"""Mock ESP32 board: speaks the bridge protocol so the server can be tested
without hardware. Prints every command it receives."""

import base64
import hashlib
import json
import os
import socket
import struct
import sys
import time

GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def recv_exact(s, n):
    b = b""
    while len(b) < n:
        c = s.recv(n - len(b))
        if not c:
            return None
        b += c
    return b


def ws_send(s, payload):
    b = bytearray([0x81])
    n = len(payload)
    if n < 126:
        b.append(0x80 | n)
    elif n < 1 << 16:
        b.append(0x80 | 126)
        b += struct.pack(">H", n)
    else:
        b.append(0x80 | 127)
        b += struct.pack(">Q", n)
    mask = os.urandom(4)
    b += mask
    b += bytes(c ^ mask[i % 4] for i, c in enumerate(payload))
    s.sendall(bytes(b))


def ws_recv(s):
    h = recv_exact(s, 2)
    if not h:
        return None
    op = h[0] & 0x0F
    n = h[1] & 0x7F
    if n == 126:
        n = struct.unpack(">H", recv_exact(s, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", recv_exact(s, 8))[0]
    if h[1] & 0x80:
        mask = recv_exact(s, 4)
        data = recv_exact(s, n)
        data = bytes(c ^ mask[i % 4] for i, c in enumerate(data))
    else:
        data = recv_exact(s, n) if n else b""
    return op, data


def main():
    if len(sys.argv) != 4:
        print("usage: mock_board.py <host> <port> <token>", file=sys.stderr)
        print("  e.g. python3 mock_board.py 127.0.0.1 8765 $(cat ~/.config/esp32-hid-bridge/token)",
              file=sys.stderr)
        return 2
    host, port, token = sys.argv[1], int(sys.argv[2]), sys.argv[3]
    s = socket.create_connection((host, port), timeout=10)
    s.settimeout(180)
    key = base64.b64encode(os.urandom(16)).decode()
    s.sendall(
        (
            f"GET /ws?token={token} HTTP/1.1\r\nHost: {host}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n"
        ).encode()
    )
    buf = b""
    while b"\r\n\r\n" not in buf:
        c = s.recv(1)
        if not c:
            print("handshake failed", file=sys.stderr)
            return 1
        buf += c
    head = buf.decode(errors="replace")
    if "101" not in head.split("\r\n")[0]:
        print("upgrade refused:", head.split("\r\n")[0], file=sys.stderr)
        return 1
    expect = base64.b64encode(hashlib.sha1((key + GUID).encode()).digest()).decode()
    assert expect in head, "bad Sec-WebSocket-Accept"
    print("MOCK: upgraded, handshake verified", flush=True)

    ws_send(s, json.dumps({"ev": "hello", "fw": "mock-1.0", "ble": True}).encode())

    while True:
        frame = ws_recv(s)
        if frame is None:
            break
        op, data = frame
        if op == 0x8:
            break
        if op != 0x1:
            continue
        msg = json.loads(data.decode())
        print(f"MOCK <- {json.dumps(msg)}", flush=True)
        t0 = time.time()
        ws_send(
            s,
            json.dumps(
                {"id": msg.get("id"), "ev": "done", "ok": True,
                 "ms": int((time.time() - t0) * 1000)}
            ).encode(),
        )


if __name__ == "__main__":
    sys.exit(main() or 0)