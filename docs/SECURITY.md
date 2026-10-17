# 安全模型

## 威胁模型

假设攻击者可能：

- 拿到你的 token（配置泄露、日志粘贴、聊天窗口）
- 在你的 LAN 上跑任意程序
- 通过 prompt injection 操控你的 AI agent（**这是最现实的威胁**）
- 物理接触板子或手机

**不在威胁模型内**：攻破 WiFi 加密、攻破 BLE 配对、从互联网直接打进来
（服务端只绑一个 LAN 地址，没有公网入口）。

## 防御层次

### 传输层

**只绑一个 LAN 地址，永不绑 `0.0.0.0`。**

```python
if args.bind in ("0.0.0.0", "::"):
    sys.exit("refusing to bind 0.0.0.0 — pass an explicit LAN address")
```

绑 `0.0.0.0` 意味着任何网卡上的任何人都能连上这台服务。加上无 TLS 的事实，
那就是把 token 广播给整个局域网。

**实测提醒**：开发时我会用 `--bind 127.0.0.1`（更安全），但那样板子连不上 ——
脚本会在 stderr 明确警告：

```
warning: bound to loopback only; an ESP32 on the LAN cannot reach it.
         pass --bind <your-lan-ip> if the board is remote.
```

### 认证层

**WebSocket 升级**需要 URL 里带 token：

```python
hmac.compare_digest(got, want)    # 常量时间比较
```

常量时间很重要 —— 普通的 `==` 会在第一个不匹配的字节就返回，攻击者可以通过
计时差异逐字节爆破 token。

**命令提交**需要额外的一个 HTTP header：

```
X-HID-Guard: sha256(token)
```

为什么不直接用 token 当 header？因为 **WebSocket 升级的 URL 会出现在各种日志里**
（服务器 access log、反向代理日志、`ps` 输出）。用它的 sha256 当提交凭证，
泄露 URL token 的人无法提交命令，只能连上看板子的输出。

sha256 在这里不是加密，是**最小化的凭证派生**：不引入第二个 secret，
但把「能看」和「能做」拆开。

**来源限制**：

```python
trusted = {"127.0.0.1", "::1"}
if args.listen_lan:
    trusted.add(args.bind)
```

注意 `--listen-lan` 加的是**那一个地址**，不是整个网段。从 LAN 上另一台机器
curl 依然 403。这不是 bug，是刻意的 —— 已实测验证。

### 命令策略层

**没有「发送任意 keycode」的代码路径。**

这是整个设计里最重要的一条。服务端只有 7 个 op 的分支：

```python
if op == "type": ...
if op == "key": ...
if op == "combo": ...
if op == "tap": ...
if op == "swipe": ...
if op == "scroll": ...
if op == "ping": ...
raise PolicyError(f"unknown op: {op!r}")
```

没有 fallback 到「发送任意 usage」的分支。所以即使攻击者完全控制服务端逻辑，
固件也只会执行它认识的那几种操作 —— 而每种都有范围限制。

**具体限制**：

| 类型 | 限制 | 理由 |
|---|---|---|
| 文本 | ASCII 可打印白名单，≤200 字符 | CJK/emoji 没有 keycode；长度上限防刷屏 |
| 键名 | 固定白名单 20 个 | 防止 `key "rm -rf"` 这类 |
| 不支持的键 | 明确 400 + 原因 | 硬映射会打到别的键上 |
| 坐标 | 0..4000，固件侧再按屏幕 clamp | 防御纵深 |
| 组合键 | 必须含至少一个修饰键，长度 ≤2 | 纯单键组合没什么意义，也限制了 blast radius |
| 手势时长 | swipe ≤5000ms，tap ≤1000ms | 限制物理急停的最坏响应时间 |

**令牌桶限流**：突发 20 条，持续 3 条/秒。

这不是防攻击，是**防失控**：LLM 的 retry 循环、脚本的 bug、agent 的死循环，
都不应该能把手机刷爆。已实测：30 连发 → 20 条 200 + 10 条 429。

### 审计层

每个事件都落 append-only 日志：

| 事件 | 含义 |
|---|---|
| `board_attached` / `board_detached` | 板子连上/断开 |
| `board_replaced` | 新板子顶掉旧板子 |
| `board_hello` | 板子报上固件版本 |
| `cmd` | 命令已下发（**成功的都在这**） |
| `cmd_rejected` | 命令被策略拒绝 + 原因 |
| `cmd_throttled` | 被限流 |
| `post_rejected` | 提交被拒（来源或 guard 不对） |
| `ws_rejected` | WebSocket 升级失败（token 错） |

这不只是「留痕」。它是**回答「昨晚它到底往我手机上打了什么」的唯一手段**。
跑在别人机器上的 agent —— 你需要能审计它。

查：

```bash
jq -r '[.ts, .event, (.detail|tostring)] | @tsv' \
  ~/.local/state/esp32-hid-bridge/audit.jsonl | less -S
```

只看出键动作：

```bash
jq -r 'select(.event=="cmd") | [.ts, .detail.op, (.detail.key//.detail.text//"")] | @tsv' \
  ~/.local/state/esp32-hid-bridge/audit.jsonl
```

### 物理层：BOOT 按钮急停

**这是整个系统唯一一条不依赖任何软件的攻击防线。**

```
按住板子的 BOOT 按钮 → HID 输出立即永久停止，直到重启
```

不管服务端被攻破成什么样、不管 agent 发什么循环、不管 token 泄露，
**人手里一根手指就能掐断整条链路**。

已实现两道保险：

1. `loop()` 里检测 BOOT（低电平），置 `killed` 标志
2. 每条命令执行前再查一次 `killAsserted()`

**已知的响应延迟**：`swipeLinear()` 是阻塞调用，整段手势跑完才返回，所以急停
只能在手势**开始前**检查。手势时长上限 5 秒 —— 最坏情况按下按钮后 5 秒才会停止
输出。对一个物理急停来说这个延迟是可接受的，但不能不说明。

```cpp
if (killAsserted()) { reply(id, "done", false, "killed"); return; }
bleMouse.swipeLinear(...);   // 阻塞，5s 上限
```

## 部署清单

1. **token 用 `scripts/gen_token.sh` 生成** —— 24 随机字节，URL-safe base64，权限 0600
2. **别手搓 token** —— 人写的 token 熵不够
3. **`config.h` 在 `.gitignore` 里** —— WiFi 密码和 token 都不会被误提交
4. **防火墙只放行自己的网段**：
   ```
   sudo nft add rule inet filter input iifname "wlan*" \
     ip saddr 192.168.1.0/24 tcp dport 8765 accept
   ```
   不要写 `0.0.0.0/0`
5. **server 和 agent 同机**，或走 SSH 隧道。`--listen-lan` 会扩大信任面，
   清楚代价再用
6. **定期看审计日志** —— 尤其是你不确定 agent 干了什么的时候
7. **固件的物理急停要知道在哪** —— BOOT 按钮，通常 devkit 上是两个按钮里左边那个
8. **手机端**：无线 ADB 的配对信息会随重启/换网络失效，这是好事，别想办法绕过

## 明确不做的事

| 不做 | 为什么 |
|---|---|
| TLS | 局域网内明文；加证书管理对树莓派/软路由场景负担过大。用 SSH 隧道跨网段。 |
| 「发送任意键码」API | 一旦有这个，整个安全模型就塌了 |
| 固件 OTA 升级 | 攻击面（签名验证、密钥管理、回滚保护）远大于收益。USB 插线重刷。 |
| 命令签名（每条独立签名） | token 已经过了认证，再签一层收益有限；复杂度上升 |
| 命令 TTL / 时间戳 | 见 `ARCHITECTURE.md`：没有可信时钟，实现了也是假的 |

## 已知的安全弱点

诚实地列出来：

1. **明文 HTTP/WS**。token 在 URL 里，虽然只在局域网内。任何能嗅探的东西
   （ARP 欺骗、compromised 路由器、被攻破的同网段主机）都能拿到它。

2. **`--listen-lan` 下同机任意进程可提交命令**。任何一个能在 server 主机上
   跑代码的东西（其他服务、某个被攻破的容器）都能操作你手机。默认 loopback
   模式不存在这个问题。

3. **速率限制是全局的，不是按客户端**。被限流的攻击者和被限流的合法 agent
   无法区分。

4. **没有 per-agent 身份**。所有命令进同一个审计流，多个 agent 共用一个 token
   时分不清是谁发的。

5. **固件无固件级密钥**。token 存在 flash 里明文，能读 flash 就能拿到。
   （对物理攻击者来说这本来也不是防线 —— 他还能直接拔电。）

6. **BLE HID 本身没有加密**。BLE 配对提供链路加密，但键盘输入内容不加密 ——
   任何嗅探 BLE 流量的人都能看到你 agent 打出的每个字符。（键盘协议如此。）

7. **审计日志无完整性保护**。有写权限的人可以删改记录。真要防篡改需要
   外部日志收集或哈希链。