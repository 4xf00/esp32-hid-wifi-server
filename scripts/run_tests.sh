#!/usr/bin/env bash
#
# run_tests.sh — 不需要硬件的自测。CI 和改完代码后跑这个。
#
# 覆盖：协议握手、命令策略白名单、认证、限流、审计日志。
# 不覆盖：真实 BLE HID 报告（那需要板子和手机）。
#
set -euo pipefail

cd "$(dirname "$0")/.."

PY="${PYTHON:-python3}"
export PYTHONPATH=""

c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_red()   { printf '\033[31m%s\033[0m\n' "$*"; }
c_bold()  { printf '\033[1m%s\033[0m\n' "$*"; }

c_bold "== 语法检查 =="
for f in server/*.py examples/*.py; do
  "$PY" -m py_compile "$f" && c_green "  ✓ $f"
done

c_bold
c_bold "== shell 脚本语法检查 =="
for f in scripts/*.sh; do
  bash -n "$f" && c_green "  ✓ $f"
done

c_bold
c_bold "== 服务端默认拒绝 0.0.0.0 =="
# Capture first, then grep: server.py exits 1 on refusal, and under `pipefail`
# that would mask the grep result and abort the script.
BIND_OUT="$("$PY" server/server.py --bind 0.0.0.0 2>&1 || true)"
if printf '%s' "$BIND_OUT" | grep -q "refusing to bind"; then
  c_green "  ✓ 拒绝绑 0.0.0.0"
else
  c_red "  ✗ 竟然允许了 0.0.0.0：$BIND_OUT"
  exit 1
fi

c_bold
c_bold "== 全链路自测（server + mock board）=="
# mock_agent.py --all 自己起进程、跑完清理。退出码非 0 表示有该被拒的没被拒。
if "$PY" examples/mock_agent.py --all > /tmp/esp32_hid_selftest.log 2>&1; then
  grep -E '被正确拒绝|条通过|条被限流|HTTP 40' /tmp/esp32_hid_selftest.log | sed 's/^/  /'
  c_green "  ✓ 全部断言通过"
else
  cat /tmp/esp32_hid_selftest.log
  c_red "  ✗ 自测失败"
  exit 1
fi

c_bold
c_green "全绿。注意：真实 BLE HID 效果仍需板子 + 手机实测。详见 README「已验证 / 未验证」。"