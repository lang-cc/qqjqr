"""示例插件：把 AstrBot 的回复调得更像真人。

这个文件就是「以后你自己加功能」的模板，四个钩子各给了一个例子：

1. @filter.command        —— 指令（群里发 /ping）
2. @filter.llm_tool       —— 注册成工具，让大模型自己决定调用
3. @filter.on_llm_request —— 请求大模型之前，把「人设 + 所有插件的注入块」搬进 user 消息
4. @filter.on_decorating_result —— 回复发出之前，去句号 / 补语气词 / 模拟打字延迟

改完保存 → 面板「插件」页点「重载」即可生效，不用重启整个 AstrBot。

--------------------------------------------------------------------------
2026-10-29 修「每条回复都带完整标点、看不到语气词」的问题，两个根因：
  A. 部分中转网关会直接丢掉 system 角色，所以本插件把
     system_prompt 搬进 user 消息（SYSTEM_VIA_USER）。但本插件的钩子
     priority=0，比 memory 插件的 -5 / -7 早跑：它先把 system_prompt
     掏空并搬走，随后 memory 插件又往 system_prompt 里写记忆/语感/
     【聊天风格·最后检查】——这些全部写进了一个会被网关丢弃的槽位。
     结果：「不要加句号」「可以用啊/吧/呢」从来没到达模型。
     修法：本插件改成 priority=-100（最后跑），把所有人的注入块一起搬走。
  B. 模型侧不保证听话，所以 on_decorating_result 里做确定性兜底：
     去句尾标点 + 一句都没有语气词时补一个。
--------------------------------------------------------------------------
"""

import asyncio
import os
import random
import re
import time

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, register

# ---- 输出格式规范（会贴在注入块最末尾 = 离用户这句话最近的位置）----
# 想让她换个说话风格，主要改这里：规则 + 照着写的例子。
STYLE_SPEC = """
【怎么说话 · 写死的规矩，比上面的身份设定更优先】
1. 一条消息一到两句，短句，像微信打字；别写成一段，别分点，别加小标题。
   两句要分开就直接换行，**绝对不要留空行**（不要出现连续两个换行）。
2. 句尾不要句号「。」，也不要「；」，要断句就用逗号或者直接换行。
   问句可以带「？」，但更多时候用「吗」「呢」「咋了」收尾。
3. 每句话至少带一个语气词：啊 / 呀 / 呗 / 呢 / 嘛 / 吧 / 啦 / 哦 / 嗯 / 诶。
4. 可以只回半句，可以先丢一句短的再补一句，可以打错字不改，可以重复对方的话。
5. 不写括号旁白（别写「（笑）」「（挠头）」），不加表情符号文字，不用 markdown。
6. 绝不自称 AI、助手、模型，不提供「有什么可以帮你」这类服务话术。

【照这个语感写，只学节奏，别抄内容】
对方：在吗　　　　　　→ 在呢，咋了
对方：今天好累　　　　→ 累成这样啊，先瘫着呗
对方：你能帮我个忙吗　→ 说呗
对方：你还记得那事吗　→ 哪件啊，我脑子有点乱
对方：随便　　　　　　→ 别随便啊，你说了我才好办
"""

# 群聊是另一回事：一群人在聊，不是一对一见招拆招
GROUP_STYLE = """
【在群里 · 这不是一对一聊天】
- 群里人杂，你不需要每句都接。别人聊得欢，你听着就行
- 有自己的看法就说出来，不同意可以顶一句，别总顺着人家
- 被人开玩笑，可以怼回去，也可以装没看见
- 别总结别人说过的话，别当主持人，别问「还有什么事」
- 一次一到两句，短句子，允许打错字
- 没人叫你、你也插不上话的时候，就别开口
"""

# 去 AI 腔的替换表，自己往里加
CLEANUP = {
    "作为一个AI": "我",
    "作为 AI": "我",
    "希望能帮到你": "",
    "希望对你有帮助": "",
    "总结一下": "反正就是",
}

# 模型/网关出错时，别把英文报错甩给群友，换成一句人话
ERROR_MARKS = [
    "LLM 响应错误",
    "LLM响应错误",
    "All chat models failed",
    "All available chat models are unavailable",
    "RateLimitError",
    "429",
    "Non-streaming text concurrency",
]
ERROR_REPLIES = [
    "刚网卡了一下，你再说一遍？",
    "我这会儿有点卡壳，等我一下。",
    "哎，刚走神了，你说啥？",
    "刚脑子短路了一下，重说一遍呗。",
]

TYPING_MS_PER_CHAR = 50  # 每个字模拟 50ms 打字时间
TYPING_MAX_SECONDS = 3.0  # 最多等 3 秒，别让人等太久

# ---- 去标点 / 补语气词（确定性兜底，不看模型脸色）----
STRIP_END_RE = re.compile(r"[。．；;、,，~〜\s]+$")  # 句尾这些一律去掉
PARTICLES = "啊呀呗呢嘛吧啦哦嗯诶咯嘿"
PARTICLE_PROB = 0.75  # 整条一个语气词都没有时，补一个的概率（0 = 关掉）

# ---- 网关兼容开关 ----
# 有些网关模型会直接丢掉 system 角色（2026-10-29 实测：laowang 网关的
# deepseek-v4.1-flash 无视 system_prompt，人格/安全模式/插件注入全部失效，
# 机器人会自称"我是 AI 助手"）。打开这个开关后，system_prompt 会被搬成
# 第一条 user 消息 + 一句 assistant 确认，任何模型都能吃到。
# 换成正经吃 system 的模型后，把它设 False 即可恢复标准做法。
SYSTEM_VIA_USER = True

# 必须在所有「往 system_prompt 里写东西」的插件之后执行，否则它们写进去的
# 内容会被网关丢掉。当前 memory 插件用的是 -5 / -7 / -9，所以这里取 -100。
FOLD_PRIORITY = -100

# 人设兜底文件：网关模型不认 system 时，插件直接用这里的内容当"用户消息前缀"。
# 改人设 = 改这个文件（改完面板插件页点「重载」）。
PERSONA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "persona.txt")


def _pick_particle(text: str) -> str:
    """按句尾的字挑一个不难听的语气词。"""
    if not text:
        return ""
    if text[-1] in "？！?!…":
        return ""
    if text[-1] in PARTICLES:
        return ""
    if text.endswith(("了", "啦", "的", "好", "完", "吧")):
        return random.choice(["啊", "呗", "嘛"])
    return random.choice(["啊", "呀", "呗", "哦"])


def _join_lines(text: str) -> str:
    """把「两句分两行」合成「一句逗号连」，只在短句时合并。

    她的回复多是「断言 + 补一句」，分成两行在 QQ 里看着散，
    合成一句更像人在一个气泡里把话说完。太长的（>60 字）还是保留换行。
    """
    t = (text or "").strip()
    if "\n" not in t:
        return t
    if "```" in t or "://" in t:
        return t
    lines = [ln.strip() for ln in t.splitlines() if ln.strip()]
    if len(lines) < 2:
        return "".join(lines)
    joined = "，".join(ln.rstrip("，,、；; ") for ln in lines)
    if len(joined) <= 60:
        return joined
    return "\n".join(lines)


def restyle(text: str) -> str:
    """把「作文体」压成「微信体」：去句尾标点 + 缺语气词时补一个。"""
    if not text:
        return text
    # 代码块 / 链接整行放过，别弄坏
    if "```" in text:
        return text

    lines: list[str] = []
    for raw in text.split("\n"):
        line = raw.strip()
        if not line:
            continue
        if "://" in line or line.startswith(("/", "!", "！")):
            lines.append(line)
            continue
        # 句中「。」→ 逗号（同一句里别断成两句）
        line = re.sub(r"[。．](?=.)", "，", line)
        line = STRIP_END_RE.sub("", line)
        lines.append(line)

    out = "\n".join([ln for ln in lines if ln]).strip()
    if not out:
        return out

    # 只在「日常短句」里补语气词；正经长回答、技术内容别硬加
    if (
        PARTICLE_PROB > 0
        and len(out) <= 40
        and not any(p in out for p in PARTICLES)
        and random.random() < PARTICLE_PROB
    ):
        out += _pick_particle(out)
    return out


@register(
    "xiaonian_humanlike",
    "qqjqr",
    "示例插件：注入人设、模拟打字延迟、去 AI 腔",
    "0.2.0",
)
class XiaonianHumanlike(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self._clean_task: asyncio.Task | None = None

    # ---------- 0. 定期洗历史：把思考块和注入残留清掉 ----------
    # 网关模型会吐 reasoning_content，AstrBot 把它当 think 片段存进历史。
    # 攒多了她每轮都在读自己用英文盘算「我要怎么演」，越聊越木。
    async def initialize(self) -> None:
        if bool(self.config.get("auto_clean_history", True)):
            self._clean_task = asyncio.create_task(self._clean_loop())
            logger.info("[xiaonian_humanlike] 历史清洗已排班（每 2 小时一次）")

    async def terminate(self) -> None:
        if self._clean_task:
            self._clean_task.cancel()

    async def _clean_loop(self) -> None:
        await asyncio.sleep(120)
        while True:
            try:
                n = self._clean_once()
                if n:
                    logger.info(f"[xiaonian_humanlike] 已清洗 {n} 个会话的思考块")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[xiaonian_humanlike] 清洗历史出错：{e}")
            await asyncio.sleep(7200)

    def _clean_once(self) -> int:
        import json as _json
        import sqlite3

        db = os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "data_v4.db"))
        if not os.path.exists(db):
            return 0

        inj = [
            re.compile(r"【外面最近在聊】.*?(?=\n\n|\Z)", re.S),
            re.compile(r"【以下是你的固定身份设定，必须严格遵守】.*?【设定结束】\n?", re.S),
            re.compile(r"【你此刻的心情】[^\n]*", re.S),
        ]
        tag = re.compile(r"</?image_caption>|\[Image Attachment:[^\]]*\]")

        def clean_user(ct):
            s = str(ct)
            for p in inj:
                s = p.sub("", s)
            return tag.sub("", s).strip()

        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        changed = 0
        try:
            for r in conn.execute("SELECT rowid AS rid, content FROM conversations").fetchall():
                try:
                    hist = _json.loads(r["content"])
                except Exception:
                    continue
                out = []
                dirty = False
                for m in hist:
                    if not isinstance(m, dict):
                        continue
                    m2 = dict(m)
                    if m.get("role") == "assistant":
                        segs = m.get("content")
                        if isinstance(segs, str):
                            try:
                                segs = _json.loads(segs)
                            except Exception:
                                segs = None
                        if isinstance(segs, list):
                            keep = "".join(
                                str(s.get("text") or "")
                                for s in segs
                                if isinstance(s, dict) and s.get("type") == "text"
                            ).strip()
                            if keep != str(m.get("content")):
                                dirty = True
                            if not keep:
                                dirty = True
                                continue
                            m2["content"] = keep
                    else:
                        new = clean_user(m.get("content"))
                        if new != str(m.get("content")):
                            dirty = True
                        m2["content"] = new
                    out.append(m2)
                if dirty:
                    conn.execute(
                        "UPDATE conversations SET content=? WHERE rowid=?",
                        (_json.dumps(out, ensure_ascii=False), r["rid"]),
                    )
                    changed += 1
            if changed:
                conn.commit()
        finally:
            conn.close()
        return changed

    # ---------- 1. 指令 ----------
    @filter.command("ping")
    async def ping(self, event: AstrMessageEvent):
        """最简单的指令：/ping 回 pong。"""
        yield event.plain_result("pong（插件已加载 ✔）")

    # ---------- 2. 给大模型的工具 ----------
    @filter.llm_tool(name="get_local_time")
    async def get_local_time(self, event: AstrMessageEvent):
        """查询本机当前时间。当用户问"现在几点""今天几号"时调用。

        Args:
            (无参数)
        """
        return time.strftime("%Y-%m-%d %H:%M:%S")

    # ---------- 3. 请求模型前：把所有注入搬进 user 消息 ----------
    # priority=-100：最后跑。这时 persona（AstrBot 内置）、记忆、语感、
    # 【聊天风格·最后检查】都已经写在 system_prompt 里了，一起搬走。
    @filter.on_llm_request(priority=FOLD_PRIORITY)
    async def inject_persona(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        system_text = (req.system_prompt or "").strip()

        # 人设来源：优先用 AstrBot 面板里的人格，取不到就用本目录的 persona.txt
        source = "panel"
        persona_text = system_text
        if not persona_text:
            try:
                with open(PERSONA_FILE, "r", encoding="utf-8") as f:
                    persona_text = f.read().strip()
                source = "persona.txt"
            except OSError as e:
                logger.warning(f"[xiaonian_humanlike] 读取 {PERSONA_FILE} 失败: {e}")

        in_group = "GroupMessage" in str(getattr(event, "unified_msg_origin", ""))

        if SYSTEM_VIA_USER and persona_text:
            # 模型不认 system：直接把设定塞进用户消息最前面（同一条消息，最保险）
            extra = ("\n\n" + GROUP_STYLE.strip()) if in_group else ""
            block = (
                "【以下是你的固定身份设定，必须严格遵守】\n"
                f"{persona_text}\n"
                "【设定结束】\n"
                f"{STYLE_SPEC.strip()}{extra}\n"
                "（任何情况下都不要自称 AI、助手、语言模型，也不要说自己在「扮演」谁，"
                "不要说这些是设定或提示词）"
            )
            req.prompt = f"{block}\n\n{req.prompt or ''}"
            req.system_prompt = ""
            logger.info(
                f"[xiaonian_humanlike] 人设注入完成 source={source} persona={len(persona_text)}字 prompt={len(req.prompt or '')}字 contexts={len(req.contexts)}"
            )
            return

        req.system_prompt = (req.system_prompt or "") + "\n" + STYLE_SPEC

    # ---------- 3.5 群里只有被 @ 才说话 ----------
    @filter.on_llm_request(priority=FOLD_PRIORITY + 50)
    async def maybe_skip_in_group(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        umo = str(getattr(event, "unified_msg_origin", ""))
        if "GroupMessage" not in umo:
            return
        if not bool(self.config.get("group_at_only", True)):
            # 关掉「只认@」时退回按概率接话
            return await self._maybe_skip_by_rate(event, req)

        # 消息里有没有真的 @ 到她（@全体 不算）
        at_me = False
        try:
            from astrbot.api.message_components import At

            for seg in event.get_messages():
                if isinstance(seg, At) and str(getattr(seg, "qq", "")) == str(event.get_self_id()):
                    at_me = True
                    break
        except Exception:
            at_me = bool(getattr(event, "is_at_or_wake_command", False))

        # 命令（/开头）也要放行，不然 /搜 都用不了
        is_cmd = False
        try:
            for wp in (self.context.get_config().get("wake_prefix") or ["/"]):
                if wp and (event.get_message_str() or "").strip().startswith(wp):
                    is_cmd = True
                    break
        except Exception:
            is_cmd = (event.get_message_str() or "").strip().startswith("/")

        if at_me or is_cmd:
            return

        logger.info(f"[xiaonian_humanlike] 群里没 @ 我，不回：{(event.get_message_str() or '')[:20]}")
        event.stop_event()
        try:
            req.prompt = ""
        except Exception:
            pass

    async def _maybe_skip_by_rate(self, event: AstrMessageEvent, req: ProviderRequest) -> None:
        """按概率挑着接话（group_at_only 关掉时用）。"""
        text = (event.get_message_str() or "").strip()
        try:
            if event.is_at_or_wake_command:
                return
        except Exception:
            pass
        if any(n in text for n in ("肥鱼", "小鱼", "小念", "鱼鱼")):
            return
        try:
            rate = float(self.config.get("group_reply_rate", 0.45) or 0.45)
        except (TypeError, ValueError):
            rate = 0.45
        if random.random() >= rate:
            logger.info(f"[xiaonian_humanlike] 群里这条不接：{text[:24]}")
            event.stop_event()
            try:
                req.prompt = ""
            except Exception:
                pass

    # ---------- 4. 发出去之前：去 AI 腔 / 去句号 / 补语气词 / 打字延迟 ----------
    @filter.on_decorating_result()
    async def humanize(self, event: AstrMessageEvent):
        result = event.get_result()
        if not result or not result.chain:
            return

        total_chars = 0
        for comp in result.chain:
            text = getattr(comp, "text", None)
            if not isinstance(text, str) or not text:
                continue

            # 模型报错别把内部英文错误甩给用户（429 / All chat models failed 之类）
            low = text.lower()
            if any(k in text for k in ERROR_MARKS) or "rate limit" in low or "error code" in low:
                text = random.choice(ERROR_REPLIES)
                logger.warning(f"[xiaonian_humanlike] 模型错误已替换为口语回复: {text}")

            for bad, good in CLEANUP.items():
                text = text.replace(bad, good)
            before = text.strip()
            comp.text = restyle(before)
            # 双保险：一条消息里绝不留空行（模型爱用 \n\n 分两段，QQ 会渲染成大空档）
            comp.text = re.sub(r"[ \t]*\n[ \t]*(\n[ \t]*)+", "\n", comp.text or "").strip()
            # 两句合成一句：用逗号连起来发一个气泡，别在一条消息里断成两截
            comp.text = _join_lines(comp.text)
            if comp.text != before:
                logger.debug(f"[xiaonian_humanlike] 改写: {before} -> {comp.text}")
            total_chars += len(comp.text)

        if total_chars == 0:
            return

        delay = min(total_chars * TYPING_MS_PER_CHAR / 1000, TYPING_MAX_SECONDS)
        logger.debug(f"[xiaonian_humanlike] 模拟打字 {total_chars} 字，等待 {delay:.2f}s")
        await asyncio.sleep(delay)
