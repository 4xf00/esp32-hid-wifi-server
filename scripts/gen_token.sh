#!/usr/bin/env bash
#
# gen_token.sh — 生成 bridge token
#
# token 是 server 和 ESP32 之间唯一的共享密钥：
#   · ESP32 用它连 WebSocket   ws://<server>:8765/ws?token=<TOKEN>
#   · agent 用它的 sha256 当 HTTP 头   X-HID-Guard: sha256(<TOKEN>)
#
# server.py 首次启动会自动生成（如果 token 文件不存在）。
# 这个脚本是给「想单独生成、或者要给多台板子配不同 token」的场景用的。
#
# 用法：
#   ./gen_token.sh                    # 生成到默认位置，打印出来
#   ./gen_token.sh --file /tmp/tok    # 生成到指定位置
#   ./gen_token.sh --show             # 只显示当前 token，不重新生成
#
set -euo pipefail

TOKEN_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/esp32-hid-bridge/token"
MODE="gen"

while [ $# -gt 0 ]; do
  case "$1" in
    --file) TOKEN_FILE="$2"; shift 2 ;;
    --show) MODE="show"; shift ;;
    -h|--help)
      awk 'NR>1 { if ($0 !~ /^#/) exit; sub(/^#[ ]?/, ""); print }' "$0"
      exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 1 ;;
  esac
done

if [ "$MODE" = "show" ]; then
  [ -f "$TOKEN_FILE" ] || { echo "token 文件不存在: $TOKEN_FILE" >&2; exit 1; }
  cat "$TOKEN_FILE"
  exit 0
fi

mkdir -p "$(dirname "$TOKEN_FILE")"

if [ -f "$TOKEN_FILE" ]; then
  echo "⚠ token 已存在，未覆盖：$TOKEN_FILE" >&2
  echo "  旧板子还在用旧 token，覆盖会让它连不上。" >&2
  echo "  确认要重新生成？旧 token 文件先备份为 .bak" >&2
  read -rp "  继续？(y/N) " yn
  case "$yn" in
    [yY]*) cp "$TOKEN_FILE" "$TOKEN_FILE.bak" ;;
    *) echo "已取消"; exit 0 ;;
  esac
fi

# 32 字符 URL-safe base64（24 随机字节，无 padding）
# 用 openssl 而不是 head -c /dev/urandom：macOS 和 Linux 都有 openssl
TOKEN="$(openssl rand -base64 24 | tr '+/' '-_' | tr -d '=\n')"

# 原子写 + 0600
TMP="$(mktemp)"
printf '%s' "$TOKEN" > "$TMP"
chmod 600 "$TMP"
mv "$TMP" "$TOKEN_FILE"

cat <<EOF
✓ token 已生成

  文件    : $TOKEN_FILE  (权限 0600)
  token   : $TOKEN

填到固件 config.h：

  static const char *SERVER_TOKEN = "$TOKEN";

填到 agent 配置：

  export BRIDGE_TOKEN="$TOKEN"
  export BRIDGE_URL="http://<server-ip>:8765"

⚠ 这个值等同于「谁能操作你手机键盘」的钥匙，别提交到 git、别贴到聊天窗口。
EOF