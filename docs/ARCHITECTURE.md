# 架构与设计决策

## 三个组件

```
┌─────────────────┐
│  AI agent       │  hermes / openclaw / 任意脚本 / LLM tool call
│  (server 主机)   │
└────────┬────────┘
         │ POST /cmd  {"op":"tap","x":610,"y":1356}
         │ header: X-HID-Guard = sha256(token)
         ▼
┌──────────────────────────────────────────────────────────┐
│  server.py  (Python 3 标准库，零依赖)                    │
│                                                          │
│  ┌────────────┐  ┌──────────┐  ┌──────────────────┐    │
│  │ 命令策略   │→ │ 限流器   │→ │ WebSocket 服务    │    │
│  │ 白名单校验 │  │ 令牌桶   │  │ 手写 RFC6455      │    │
│  └────────────┘  └──────────┘  └────────┬─────────┘    │
│                                           │              │
│                              ┌────────────┴──────────┐   │
│                              │ 审计日志（append-only）│   │
│                              └───────────────────────┘   │
└──────────────────────────────┬───────────────────────────┘
                               │ ws://server:8765/ws?token=…
                               │ WiFi 2.4GHz
                               ▼
┌──────────────────────────────────────────────────────────┐
│  ESP32-C3                                                 │
│                                                          │
│  WebSocketsClient ──► handleCommand() ──► BleComboKeyboard │
│                            │                   BleComboMouse │
│                            │                          │      │
│                     命令解析+校验                  BLE 复合 HID│
└────────────────────────────────────────────────────┬─────┘
                                                     │ BLE 5.0
                                                     ▼
                                              ┌─────────────┐
                                              │ Android 手机 │
                                              └─────────────┘
```

## 为什么这么设计

### server 用标准库手写 WebSocket

`websockets` / `aiohttp` 之类的库需要 pip install。这个 server 的目标部署位置是
**树莓派、软路由、NAS、闲置笔记本** —— 这些设备上：

- 可能没有 pip，或者 pip 装包要编译
- OpenWrt / iStoreOS 这类系统 python 环境是只读的
- 依赖越少，能跑的地方越多

RFC6455 服务端只需要三件事：`Sec-WebSocket-Accept` 的 SHA1+base64、帧头解析、
帧掩码解码。200 行足够，没有流控需求也没有背压问题。**这是有意识的取舍，
不是偷懒** —— 如果要做多客户端广播或压测，就应该上真正的库。

### 板子主动连服务器（而不是反向）

服务端主动连板子不可行：板子在局域网里，没有固定地址也没有公网入口。
反过来板子有 WiFi，能主动发起 TCP 连接，而且**只需要出站方向**的防火墙规则。

代价是服务端必须实现 WebSocket 服务端而不是客户端。收益是：
只需要开一个入站端口（8765），不需要 UPnP 打洞、不需要 VPN。

### 命令用 JSON 而不是二进制

JSON 让 mock_board.py 能用几十行 Python 完整实现协议，agent 侧也能用 curl 调试。
带宽不是问题 —— 一条命令几十字节，BLE 键盘本身比这慢几个数量级。

### 服务端信任边界窄于固件

固件自己会再校验一遍：

| 检查 | 服务端 | 固件 |
|---|:---:|:---:|
| 只认白名单 op | ✅ | ✅（未知 op 回 error） |
| 文本仅 ASCII | ✅ | ✅（逐字符检查 0x20-0x7E） |
| 长度 ≤200 | ✅ | ✅ |
| 坐标范围 | ✅ | ✅（按真实屏幕尺寸 clamp） |
| BOOT 急停 | ❌（服务端做不到） | ✅ |

**服务端被攻破能拿到什么？** 最多让固件发它本来就会发的那些东西。
它没法让固件发服务端没要求过的键，因为固件只认那 7 个 op。

### 固件为什么锁 Arduino core 2.x

core 3.x 重构了 BLE 相关 API，`BleKeyboard` / `BleMouse` 这套写法不存在了。
现有的组合 HID 库（`ESP32-BLE-HID`）适配的是 2.x。

core 2.0.17 的 bootloader 在部分 ESP32-C3 上有兼容问题（崩溃循环）。
绕过办法是烧 ESP-IDF 的 bootloader + 自定义分区表，见 README「烧录踩坑警告」。

**这不是最优解，只是当前能跑通的解。** 长期方向是把 HID 层迁到纯 ESP-IDF
（`BLEServer` + HID service），那样 bootloader、分区表、工具链全统一，
core 升级也不用重写。要做的话工作量约 800 行，见固件注释里指向的迁移说明。

## 分区表的那个坑

Arduino 的 `no_ota` 方案给 2MB app 空间，但它的分区名叫 **`app0`（subtype `ota_0`）**：

```
nvs,      data, nvs,     0x9000,   20K
otadata,  data, ota,     0xe000,   8K
app0,     app,  ota_0,   0x10000,  2M      ← 名字不是 factory
spiffs,   data, spiffs,  0x210000, 1920K
coredump, data, coredump,0x3f0000, 64K
```

而 ESP-IDF 的 bootloader **只认 `factory`**。用 arduino-cli 直接烧会烧出一个
bootloader 认为「没有 app」的状态 —— 表现就是循环 reset。

所以 `build_firmware.sh upload-flash` 自己生成一张表：

```
nvs,      data, nvs,     0x9000,  0x6000,
phy_init, data, phy,     0xf000,  0x1000,
factory,  app,  factory, 0x10000, 0x200000,
```

然后用 esptool 分三段烧：IDF bootloader @ 0x0、自定义分区表 @ 0x8000、app @ 0x10000。

## 协议

### WebSocket 帧

标准 RFC6455。客户端（板子）发的帧**必须掩码**（规范要求），服务端发的**不掩码**。

### 握手

```
GET /ws?token=<32字符token> HTTP/1.1
Upgrade: websocket
Connection: Upgrade
Sec-WebSocket-Key: <base64 16字节>
Sec-WebSocket-Version: 13
```

token 用 `hmac.compare_digest` 常量时间比较（防时序侧信道）。失败回 401 并记 `ws_rejected`。

### 板子 → 服务端

```json
{"ev":"hello","fw":"c3-1.0","ble":true}
{"id":"7","ev":"done","ok":true}
{"id":"8","ev":"done","ok":false,"msg":"non-ascii"}
```

> **实现陷阱**：hello 那条曾经多发 1 字节（按 40 写、实际 39 字符），尾部的 NUL
> 让服务端 `json.loads` 抛异常，于是 `board_hello` 永远进不了审计日志 ——
> 服务端看起来一切正常，只是审计缺了一条。固件里现在用 `strlen()` 而非手数长度。

### 服务端 → 板子

```json
{"id":"7","op":"type","text":"hello"}
{"id":"8","op":"key","key":"ENTER"}
{"id":"9","op":"tap","x":610,"y":1356,"ms":70}
{"id":"10","op":"swipe","x1":610,"y1":2000,"x2":610,"y2":800,"ms":300}
{"id":"11","op":"scroll","dy":-3}
{"id":"12","op":"combo","keys":["CTRL","A"]}
{"id":"13","op":"ping"}
```

`id` 由服务端生成（自增数字的字符串），板子在回复里原样带回，用于配对请求与回执。

### 为什么协议里没有时间戳

早期设计有个「命令 10 秒过期」的 TTL 检查，实现后发现它**永远不触发**：
`onMessage` 先给 `lastMsgMs` 赋值再检查时间差，差值恒为 0。

要正确实现需要协议携带时间戳，但那要求板子有可信时钟 —— ESP32-C3 断网重启后
RTC 是零。一条会骗人的检查比没有检查更糟，所以删掉了。

过期控制改由两处负责：**服务端令牌桶限流**（3 条/秒，跑不出洪泛）和
**审计日志**（事后能查是谁在什么时候发了什么）。加上固件的物理急停。

## HID 报告细节

### 复合设备

`BleComboKeyboard` + `BleComboMouse` 共用一个 BLE 设备，手机看到的是**一个**
HID 键盘鼠标复合设备，不是两个。所以：

- 不需要手机蓝牙设置里配对两次
- 切换输入目标时不会掉线
- 占用一个 HID 设备槽位

### 绝对坐标点按

Android 上的鼠标是相对坐标，没有光标查询接口（这是 Android 和桌面栈的根本差异，
Wayland 也是同样的死路）。但 **digitizer**（触摸屏）是绝对坐标的：

```
bleMouse.sendAbsolutePixel(x, y)                    // 按下
bleMouse.sendAbsolutePixel(x, y, false, false)      // 抬起
```

`setScreenSize(SCREEN_W, SCREEN_H)` 让库把像素映射到 digitizer 的逻辑坐标范围。
**这个映射必须准**，所以 `config.h` 里的屏幕分辨率必须和手机实际一致 ——
填错会导致点到的位置有偏移。

### 为什么组合键要支持键名

服务端允许 `["CTRL","ENTER"]` 这种组合（修饰键 + 命名键），不只是 `["CTRL","A"]`
（修饰键 + 单字符）。早期版本只支持后者，于是 `Ctrl+Enter`（在很多 App 里是
"打开/确认"）用不了。

### 媒体键是 2 字节 consumer report

`VOLUME_UP` 不是 keycode，是 consumer usage，走独立的 report。所以实现是
`bleKeyboard.press(uint8_t[2])` 而不是 `press(code)`。

### 明确不映射的键

`APP_SWITCH / MENU / CAMERA / POWER / WAKEUP / SLEEP` —— BLE HID 键盘 usage 表里
没有可靠定义。早期版本硬映射的结果是：

| 名字 | 早期错误映射 | 实际打出的键 |
|---|---|---|
| `POWER` | `0x30` | `)` |
| `WAKEUP` | `0x83` | Meta (Win) |
| `SLEEP` | `0x84` | 右 Ctrl |

比报错更糟 —— 用户以为自己按了电源键，实际上打了三个普通键。
现在服务端直接返回带原因的 400。

## BLE + WiFi 共存

ESP32-C3 的射频是共享的。同时开 WiFi 和 BLE 会周期性争抢，芯片里有固化的共存
仲裁（`coex firmware`，本板读到 version `831ec70`）。

实测表现：

- WiFi 持续连接时 BLE 仍能维持（扫描周期被拉长，但不丢包）
- BLE 大流量（连续 typing）时 WiFi 有轻微延迟
- 对本项目无影响：命令量极小，且服务端 3 秒重连可自愈

固件启动顺序刻意把 `setupHid()` 放在 WiFi 之前：**已配对的手机几秒内自己就重连了**，
而 `WiFi.begin()` 最多要等 15 秒。反过来会让手机每次开机多等十几秒。

## 已知限制

| 限制 | 原因 | 绕法 |
|---|---|---|
| BLE HID 打不出中文 | 硬件规范：键盘报告键位，不报告字符 | ADB `input text` 或剪贴板 |
| 没有长按（press-and-hold） | 协议没有 key_down/key_up op | 需要自己加 op（见 `agent_bridge.py` 的 `hold()`） |
| tap 坐标依赖配置正确 | digitizer 缩放由 `setScreenSize` 决定 | 填对 `SCREEN_W/H`，用 ADB 截图核对 |
| 一次只能连一台手机 | BLE 外设同一时刻只有一个主机 | 手机先释放再配下一台 |
| 无 HTTPS / 无 TLS | 局域网内明文 | token 认证 + 白名单；跨网段请走 SSH 隧道 |
| 固件锁 core 2.x | 现有 HID 库只适配 2.x | 长期迁到纯 ESP-IDF（见上） |