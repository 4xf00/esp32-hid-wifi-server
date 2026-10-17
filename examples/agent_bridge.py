#!/usr/bin/env python3
"""
agent_bridge.py — 把 BLE HID 和无线 ADB 组合成一个能自我验证的 agent 接口。

为什么需要这个文件

BLE HID 只解决"输入"。它发出去的每一条指令，agent 都收不到任何回执 ——
`tap(610, 1356)` 返回 {"ok": true} 只说明 JSON 校验通过、命令写进了 WebSocket，
手机屏幕上到底发生了什么，没有任何反馈通道。

于是 agent 会陷入一个很糟的循环：点了一下、没反应、不知道是坐标错了还是页面还没
加载完、再点一下、更糟。

解法是补上 ADB 那一侧的观察能力：

    hid.tap(x, y)          →  真实硬件输入，能进安全键盘
    adb.screenshot()        →  屏幕上现在是什么
    adb.ui_dump()           →  界面上有哪些可点的元素（带 resource-id / text）

组合起来就能写"点了就确认"的闭环，而不是盲点。

用法
----
    python3 agent_bridge.py status
    python3 agent_bridge.py screenshot /tmp/shot.png
    python3 agent_bridge.py tap-and-verify 610 1356 --expect "已保存"
    python3 agent_bridge.py tap-text "设置"        # 按文本找元素再点（更稳）
    python3 agent_bridge.py type-cjk "你好世界"     # 中文必须走 ADB

要求
----
1. HID 侧：server.py 在本机跑着（见 README）
2. ADB 侧：跑过 scripts/setup_wireless_adb.sh，或用 USB 连着
   ADB_SERIAL 环境变量指定设备（无线 ADB 模式下形如 192.168.1.50:41337）
"""

import os
import re
import subprocess
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "server"))
try:
    from bridge_client import Bridge, BridgeError
except ImportError:
    print("✗ 找不到 bridge_client.py。先 cd 到 server/ 跑一次 server.py，或检查路径。",
          file=sys.stderr)
    raise SystemExit(1)


# ===========================================================================
# ADB 侧
# ===========================================================================

def _serial():
    return os.environ.get("ADB_SERIAL")


def adb(*args, timeout=25, binary=False):
    """Run an adb command. Returns stdout as str, or bytes when binary=True."""
    cmd = ["adb"]
    s = _serial()
    if s:
        cmd += ["-s", s]
    cmd += list(args)
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout,
                           text=not binary)
    except FileNotFoundError:
        raise RuntimeError("adb not found on PATH — install platform-tools") from None
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"adb timed out: {' '.join(args)}") from None
    if r.returncode != 0:
        err = (r.stderr if isinstance(r.stderr, str)
               else r.stderr.decode("utf-8", "replace"))
        raise RuntimeError(f"adb {' '.join(args)} failed: {err.strip()}")
    return r.stdout if not binary else r.stdout


def adb_connected():
    try:
        out = subprocess.run(["adb", "devices"], capture_output=True,
                             text=True, timeout=10).stdout
    except Exception:
        return False
    return bool(re.search(r"\tdevice$", out, re.M))


def screen_size():
    """Physical resolution, falling back to the override when one is set."""
    out = adb("shell", "wm", "size")
    sizes = re.findall(r"(\d+)x(\d+)", out)
    if not sizes:
        raise RuntimeError("cannot read screen size: `wm size` gave nothing")
    # wm size prints Override first when present, then Physical.
    w, h = sizes[-1] if "Override" not in out else sizes[0]
    return int(w), int(h)


def screenshot(path):
    """Grab the screen to a local PNG. Uses exec-out so no temp file on device."""
    data = adb("exec-out", "screencap", "-p", binary=True)
    with open(path, "wb") as fh:
        fh.write(data)
    return path


class UiNode:
    __slots__ = ("cls", "rid", "text", "desc", "bounds", "clickable")

    def __init__(self, **kw):
        for k in self.__slots__:
            setattr(self, k, kw.get(k))

    @property
    def center(self):
        m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", self.bounds or "")
        if not m:
            return None
        x1, y1, x2, y2 = map(int, m.groups())
        return (x1 + x2) // 2, (y1 + y2) // 2

    @property
    def label(self):
        return self.text or self.desc or self.rid or self.cls or "?"

    def __repr__(self):
        return (f"<UiNode {self.cls} rid={self.rid!r} text={self.text!r} "
                f"desc={self.desc!r} bounds={self.bounds!r} clickable={self.clickable}>")


def ui_dump(remote="/sdcard/ui.xml", local=None):
    """Dump the view hierarchy. Returns (root, [UiNode])."""
    adb("shell", "uiautomator", "dump", remote, timeout=30)
    xml = adb("exec-out", "cat", remote)
    local = local or "/tmp/ui_dump.xml"
    with open(local, "wb") as fh:
        fh.write(xml)
    try:
        root = ET.fromstring(xml)
    except ET.ParseError as e:
        raise RuntimeError(f"uiautomator output is not valid XML: {e}") from None

    nodes = []
    for el in root.iter("node"):
        nodes.append(UiNode(
            cls=el.get("class"),
            rid=el.get("resource-id"),
            text=el.get("text"),
            desc=el.get("content-desc"),
            bounds=el.get("bounds"),
            clickable=el.get("clickable") == "true",
        ))
    return root, nodes


def find_node(nodes, target, require_clickable=True):
    """Locate by resource-id, then exact text, then content-desc.

    Returns (node, how). Reports ambiguity rather than silently taking the
    first of five matches — picking the wrong '确定' is worse than failing.
    """
    target = target.strip()

    def collect(pred):
        return [n for n in nodes if pred(n)]

    for how, pred in (
        ("resource-id", lambda n: n.rid and (n.rid == target or n.rid.rsplit("/", 1)[-1] == target)),
        ("text",        lambda n: n.text == target),
        ("content-desc", lambda n: n.desc == target),
        ("text~",       lambda n: n.text and target in n.text),
    ):
        hits = collect(pred)
        if require_clickable:
            clickable = [n for n in hits if n.clickable]
            if clickable:
                hits = clickable
        if len(hits) == 1:
            return hits[0], how
        if len(hits) > 1:
            raise LookupError(
                f"{target!r} matched {len(hits)} nodes via {how}; "
                f"be specific. first: {hits[0]!r}")
    raise LookupError(f"{target!r} not found in view hierarchy")


# ===========================================================================
# 组合动作
# ===========================================================================

class AgentPhone:
    """HID for input, ADB for observation. Use both or the agent is blind."""

    def __init__(self, verify_delay=0.6):
        self.hid = Bridge()
        self.verify_delay = verify_delay

    # ---- status ----
    def status(self):
        return {"hid": self.hid.status(), "adb_connected": adb_connected(),
                "adb_serial": _serial()}

    def preflight(self):
        """Check both channels before starting a multi-step task.

        Cheap to call, and it turns 'nothing happened' into a specific message
        instead of a mystery.
        """
        problems = []
        try:
            st = self.hid.status()
            if not st.get("board"):
                problems.append("no ESP32 connected to the bridge")
        except BridgeError as e:
            problems.append(f"bridge unreachable: {e}")
        if not adb_connected():
            problems.append(
                "no adb device (run scripts/setup_wireless_adb.sh, or set ADB_SERIAL)")
        return problems

    # ---- input via HID ----
    def type_ascii(self, text):
        return self.hid.type_text(text)

    def tap(self, x, y, ms=70):
        return self.hid.tap(x, y, ms)

    def swipe(self, x1, y1, x2, y2, ms=300):
        return self.hid.swipe(x1, y1, x2, y2, ms)

    def scroll(self, dy):
        return self.hid.scroll(dy)

    def key(self, name):
        return self.hid.key(name)

    def combo(self, mod, k):
        return self.hid.combo(mod, k)

    def hold(self, key, seconds):
        """ADB has no press-and-hold, so long-presses must go over HID.

        The bridge protocol has no separate press/release ops, so this is
        approximated: it says so rather than pretending.
        """
        raise NotImplementedError(
            "the bridge protocol exposes no key_down/key_up. Add them to "
            "server.py ALLOWED_OPS and firmware handleCommand() if you need "
            "real long-press (IME confirm, slider drag, pull-to-refresh).")

    # ---- input via ADB (for what HID cannot do) ----
    def type_cjk(self, text):
        """Non-ASCII text. adb input text cannot handle spaces or most punctuation
        reliably, so shell-quote carefully."""
        return adb("shell", "input", "text", _adb_quote(text))

    def adb_tap(self, x, y):
        return adb("shell", "input", "tap", str(x), str(y))

    # ---- verify (the part that makes this more than a remote control) ----
    def shot(self, path="/tmp/phone.png"):
        return screenshot(path)

    def texts(self):
        """Every non-empty text/content-desc currently on screen."""
        _, nodes = ui_dump()
        return [n.label for n in nodes if n.text or n.desc]

    def wait_for(self, needle, timeout=8.0, poll=0.7):
        """Wait until `needle` appears in the view hierarchy."""
        deadline = time.time() + timeout
        last = []
        while time.time() < deadline:
            try:
                last = self.texts()
            except RuntimeError:
                last = []
            if any(needle in t for t in last):
                return True, last
            time.sleep(poll)
        return False, last

    def tap_and_verify(self, x, y, expect, timeout=8.0):
        """Tap over HID, then confirm via the view hierarchy.

        This is the whole point of running both channels.
        """
        self.tap(x, y)
        time.sleep(self.verify_delay)
        ok, seen = self.wait_for(expect, timeout)
        return {
            "tapped": (x, y),
            "expect": expect,
            "verified": ok,
            "visible": seen[:25],
        }

    def tap_text_and_verify(self, target, expect=None, timeout=8.0):
        """Locate an element by id/text/desc, tap its center over HID, verify.

        More robust than fixed coordinates: survives a layout change, a font
        scale change, or a different device.
        """
        _, nodes = ui_dump()
        node, how = find_node(nodes, target)
        c = node.center
        if not c:
            raise RuntimeError(f"node has unusable bounds: {node.bounds!r}")
        result = {"matched_by": how, "node": repr(node), "center": c}
        result.update(self.tap_and_verify(c[0], c[1], expect or node.label, timeout))
        return result


def _adb_quote(s):
    """Quote for `adb shell input text`.

    `input text` treats %s as a space and chokes on most shell metacharacters,
    so spaces become %s and everything else is single-quoted for the device shell.
    """
    return "'" + s.replace(" ", "%s").replace("'", "'\\''") + "'"


# ===========================================================================
# CLI
# ===========================================================================

def _cmd_status(p):
    print(p.status())
    problems = p.preflight()
    print("\npreflight:", "OK" if not problems else "")
    for x in problems:
        print("  ✗", x)


def _cmd_screenshot(p, args):
    path = args[0] if args else "/tmp/phone.png"
    print(p.shot(path))


def _cmd_texts(p, args):
    for t in p.texts():
        print(t)


def _cmd_tap(p, args):
    print(p.tap(int(args[0]), int(args[1])))
    if len(args) > 2:
        print(p.wait_for(args[2])[0])


def _cmd_tap_verify(p, args):
    r = p.tap_and_verify(int(args[0]), int(args[1]), args[2])
    print(r)
    raise SystemExit(0 if r["verified"] else 1)


def _cmd_tap_text(p, args):
    r = p.tap_text_and_verify(args[0], args[1] if len(args) > 1 else None)
    print(r)
    raise SystemExit(0 if r["verified"] else 1)


def _cmd_type_cjk(p, args):
    print(p.type_cjk(" ".join(args)))


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        return 2
    p = AgentPhone()
    cmd, args = sys.argv[1], sys.argv[2:]
    table = {
        "status":            lambda: _cmd_status(p),
        "screenshot":        lambda: _cmd_screenshot(p, args),
        "texts":             lambda: _cmd_texts(p, args),
        "tap":               lambda: _cmd_tap(p, args),
        "tap-and-verify":    lambda: _cmd_tap_verify(p, args),
        "tap-text":          lambda: _cmd_tap_text(p, args),
        "type-cjk":          lambda: _cmd_type_cjk(p, args),
    }
    if cmd not in table:
        print(f"unknown command {cmd!r}. try: {', '.join(sorted(table))}", file=sys.stderr)
        return 2
    try:
        table[cmd]()
    except (RuntimeError, BridgeError, LookupError) as e:
        print(f"✗ {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())