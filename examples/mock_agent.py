#!/usr/bin/env python3
"""
mock_agent.py — 不用硬件、不用手机，跑通「server + 客户端」全链路。

用途
----
1. 改完 server.py 之后，确认协议没被我改坏
2. 理解 agent 该怎么和这个 bridge 交互（打印每条命令）
3. CI 里当冒烟测试

跑法（三 个终端，或者用 --all 一条命令）::

    # 终端 1
    cd server && python3 server.py --port 8765

    # 终端 2（模拟板子）
    python3 mock_board.py 127.0.0.1 8765 "$(cat ~/.config/esp32-hid-bridge/token)"

    # 终端 3（agent）
    python3 examples/mock_agent.py

    # 或者一条命令全跑（自己起 server 和 mock board，跑完自动清理）
    python3 examples/mock_agent.py --all

注意：mock board 只是打印命令，**不产生任何真实 HID 报告**。它验证的是
「服务端策略 + 协议 + 认证 + 限流 + 审计」，不是「手机会动」。
"""

import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.abspath(os.path.join(HERE, ".."))
SERVER = os.path.join(ROOT, "server", "server.py")
MOCK = os.path.join(ROOT, "server", "mock_board.py")
sys.path.insert(0, os.path.join(ROOT, "server"))

from bridge_client import Bridge, BridgeError  # noqa: E402


def demo(b):
    print("=" * 62)
    print("1. 链路自检")
    print("=" * 62)
    print("  health:", b.health())
    print("  status:", b.status())

    print()
    print("=" * 62)
    print("2. 七个 op 正常路径")
    print("=" * 62)
    ok_ops = [
        ("ping",                lambda: b.ping()),
        ("type 'hello'",        lambda: b.type_text("hello")),
        ("key VOLUME_UP",       lambda: b.key("VOLUME_UP")),
        ("tap(610,1356)",       lambda: b.tap(610, 1356)),
        ("swipe up",            lambda: b.swipe(610, 2000, 610, 800)),
        ("scroll(-3)",          lambda: b.scroll(-3)),
        ("combo CTRL+A",        lambda: b.combo("CTRL", "A")),
    ]
    for label, fn in ok_ops:
        try:
            print(f"  {label:22} -> {fn()}")
        except BridgeError as e:
            print(f"  {label:22} -> ✗ {e}")

    print()
    print("=" * 62)
    print("3. 安全边界：这些必须被拒绝")
    print("=" * 62)
    bad_ops = [
        ("中文（BLE HID 打不出）", lambda: b.type_text("你好世界")),
        ("任意键名",            lambda: b.key("rm -rf")),
        ("越界坐标",            lambda: b.tap(99999, 0)),
        ("BLE 无此 usage",      lambda: b.key("POWER")),
        ("超长文本 201 字符",   lambda: b.type_text("a" * 201)),
        ("组合键无修饰键",      lambda: b.combo("ENTER", "A")),
        ("未知 op",             lambda: b._post({"op": "sudo"})),
    ]
    rejected = 0
    for label, fn in bad_ops:
        try:
            r = fn()
            print(f"  {label:22} -> ✗✗ 竟然通过了！{r}")
        except BridgeError as e:
            rejected += 1
            print(f"  {label:22} -> 已拒绝: {str(e)[:52]}")
    print(f"\n  {rejected}/{len(bad_ops)} 被正确拒绝")

    print()
    print("=" * 62)
    print("4. 限流：令牌桶（突发 20，之后 429）")
    print("=" * 62)
    ok = lim = 0
    for _ in range(30):
        try:
            b.ping(); ok += 1
        except BridgeError:
            lim += 1
    print(f"  30 连发：{ok} 条通过，{lim} 条被限流")
    print("  （令牌桶会以 3 条/秒的速度回补，等一秒再试就能过）")

    print()
    print("=" * 62)
    print("5. 无认证信息 → 必须 403/401")
    print("=" * 62)
    import urllib.error
    import urllib.request
    try:
        req = urllib.request.Request(b.base + "/cmd", data=b'{"op":"ping"}',
                                     method="POST",
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=5)
        print("  ✗✗ 无 guard header 竟然通过了")
    except urllib.error.HTTPError as e:
        print(f"  无 guard header -> HTTP {e.code}")
    try:
        urllib.request.urlopen(b.base + "/ws?token=wrongtoken", timeout=5)
        print("  ✗✗ 错 token 竟然握手成功了")
    except urllib.error.HTTPError as e:
        print(f"  错 token 升级 WS -> HTTP {e.code}")

    print()
    print("=" * 62)
    print("全部跑完。审计日志在：")
    print("  ~/.local/state/esp32-hid-bridge/audit.jsonl")
    print("=" * 62)
    return rejected == len(bad_ops)


def run_all():
    """Start server + mock board, run the demo, tear everything down."""
    port = 18765
    env = dict(os.environ)
    tmp = "/tmp/mock_agent_demo"
    os.makedirs(tmp, exist_ok=True)
    env["BRIDGE_TOKEN_FILE"] = f"{tmp}/token"
    env["BRIDGE_AUDIT_FILE"] = f"{tmp}/audit.jsonl"
    env["BRIDGE_BIND"] = "127.0.0.1"
    env["BRIDGE_PORT"] = str(port)
    env["BRIDGE_URL"] = f"http://127.0.0.1:{port}"
    env["PYTHONPATH"] = ""

    srv = subprocess.Popen([sys.executable, SERVER], env=env,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        # Wait for the token file the server generates on first run.
        for _ in range(40):
            if os.path.exists(env["BRIDGE_TOKEN_FILE"]):
                break
            time.sleep(0.1)
        time.sleep(0.4)

        with open(env["BRIDGE_TOKEN_FILE"]) as fh:
            token = fh.read().strip()
        mock = subprocess.Popen(
            [sys.executable, MOCK, "127.0.0.1", str(port), token], env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            time.sleep(1.0)
            # Bridge() reads os.environ, and `env` above is only what the
            # child processes get — so publish it here too.
            for k in ("BRIDGE_URL", "BRIDGE_TOKEN_FILE"):
                os.environ[k] = env[k]
            all_rejected = demo(Bridge())
        finally:
            mock.terminate()
        return 0 if all_rejected else 1
    finally:
        srv.terminate()
        try:
            srv.wait(timeout=5)
        except subprocess.TimeoutExpired:
            srv.kill()


if __name__ == "__main__":
    if "--all" in sys.argv:
        raise SystemExit(run_all())
    demo(Bridge())