# esp32-hid-wifi-server

**Give an AI agent hands on your Android phone — over WiFi, over Bluetooth, with no USB cable and no root.**

一台 ESP32-C3（约 ¥15）插在手机旁边当"蓝牙键盘鼠标"，你的 AI agent 跑在服务器上，
通过 WiFi 下发指令。手机把它当成**真正的蓝牙 HID 设备** —— 输入走的是和物理键盘
完全相同的驱动路径，不是注入的虚拟事件。

```
  ┌──────────────┐   HTTP/WS    ┌──────────┐  WiFi   ┌─────────┐  BLE HID  ┌───────┐
  │  AI agent    │ ───────────▶ │ ESP32-C3 │ ◀─────▶ │  server │ ────────▶ │ phone │
  │ hermes /     │              │          │  2.4G   │  Python │  (你家的) │Android│
  │ openclaw /   │              │  ~¥15    │         │ stdlib  │           │       │
  │ 任何脚本      │              │          │         │ no deps │           │       │
  └──────────────┘              └──────────┘         └─────────┘           └───────┘
                                     ▲
                              不需要 USB 连手机
```

**手机上需要开无线 ADB**（补齐 BLE HID 打不了的两件事：中文输入、截图/读界面）。
两条通道互补，见下面的[能力对照表](#能力对照表)。

---

## 目录

- [为什么需要它](#为什么需要它)
- [能力对照表](#能力对照表)
- [硬件清单](#硬件清单)
- [5 分钟快速开始](#5-分钟快速开始)
- [无线 ADB 详细步骤](#无线-adb-详细步骤)
- [接进你的 agent](#接进你的-agent)
- [安全模型](#安全模型)
- [完整命令参考](#完整命令参考)
- [故障排查](#故障排查)
- [项目结构](#项目结构)
- [已验证 / 未验证](#已验证--未验证)
- [License](#license)

---

## 为什么需要它

**1. ADB 的输入注入在很多地方进不去。**

`adb shell input` 走的是 Android 的虚拟事件注入通道，系统安全层能识别。银行 App 的
安全键盘、DRM 播放器的防录屏界面、需要"证明有物理按键"的登录流程 —— 这些地方
ADB 点不动，但物理蓝牙键盘可以，因为对系统来说那就是一把键盘。

反过来，BLE HID 有它做不到的事（中文、截图、读界面树）。所以这套方案是**两个通道
一起用**，不是二选一。

**2. 服务器上的 agent 不该拖着一条 USB 线。**

常见的 agent 手机控制方案要么要求 USB 常连（手机插在服务器旁边的机柜里），要么纯
靠 ADB（能力受限）。这个方案里：

- ESP32 只需要 WiFi 和 BLE，天生适合放在手机旁边
- agent 在任何能访问服务器的地方都能下发指令
- 手机不用插线，扔床头、扔桌上都行

**3. 安全边界是设计出来的，不是事后补的。**

服务端没有"发送任意 keycode"的代码路径，只有 7 个固定 op；文本只允许 ASCII 白名单
字符；令牌桶限流防止 agent 失控刷屏；每条命令落审计日志；板子上还有一个**物理急停
按钮** —— 服务端最多只能要求固件少发，永远不能让固件多发。细节见[安全模型](#安全模型)。

---

## 能力对照表

| 想做的事 | BLE HID（ESP32） | 无线 ADB | 结论 |
|---|:---:|:---:|---|
| 打字（ASCII） | ✅ | ✅ | 都行，HID 更像真人 |
| 打中文 / emoji | ❌ | ✅ | **必须走 ADB** |
| 组合键 `Ctrl+A` / `Ctrl+Shift+Z` | ✅ | ⚠️ | 用 HID 更可靠 |
| 音量 / 静音 / 电源媒体键 | ✅ | ⚠️ | 用 HID |
| 方向键、Tab、Enter、Esc | ✅ | ⚠️ | 用 HID |
| 长按（按住不松） | ✅ | ❌ | **只能走 HID** |
| 绝对坐标点按 `tap(x,y)` | ✅ | ✅ | 都行 |
| 滑动 / 滚轮 | ✅ | ⚠️ | 用 HID，手势更自然 |
| 进入安全键盘 / 密码框 | ✅ | ❌ | **HID 的核心价值** |
| 截图 | ❌ | ✅ | **必须走 ADB** |
| 读 UI 层级树（找按钮） | ❌ | ✅ | **必须走 ADB** |
| 需要 root / 安装 app | ❌ | ❌ | 都做不到 |

> **BLE HID 打不出中文的根本原因**：HID 键盘报告的是**键位位置**，不是字符。
> 中文字符没有对应的物理键，也就没有 keycode。这不是实现缺陷，是硬件规范决定的。
> 原项目 README 没说清这点，是最常见的困惑来源。

**为什么「长按」只有 HID 能做**：ADB 没有"按住某键 3 秒"这种语义，只能用
`input keyevent` 敲一下松开。IME 上屏、滑动条拖动、下拉刷新这些都依赖按住。

---

## 硬件清单

| 东西 | 要求 | 备注 |
|---|---|---|
| ESP32-C3 devkit | 必须带 WiFi + BLE | ESP32-C3 / C3 super mini 都行 |
| 一根数据线 | 仅首次烧录 | 烧完可以拔掉 |
| Android 手机 | Android 8.0+ | 支持 BLE HID |
| 服务器 | Linux，Python 3.8+ | 树莓派 / 软路由 / NAS / 旧笔记本都行 |

**不用买**：USB 延长线、转接器、ADB 授权线 —— 手机全程不插线。

⚠️ **ESP32-C3 只支持 2.4GHz WiFi**。5GHz 频段的路由器必须把 2.4G 单独开出来，
且 SSID 不能是纯中文（有些路由器会隐藏中文 SSID 导致连不上）。

---

## 5 分钟快速开始

### 步骤 0：装工具

```bash
# 服务器端：只需要 Python 3.8+，无第三方依赖
python3 --version

# 烧录固件用（一次性）：
#   arduino-cli   https://arduino.ccc.cc/download
#   esptool.py   pip install esptool
```

### 步骤 1：起服务器

```bash
git clone https://github.com/4xf00/esp32-hid-wifi-server.git
cd esp32-hid-wifi-server/server

# 直接跑。--bind 不填会自动探测本机 LAN 地址，并拒绝绑 0.0.0.0
python3 server.py
```

输出会告诉你 token 和板子该连哪里：

```
esp32-hid-bridge listening on http://192.168.1.100:8765
  token file : /root/.config/esp32-hid-bridge/token
  audit log  : /root/.local/state/esp32-hid-bridge/audit.jsonl
  POST /cmd trusted peers: ['127.0.0.1', '::1']
  ESP32 should connect to: ws://192.168.1.100:8765/ws?token=<32字符>
```

首次启动会自动生成 token（24 随机字节，权限 0600）。**把这个值填进固件。**

要开机自启见 [`docs/esp32-hid-bridge.service`](docs/esp32-hid-bridge.service)。

⚠️ **防火墙**：服务器要放行 8765 端口的**入站**，且只放行你自己的网段：

```bash
# nftables（OpenWrt / 新 Linux）
sudo nft add rule inet filter input iifname "wlan*" ip saddr 192.168.1.0/24 tcp dport 8765 accept

# iptables（老 Linux）
sudo iptables -A INPUT -i wlan0 -s 192.168.1.0/24 -p tcp --dport 8765 -j ACCEPT
```

### 步骤 2：烧固件

```bash
cd esp32-hid-wifi-server

# 装依赖库（一次性：clone 到 arduino-cli 认的 sketchbook 目录并打补丁）
./scripts/build_firmware.sh install-lib
arduino-cli core install esp32:esp32@2.0.17
arduino-cli lib install WebSockets@2.7.2

# 填配置
cd firmware/esp32c3_hid_agent
cp config.example.h config.h
$EDITOR config.h        # 填 WiFi / 服务器地址 / token / 屏幕分辨率
cd ../..

# 编译 + 烧录
./scripts/build_firmware.sh upload-flash
```

> **`install-lib` 必须用这个脚本，不能手动 clone。** ESP32-BLE-HID 不在 Arduino
> Library Manager 里；而 Arduino IDE 的 `sketch/lib/` 约定 **arduino-cli 不认** ——
> 手动 clone 到那里编译会报 `BleCombo.h: No such file or directory`。脚本会装到
> `arduino-cli config get directories.user` 指向的 `libraries/` 下，并当场验证
> arduino-cli 确实能看到它。

`config.h` 里要填的：

| 字段 | 怎么填 |
|---|---|
| `WIFI_SSID` / `WIFI_PASSWORD` | 2.4GHz 那个。**大小写必须和路由器的完全一致** |
| `SERVER_HOST` | 跑 server.py 那台的 IP，或主机名 |
| `SERVER_TOKEN` | 步骤 1 输出里那串 32 字符 |
| `SCREEN_W` / `SCREEN_H` | 手机分辨率，见步骤 3 |

**分区必须用 `no_ota`**：固件 1.54MB，Arduino 默认分区表只有 1.2MB app 空间，
会报 `117%` 装不下。脚本已经帮你选好了。

> **烧录踩坑警告**：core 2.0.17 自带的 bootloader 在部分 ESP32-C3 上会进
> `rst:0x3` 崩溃循环（串口一直刷 reset、零 app 输出）。`upload-flash` 命令
> 用 **ESP-IDF 的 bootloader + 自定义 2MB 分区表 + esptool 手动烧**，
> 绕开这个问题。如果 `upload` 之后板子进死循环，改用 `upload-flash`。

### 步骤 3：配对手机 + 开无线 ADB

```bash
./scripts/setup_wireless_adb.sh
```

脚本会引导你走完 mDNS 发现 → `adb pair`（输入 6 位配对码）→ `adb connect`，
最后打印手机分辨率，**把它填回 `config.h` 的 `SCREEN_W` / `SCREEN_H`**。
细节和图文步骤见[下一节](#无线-adb-详细步骤)。

### 步骤 4：验证

板子烧完插着 USB 也能看串口（115200）：

```
[boot] esp32-c3-hid-agent
[ble] ESP32C3-HID up, screen 1220x2712
[wifi] up: 192.168.1.77
[ws] ws://192.168.1.100:8765/ws?token=***
[ws] connected          ← 板子接上服务器了
[stat] wifi=up ip=192.168.1.77 ws=up killed=0 idle=0ms
```

然后在**服务器本机**下发命令：

```bash
cd server
export BRIDGE_URL=http://192.168.1.100:8765
python3 bridge_client.py status        # → {"board": {"peer": "...", "fw": "c3-1.0", ...}}
python3 bridge_client.py key VOLUME_UP  # 手机音量应该变了
```

> **为什么必须 `cd server`**：`server.py` 的 POST /cmd 默认只信任 loopback
> 和它自己的 bind 地址。从别的机器 curl 会拿到 `403 {"error":"loopback only"}`。
> 这是故意的 —— agent 和 server 同机最简单；异地下发走 SSH 隧道，见
> [接进你的 agent](#接进你的-agent)。

---

## 无线 ADB 详细步骤

**为什么需要它**：BLE HID 只有键盘 + 鼠标。中文输入、截图、读界面树都得靠 ADB。
开完无线 ADB 后手机全程不插线，服务器就能读手机屏幕 —— 这是 agent 判断
"刚才那一下到底生效没有"的唯一手段。

### Android 11 及以上（无线调试）

1. **设置 → 关于手机** → 连点 7 次「版本号」打开开发者选项
2. **设置 → 开发者选项 → 无线调试** → 打开
3. 点「使用配对码配对设备」→ 记下 6 位配对码，**别关这个窗口**（5 分钟过期）
4. 跑脚本：

```bash
./scripts/setup_wireless_adb.sh
# 输 IP 地址和端口 → 输入配对码 → 再输连接端口
```

5. 验证：

```bash
adb devices                              # 看到 device 就成了
adb shell input text 'hello'             # 屏幕上应该出现 hello
```

> ⚠️ **配对端口和连接端口是两个不同的数字**。这是 90% 卡住的人：配对窗口里的端口
> 和主页面上的端口不一样，两个都要输。脚本会分别问。

也可以先看看自动发现了什么：

```bash
./scripts/setup_wireless_adb.sh --list
# List of discovered mdns services
# 192.168.1.50:41337   _adb-tls-connect._tcp
```

看不到就是 mDNS 没通：手机和服务器必须在同一网段（不能一个 2.4G 一个 5G 连不同
子网），或者路由器不支持 mDNS 穿透 —— 这时手动输 IP 就行。

### Android 10 及以下

需要**先 USB 连一次**（只这一次，之后线可以拔）：

```bash
adb devices                              # 确认手机已授权
adb tcpip 5555                           # 手机上「USB 调试」要开着
# 拔掉 USB 线
adb connect 192.168.1.50:5555
```

端口固定 5555，服务器防火墙要放行。

### 查屏幕分辨率（填 config.h 用）

```bash
adb shell wm size
# Physical size: 1220x2712
```

有 `Override size` 就用 Override 那个 —— 那是实际生效的分辨率。

### 让手机别中途熄屏

agent 跑多步流程时手机锁屏会让后面全部失败：

```bash
adb shell settings put system screen_off_timeout 1800000    # 30 分钟
```

（`setup_wireless_adb.sh` 会问你要不要顺手改掉）

### 无线 ADB 的寿命

配对信息在**手机**里，不在电脑里。以下情况要重新 `adb pair`：

- 手机重启
- 换了 WiFi
- 系统回收了无线调试（省电策略）
- 长期不用

固件那侧不受影响 —— BLE 配对和 WiFi 连接都会自己重连。

---

## 接进你的 agent

### 方式一：Python（最简单）

```python
from bridge_client import Bridge

b = Bridge()                                  # 读环境变量/配置
b.type_text("hello")
b.key("ENTER")
b.tap(610, 1356)                              # 绝对坐标
b.combo("CTRL", "A")
b.scroll(-3)
print(b.status())                             # {'board': {'peer': ..., 'fw': 'c3-1.0'}}
```

配置优先级：构造函数参数 > 环境变量 > `~/.config/esp32-hid-bridge/client.conf`

```bash
export BRIDGE_URL=http://192.168.1.100:8765
export BRIDGE_TOKEN=...          # 或 BRIDGE_TOKEN_FILE=/path
```

### 方式二：CLI（shell / hook 用）

```bash
cd server
python3 bridge_client.py key VOLUME_UP
python3 bridge_client.py type "hello"
python3 bridge_client.py tap 610 1356
python3 bridge_client.py status
```

成功返回 `{"ok": true, "id": "12", "op": "key"}`，失败退出码非 0 且 stderr 有原因。

### 方式三：HTTP（任何语言）

```bash
GUARD=$(printf %s "$BRIDGE_TOKEN" | sha256sum | cut -d' ' -f1)
curl -X POST http://192.168.1.100:8765/cmd \
     -H "Content-Type: application/json" \
     -H "X-HID-Guard: $GUARD" \
     -d '{"op":"tap","x":610,"y":1356}'
```

### 方式四：agent 的纪律（重要）

**只看到「命令已接受」不等于「手机执行了」。** 服务端返回 `{"ok":true}` 只表示
JSON 校验通过并写进了 WebSocket —— 手机屏幕上发生了什么，没有任何反馈通道。

所以 agent 应该**发完就验证**：

```python
b.tap_element_or(x=610, y=1356)    # HID 点按
shot = adb_screenshot()            # 无线 ADB 截图
assert "已保存" in ocr(shot)       # 确认结果
```

配套的 ADB 侧工具（截图 / `uiautomator dump` / 绝对点按）建议自己包一层，
或者参考 [`examples/agent_bridge.py`](examples/agent_bridge.py) —— 里面把
HID 和 ADB 组合成了一个「点按钮并验证」的完整动作。

### 异地下发

服务端默认只信 loopback。远程 agent 要下发，两个办法：

```bash
# A. SSH 隧道（最简单，不用改服务端配置）
ssh -L 8765:127.0.0.1:8765 user@server
export BRIDGE_URL=http://127.0.0.1:8765
```

```bash
# B. 显式开 --listen-lan
python3 server.py --listen-lan
```

> `--listen-lan` 信任的是 **`--bind` 那一个地址**，不是整个网段。所以开它
> 并不会让 LAN 上任意机器都能提交命令。但它确实比 loopback 宽，B 方案要清楚代价。

---

## 安全模型

**核心原则：服务端能要求的，永远少于固件愿意发的。**

| 层 | 限制 |
|---|---|
| 传输 | 只绑一个 LAN 地址，**代码里拒绝 `0.0.0.0`** |
| 认证 | WebSocket 升级要 token，`hmac.compare_digest` 常量时间比较 |
| 提交 | POST /cmd 默认只信 loopback；且必须带 `X-HID-Guard: sha256(token)` |
| 命令 | 只有 7 个固定 op。**没有"发送任意 keycode"的代码路径** |
| 文本 | 仅 ASCII 可打印白名单字符，≤200 字符 |
| 坐标 | 范围校验（0..4000），且固件侧按真实屏幕尺寸二次 clamp |
| 组合键 | 必须含至少一个修饰键，长度 ≤2 |
| 限流 | 令牌桶：突发 20 条，持续 3 条/秒 |
| 审计 | 每条接受的命令 + 每个拒绝 + 每次限流 + 认证失败，全部落 append-only 日志 |
| **物理急停** | **按住板子的 BOOT 按钮 → HID 输出立即永久停止，直到重启** |

急停那一条是重点：不管服务端被攻破成什么样、agent 发什么循环，**人手里那根手指
按下按钮就能掐断整条链路**。`swipe` 手势是阻塞的，最坏情况下要在手势结束（≤5 秒）后才
响应按钮。

### 部署建议

1. token 用 `scripts/gen_token.sh` 生成，别手搓
2. token 等同于「谁能操作你手机键盘」的钥匙 —— 别提交 git，别贴聊天窗口
3. 防火墙只放行自己的网段
4. 定期看审计日志：`jq -r '.detail' ~/.local/state/esp32-hid-bridge/audit.jsonl`
5. 固件里的 `config.h` 已在 `.gitignore` 中

---

## 完整命令参考

| op | 参数 | 说明 |
|---|---|---|
| `ping` | — | 链路自检，不产生输入 |
| `type` | `text` ≤200 | ASCII 可打印字符 |
| `key` | `key` | 见下方键名表 |
| `combo` | `keys[mod, key]` | 必须含 CTRL/ALT/SHIFT |
| `tap` | `x` `y` `ms`(20-1000) | 绝对坐标，HID digitizer |
| `swipe` | `x1 y1 x2 y2` `ms`(50-5000) | 线性滑动 |
| `scroll` | `dy`(-20..20) | 滚轮，负数向下 |

**支持的键名**：

```
ENTER  BACK  BACKSPACE  BACKSPACE_DEL  DEL  TAB  TAB_FORWARD  ESC  SPACE
UP  DOWN  LEFT  RIGHT  HOME  PAGE_UP  PAGE_DOWN
VOLUME_UP  VOLUME_DOWN  MUTE
```

**明确不支持**（BLE HID 键盘 usage 表里没有，硬映射会打到别的键上）：

```
APP_SWITCH  MENU  CAMERA  POWER  WAKEUP  SLEEP
```

这些名字服务端会返回带原因的 400，而不是静默打个别的键。

---

## 故障排查

### 板子一直 `rst:0x3` 循环 / 串口只刷 reset

**bootloader 问题，不是固件问题。** core 2.0.17 自带 bootloader 在部分 C3 上不兼容。

```bash
./scripts/build_firmware.sh upload-flash    # 用 IDF bootloader 手动烧
```

### WiFi 连不上

**先查大小写。** 802.11 SSID 是字节串，`Xiaomi_Home` 和 `xiaomi_home` 是两个不同的网络。
看串口日志里的提示，去路由器管理页抄准确的 SSID。

其他原因：
- 路由器只开了 5GHz → C3 不支持，必须开 2.4G
- SSID 含中文且路由器隐藏了它 → 改成纯英文
- WiFi 密码错 → 日志会显示连不上

### `[ws] closed, retrying...` 反复出现

token 不对，或者服务器防火墙没放行。

```bash
# 服务器本机验一下
curl -s http://127.0.0.1:8765/health
nc -vz <server-ip> 8765          # 从板子那个网段测
```

常见原因：token 复制时带了空格/引号/尾部换行。

### 手机蓝牙里看不到板子

- 固件跑到 `[ble] starting` 之后了吗
- 手机蓝牙设置里搜 `ESP32C3-HID`
- 配对过但手机忘了 → 手机「已配对的设备」里删掉，重新配

### `403 {"error": "loopback only"}`

预期行为，不是 bug。POST /cmd 只信 loopback 和 server 自己的 bind 地址。
在服务器本机执行，或走 SSH 隧道。

### `characters not typeable over BLE HID`

中文/emoji。要么改用 ADB `input text`，要么走剪贴板粘贴。

### tap 点不准

`SCREEN_W` / `SCREEN_H` 和手机实际分辨率不符。`adb shell wm size` 重查，
注意 `Override size` 优先。改完要**重新编译烧录**。

### 板子掉线

- WiFi 信号：看串口 `-stat` 行的 rssi（弱于 -75dBm 就不稳）
- 服务器重启 → 板子 3 秒自动重连，不用管
- 手机离开蓝牙范围 → 进范围自己重连

---

## 项目结构

```
esp32-hid-wifi-server/
├── firmware/esp32c3_hid_agent/
│   ├── esp32c3_hid_agent.ino     # 固件主体
│   └── config.example.h          # 配置模板（复制成 config.h）
│
├── server/
│   ├── server.py                 # WebSocket 服务端 + 命令策略 + 审计
│   ├── bridge_client.py          # Python 客户端封装
│   └── mock_board.py             # 模拟板子，无硬件也能测全链路
│
├── scripts/
│   ├── setup_wireless_adb.sh     # 无线 ADB 一键配对
│   ├── gen_token.sh              # 生成 token
│   ├── build_firmware.sh         # 装库 / 编译 / 烧录
│   └── run_tests.sh              # 不需要硬件的自测（CI 跑的就是它）
│
├── examples/
│   ├── agent_bridge.py           # HID + ADB 组合的 agent 示例
│   └── mock_agent.py             # 不用硬件跑通全链路
│
└── docs/
    ├── esp32-hid-bridge.service  # systemd unit
    ├── ARCHITECTURE.md           # 协议与设计决策
    └── SECURITY.md               # 威胁模型
```

### 不用硬件先跑一遍

```bash
# 终端 1：起 server
cd server && python3 server.py --port 8765

# 终端 2：起 mock board
python3 mock_board.py 127.0.0.1 8765 "$(cat ~/.config/esp32-hid-bridge/token)"

# 终端 3：发命令
python3 bridge_client.py type "hello"
```

mock board 会打印收到的每条命令。全链路（握手、认证、限流、审计）都能验证。

---

## 已验证 / 未验证

这一节是刻意写的。开源项目最容易被高估的地方就是"看起来能用"。

### ✅ 已在真实硬件上验证

- 固件用 `arduino-cli 2.0.17 + WebSockets 2.7.2 + ESP32-BLE-HID` 编译通过（1,542,380 字节 / 2MB，73%）
- ESP32-C3 实际连上 2.4GHz WiFi，串口打印 `[wifi] up: <ip>`
- 板子实际完成 WebSocket 握手，服务端 `/status` 显示 `fw: c3-1.0, ble: true`
- 板子 → WiFi → WebSocket → 服务端 → 命令下发，7 个 op 全部返回 `{"ok":true}`
- 服务端安全边界（真机 + mock 双跑）：
  - 中文 `type` → 400
  - 非法键名 `rm -rf` → 400
  - 越界坐标 `tap 99999 0` → 400
  - 无 guard header → 403；错 token 的 WS 升级 → 401
  - 30 连发 → 20 条 200 + 10 条 429（正好是设计的突发上限）
  - 审计日志完整记录 `board_attached` / `board_hello` / `cmd` / `cmd_rejected` / `cmd_throttled` / `ws_rejected` / `post_rejected`
- Arduino core 2.0.17 bootloader 在本机的崩溃循环问题已定位并给出绕过配方（IDF bootloader + 自定义 2MB 分区表）

### ❌ 尚未在真实手机屏幕上验证

- **BLE HID 报告在实际手机上的按键视觉效果**（音量条是否变化、键盘是否真的敲出字符）——
  服务端只能证明命令到达了板子，证明不了手机执行了。请用
  `bridge_client.py key VOLUME_UP` 亲眼确认一次。
- BOOT 按钮物理急停的实际手感
- 长时间运行的稳定性（WiFi 断流、手机息屏后的恢复）

这些是硬件交互层面的，需要你手上那块板子和那台手机。

---

## License

MIT — 见 [LICENSE](LICENSE)。

第三方组件：

| 组件 | 作者 | 许可 |
|---|---|---|
| [ESP32-BLE-HID](https://github.com/Autfy/ESP32-BLE-HID) | coco / Autfy | 上游未附 LICENSE，脚本从源码安装 |
| [WebSockets](https://github.com/MarkSkroob/ArduinoWebSockets) | Markus Sattler | LGPL-2.1 |
| Arduino ESP32 core | Espressif | LGPL-2.1 |

> ⚠️ **ESP32-BLE-HID 上游没有 LICENSE 文件**，法律上属于"保留所有权利"。
> 本仓库不 vendored 它，用 `scripts/build_firmware.sh install-lib` 从 GitHub 拉。
> 要商用请先联系作者确认授权。