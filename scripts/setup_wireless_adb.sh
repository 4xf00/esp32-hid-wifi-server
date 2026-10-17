#!/usr/bin/env bash
#
# setup_wireless_adb.sh — 把 Android 手机切成「无线 ADB」模式
#
# 无线 ADB 提供两件 BLE HID 给不了的能力：
#   1. 中文/emoji 输入（`adb shell input text` 走的是 IME 通道，能打非 ASCII）
#   2. 截图 + UI 层级树读取 —— agent 判断「刚才那一下到底生效没有」的唯一手段
#
# 两条通道互补，不是替代关系。详见 README。
#
# 用法：
#   ./setup_wireless_adb.sh                 # 交互式，自动发现并询问配对码
#   ./setup_wireless_adb.sh --list          # 只列出 mDNS 发现到的设备
#   ./setup_wireless_adb.sh --ip 192.168.1.50 --pair-port 37123
#
# Android 版本走两条完全不同的路径：
#   Android 11+  开发者选项 →「无线调试」→ 用配对码配对（pair 然后 connect，两个不同端口）
#   Android 10及以下  需先 USB 连一次，开「USB 调试」→ adb tcpip 5555 → 拔线（端口固定 5555）
#
set -euo pipefail

ADB="${ADB:-adb}"
LIST_ONLY=0
IP=""
PAIR_PORT=""

c_red()   { printf '\033[31m%s\033[0m\n' "$*"; }
c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
die()     { c_red "✗ $*"; exit 1; }

usage() {
  # 从第 2 行起，取连续的 # 注释行，去掉 # 和一个空格
  awk 'NR>1 { if ($0 !~ /^#/) exit; sub(/^#[ ]?/, ""); print }' "$0"
  exit 0
}

while [ $# -gt 0 ]; do
  case "$1" in
    --list)      LIST_ONLY=1; shift ;;
    --ip)        IP="$2"; shift 2 ;;
    --pair-port) PAIR_PORT="$2"; shift 2 ;;
    -h|--help)   usage ;;
    *) die "unknown option: $1  (try --help)" ;;
  esac
done

command -v "$ADB" >/dev/null 2>&1 || die "找不到 adb。先装 platform-tools：https://developer.android.com/tools/releases/platform-tools"

c_bold "== 环境 =="
"$ADB" version | head -2
echo

# ---------------------------------------------------------------------------
# 1. mDNS 发现
# ---------------------------------------------------------------------------
# adb 从 31.0 起支持 mDNS 自动发现（`adb mdns services`）。
# 手机上要打开「开发者选项 → 无线调试」才会广播。

mdns_list() {
  "$ADB" mdns services 2>/dev/null || true
}

discover() {
  mdns_list | awk -v svc="$1" '$0 ~ svc {print}' | sed 's/.*adb-\([0-9]*\) [_a-z]*$/\1/' | sort -u
}

if [ "$LIST_ONLY" = 1 ]; then
  c_bold "== mDNS 发现到的无线调试服务 =="
  if ! mdns_list | grep -q .; then
    c_red "没发现任何服务。检查："
    echo "  1. 手机：设置 → 开发者选项 → 无线调试 已打开"
    echo "  2. 手机和这台机器在同一 WiFi（不是一个 2.4G 一个 5G 网段）"
    echo "  3. adb 版本 ≥ 31.0（当前：$("$ADB" version | head -1)）"
  else
    mdns_list
  fi
  exit 0
fi

c_bold "== 1/4 无线调试设备发现 =="

# 已 connect 的设备先列出来
CONNECTED="$("$ADB" devices | awk 'NR>1 && $2=="device" {print $1}' || true)"

if [ -z "$IP" ]; then
  # 优先从 mDNS 拿 pair 服务的地址
  MDNS_RAW="$(mdns_list)"
  PAIR_LINE="$(echo "$MDNS_RAW" | grep -E '_adb-tls-(pairing|connect)\._tcp' | head -1 || true)"

  if [ -n "$PAIR_LINE" ]; then
    ADDR="$(echo "$PAIR_LINE" | grep -oE '[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+:[0-9]+' | head -1 || true)"
    if [ -n "$ADDR" ]; then
      IP="${ADDR%%:*}"
      c_green "✓ mDNS 发现：$ADDR"
    fi
  fi
fi

if [ -z "$IP" ]; then
  # 没有 mDNS（adb 老版本，或路由器不让 mDNS 穿透）→ 退到交互式输入
  c_bold "没能自动发现。请在手机「开发者选项 → 无线调试」页面看："
  echo "  · IP 地址和端口，例如 192.168.1.50:37123"
  echo
  read -rp "  手机 IP（不含端口）: " IP
  [ -n "$IP" ] || die "IP 不能为空"
fi

# 端口分别问：配对端口和连接端口是两个不同的数字
if [ -z "$PAIR_PORT" ]; then
  read -rp "  配对端口（页面上「IP 地址和端口」的那个）: " PAIR_PORT
fi
[ -n "$PAIR_PORT" ] || die "配对端口不能为空"

# ---------------------------------------------------------------------------
# 2. 配对
# ---------------------------------------------------------------------------
c_bold
c_bold "== 2/4 配对 (adb pair) =="
c_bold "现在拿起手机：无线调试页面 →「使用配对码配对设备」"
c_bold "屏幕上会出现一个六位配对码（形如 123456），等下粘贴。"
echo

read -rp "  配对码（6 位数字）: " PAIR_CODE
[ -n "$PAIR_CODE" ] || die "配对码不能为空"

# 注意：adb pair 的密码不能带引号，也不能有尾部空格
"$ADB" pair "$IP:$PAIR_PORT" "$PAIR_CODE" \
  || die "配对失败。常见原因：① 配对码已过期（有效期约 5 分钟，重新打开那个窗口）② IP/端口抄错 ③ 手机锁屏时无线调试会断"

c_green "✓ 配对成功"

# ---------------------------------------------------------------------------
# 3. 连接
# ---------------------------------------------------------------------------
c_bold
c_bold "== 3/4 连接 (adb connect) =="
c_bold "连接端口是另一个数字（和配对端口不同！），在无线调试主页面看「IP 地址和端口」。"
echo

CONNECT_PORT=""
while [ -z "$CONNECT_PORT" ]; do
  read -rp "  连接端口: " CONNECT_PORT
  if ! printf '%s' "$CONNECT_PORT" | grep -qE '^[0-9]+$'; then
    c_red "端口必须是数字"
    CONNECT_PORT=""
  fi
done

"$ADB" connect "$IP:$CONNECT_PORT" || die "连接失败。配对信息在手机重启/换网络后会失效，需要重新 pair"

# connect 是异步的，等一下再验
sleep 1
if "$ADB" devices | grep -q "^$IP:$CONNECT_PORT[[:space:]]*device"; then
  c_green "✓ 已连接 $IP:$CONNECT_PORT"
else
  STATE="$("$ADB" devices | awk -v t="$IP:$CONNECT_PORT" '$1==t {print $2}')"
  die "连上了但状态是 '${STATE:-unknown}'。offline 通常是手机锁屏了，unauthorized 说明配对信息失效，重新 pair"
fi

# ---------------------------------------------------------------------------
# 4. 环境自检
# ---------------------------------------------------------------------------
c_bold
c_bold "== 4/4 环境自检 =="

SERIAL="$IP:$CONNECT_PORT"

SIZE="$("$ADB" -s "$SERIAL" shell wm size 2>/dev/null | tr -d '\r' | head -1)"
[ -n "$SIZE" ] && echo "  屏幕     : $SIZE"
echo "  ^ 把这个数字填进 firmware/.../config.h 的 SCREEN_W / SCREEN_H"
echo "    (若有 Override size，用 Override 那个 —— 那是实际生效的分辨率)"
echo

MODEL="$("$ADB" -s "$SERIAL" shell getprop ro.product.model 2>/dev/null | tr -d '\r')"
REL="$("$ADB" -s "$SERIAL" shell getprop ro.build.version.release 2>/dev/null | tr -d '\r')"
echo "  设备     : $MODEL / Android $REL"
echo

# 输入锁屏超时会影响长任务 —— 自动放宽
TIMEOUT="$("$ADB" -s "$SERIAL" shell settings get system screen_off_timeout 2>/dev/null | tr -d '\r' || echo 0)"
if [ "${TIMEOUT:-0}" -lt 1800000 ] 2>/dev/null; then
  echo "  息屏超时 : 当前 ${TIMEOUT}ms，偏短。agent 跑长流程时手机会中途熄屏。"
  read -rp "  放宽到 30 分钟？(y/N) " yn
  case "$yn" in
    [yY]*) "$ADB" -s "$SERIAL" shell settings put system screen_off_timeout 1800000 \
             && c_green "  ✓ 已设为 30 分钟" ;;
  esac
fi
echo

c_bold "== 完成 =="
cat <<EOF
已就绪。把这两个值填进 agent 的配置里：

  ADB_SERIAL = ${SERIAL}

快速验证：

  adb -s ${SERIAL} shell input text 'hello'
  adb -s ${SERIAL} shell screencap -p /sdcard/t.png && adb -s ${SERIAL} pull /sdcard/t.png

注意：
  · 手机重启、换 WiFi、或无线调试被系统回收后，配对信息会失效，重新跑本脚本。
  · 这台机器防火墙若开了，要放行 adb 的 5555/tcp（Android 10 及以下）。
  · 开无线调试时手机会显示「已连接 xx.xx.xx.xx」，那是正常的。
EOF