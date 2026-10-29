# AstrBot + SnowLuma 本机（Windows）部署一条龙

> 适用：一台 **Windows 10/11 桌面电脑**，用 QQ 桌面客户端（QQNT）+ SnowLuma 接入，AstrBot 做大脑。
> 全程约 **40~60 分钟**，不需要写代码，照着敲/点即可。
> 最后更新：2026-10-29（对应 AstrBot 4.29.3 / SnowLuma 2.2.1 / QQNT 9.9.x）
> 代码仓库：https://github.com/lang-cc/qqjqr

### 先把代码弄到本机

```bat
git clone https://github.com/lang-cc/qqjqr.git D:\qqjqr
cd /d D:\qqjqr
```

没装 git 就去 <https://git-scm.com/download/win> 装一个（一路下一步即可）；
或者直接打开仓库网页 → 绿色 **Code** → **Download ZIP** → 解压成 `D:\qqjqr`。
下面所有 `D:\qqjqr` 都可以换成你自己的路径。

---

## 0. 先看懂要装哪几块

```
    群友 / 你自己
         │  (QQ 消息)
         ▼
  ┌───────────────┐   注入   ┌──────────────────┐
  │ QQ 桌面客户端  │ <──────> │ SnowLuma（协议端）│
  │  QQNT 9.9.x   │          │ WebUI  :5099      │
  └───────────────┘          │ HTTP   :3000      │
                             └────────┬─────────┘
                                      │ OneBot v11 反向 WS
                                      ▼ ws://127.0.0.1:6199/ws
                             ┌──────────────────┐
                             │ AstrBot 4.29.3   │
                             │ 面板    :6185     │
                             │ 反向WS  :6199     │
                             └────────┬─────────┘
                                      │ 若网关有并发限制
                                      ▼
                             ┌──────────────────┐
                             │ 排队代理 :8878    │   ← 可选但强烈建议
                             └────────┬─────────┘
                                      ▼
                              LLM 网关 / 官方 API
```

### 端口占用一览（先确认没被别的东西占）

| 端口 | 谁在用 | 说明 |
|---|---|---|
| 6185 | AstrBot 面板 | 只给本机浏览器访问 |
| 6199 | AstrBot 反向 WS | SnowLuma 连进来 |
| 5099 | SnowLuma WebUI | 扫码 / 配 OneBot |
| 3000 | SnowLuma HTTP API | 给脚本调用（可选） |
| 8878 | 本地排队代理 | 压并发，避免 429 |

### 需要提前准备的东西

| 项目 | 说明 |
|---|---|
| 一个 QQ 小号 | 建议新注册，别用主号；手机能收验证码 |
| LLM 接口 | 官方 API 或中转网关的 `base_url` + `api_key` + 模型名 |
| TTS 接口（可选） | 阿里百炼 DashScope 的 `api_key`（要发语音才需要） |
| Python 3.10+ | 推荐 3.12 |


### 现场必问清单（给别人的机器部署时，照着问，别自己猜）

| # | 要问什么 | 填到哪里 |
|---|---|---|
| 1 | 机器人用哪个 QQ 号登录 | QQ 客户端 / SnowLuma 扫码（或 NapCat 扫码） |
| 2 | 你自己的 QQ 号 | `admins_id`；白名单私聊写 `平台ID:FriendMessage:你的QQ` |
| 3 | 要在哪些群生效 | `id_whitelist` 里写裸群号；兴趣接话插件的 `groups` |
| 4 | 用哪个模型 / 网关 | 供应商的 `base_url`+`api_key`+模型名；网关域名填 `tools/gw.local.json` 或 `GW_UPSTREAM` |
| 5 | 要不要发语音 | 要 → TTS 的 api_key（DashScope），`dual_output=false` |
| 6 | 面板/WebUI 密码谁定 | 首次登录自己设，**SnowLuma WebUI 只存哈希，丢了只能重置** |

> 本仓库所有模板里的账号、群号、域名、密钥**都是空的**，就是留给现场填的。
---

## 1. 装 Python

1. 打开 <https://www.python.org/downloads/windows/> 下 **Python 3.12.x**（Windows installer 64-bit）。
2. 安装时**务必勾选** `Add python.exe to PATH`。
3. 验证（Win+R → `cmd`）：

```bat
python --version
```

看到 `Python 3.12.x` 就对了。若提示"不是内部或外部命令"，重装并勾上 PATH。

---

## 2. 拉代码 / 建目录

本文假设你放在 `D:\qqjqr`（换个盘也行，后面路径自己替换）。

```bat
cd /d D:\qqjqr
```

仓库克隆下来后目录长这样（已经齐了，不用再往里拷东西）：

```
D:\qqjqr\
├─ tools\               gw_proxy.py、watchdog_gui.pyw、qqbot.ico
├─ plugins\             （7 个插件，稍后拷进 data\plugins）
├─ config-templates\    脱敏配置模板
├─ 启动.bat  停止.bat
```

---

## 3. 装 AstrBot（虚拟环境 + 固定版本）

```bat
cd /d D:\qqjqr
python -m venv .venv
.venv\Scripts\python.exe -m pip install -U pip
.venv\Scripts\python.exe -m pip install astrbot==4.29.3
```

> 装完验证：`.venv\Scripts\astrbot.exe --version` 应输出 `AstrBot, version 4.29.3`

第一次启动：

```bat
.venv\Scripts\astrbot.exe run
```

- 会自动在 `D:\qqjqr\data\` 生成配置（`cmd_config.json`）和数据库。
- 浏览器打开 <http://127.0.0.1:6185>，**默认账号 `astrbot` / 密码 `astrbot`**，登录后立刻改密码。
- 看到面板 = 装好了。先 `Ctrl+C` 停掉，接着装协议端。

---

## 4. 装 QQNT + SnowLuma

### 4.1 装 QQ

1. 到 <https://im.qq.com/pcqq> 下 **QQ Windows 版（QQNT 版本，9.9.x）**。
2. 默认装到 `C:\Program Files\Tencent\QQNT\`。
3. 启动一次、扫码登录一次，然后**退出**（后面要用 SnowLuma 拉起）。

### 4.2 装 SnowLuma

1. 下载 SnowLuma 发行包（GitHub Release），解压到比如 `D:\snowluma`。
2. 目录长这样：

```
D:\snowluma\
├─ launcher.bat      双击启动（会拉起 QQ 并注入）
├─ node.exe
├─ config\           onebot.json / webui.json / runtime.json
└─ data\  logs\
```

3. 双击 `launcher.bat`（或用 `run_hidden.bat` 无窗口启动）。它会把 QQ 拉起来并注入。
4. 浏览器打开 <http://127.0.0.1:5099>，首次进入设置 WebUI 密码。

> **重要**：SnowLuma 的 WebUI 只存密码哈希，**密码丢了只能改哈希重置**，记牢。
> 想用命令行重置：
> ```bat
> cd /d D:\qqjqr
> .venv\Scripts\python.exe -c "import hashlib,base64,os,json;p='你的新密码';s=base64.b16encode(os.urandom(16)).decode();print(json.dumps({'salt':s,'hash':base64.b64encode(hashlib.pbkdf2_hmac('sha256',p.encode(),s.encode(),100000)).decode()}))"
> ```
> 具体格式以 SnowLuma 版本为准，最稳的是删掉 `config\webui.json` 让它重新生成。

### 4.3 登录 QQ

- SnowLuma WebUI 里点「扫码登录」→ 手机 QQ 扫码。
- 或者直接在 QQ 客户端里登录（SnowLuma 会复用已登录的账号）。
- 登录态保持后，**别关 QQ**，关掉就等于机器人下线。

---

## 5. 配 SnowLuma 的 OneBot（关键一步）

编辑 `D:\snowluma\config\onebot.json`（或直接在 WebUI 的「网络设置」里点），改成：

```json
{
  "networks": {
    "httpServers": [
      {
        "name": "local-actions",
        "enabled": true,
        "host": "127.0.0.1",
        "port": 3000,
        "path": "/",
        "accessToken": "自己设一个token",
        "messageFormat": "array",
        "reportSelfMessage": false
      }
    ],
    "httpClients": [],
    "wsServers": [],
    "wsClients": [
      {
        "name": "astrbot",
        "enabled": true,
        "url": "ws://127.0.0.1:6199/ws",
        "role": "Universal",
        "accessToken": "",
        "reconnectIntervalMs": 5000,
        "messageFormat": "array",
        "reportSelfMessage": false
      }
    ]
  },
  "musicSignUrl": ""
}
```

要点：

| 字段 | 为什么 |
|---|---|
| `wsClients` → `url: ws://127.0.0.1:6199/ws` | AstrBot 是**反向 WS 服务端**，所以协议端要主动"连出去"，路径**必须带 `/ws`** |
| `messageFormat: "array"` | 用消息段数组，别用 CQ 码字符串 |
| `role: "Universal"` | 同时收群消息和私聊 |
| `accessToken: ""` | 本机回环，留空即可；若填了，AstrBot 那边也要同步 |

改完在 WebUI 里点保存/重载，或重启 SnowLuma。

---

## 6. 配 AstrBot

浏览器打开 <http://127.0.0.1:6185> 登录。

### 6.1 平台：接入 OneBot

面板 → **配置 → 平台 → 新增 → `aiocqhttp`（OneBot v11）**：

| 项 | 值 |
|---|---|
| ID | `snowluma-onebot`（随便，但后面白名单要用这个前缀） |
| 启用 | 开 |
| 反向 WS Host | `127.0.0.1` |
| 反向 WS 端口 | `6199` |
| Token | 留空（和 SnowLuma 的 `accessToken` 保持一致） |

保存后重启 AstrBot，日志里应出现：

```
aiocqhttp(OneBot v11) 适配器已连接。
```

没出现就去看第 11 节排障。

### 6.2 模型供应商

面板 → **配置 → 服务提供商 → 新增**：

| 项 | 值 |
|---|---|
| 类型 | OpenAI 兼容 / 自定义 OpenAI |
| base_url | 你网关的 `https://xxx/v1`（**不要**多写或少写 `/v1`） |
| api_key | 你的 key |
| 模型名 | 例如 `deepseek-v4.1-flash`（必须和网关支持的完全一致） |
| 流式 | 开 |

保存后点「测试」，能返回内容就对了。

### 6.3 人格

面板 → **人格** → 编辑或新建，把你的角色设定写进去（叫什么、几岁、说话风格、禁区）。
保存后在该会话里选中这个人格（或在配置里设默认人格）。

### 6.4 访问控制（白名单）—— 最容易踩坑的一步

面板 → **配置 → 平台设置**：

| 项 | 值 | 说明 |
|---|---|---|
| `enable_id_white_list` | **开** | 开了只有白名单里的人/群能触发 |
| `wl_ignore_admin_on_group` | 开 | 管理员（你）在群里不受白名单限制 |
| `wl_ignore_admin_on_friend` | 开 | 管理员私聊不受限 |
| `id_whitelist` | 见下 | **两种写法不一样** |
| `no_permission_reply` | 建议关 | 关了就不会有人乱发时被回"你权限不足" |

白名单两种格式：

```jsonc
[
  "snowluma-onebot:FriendMessage:<你的QQ>",  // 私聊：完整 UMO（平台ID:消息类型:QQ号）
  "<你的群号>"                                   // 群聊：直接写群号，不要加前缀
]
```

> 怎么拿到自己的完整 UMO？给机器人发一句 `/sid`，日志/回复里会打印。
> 写错了就是"发消息没反应"，最常见的坑。

### 6.5 群聊唤醒方式

| 项 | 值 | 说明 |
|---|---|---|
| `wake_prefix` | `["/"]` | 群里打 `/` 开头才叫醒它 |
| @ 机器人 | 自动 | 不用前缀也会回 |
| 引用机器人消息 | 自动 | 等同于 @ |

想让它**不 @ 也主动接话**：见第 8 节「兴趣接话插件」。

### 6.6 分段回复（更像真人）

面板 → 平台设置 → `segmented_reply`：

```json
{
  "enable": true,
  "only_llm_result": true,
  "interval_method": "random",
  "interval": "1.2,2.8",
  "split_mode": "regex",
  "regex": ".*?[。！？~…]+|.+$"
}
```

### 6.7 语音回复（TTS，可选）

面板 → **配置 → 语音合成（TTS）**：

| 项 | 值 |
|---|---|
| 启用 | 开 |
| 提供商 | 阿里百炼 / DashScope（`xiaoling-tts` 之类） |
| api_key | DashScope key |
| `dual_output` | **关**（关 = 只发语音，不发文字；这是"双输出"开关） |
| `trigger_probability` | 0.35 之类，别 1.0，太吵 |

实测坑：

| 坑 | 说明 |
|---|---|
| `dual_output=true` | 文字+语音各发一条，群里像刷屏 → 建议关 |
| 服务器/本机没有 ffmpeg | 语音合成后要转码，缺 ffmpeg 会直接失败。Windows 上把 `ffmpeg.exe` 放进 `tools\` 或加进 PATH（本机就放了一份在 `toolsfmpeg.exe`，**不进仓库**） |

### 6.8 引用回复（为什么它不引用）

AstrBot 内置的 `reply_with_quote` 有个隐藏限制：**只对"纯文本/图文"生效**。

```python
# astrbot/core/pipeline/result_decorate/stage.py
can_decorate = all(isinstance(item, (Plain, Image)) for item in result.chain)
```

一旦回复链里有 `Record`（语音），引用和 @ 会被**静默跳过**。
解决办法：本仓库的 `astrbot_plugin_xiaonian_interest` 里带了一段补丁（`on_decorating_result` 里自己插 `Reply`），装插件即生效，见第 8 节。

### 6.9 Agent / Computer Use（要不要给电脑权限）

面板 → 配置 → AI → 能力 → **Agent Computer Use**：

| 项 | 含义 |
|---|---|
| `computer_use_runtime = none` | 完全不给（最安全） |
| `= local` | 允许它在本机跑命令 / 读写文件 / 点鼠标 |
| 本地权限策略 | 按 `member`（普通用户）和 `admin`（管理员）分别设 执行/联网/文件范围 |

Windows 上有个硬规则：**权限矩阵里只要出现"不许联网"或"只能看工作区"，AstrBot 就要求操作系统级隔离，而 Windows 后端是 `unsupported`，结果直接拒绝执行**。想让它在 Windows 上真能跑命令，只能是 `执行=开 + 联网=开 + 文件=host`。

⚠️ 一旦打开，**能对它说话的人就能在你的电脑上跑命令**。至少做到：
- `admins_id` 只放你自己
- 群聊白名单只放少数可信群
- 先想清楚再开 `member` 档

---

## 7. 429 并发限制 → 本地排队代理（强烈建议）

很多中转网关限制"**非流式并发的文本请求 = 2**"，超了就回：

```
429 Non-streaming text concurrency limit exceeded
```

AstrBot 同时跑「分段回复 + 记忆 + 表情包」很容易超。解决办法：本地加一层排队代理。

`tools\gw_proxy.py` 做的事：对外暴露 `127.0.0.1:8878`，内部用 `asyncio.Semaphore(2)` 把请求排成队列打给真网关。

**上游网关地址有两种填法**（脚本不会硬编码，避免泄露）：

| 方式 | 做法 |
|---|---|
| 环境变量 | 启动前 `set GW_UPSTREAM=https://你的网关域名` |
| 私有文件（推荐） | 在 `tools\` 下新建 `gw.local.json`：`{"upstream": "https://你的网关域名"}`（已 gitignore） |

### 7.1 启动

```bat
cd /d D:\qqjqr
.venv\Scripts\python.exe tools\gw_proxy.py
```

### 7.2 把模型 base_url 指到代理

面板 → 服务提供商 → 把 `base_url` 改成：

```
http://127.0.0.1:8878/v1
```

上游地址见上面的两种填法；`key` 由 AstrBot 的供应商配置带过去，代理本身不存 key。改完重启 AstrBot。

### 7.3 验证

```bat
curl http://127.0.0.1:8878/health
```

返回 `{"limit":2,"req":N,"queued":0,"err":0,"cancel":0}` 就对了。

> `cancel` 是 AstrBot 主动取消（正常），`err` 才是真错误。

---

## 8. 插件部署

把仓库 `plugins\` 里的 7 个目录拷进 `D:\qqjqr\data\plugins\`：

```
data\plugins\
├─ astrbot_plugin_xiaonian_humanlike    人设注入 + 去 AI 腔 + 打字延迟 + 错误话术替换
├─ astrbot_plugin_xiaonian_memory       长期记忆（账本/历史）
├─ astrbot_plugin_xiaonian_sticker      表情包收藏与发送
├─ astrbot_plugin_xiaonian_toolbox      常用小工具
├─ astrbot_plugin_xiaonian_web          联网搜索 / 读网页
├─ astrbot_plugin_xiaonian_heart        情绪与主动消息（会等你、主动找你）
└─ astrbot_plugin_xiaonian_interest     兴趣接话 + 引用回复补丁  ← 本文新增
```

拷完在面板「插件」页点 **重载**（不用重启 AstrBot）。

### 8.1 兴趣接话（不 @ 也会回）

装了 `xiaonian_interest` 后，群里没人叫它时它也能自己判断要不要接话：

| 机制 | 说明 |
|---|---|
| 关键词粗筛 | 命中兴趣牌 → 送去打分 |
| 盲抽 | 没命中也有 15% 概率送去打分 |
| 模型打分 | 0~10 分，≥ 阈值才回 |
| 限流 | 冷却 90 秒 + 每小时上限（默认 30 条）+ 打分 20 秒一次 |

指令（群里/私聊都行）：

```
/兴趣                 看状态
/兴趣 关 / 开
/兴趣 阈值 7
/兴趣 冷却 90
/兴趣 上限 30
/兴趣 话题 游戏,猫
/兴趣 引用 开|关        群聊是否引用
/兴趣 私聊引用 开|关
/兴趣 测试 这句话      手动打分看它想不想接
```

### 8.2 人设兜底（网关不认 system 时）

有些网关的模型**会丢掉 system 角色**，导致人设失效、它自称"我是 AI 助手"。
`xiaonian_humanlike/main.py` 顶部有个开关：

```python
SYSTEM_VIA_USER = True   # system_prompt 搬成第一条用户消息，任何模型都吃得下
```

人设文本可放 `data/plugins/astrbot_plugin_xiaonian_humanlike/persona.txt`。

---

## 9. 桌面控制台（看门狗）

`tools\watchdog_gui.pyw` 是一个 1180×520 的 tkinter 面板，三栏：

| 栏 | 内容 |
|---|---|
| 左·监控 | 排队代理 / AstrBot / OneBot 接入 / SnowLuma / QQ 账号 / 模型网关 / 入站消息 共 7 项 + 累计请求·排队·出错 三个小卡 + 三个开关（自动拉起 / 开机自启 / 窗口置顶） |
| 中·登录信息 | AstrBot 面板、SnowLuma WebUI、SnowLuma OneBot Token 三行 + 今日入站 / 发送成功率 / 429 统计 |
| 右·运行日志 | 滚动日志 |

底部 7 个按钮：全部启动 / 全部停止 / 重启 AstrBot ｜ 打开面板 / SnowLuma WebUI / 扫码登录 / 重置密码。

### 9.1 启动方式

```bat
cd /d D:\qqjqr
.venv\Scripts\pythonw.exe tools\watchdog_gui.pyw
```

**别用 `nohup` / 后台方式启动**，否则窗口会以最小化状态落在屏幕外（`-25600,-25600`）。用 `cmd /c start "" ...` 或双击快捷方式。

### 9.2 建桌面快捷方式

| 项 | 值 |
|---|---|
| 目标 | `D:\qqjqr\.venv\Scripts\pythonw.exe` |
| 参数 | `"D:\qqjqr\tools\watchdog_gui.pyw"` |
| 起始位置 | `D:\qqjqr` |
| 图标 | `D:\qqjqr\tools\qqbot.ico,0` |

### 9.3 它自动干什么

| 行为 | 说明 |
|---|---|
| 自动拉起代理 | 连续 2 次探测不到 8878 → 起 `gw_proxy.py` |
| 自动拉起 AstrBot | 连续 3 次探测不到 6185 + 60 秒冷却 → 起 `astrbot run` |
| 统计 429 | 增量扫描日志（断点存在 `data\logs\console_stats.json`），跨重启累计、跨天归零 |
| 单实例 | 绑 `127.0.0.1:8898`，第二次点快捷方式会自动退出，避免两个看门狗互抢 |

---

## 10. 验收清单

| # | 做什么 | 期望 |
|---|---|---|
| 1 | 私聊发「在吗」 | 回复（随机可能带语音） |
| 2 | 群里 @ 机器人 | 回复，且**引用你的消息** |
| 3 | 群里别人随便聊一句含兴趣词 | 可能自己插一句（30 条/小时内） |
| 4 | 发一张表情包 | 表情包插件收藏/回图 |
| 5 | `curl http://127.0.0.1:8878/health` | `err=0` |
| 6 | 面板「今日网关 429」 | 代理上线后不再增长 |
| 7 | 桌面控制台 7 项监控 | 全绿 |

---

## 11. 常见故障速查

| 现象 | 原因 | 解决 |
|---|---|---|
| 发消息完全没反应 | 白名单没写对 | `id_whitelist` 里私聊写完整 UMO、群写裸群号；确认 `enable_id_white_list` 开 |
| 日志里没有 `适配器已连接` | 反向 WS 路径/端口不对 | SnowLuma 的 `wsClients.url` 必须是 `ws://127.0.0.1:6199/ws`（带 `/ws`） |
| 回了但群里看不到 | QQ 限流（本群每分钟 5 条） | 降低发言频率、拉长冷却 |
| 日志刷 `429 Non-streaming text concurrency` | 网关并发 2 | 上 `gw_proxy.py`，base_url 指 `127.0.0.1:8878/v1` |
| 它自称"我是一个 AI 助手" | 网关丢 system 角色 | `SYSTEM_VIA_USER = True` |
| 语音回复不引用 | 内置逻辑只处理纯文本 | 装 `xiaonian_interest`（带 `Reply` 补丁） |
| 桌面控制台窗口消失 | 被最小化到屏幕外 | 双击快捷方式重新拉起；别用后台方式启动 |
| 面板密码忘了 | 只存哈希 | 用看门狗的「重置密码」按钮（改成配置文件里的新密码并重启） |
| 网关 key 泄露风险 | `data\` 里有明文 | 不要打包 `data\` 给别人；需要迁移时单独加密传 |

---

## 12. 备份与迁移

只备份这些就够（**注意含密钥，别人给的机器别直接带走**）：

```bat
cd /d D:\qqjqr
tar -czf qqjqr-data-%date:~0,4%%date:~5,2%%date:~8,2%.zip data\cmd_config.json data\data_v4.db data\plugins data\logs\console_stats.json
```

恢复：解压回 `D:\qqjqr\data\`，启动即可。

---

## 13. 一键启动 / 停止

`启动.bat`：

```bat
@echo off
chcp 65001 >nul
set PYTHONIOENCODING=utf-8
cd /d "%~dp0"
rem 先起排队代理（避免 429）
netstat -ano | findstr ":8878" | findstr "LISTENING" >nul
if errorlevel 1 (
    start "" /b ".venv\Scripts\python.exe" "tools\gw_proxy.py"
    timeout /t 2 >nul
)
".venv\Scripts\astrbot.exe" run
pause
```

`停止.bat`：

```bat
@echo off
chcp 65001 >nul
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":6185" ^| findstr "LISTENING"') do taskkill /F /PID %%a
for /f "tokens=5" %%a in ('netstat -ano ^| findstr ":8878" ^| findstr "LISTENING"') do taskkill /F /PID %%a
```

---

## 附：开机自启

控制台里「开机自启」开关会写注册表：

```
HKCU\Software\Microsoft\Windows\CurrentVersion\Run
  → qqjqr_console = "D:\qqjqr\.venv\Scripts\pythonw.exe" "D:\qqjqr\tools\watchdog_gui.pyw"
```

想手动加：`Win+R` → `shell:startup` → 放一个快捷方式进去也行。
