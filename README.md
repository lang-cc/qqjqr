# qqjqr · AstrBot + QQ 机器人部署仓库

一套**能长期挂着、还会自己找话说**的 QQ 机器人部署方案，沉淀了 2026-10-29 全天的踩坑结果。

- 大脑：**AstrBot 4.29.3**（Agent 模式、人格、记忆、TTS、插件）
- 协议端：**SnowLuma**（Windows，注入 QQNT）或 **NapCat**（Linux 云服务器，无头）
- 附带：本地排队代理（治 429）、桌面控制台看门狗、7 个自研插件

---

## 两条部署路线

| 路线 | 适合 | 文档 |
|---|---|---|
| **Windows 本机** | 自己有台常开的电脑、要扫码/语音/表情包 | [`docs/01-Windows本机部署一条龙.md`](docs/01-Windows本机部署一条龙.md) |
| **Linux 云服务器** | 7×24 挂机、断电也能活 | [`docs/02-云服务器部署一条龙.md`](docs/02-云服务器部署一条龙.md) |

变更与踩坑全记录：[`docs/03-今日变更记录-2026-10-29.md`](docs/03-今日变更记录-2026-10-29.md)
（旧的 `部署任务文档.md` 保留作最早期的骨架记录。）

---

## 架构

```
群友 / 你自己
     │ QQ 消息
     ▼
┌──────────────┐   注入/协议   ┌──────────────┐
│ QQ 客户端     │ ───────────> │ SnowLuma     │  Windows
│ (QQNT)       │              │ 或 NapCat     │  / Linux
└──────────────┘              └──────┬───────┘
                                     │ OneBot v11 反向 WS (6199/ws)
                                     ▼
                              ┌──────────────┐
                              │ AstrBot      │  面板 6185
                              └──────┬───────┘
                                     │ 网关有并发限制时
                                     ▼
                              ┌──────────────┐
                              │ 排队代理 8878 │
                              └──────┬───────┘
                                     ▼
                                 LLM 网关
```

### 端口

| 端口 | 用途 |
|---|---|
| 6185 | AstrBot 面板 |
| 6199 | AstrBot 反向 WS（协议端连入，路径必须 `/ws`） |
| 5099 | SnowLuma WebUI（Windows） |
| 3000 | SnowLuma HTTP API |
| 6099 | NapCat WebUI（Linux） |
| 8878 | 本地排队代理 |

---

## 目录结构

```
qqjqr/
├─ docs/                       部署文档 + 变更记录
├─ plugins/                    7 个 AstrBot 插件（部署时拷进 data/plugins/）
│   ├─ astrbot_plugin_xiaonian_humanlike   人设注入 / 去 AI 腔 / 打字延迟 / 错误话术
│   ├─ astrbot_plugin_xiaonian_memory      长期记忆
│   ├─ astrbot_plugin_xiaonian_sticker     表情包收藏与发送
│   ├─ astrbot_plugin_xiaonian_toolbox     常用小工具
│   ├─ astrbot_plugin_xiaonian_web         联网搜索 / 读网页
│   ├─ astrbot_plugin_xiaonian_heart       情绪与主动消息
│   └─ astrbot_plugin_xiaonian_interest    兴趣接话 + 引用回复补丁
├─ tools/
│   ├─ gw_proxy.py             排队代理（压并发，治 429）
│   ├─ watchdog_gui.pyw        桌面控制台（三栏监控面板）
│   ├─ watchdog.py             命令行版看门狗
│   ├─ clean_history.py        历史清理
│   └─ qqbot.ico               机器人图标（16~256）
├─ config-templates/
│   ├─ cmd_config.example.json         AstrBot 配置模板（已脱敏）
│   └─ snowluma-onebot.example.json    SnowLuma OneBot 模板
├─ deploy/linux/               systemd unit / docker-compose / 一键脚本
├─ 启动.bat / 停止.bat          Windows 一键启停
└─ data/                       运行时（数据库/密钥/日志，不进仓库）
```

---

## 快速开始（Windows 最短路径）

```bat
cd /d D:\qqjqr
python -m venv .venv
.venv\Scripts\python.exe -m pip install astrbot==4.29.3
xcopy /E /I plugins data\plugins
启动.bat
```

细节见 [`docs/01`](docs/01-Windows本机部署一条龙.md)。

---

## 这套东西解决的问题（也就是踩过的坑）

| 坑 | 解法 |
|---|---|
| 网关 `429 Non-streaming text concurrency limit exceeded` | `tools/gw_proxy.py` 队列代理，`base_url` 指 `127.0.0.1:8878/v1` |
| 群里没 @ 就不回、@ 了又偶尔死于限流 | 兴趣接话插件（关键词 + 模型打分 + 冷却 + 每小时上限） |
| 语音回复从不引用消息 | 内置 `can_decorate` 只认纯文本/图文，插件里自己补 `Reply` |
| 模型自称"我是一个 AI 助手" | 网关丢 system 角色，`SYSTEM_VIA_USER = True` 把设定搬进 user 消息 |
| 桌面面板窗口凭空消失 | 后台启动被最小化到屏幕外，用 `cmd /c start` 或双击快捷方式 |
| 面板同时开两个、互相抢着重启服务 | 单实例锁 `127.0.0.1:8898` + 手动操作后 60 秒冷却 |
| Windows 上 Agent 有权限也不执行命令 | 权限矩阵要求"联网开 + 文件 host"，否则需要 OS 隔离而 Windows 不支持 |
| 白名单写了却不生效 | 私聊写完整 UMO `平台ID:FriendMessage:QQ号`，群聊写裸群号 |

---

## ⚠️ 安全提醒

1. **`data/` 里有明文密钥**（模型 key、TTS key、数据库）。别提交、别随手打包发给别人。
2. **Computer Use 全开**（执行 + 联网 + 全盘）意味着"能对它说话的人就能在你机器上跑命令"：
   - `admins_id` 只放自己
   - 群白名单只放可信群
   - 不用时把 `computer_use_runtime` 设回 `none`
3. **Agent 能改自己**：人格（`data/data_v4.db`）、权限（`data/cmd_config.json`）、插件逻辑（`plugins/*.py`）都在可写范围内。需要防护就加写入黑名单（见变更记录待办）。
4. 面板 / WebUI **不要直接暴露公网**，用 SSH 隧道或 nginx + basic auth。

---

## 归属

- AstrBot：AGPL-3.0 · <https://github.com/AstrBotDevs/AstrBot>
- SnowLuma · NapCat：见各自仓库许可
- 本仓库自研部分（插件、代理、控制台、文档）：随你使用
