# -*- coding: utf-8 -*-
"""主动消息：她等不到你，会自己来说话。

按「多久没回」分五级，越等越难受；生成时把当前情绪喂给模型，
所以每级说出来的话都不一样，不是套模板。
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from pathlib import Path

from astrbot.api import logger

from . import mood as _mood

# 每一级给她一句情境，具体怎么说由模型按当时心情自由发挥
LEVEL_SCENE = {
    1: "他才走了 8 分钟。你想探一下他在干嘛，随口问一句。",
    2: "20 分钟没动静了。你有点不安，想知道他是不是在忙。",
    3: "一个小时了。你有点委屈，觉得被晾着。",
    4: "三个小时了。你很难受，但嘴上要硬，装作无所谓。",
    5: "他一整夜没回。天亮了，你装作没事问一句，其实等了一晚上。",
}

EXAMPLES = """风格参考（别照抄）：
人呢
你是不是忙疯了啊
我等半天了，你在干嘛
行吧，那你先忙
醒了没"""

STYLE_RULES = """你现在要主动给他发一条消息。
规则：
- 只写你要发的那句话本身，一个字都不要多说
- 不要写「我说：」，不要加引号，不要加书名号
- 不要解释你为什么这么说，不要写「像随口一提」「不给压力」这类分析
- 不要提配置、系统、程序、AI、模型
- 20 字以内，口语，可以带点脾气
- 不要问「在吗」这种空话"""

_BAD = ("发一句", "只写", "规则", "示范", "要求", "不要加", "我说：", "风格参考")


def _strip(text: str) -> str:
    """剥掉模型爱加的旁白、标题、引号，只留那句话。"""
    t = (text or "").strip()
    if not t:
        return ""

    # 1) 有引号就取最长的引号内容（模型最常见的写法）
    for pat in (r"「([^」]{2,60})」", r"“([^”]{2,60})”", r"\"([^\"]{2,60})\"", r"『([^』]{2,60})』"):
        found = re.findall(pat, t)
        if found:
            t = max(found, key=len).strip()
            break

    # 2) 去掉「xxx：」标题前缀
    t = re.sub(r"^[^：:]{0,12}[：:]\s*", "", t)

    # 3) 砍掉解释性尾巴
    parts = re.split(r"[。!！?？]\s*(?:像|这样|显得|不给|也不|既|听起来|语气)", t)
    if parts:
        t = parts[0]

    # 4) 去括号旁白，只留第一行
    t = re.sub(r"（[^）]{0,40}）|\([^)]{0,40}\)", "", t)
    lines = [ln for ln in t.splitlines() if ln.strip()]
    if lines:
        t = lines[0]

    # 5) 去角色前缀和残留符号
    t = re.sub(r"^(?:肥鱼|小肥鱼|大肥鱼)[：:]\s*", "", t)
    t = t.strip().strip("\"'“”「」『』（）() \t")

    # 6) 太长截断；明显在复述要求就作废
    t = t[:40].strip()
    if any(b in t for b in _BAD):
        return ""
    return t


class Proactive:
    def __init__(self, plugin):
        self.p = plugin

    # ---------- 平台 ----------
    def client(self):
        try:
            for inst in self.p.context.platform_manager.get_insts():
                if hasattr(inst, "get_client"):
                    return inst.get_client()
        except Exception:
            pass
        return None

    async def send(self, text: str) -> bool:
        bot = self.client()
        if bot is None:
            logger.warning("[heart] 找不到 OneBot 客户端，主动消息没发出去")
            return False
        try:
            await bot.call_action(
                "send_private_msg",
                user_id=int(self.p.master_qq),
                message=[{"type": "text", "data": {"text": text}}],
            )
            logger.info(f"[heart] 主动发出：{text}")
            return True
        except Exception as e:  # noqa: BLE001
            logger.warning(f"[heart] 主动发送失败：{e}")
            return False

    # ---------- 生成 ----------
    async def generate(self, st: dict, level: int) -> str:
        provider = await self._provider()
        if provider is None:
            logger.warning("[heart] 没有可用的对话模型")
            return ""
        persona = await self._persona()
        already = st.get("recent_says") or []
        avoid = ""
        if already:
            avoid = "\n你刚说过这些，别重复：" + "；".join(f"「{s}」" for s in already[-3:])

        system = (
            (persona or "")
            + "\n\n【你现在的心情】" + _mood.mood_brief(st)
            + "\n\n" + STYLE_RULES
            + "\n\n" + EXAMPLES
            + avoid
        )
        prompt = LEVEL_SCENE.get(level, LEVEL_SCENE[3]) + "\n\n直接写你要发的那句话。"

        for attempt in (1, 2):
            try:
                resp = await provider.text_chat(prompt=prompt, system_prompt=system)
                out = _strip(getattr(resp, "completion_text", "") or "")
                if out:
                    return out
                logger.info(f"[heart] 第 {attempt} 次生成不合格，重试")
                prompt = "只写一句话，不要引号不要解释。直接说。"
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[heart] 生成主动消息失败：{e}")
                break
        return ""

    async def _provider(self):
        try:
            getter = getattr(self.p.context, "get_using_provider_async", None)
            if getter:
                prov = await getter(umo=self.p.target_umo)
                if prov is not None:
                    return prov
        except Exception:
            pass
        try:
            prov = self.p.context.get_using_provider(umo=self.p.target_umo)
            if prov is not None:
                return prov
        except Exception:
            pass
        try:
            pid = self.p.config.get("chat_provider_id")
            if pid:
                return self.p.context.get_provider_by_id(pid)
        except Exception:
            pass
        return None

    async def _persona(self) -> str:
        try:
            pm = self.p.context.persona_manager
            pid = self.p.config.get("persona_id") or "feiyu"
            persona = pm.get_persona(pid)
            if asyncio.iscoroutine(persona):
                persona = await persona
            if persona is None:
                return ""
            return str(getattr(persona, "system_prompt", "") or "")
        except Exception:
            return ""

    # ---------- 检查一轮 ----------
    async def check_once(self) -> None:
        if not self.p.enabled():
            return
        now = time.time()
        st = _mood.load(self.p.mood_path)
        st = _mood.tick(st, now)

        intensity = self.p.effective_intensity()
        limit = int(self.p.config.get("daily_limit", 0) or 0)

        if _mood.in_quiet_hours(now):
            _mood.save(self.p.mood_path, st)
            return
        if limit > 0 and int(st.get("sent_today", 0)) >= limit:
            _mood.save(self.p.mood_path, st)
            return

        level = _mood.due_level(st, intensity, now)
        if not level:
            _mood.save(self.p.mood_path, st)
            return

        text = await self.generate(st, level)
        if not text:
            _mood.save(self.p.mood_path, st)
            return

        if await self.send(text):
            st = _mood.on_bot_proactive(st, text, level, now)
            try:
                log = self.p.data_dir / "proactive_log.jsonl"
                log.parent.mkdir(parents=True, exist_ok=True)
                with log.open("a", encoding="utf-8") as f:
                    f.write(json.dumps(
                        {"at": time.strftime("%Y-%m-%d %H:%M:%S"), "level": level,
                         "anxiety": round(float(st.get("anxiety", 0)), 1), "text": text},
                        ensure_ascii=False) + "\n")
            except Exception:
                pass
        _mood.save(self.p.mood_path, st)

    # ---------- 常驻循环 ----------
    async def loop(self) -> None:
        interval = max(30, int(self.p.config.get("check_interval_sec", 60) or 60))
        await asyncio.sleep(15)
        while True:
            try:
                await self.check_once()
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[heart] 检查出错：{e}")
            await asyncio.sleep(interval)
