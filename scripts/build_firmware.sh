#!/usr/bin/env bash
#
# build_firmware.sh — 编译 + 烧录 ESP32-C3 固件
#
# 依赖：
#   arduino-cli                    https://arduino.ccc.cc/download
#   core: esp32:esp32@2.0.17       arduino-cli core install esp32:esp32@2.0.17
#   lib : WebSockets@2.7.2         arduino-cli lib install WebSockets@2.7.2
#   lib : ESP32-BLE-HID            源码安装（不在库管理器里），见下面 install_lib()
#
# 用法：
#   ./build_firmware.sh build            # 只编译
#   ./build_firmware.sh upload           # 编译 + 用 arduino-cli 烧录
#   ./build_firmware.sh upload-flash     # 编译 + esptool 手动烧（绕过坏 bootloader，推荐）
#   ./build_firmware.sh size             # 只看体积
#   ./build_firmware.sh install-lib      # 装 ESP32-BLE-HID 库（只需一次）
#
set -euo pipefail

SKETCH_DIR="$(cd "$(dirname "$0")/.." && pwd)/firmware/esp32c3_hid_agent"
BUILD_DIR="${SKETCH_DIR}/build"
# 关键：no_ota 方案给 2MB app 分区。默认方案只有 1.2MB，装不下（会报 117%）。
FQBN="esp32:esp32:esp32c3:PartitionScheme=no_ota"

# 自定义 2MB 单分区表，供 upload-flash 用。
# 不能直接用 arduino-cli 的 no_ota 烧录：它那张表叫 app0/ota_0，
# 而 IDF bootloader（下面烧的）只认 factory —— 见 README「烧录」一节。
PART_CSV="$BUILD_DIR/partitions_2mb.csv"
PART_BIN="$BUILD_DIR/partitions_2mb.bin"

c_bold()  { printf '\033[1m%s\033[0m\n' "$*"; }
c_green() { printf '\033[32m%s\033[0m\n' "$*"; }
c_red()   { printf '\033[31m%s\033[0m\n' "$*"; }
die()     { c_red "✗ $*"; exit 1; }

usage() {
  awk 'NR>1 { if ($0 !~ /^#/) exit; sub(/^#[ ]?/, ""); print }' "$0"
  exit 0
}

install_lib() {
  c_bold "== 安装 ESP32-BLE-HID 库 =="
  # 这个库不在 Arduino Library Manager 里（上游 0cococ/ESP32-BLE-HID 未发布到注册表），
  # 所以只能从源码装。Autfy 那个 fork 是唯一在 C3 上验证过的。
  #
  # 装到 sketch 里的 lib/ 是**错的**：那是 Arduino IDE 的约定，arduino-cli 完全不看它
  # （它的库搜索路径只有 arduino-cli 的 directories.user + directories.data 下那几处）。
  # 必须装到 arduino-cli 自己认的 sketchbook libraries 目录。
  SKETCHBOOK="$(arduino-cli config get directories.user 2>/dev/null || true)"
  if [ -z "$SKETCHBOOK" ] || [ "$SKETCHBOOK" = "null" ]; then
    SKETCHBOOK="$HOME/Arduino"
  fi
  LIB_ROOT="$SKETCHBOOK/libraries"
  LIB_DST="$LIB_ROOT/ESP32-BLE-HID"
  REPO="https://github.com/Autfy/ESP32-BLE-HID.git"

  echo "  sketchbook : $SKETCHBOOK"

  if [ -d "$LIB_DST/.git" ] || [ -f "$LIB_DST/library.properties" ]; then
    c_green "  已存在: $LIB_DST"
  else
    if [ -e "$LIB_DST" ]; then
      die "$LIB_DST 已存在但不是 git 库，请手动删掉后重试"
    fi
    mkdir -p "$LIB_ROOT"
    git clone --depth 1 "$REPO" "$LIB_DST" || die "clone 失败：$REPO"
    c_green "  ✓ 已 clone 到 $LIB_DST"
  fi

  # 打补丁：库里用了 delay()/millis() 但没 include <Arduino.h>。
  # Arduino IDE 会宽容地补上，arduino-cli 的严格模式不会，直接编译失败。
  c_bold "== 打补丁（补 #include <Arduino.h>）=="
  PATCHED=0
  for f in "$LIB_DST"/*.cpp; do
    if ! grep -q '#include <Arduino.h>' "$f"; then
      printf '#include <Arduino.h>  // patched: delay()/millis() need Arduino.h under arduino-cli\n' \
        | cat - "$f" > "$f.tmp" && mv "$f.tmp" "$f"
      c_green "  ✓ $(basename "$f")"
      PATCHED=$((PATCHED+1))
    fi
  done
  [ "$PATCHED" = 0 ] && c_green "  已是最新，无需补丁"

  # 验证 arduino-cli 真的认得它 —— 这一步是必要的：装错目录时 arduino-cli 只会
  # 在编译最后报 "BleCombo.h: No such file or directory"，看不出根因。
  c_bold "== 验证 arduino-cli 能看到该库 =="
  if arduino-cli lib list 2>/dev/null | grep -qi 'ESP32 BLE HID'; then
    c_green "  ✓ arduino-cli 已识别 ESP32 BLE HID"
  else
    c_red "  ✗ arduino-cli 看不到该库，编译会失败。"
    echo "    期望路径: $LIB_DST"
    echo "    实际 sketchbook: $SKETCHBOOK"
    echo "    可显式指定: arduino-cli config set directories.user <你的 sketchbook 路径>"
    exit 1
  fi
  c_green "✓ 库就绪"
}

check_env() {
  command -v arduino-cli >/dev/null 2>&1 || die "找不到 arduino-cli"
  if ! arduino-cli core list 2>/dev/null | grep -q 'esp32:esp32.*2\.0\.17'; then
    die "没装 esp32 core 2.0.17。执行：arduino-cli core install esp32:esp32@2.0.17
  注意：arduino-cli 同一时间只保留一个 core 版本，装 2.0.17 会顶掉已装的 3.x。"
  fi
  arduino-cli lib list 2>/dev/null | grep -q '^WebSockets' \
    || die "没装 WebSockets 库。执行：arduino-cli lib install WebSockets@2.7.2"
  # 查 arduino-cli 实际能看到的库，而不是查某个磁盘路径。
  arduino-cli lib list 2>/dev/null | grep -qi 'ESP32 BLE HID' \
    || die "arduino-cli 看不到 ESP32-BLE-HID 库。执行：$0 install-lib"
  [ -f "$SKETCH_DIR/config.h" ] \
    || die "没填配置。先：cd firmware/esp32c3_hid_agent && cp config.example.h config.h 然后填 WiFi/服务器/token"
}

build() {
  check_env
  c_bold "== 编译 =="
  c_bold "  fqbn : $FQBN"
  c_bold "  分区 : no_ota（2MB app）—— 默认方案只有 1.2MB 会装不下"
  echo
  mkdir -p "$BUILD_DIR"
  # 一次编译，同时抓体积行（不要为了拿 size 再编一遍，那要一分钟）
  LOG="$(mktemp)"
  if ! arduino-cli compile --fqbn "$FQBN" --output-dir "$BUILD_DIR" "$SKETCH_DIR" 2>&1 | tee "$LOG"; then
    rm -f "$LOG"; die "编译失败"
  fi
  APP="$BUILD_DIR/esp32c3_hid_agent.ino.bin"
  if [ ! -f "$APP" ]; then
    rm -f "$LOG"; die "产物不存在: $APP（编译没真正完成）"
  fi
  rm -f "$LOG"
  echo
  c_green "✓ 编译成功"
  grep -E 'Sketch uses|Global variables' "$LOG" 2>/dev/null || true
  ls -lh "$APP" | awk '{print "  app :", $5, $9}'
  ls -lh "$BUILD_DIR/esp32c3_hid_agent.ino.elf" 2>/dev/null | awk '{print "  elf :", $5, $9}' || true
}

make_partitions() {
  mkdir -p "$BUILD_DIR"
  cat > "$PART_CSV" <<'EOF'
# 2MB 单 app 分区（无 OTA）。比 arduino-cli 的 no_ota 表好用：
# 那张表叫 app0/ota_0，IDF bootloader 不认，只认 factory。
nvs,        data, nvs,     0x9000,  0x6000,
phy_init,   data, phy,     0xf000,  0x1000,
factory,    app,  factory, 0x10000, 0x200000,
EOF
  GEN="$(find "$HOME/esp" "$HOME/.espressif" /usr/local/share /opt -name gen_esp32part.py 2>/dev/null | head -1 || true)"
  if [ -n "$GEN" ]; then
    python3 "$GEN" "$PART_CSV" "$PART_BIN"
    c_green "✓ 分区表: $PART_BIN"
  else
    c_red "! 找不到 gen_esp32part.py（ESP-IDF 自带）。"
    echo "  装 ESP-IDF 后重试，或手动把 app.elf 用 ESP-IDF 烧。"
    return 1
  fi
}

find_idf_bootloader() {
  # IDF bootloader 在 C3 上验证可用；Arduino core 2.0.17 自带的那个会崩溃循环。
  for p in "$HOME/esp/esp-idf/components/bootloader/subproject/main/bootloader.bin" \
           "$HOME/.espressif/components/esp32c3/bootloader/bootloader.bin"; do
    [ -f "$p" ] && { echo "$p"; return 0; }
  done
  find "$HOME/esp" "$HOME/.espressif" -path "*esp32c3*bootloader.bin" 2>/dev/null | head -1
}

upload_arduino() {
  build
  c_bold "== 用 arduino-cli 烧录 =="
  c_red "⚠ 已知问题：core 2.0.17 自带 bootloader 在部分 C3 上会进 rst:0x3 崩溃循环。"
  echo "  如果烧完串口只见 rst:0x3 刷屏、零 app 输出，改用 upload-flash。"
  echo
  arduino-cli upload --fqbn "$FQBN" --input-dir "$BUILD_DIR" --port "${PORT:-/dev/cu.usbmodem1101}" "$SKETCH_DIR" \
    || die "烧录失败。用 arduino-cli board list 确认串口路径"
}

upload_flash() {
  build
  make_partitions || exit 1

  PORT="${PORT:-/dev/cu.usbmodem1101}"
  [ -e "$PORT" ] || { c_red "✗ 串口 $PORT 不存在。跑 arduino-cli board list 看看。"; exit 1; }

  BOOTLOADER="$(find_idf_bootloader)"
  if [ -z "$BOOTLOADER" ]; then
    c_red "✗ 找不到 ESP-IDF 的 bootloader.bin"
    echo "  装 ESP-IDF 后重试，或改用 'upload' 模式。"
    exit 1
  fi
  c_green "✓ bootloader: $BOOTLOADER"

  APP="$BUILD_DIR/esp32c3_hid_agent.ino.bin"
  c_bold "== esptool 手动烧录 =="
  c_bold "  port : $PORT"
  c_bold "  app  : $(basename "$APP") ($(wc -c < "$APP") bytes)"
  echo
  # --flash_mode dio: 这块板子的 flash 不跑 quad 模式，quad 会起不来
  esptool.py --chip esp32c3 --port "$PORT" --baud 460800 \
    write_flash --flash_mode dio --flash_size 4MB \
    0x0    "$BOOTLOADER" \
    0x8000 "$PART_BIN" \
    0x10000 "$APP"
  echo
  c_green "✓ 烧录完成。打开串口（115200）应该看到："
  echo "  [boot] esp32-c3-hid-agent"
  echo "  [ble] ESP32C3-HID up, screen WxH"
  echo "  [wifi] up: <ip>"
  echo "  [ws] ws://<server>:8765/ws?token=***"
  echo "  [ws] connected"
}

[ $# -eq 0 ] && usage

case "$1" in
  build)        build ;;
  upload)       upload_arduino ;;
  upload-flash) upload_flash ;;
  size)         build ;;
  install-lib)  install_lib ;;
  -h|--help)    usage ;;
  *) die "unknown command: $1  (try --help)" ;;
esac