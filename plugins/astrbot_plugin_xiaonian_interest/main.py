"""小年 · 兴趣接话（astrbot_plugin_xiaonian_interest）

解决的问题：
    群里没 @ 它、也没打唤醒前缀时，AstrBot 默认完全不看那条消息。
    这个插件给群消息加一层"兴趣判定"——命中兴趣牌就送去让模型打分，
    分数够高才放行到正常回复链路（人设、记忆、语音、表情包全都照旧生效）。

判定流程（混合）：
    群消息 ──长度/自己发/冷却/每小时上限──> 关键词粗筛 ──命中 or 抽样──> 模型打分
                                                        └─ 分数 ≥ 阈值 → 放行并回复
                                                        └─ 分数 < 阈值 → 静默丢弃

重要：只要注册了 handler，AstrBot 就会把"没被叫的消息"也当成唤醒状态，
      所以不想回的情况必须 event.stop_event()，否则每条群消息都会回。

配置：面板「插件 → 小年·兴趣接话 → 配置」；也可以用 /兴趣 命令在群里现改。
"""

import json
import os
import random
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Reply
from astrbot.api.star import Context, Star, register

DIR = os.path.dirname(os.path.abspath(__file__))
CFG_FILE = os.path.join(DIR, "interest_config.json")

DEFAULTS = {
    "enable": True,
    "groups": [],
    "topics": [
        "游戏", "番剧", "动画", "新番", "漫画", "编程", "代码", "bug", "python", "脚本",
        "服务器", "部署", "音乐", "歌", "耳机", "猫", "狗", "宠物", "好吃", "奶茶",
        "梗", "表情包", "好笑", "离谱", "为什么", "怎么",
    ],
    "threshold": 6,
    "cooldown_sec": 90,
    "hourly_limit": 30,
    "min_len": 6,
    "blind_sample": 0.15,
    "judge_cooldown_sec": 20,
    "quote_reply": True,
    "quote_private": True,
    "debug": True,
}

JUDGE_SYSTEM = (
    "你是一个兴趣判断器，任务只有一个：判断群里刚说的这句话，"
    "你想不想主动插一句。只输出一行 JSON，形如"
    '{"score": 7, "why": "在聊游戏"}，不要输出任何多余内容。'
)

JUDGE_TMPL = """你在群里潜水，没人叫你。
你的兴趣：{topics}

判断标准：
- 提到了你在意的话题、有人在问问题、有梗/好笑/离谱、需要你吐槽两句 → 分数高（7~10）
- 普通闲聊但你接得上、有自己的看法 → 中等（5~6）
- 纯灌水、只 @ 别人、表情或单字、跟你完全无关的私事、广告 → 分数低（0~4）

群里刚有人说：「{text}」

只输出 JSON。"""

_SCORE_RE = re.compile(r'"?score"?\s*[:：]\s*(\d{1,2})')


@register(
    "xiaonian_interest",
    "qqjqr",
    "群里没人叫它时，按兴趣分决定要不要插一句（关键词粗筛 + 模型打分）",
    "0.1.0",
)
class XiaonianInterest(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self._panel_cfg = {}
        try:  # 面板里保存的插件配置（_conf_schema.json）
            from astrbot.core.star.star import star_map

            meta = star_map.get(self.__class__.__module__)
            if meta and isinstance(meta.config, dict):
                self._panel_cfg = dict(meta.config)
        except Exception:
            self._panel_cfg = {}
        self._local_cfg = {}
        self._load_local()
        self._last_reply = 0.0
        self._last_judge = 0.0
        self._recent: list[float] = []
        self._total = 0
        logger.info(
            f"[interest] 已加载：阈值={self.cfg('threshold')} 冷却={self.cfg('cooldown_sec')}s "
            f"上限={self.cfg('hourly_limit')}/时 群={self.cfg('groups')}"
        )

    # ---------------- 配置 ----------------
    def _load_local(self) -> None:
        try:
            with open(CFG_FILE, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                self._local_cfg = data
        except FileNotFoundError:
            self._save_local({})
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[interest] 读 {CFG_FILE} 失败: {e}")

    def _save_local(self, patch: dict) -> None:
        self._local_cfg.update(patch)
        try:
            with open(CFG_FILE, "w", encoding="utf-8") as f:
                json.dump(self._local_cfg, f, ensure_ascii=False, indent=2)
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[interest] 写 {CFG_FILE} 失败: {e}")

    def cfg(self, key: str):
        """优先级：本地 json > 面板配置 > 默认值"""
        if key in self._local_cfg and self._local_cfg[key] not in (None, ""):
            return self._local_cfg[key]
        if key in self._panel_cfg and self._panel_cfg[key] not in (None, ""):
            return self._panel_cfg[key]
        return DEFAULTS.get(key)

    # ---------------- 核心：群消息兴趣判定 ----------------
    @filter.event_message_type(filter.EventMessageType.GROUP_MESSAGE, priority=-100)
    async def watch_group(self, event: AstrMessageEvent):
        """priority=-100 → 排在其它插件后面跑，不影响表情包/指令处理。"""
        try:
            if not bool(self.cfg("enable")):
                return

            # 已经被人 @/引用/打了唤醒前缀 → 走正常回复链路，不插手
            if event.is_at_or_wake_command:
                return
            if str(event.get_sender_id()) == str(event.get_self_id()):
                return  # 自己的消息

            gid = str(event.get_group_id() or "")
            groups = [str(g) for g in (self.cfg("groups") or [])]
            if groups and gid not in groups:
                event.stop_event()
                return

            text = (event.message_str or "").strip()
            if len(text) < int(self.cfg("min_len") or 0):
                event.stop_event()
                return

            now = time.time()
            if now - self._last_reply < float(self.cfg("cooldown_sec") or 0):
                event.stop_event()
                return
            self._recent = [t for t in self._recent if now - t < 3600]
            if len(self._recent) >= int(self.cfg("hourly_limit") or 0):
                event.stop_event()
                return

            # 关键词粗筛：命中就走快速通道；没命中按抽样概率放行（避免只认死词）
            low = text.lower()
            hits = [k for k in (self.cfg("topics") or []) if k and str(k).lower() in low]
            if not hits and random.random() > float(self.cfg("blind_sample") or 0):
                event.stop_event()
                return

            # 打分本身也限速：不管命中多少关键词，20 秒内只判一次
            jcool = float(self.cfg("judge_cooldown_sec") or 0)
            if jcool and now - self._last_judge < jcool:
                event.stop_event()
                return
            self._last_judge = now

            score, why = await self._judge(event, text, hits)
            need = int(self.cfg("threshold") or 6)

            if score >= need:
                # 关键一步：把"不是被叫的"消息标记成可回复，正常链路才会走 LLM
                event.is_at_or_wake_command = True
                self._last_reply = now
                self._recent.append(now)
                self._total += 1
                if bool(self.cfg("debug")):
                    logger.info(
                        f"[interest] 接话 ✔ 分={score} 词={hits[:3]} 原因={why} 群={gid} 内容={text[:40]}"
                    )
            else:
                event.stop_event()
                if bool(self.cfg("debug")):
                    logger.info(
                        f"[interest] 跳过 分={score} 词={hits[:3]} 原因={why} 内容={text[:40]}"
                    )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[interest] 判定异常 {type(e).__name__}: {e}")
            event.stop_event()

    async def _judge(self, event: AstrMessageEvent, text: str, hits: list[str]) -> tuple[int, str]:
        """让模型给这句话打分（0-10）。失败就返回 0，绝不阻塞群消息。"""
        try:
            provider = self.context.get_using_provider(umo=event.unified_msg_origin)
        except Exception:
            provider = None
        if not provider:
            return 0, "没有可用模型"

        topics = "、".join(str(k) for k in (self.cfg("topics") or [])[:20])
        prompt = JUDGE_TMPL.format(topics=topics, text=text[:400])
        if hits:
            prompt += f"\n（提醒：这句命中了你的兴趣词：{'、'.join(hits[:5])}）"

        try:
            resp = await provider.text_chat(
                prompt=prompt,
                contexts=[],
                system_prompt=JUDGE_SYSTEM,
            )
            raw = (getattr(resp, "completion_text", "") or "").strip()
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[interest] 打分请求失败 {type(e).__name__}: {e}")
            return 0, "打分失败"

        m = _SCORE_RE.search(raw)
        if not m:
            logger.warning(f"[interest] 打分返回无法解析: {raw[:80]}")
            return 0, "解析失败"
        score = max(0, min(10, int(m.group(1))))
        reason = ""
        try:
            reason = re.search(r'"(?:why|reason)"?\s*[:：]\s*"([^"]{0,40})"', raw).group(1)
        except Exception:
            reason = raw[:20]
        return score, reason

    # ---------------- 引用回复：核心会漏掉语音回复，这里补上 ----------------
    @filter.on_decorating_result()
    async def ensure_quote(self, event: AstrMessageEvent):
        """AstrBot 自带的引用回复只在「纯文本/图文」时生效；
        它的语音回复（Record）会被判定为不可装饰 → 从来不引用。
        这里不挑类型，群聊里一律补一个 Reply 引用。"""
        try:
            if not bool(self.cfg("quote_reply")):
                return
            if event.is_private_chat() and not bool(self.cfg("quote_private")):
                return
            result = event.get_result()
            if not result or not result.chain:
                return
            mid = getattr(event.message_obj, "message_id", None)
            if not mid:
                return
            if any(isinstance(c, Reply) for c in result.chain):
                return
            result.chain.insert(0, Reply(id=mid))
            if bool(self.cfg("debug")):
                logger.info(
                    "[interest] 已补引用(%s) id=%s 链=%s"
                    % (
                        "私聊" if event.is_private_chat() else "群聊",
                        mid,
                        [type(c).__name__ for c in result.chain],
                    )
                )
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[interest] 引用回复补丁失败 {type(e).__name__}: {e}")

    # ---------------- 指令：/兴趣 ----------------
    @filter.command("兴趣")
    async def interest_cmd(self, event: AstrMessageEvent, sub: str = ""):
        """查看/调整兴趣接话。用法：/兴趣 · /兴趣 开 · /兴趣 阈值 7 · /兴趣 测试 这句话"""
        sub = (sub or "").strip()
        args = sub.split()

        if not args:
            yield event.plain_result(self._status_text())
            return

        head = args[0]
        if head in ("开", "on"):
            self._save_local({"enable": True})
            yield event.plain_result("好，我开始自己找话说了。（阈值 %s 分，每小时最多 %s 条）"
                                     % (self.cfg("threshold"), self.cfg("hourly_limit")))
            return
        if head == "引用" and len(args) > 1:
            on = args[1] in ("开", "on", "1", "true", "是")
            self._save_local({"quote_reply": on})
            yield event.plain_result("群聊回复时%s引用对方那条消息。" % ("会" if on else "不再"))
            return
        if head == "私聊引用" and len(args) > 1:
            on = args[1] in ("开", "on", "1", "true", "是")
            self._save_local({"quote_private": on})
            yield event.plain_result("私聊回复时%s引用你的消息。" % ("会" if on else "不再"))
            return
        if head in ("关", "off"):
            self._save_local({"enable": False})
            yield event.plain_result("行，那我闭嘴，只在被叫的时候说话。")
            return
        if head == "阈值" and len(args) > 1:
            try:
                v = max(1, min(10, int(args[1])))
            except ValueError:
                yield event.plain_result("阈值要写 1-10 的数字，比如 /兴趣 阈值 7")
                return
            self._save_local({"threshold": v})
            yield event.plain_result(f"阈值改成 {v} 分了（越高越挑剔）。")
            return
        if head == "冷却" and len(args) > 1:
            try:
                v = max(10, int(args[1]))
            except ValueError:
                yield event.plain_result("冷却写秒数，比如 /兴趣 冷却 120")
                return
            self._save_local({"cooldown_sec": v})
            yield event.plain_result(f"两次插话至少隔 {v} 秒。")
            return
        if head == "上限" and len(args) > 1:
            try:
                v = max(1, min(60, int(args[1])))
            except ValueError:
                yield event.plain_result("上限写数字，比如 /兴趣 上限 8")
                return
            self._save_local({"hourly_limit": v})
            yield event.plain_result(f"每小时最多主动说 {v} 次。")
            return
        if head == "话题" and len(args) > 1:
            words = [w.strip() for w in re.split(r"[,，、\s]+", sub[len(head):]) if w.strip()]
            if not words:
                yield event.plain_result("话题用逗号隔开，比如 /兴趣 话题 游戏,番剧,猫")
                return
            old = list(self.cfg("topics") or [])
            merged = list(dict.fromkeys(old + words))
            self._save_local({"topics": merged})
            yield event.plain_result(f"加上了：{'、'.join(words)}\n现在兴趣牌共 {len(merged)} 个。")
            return
        if head == "测试" and len(args) > 1:
            sample = sub[len(head):].strip()
            score, why = await self._judge(event, sample, [])
            need = int(self.cfg("threshold") or 6)
            verdict = "会插话" if score >= need else "不会理"
            yield event.plain_result(
                f"「{sample[:40]}」→ {score}/10（{why}）\n阈值 {need}，所以{verdict}。"
            )
            return

        yield event.plain_result("看不懂。用法：/兴趣、/兴趣 开|关、/兴趣 阈值 7、"
                                 "/兴趣 冷却 120、/兴趣 上限 8、/兴趣 话题 游戏,猫、"
                                 "/兴趣 引用 开|关、/兴趣 测试 一句话")

    def _status_text(self) -> str:
        now = time.time()
        recent = [t for t in self._recent if now - t < 3600]
        last = ("%.0f 秒前" % (now - self._last_reply)) if self._last_reply else "还没插过话"
        return (
            "兴趣接话：%s\n"
            "阈值 %s/10 · 冷却 %ss · 上限 %s 条/时\n"
            "兴趣牌 %s 个：%s\n"
            "生效群：%s\n"
            "本小时已插 %s 条 · 累计 %s 条 · 上次 %s"
            % (
                "开" if self.cfg("enable") else "关",
                self.cfg("threshold"),
                self.cfg("cooldown_sec"),
                self.cfg("hourly_limit"),
                len(self.cfg("topics") or []),
                "、".join(str(k) for k in (self.cfg("topics") or [])[:12]),
                ",".join(str(g) for g in (self.cfg("groups") or [])) or "所有群",
                "开" if self.cfg("quote_reply") else "关",
                "开" if self.cfg("quote_private") else "关",
                len(recent),
                self._total,
                last,
            )
        )
