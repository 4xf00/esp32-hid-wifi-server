/*
 * config.h — 用戶配置。复制 config.example.h 为 config.h 后填写。
 *
 *   cp config.example.h config.h
 *
 * config.h 已在 .gitignore 中，不会被提交。请勿把真实密码/token 提交到仓库。
 */

#ifndef CONFIG_H
#define CONFIG_H

// ---------------------------------------------------------------- WiFi -----
// ESP32-C3 只支持 2.4GHz（802.11b/g/n），不支持 5GHz。
//
// SSID 大小写敏感：802.11 SSID 是字节串，空口实播什么就得填什么。填错时
// 服务端会看到 disconnect reason 201 (NO_AP_FOUND)。若你不确定，用串口命令
// `wifi scan` 扫一遍看实际拼写（见 README「排查」一节）。
static const char *WIFI_SSID     = "YOUR_WIFI_SSID";
static const char *WIFI_PASSWORD = "YOUR_WIFI_PASSWORD";

// ------------------------------------------------------------- 服务器 -----
// 跑 server.py 的那台机器（树莓派 / 软路由 / NAS / 家里的 Linux 主机）。
//
// 可以填 IP，也可以填主机名（例如树莓派开 mDNS 后是 "esp32-bridge.local"）。
// 填主机名需要路由器支持 mDNS，或在 /etc/hosts 里做静态解析。
//
// 填 0.0.0.0 / 空则不做静态 IP，DHCP 拿到的地址即为板子的地址。
// 若你的路由器给板子分配的 IP 不稳定（如租期短或按 MAC 随机化），
// 建议在路由器里给板子做 DHCP 静态绑定，或用主机名代替 IP。
static const char *SERVER_HOST = "192.168.1.100";
static const int   SERVER_PORT = 8765;

// token 从 server.py 的 token 文件读，或跑 scripts/gen_token.sh 生成。
// 粘贴时不要带引号、不要带空格、不要带尾部换行。
static const char *SERVER_TOKEN = "PASTE_TOKEN_HERE";

// ------------------------------------------------------------ 手机屏幕 -----
// 绝对坐标 tap/swipe 用。查你的手机分辨率：
//   adb shell wm size        → Physical size: 1220x2712
//   adb shell wm size        → Override size: 1080x2400   （有的话优先用这个）
//
// 这两个值同时会被 bleMouse.setScreenSize() 用来把像素映射到 HID digitizer
// 的逻辑坐标范围，所以填错会导致点不准。
static const uint16_t SCREEN_W = 1080;
static const uint16_t SCREEN_H = 2400;

// ------------------------------------------------------------- BLE 配对 ---
// BLE 设备名（手机蓝牙列表里看到的名字）
//
// 配对说明：ESP32-BLE-HID 库没有提供配对码 API，走标准 BLE HID
// JustWorks 配对 —— 手机蓝牙设置里点一下「ESP32C3-HID」即可，不需要 PIN。
// 配对信息存在手机那头，板子不用记。
static const char *BLE_DEVICE_NAME = "ESP32C3-HID";

// ------------------------------------------------------------ 安全开关 ---
// BOOT 按钮（GPIO0）作为物理急停：按住即永久停止所有 HID 输出，直到重启。
// 如果你的开发板没有 BOOT 按钮，或你想用别的引脚，改这里。
// 见 README「安全模型」一节 —— 这是整个系统唯一的物理断路。
static const uint8_t KILL_PIN        = 0;      // 大多数 C3 devkit 的 BOOT = GPIO0
static const bool    KILL_PIN_IS_BOOT = true;  // BOOT 是低电平有效

#endif // CONFIG_H