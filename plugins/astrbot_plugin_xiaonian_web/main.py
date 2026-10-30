# -*- coding: utf-8 -*-
"""联网 · 会查东西、也知道外面在聊什么的她

四件事：
1. web_search / read_page：她自己判断要查什么，查完用自己的话说；
2. bili_search / bili_hot：B站站内搜视频、看热门榜/排行榜/热搜词（走官方 API，不用 key）；
3. 每天定时学一轮热搜、热梗、时事，存在 news.json（不进记忆库）；
4. 聊天时轻轻注入「外面最近在聊」，让她接得上话，但不播新闻。
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from . import news as _news
from .web import (
    bili_hot_search,
    bili_popular,
    bili_ranking,
    bili_search,
    bili_search_text,
    bing_search,
    fetch_text,
)

INJECT_MARK = "【外面最近在聊】"


@register(
    "astrbot_plugin_xiaonian_web",
    "qqjqr",
    "联网搜索、网页读取、B站查询，以及每天自动学热搜热梗时事",
    "0.3.0",
)
class WebSearchPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = Path(__file__).resolve().parents[2] / "plugin_data" / "astrbot_plugin_xiaonian_web"
        self.news_path = self.data_dir / "news.json"
        self._task: asyncio.Task | None = None
        self._last_inject = 0.0
        self._learned_today: set[str] = set()

    # ---------------- 生命周期 ----------------
    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        if self.config.get("learn_enable", True):
            self._task = asyncio.create_task(self._learn_loop())
        items = _news.alive_items(_news._load(self.news_path), self._ttl())
        logger.info(f"[web] 已启动：热搜池 {len(items)} 条，自动学习 {'开' if self._task else '关'}")

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    def _ttl(self) -> int:
        return max(1, int(self.config.get("news_ttl_days", 3) or 3))

    def _hours(self) -> tuple[int, ...]:
        raw = str(self.config.get("learn_hours", "9,15,21") or "9,15,21")
        out = []
        for p in raw.replace("，", ",").split(","):
            p = p.strip()
            if p.isdigit() and 0 <= int(p) <= 23:
                out.append(int(p))
        return tuple(sorted(set(out))) or (9, 15, 21)

    async def _provider(self):
        umo = str(self.config.get("target_umo") or "")
        try:
            getter = getattr(self.context, "get_using_provider_async", None)
            if getter:
                prov = await getter(umo=umo)
                if prov is not None:
                    return prov
        except Exception:
            pass
        try:
            pid = self.config.get("chat_provider_id")
            if pid:
                return self.context.get_provider_by_id(pid)
        except Exception:
            pass
        try:
            return self.context.get_using_provider(umo=umo)
        except Exception:
            return None

    # ---------------- 定时学习 ----------------
    async def _learn_loop(self) -> None:
        await asyncio.sleep(40)
        while True:
            try:
                now = time.localtime()
                hour = now.tm_hour
                day = time.strftime("%Y-%m-%d")
                slot = f"{day}-{hour}"
                if hour in self._hours() and slot not in self._learned_today:
                    self._learned_today.add(slot)
                    if len(self._learned_today) > 24:
                        self._learned_today = {slot}
                    prov = await self._provider()
                    got = await _news.learn(prov, self.news_path, self._ttl())
                    if got:
                        logger.info(f"[news] 学到：{' / '.join(got)}")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[news] 学习循环出错：{e}")
            await asyncio.sleep(600)  # 每 10 分钟看一次该不该学

    # ---------------- 注入「外面在聊什么」 ----------------
    @filter.on_llm_request(priority=7)
    async def inject_news(self, event: AstrMessageEvent, req) -> None:
        try:
            if not self.config.get("inject_news", True):
                return
            # 群里不塞热搜：那是私聊时的随口一提，群里插进去反而怪
            if "GroupMessage" in str(getattr(event, "unified_msg_origin", "")):
                return
            every = max(1.0, float(self.config.get("inject_interval_hours", 3) or 3)) * 3600
            now = time.time()
            if now - self._last_inject < every:
                return
            data = _news._load(self.news_path)
            brief = _news.brief_for_prompt(data, self._ttl(), 3)
            if not brief:
                return
            # 已经注入过就别重复塞
            if INJECT_MARK in (req.prompt or ""):
                return
            req.prompt = (req.prompt or "") + "\n\n" + brief
            self._last_inject = now
            logger.info(f"[web] 已注入热搜提示（{len(brief)} 字）")
        except Exception as e:  # noqa: BLE001
            logger.debug(f"[web] 注入失败：{e}")

    # ---------------- 给模型的工具 ----------------
    @llm_tool(name="web_search")
    async def tool_web_search(self, event: AstrMessageEvent, query: str):
        """联网搜索实时信息。当需要最新消息、天气、价格、比赛结果、最近发生的事，或者你不确定的事情时使用。

        搜B站上的视频请改用 bili_search（那个能直接拿到播放量、UP主、BV号）。

        Args:
            query(string): 搜索关键词，用中文，尽量具体
        """

        n = max(3, min(8, int(self.config.get("max_results", 6) or 6)))
        results = await asyncio.to_thread(bing_search, query, n)
        if not results:
            return "没搜到结果。可能是关键词太偏，或者网络不通。"

        lines = []
        for i, r in enumerate(results, 1):
            lines.append(f"{i}. {r['title']}\n   {r['snippet']}\n   来源：{r['url']}")
        body = "\n".join(lines)
        logger.info(f"[web] 搜索「{query}」→ {len(results)} 条")

        return (
            "以下是搜到的资料。回答时用你自己的话把结论说出来，"
            "不要罗列条目、不要说「根据搜索结果」、不要贴网址。"
            "如果这些东西和问题无关，就当没搜到，直接说不知道。\n\n" + body
        )

    @llm_tool(name="read_page")
    async def tool_read_page(self, event: AstrMessageEvent, url: str):
        """读取一个网页的正文内容。当搜索结果的摘要不够、需要看全文时使用。

        B站链接（BV号、b23.tv 短链、space 主页、search 页）会自动走官方 API，
        拿到的是视频标题/UP主/播放量/简介，不是网页正文。

        Args:
            url(string): 完整网址，必须以 http:// 或 https:// 开头
        """

        text = await asyncio.to_thread(fetch_text, url, 2500)
        if not text:
            return "这个页面打不开，或者里面没有正文。"
        logger.info(f"[web] 读取 {url[:60]} 共 {len(text)} 字")
        return "网页正文如下（用你自己的话总结，不要照抄）：\n" + text

    @llm_tool(name="bili_search")
    async def tool_bili_search(self, event: AstrMessageEvent, keyword: str):
        """在B站站内搜索视频。当有人问「B站上有没有/怎么样」「某个UP主/某个视频」，或者要看视频热度时用。

        Args:
            keyword(string): 想搜的内容，中文关键词
        """

        rows = await asyncio.to_thread(bili_search, keyword, 6)
        if not rows:
            return "B站那边没搜到，可能是关键词太偏。"
        lines = []
        for i, r in enumerate(rows, 1):
            lines.append(f"{i}. {r['title']}\n   {r['snippet']}\n   {r['url']}")
        logger.info(f"[web] B站搜「{keyword}」→ {len(rows)} 条")
        return (
            "以下是B站的搜索结果（带播放量、UP主、BV号）。"
            "用你自己的话挑重点说，别罗列列表、别贴链接，除非对方要。\n\n" + "\n".join(lines)
        )

    @llm_tool(name="bili_hot")
    async def tool_bili_hot(self, event: AstrMessageEvent, kind: str = "popular"):
        """看B站现在什么火。有人问「B站最近有啥好看的/热门的」时用。

        Args:
            kind(string): popular（综合热门，默认）| ranking（排行榜）| hot（热搜词）
        """

        k = (kind or "popular").strip().lower()
        if k in ("ranking", "排行", "排行榜"):
            text = await asyncio.to_thread(bili_ranking, 8)
        elif k in ("hot", "热搜", "热搜词"):
            text = await asyncio.to_thread(bili_hot_search, 10)
        else:
            text = await asyncio.to_thread(bili_popular, 8)
        if not text:
            return "B站这三个榜这会儿都拿不到，过会儿再问。"
        logger.info(f"[web] B站{kind} 取到 {len(text)} 字")
        return "B站现在的榜（用你自己的话说，别照着念标题）：\n" + text

    # ---------------- 命令 ----------------
    @filter.command("搜")
    async def cmd_search(self, event: AstrMessageEvent, keyword: str = ""):
        """搜一下：/搜 关键词"""
        kw = (keyword or "").strip()
        if not kw:
            yield event.plain_result("用法：/搜 关键词\n例：/搜 明天北京天气")
            return
        results = await asyncio.to_thread(bing_search, kw, 6)
        if not results:
            yield event.plain_result(f"「{kw}」没搜到东西，换个说法试试。")
            return
        out = [f"搜「{kw}」："]
        for i, r in enumerate(results, 1):
            out.append(f"\n{i}. {r['title']}\n{r['snippet']}\n{r['url']}")
        yield event.plain_result("".join(out))

    @filter.command("读")
    async def cmd_read(self, event: AstrMessageEvent, url: str = ""):
        """读网页正文：/读 https://..."""
        u = (url or "").strip()
        if not u.startswith(("http://", "https://")):
            yield event.plain_result("用法：/读 https://开头 的完整网址")
            return
        text = await asyncio.to_thread(fetch_text, u, 1800)
        if not text:
            yield event.plain_result("打不开，或者页面没有正文。")
            return
        yield event.plain_result(f"正文（前 1800 字）：\n{text}")

    @filter.command("b站", alias={"B站", "bili"})
    async def cmd_bilibili(self, event: AstrMessageEvent, keyword: str = ""):
        """B站：/b站 关键词　|　/b站 热门　|　/b站 排行　|　/b站 热搜"""
        kw = (keyword or "").strip()
        if not kw or kw in ("热门", "热榜"):
            text = await asyncio.to_thread(bili_popular, 8)
            yield event.plain_result(text or "B站热门榜这会儿拿不到。")
            return
        if kw in ("排行", "排行榜", "ranking"):
            text = await asyncio.to_thread(bili_ranking, 8)
            yield event.plain_result(text or "B站排行榜这会儿拿不到。")
            return
        if kw in ("热搜", "热搜词", "hot"):
            text = await asyncio.to_thread(bili_hot_search, 10)
            yield event.plain_result(text or "B站热搜这会儿拿不到。")
            return
        text = await asyncio.to_thread(bili_search_text, kw, 6)
        yield event.plain_result(text or f"B站没搜到「{kw}」，换个词试试。")

    @filter.command("热点")
    async def cmd_news(self, event: AstrMessageEvent):
        """看她最近学到了什么。"""
        data = _news._load(self.news_path)
        items = _news.alive_items(data, self._ttl())
        if not items:
            yield event.plain_result("她还没学到东西。发 /学 让她现在学一轮。")
            return
        lines = ["她最近知道的："]
        for it in items[-8:]:
            ago = int((time.time() - float(it.get("at") or 0)) / 3600)
            lines.append(f"- [{it.get('kind') or '热点'}] {it.get('text')}（{ago} 小时前）")
        yield event.plain_result("\n".join(lines))

    @filter.command("学")
    async def cmd_learn(self, event: AstrMessageEvent):
        """立刻让她学一轮热搜。"""
        yield event.plain_result("她正在看外面发生了什么，稍等…")
        prov = await self._provider()
        got = await _news.learn(prov, self.news_path, self._ttl())
        if not got:
            yield event.plain_result("这轮没学到东西（可能搜索不通，或者内容都不够新）。")
            return
        yield event.plain_result("学到了：\n" + "\n".join(f"- {g}" for g in got))
