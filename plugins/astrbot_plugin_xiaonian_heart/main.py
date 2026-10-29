# -*- coding: utf-8 -*-
"""情绪与主动 · 会等你的人

干三件事：
1. 记住「他多久没回我」「他说了什么扎心的话」，形成焦虑/心情/亲近度；
2. 等不到人就自己去找他（分级，越等越难受）；
3. 每一轮对话都把当前心情喂给她，让她说话带情绪，而不是永远好脾气。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from . import mood as _mood
from .proactive import Proactive

HEART_MARK = "【你此刻的心情】"


@register(
    "astrbot_plugin_xiaonian_heart",
    "qqjqr",
    "情绪与主动：会等你、会慌、会自己来找你",
    "0.1.0",
)
class HeartPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = Path(__file__).resolve().parents[2] / "plugin_data" / "astrbot_plugin_xiaonian_heart"
        self.mood_path = self.data_dir / "mood.json"
        self._task: asyncio.Task | None = None
        self.proactive = Proactive(self)

    # ---------- 配置便捷读取 ----------
    @property
    def master_qq(self) -> int:
        return int(self.config.get("master_qq", 0) or 0)

    @property
    def target_umo(self) -> str:
        return str(
            self.config.get("target_umo")
            or f"snowluma-onebot:FriendMessage:{self.master_qq}"
        )

    def effective_intensity(self) -> str:
        """运行时档位优先（/黏我 改过的），否则用配置。"""
        try:
            st = _mood.load(self.mood_path)
            ov = str(st.get("intensity_override") or "").strip()
            if ov:
                return ov
        except Exception:
            pass
        return str(self.config.get("intensity", "heavy") or "heavy")

    def enabled(self) -> bool:
        return self.effective_intensity() != "off" and bool(self.config.get("enable", True))

    # ---------- 生命周期 ----------
    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        st = _mood.load(self.mood_path)
        self._task = asyncio.create_task(self.proactive.loop())
        logger.info(
            f"[heart] 已启动：黏度 {self.effective_intensity()}，"
            f"焦虑 {st.get('anxiety', 0):.0f}，心情 {st.get('mood', 0):+.1f}，"
            f"目标 {self.target_umo}"
        )

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()
        logger.info("[heart] 已停止")

    # ---------- 每轮对话：更新状态 + 注入心情 ----------
    @filter.on_llm_request(priority=6)
    async def track_and_mood(self, event: AstrMessageEvent, req) -> None:
        try:
            if not self.enabled():
                return
            umo = str(getattr(event, "unified_msg_origin", "") or "")
            # 只处理主人私聊（群聊不介入，避免占群友的便宜）
            is_target = umo == self.target_umo or str(event.get_sender_id()) == str(self.master_qq)
            if not is_target:
                return
            if getattr(event, "get_group_id", lambda: None)():
                return

            text = (event.get_message_str() or "").strip()
            if not text:
                return

            now = time.time()
            st = _mood.load(self.mood_path)
            before_shock = now < float(st.get("shock_until", 0) or 0)
            st = _mood.on_user_message(st, text, now)
            shocked = now < float(st.get("shock_until", 0) or 0) and not before_shock
            _mood.save(self.mood_path, st)

            # 把心情拼在 prompt 末尾（很短，不碰 system，避免历史膨胀）
            brief = _mood.mood_brief(st, now)
            extra = f"\n\n{HEART_MARK}{brief}"
            if shocked:
                extra += "。他刚说了让我怕的话，我有点发抖，说话会比平时软，也可能嘴硬装没事"
            try:
                req.prompt = (req.prompt or "") + extra
            except Exception:
                pass

            if st.get("anxiety", 0) >= 60 or shocked:
                logger.info(f"[heart] 心情变化：焦虑 {st.get('anxiety', 0):.0f}，心情 {st.get('mood', 0):+.1f}")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[heart] 情绪更新失败：{e}")

    # ---------- 命令 ----------
    @filter.command("心情")
    async def cmd_mood(self, event: AstrMessageEvent):
        """看她的情绪状态。"""
        st = _mood.tick(_mood.load(self.mood_path))
        yield event.plain_result(
            "她现在的状态：\n"
            f"焦虑 {st.get('anxiety', 0):.0f}/100\n"
            f"心情 {st.get('mood', 0):+.1f}（-5 到 +5）\n"
            f"亲近度 {st.get('closeness', 50):.0f}/100\n"
            f"已经主动找过你 {st.get('sent_today', 0)} 次\n"
            f"上次互动 {int((time.time() - float(st.get('last_user_msg_at', 0) or 0)) / 60)} 分钟前\n"
            f"黏度档位：{self.effective_intensity()}"
        )

    @filter.command("黏我")
    async def cmd_intensity(self, event: AstrMessageEvent, level: str = ""):
        """调黏人程度：/黏我 轻|中|重|关"""
        mapping = {"轻": "light", "中": "normal", "重": "heavy", "关": "off",
                   "light": "light", "normal": "normal", "heavy": "heavy", "off": "off"}
        key = mapping.get(str(level).strip())
        if not key:
            yield event.plain_result("用法：/黏我 轻|中|重|关")
            return
        st = _mood.load(self.mood_path)
        st["intensity_override"] = key
        _mood.save(self.mood_path, st)
        names = {"light": "轻（半小时才找你）", "normal": "中（15 分钟）",
                 "heavy": "重（8 分钟就开始念）", "off": "关（不再主动找你）"}
        yield event.plain_result(f"黏度已改成：{names[key]}")

    @filter.command("哄")
    async def cmd_coax(self, event: AstrMessageEvent):
        """把她哄好：焦虑清零、心情回正。"""
        st = _mood.load(self.mood_path)
        st["anxiety"] = 0.0
        st["mood"] = min(5.0, float(st.get("mood", 0)) + 3.0)
        st["closeness"] = min(100.0, float(st.get("closeness", 50)) + 8.0)
        st["stage"] = 0
        st["ignored_streak"] = 0
        st["shock_until"] = 0.0
        st["last_user_msg_at"] = time.time()
        _mood.save(self.mood_path, st)
        yield event.plain_result("哄好了。焦虑 0，心情回暖，她暂时不慌了。")

    @filter.command("主动")
    async def cmd_force(self, event: AstrMessageEvent):
        """立刻让她主动发一条（测试用）。"""
        yield event.plain_result("让她现在就来找你，稍等…")
        st = _mood.load(self.mood_path)
        st = _mood.tick(st)
        text = await self.proactive.generate(st, 2)
        if not text:
            yield event.plain_result("生成失败，检查模型配置。")
            return
        ok = await self.proactive.send(text)
        if ok:
            st = _mood.on_bot_proactive(st, text, 2)
            _mood.save(self.mood_path, st)
            yield event.plain_result(f"已发出：{text}")
        else:
            yield event.plain_result("发送失败，看日志。")

    @filter.command("heart")
    async def cmd_help(self, event: AstrMessageEvent):
        """情绪插件帮助。"""
        yield event.plain_result(
            "情绪与主动\n"
            "/心情 —— 看她现在的焦虑、心情、亲近度\n"
            "/黏我 轻|中|重|关 —— 调她多黏人\n"
            "/哄 —— 把她哄好（焦虑清零）\n"
            "/主动 —— 立刻让她来找你（测试）"
        )
