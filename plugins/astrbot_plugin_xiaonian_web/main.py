# -*- coding: utf-8 -*-
"""联网 · 会查东西、也知道外面在聊什么的她

五件事：
1. web_search / read_page：她自己判断要查什么，查完用自己的话说；
2. bili_search / bili_hot：B站站内搜视频、看热门榜/排行榜/热搜词（走官方 API，不用 key）；
3. bili_feed / bili_comments / bili_digest：像人一样刷推荐流、看评论区、搞懂一个视频在讲什么；
4. 闲着没事自己刷B站（刷到的东西存 bili_watch.json，聊天时偶尔提一嘴）；扫码登录后还能点赞/投币/关注；
5. 每天定时学一轮热搜、热梗、时事，顺带刷一遍B站，存在 news.json（不进记忆库）；聊天时轻注入「外面最近在聊」。
"""

from __future__ import annotations

import asyncio
import json
import random
import time
from pathlib import Path

from astrbot.api import llm_tool, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

from . import bili_auth as _auth
from . import news as _news
from .web import (
    bili_bangumi,
    bili_coin,
    bili_comments,
    bili_digest,
    bili_feed,
    bili_feed_text,
    bili_follow,
    bili_hot_search,
    bili_like,
    bili_myinfo,
    bili_popular,
    bili_ranking,
    bili_search,
    bili_search_text,
    bing_search,
    fetch_text,
    has_session,
    set_session,
)

INJECT_MARK = "【外面最近在聊】"
WATCH_MARK = "【她刚在B站刷到】"


@register(
    "astrbot_plugin_xiaonian_web",
    "qqjqr",
    "联网搜索、网页读取、B站（能刷推荐流/看评论/扫码登录），以及每天自动刷B站学热搜",
    "0.5.1",
)
class WebSearchPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.config = config or {}
        self.data_dir = Path(__file__).resolve().parents[2] / "plugin_data" / "astrbot_plugin_xiaonian_web"
        self.news_path = self.data_dir / "news.json"
        self.watch_path = self.data_dir / "bili_watch.json"
        self.cookie_path = self.data_dir / "bili_cookie.json"
        self._task: asyncio.Task | None = None
        self._idle_task: asyncio.Task | None = None
        self._last_inject = 0.0
        self._last_active = 0.0
        self._learned_today: set[str] = set()

    # ---------------- 生命周期 ----------------
    async def initialize(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)

        # 有存过的登录态就先灌进去
        sess = _auth.load_session(self.cookie_path)
        if sess:
            set_session(sess.get("cookies"))
            info = _auth.nav(sess.get("cookies") or {})
            if info:
                logger.info(f"[web] B站已登录：{info.get('uname')}（UID {info.get('mid')}）")
            else:
                logger.warning("[web] B站登录态可能过期了，发 /b站登录 重新扫码")

        if self.config.get("learn_enable", True):
            self._task = asyncio.create_task(self._learn_loop())
        if self.config.get("bili_idle_enable", True):
            self._idle_task = asyncio.create_task(self._idle_loop())

        items = _news.alive_items(_news._load(self.news_path), self._ttl())
        logger.info(
            f"[web] 已启动：热搜池 {len(items)} 条，自动学习 {'开' if self._task else '关'}，"
            f"自己刷B站 {'开' if self._idle_task else '关'}"
            f"（间隔 {self._idle_minutes() or '连续'}，时间限制 {self._quiet_minutes() or '无'}）"
            f"{'（已登录）' if has_session() else '（未登录）'}"
        )

    async def terminate(self) -> None:
        for t in (self._task, self._idle_task):
            if t:
                t.cancel()

    def _ttl(self) -> int:
        return max(1, int(self.config.get("news_ttl_days", 3) or 3))

    def _bili_learn(self) -> bool:
        """学习时要不要顺带刷一遍B站。"""
        v = self.config.get("bili_learn", True)
        return True if v is None else bool(v)

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
                    got = await _news.learn(
                        prov, self.news_path, self._ttl(), self._bili_learn()
                    )
                    if got:
                        logger.info(f"[news] 学到：{' / '.join(got)}")
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.warning(f"[news] 学习循环出错：{e}")
            await asyncio.sleep(600)  # 每 10 分钟看一次该不该学

    # ---------------- 闲着没事自己刷B站 ----------------
    def _idle_minutes(self) -> int:
        """刷的间隔（分钟）。0 = 连续刷：每条之间只歇 2~6 分钟。"""
        try:
            return max(0, int(self.config.get("bili_idle_minutes", 20) or 0))
        except Exception:
            return 20

    def _quiet_minutes(self) -> int:
        """要多久没人聊天才刷。0 = 不限制（有人在聊也照刷）。"""
        v = self.config.get("bili_quiet_minutes", 0)
        if v is None:
            return 0
        try:
            return max(0, int(v))
        except Exception:
            return 0

    def _idle_gap(self) -> float:
        m = self._idle_minutes()
        if m <= 0:
            return random.uniform(2, 6) * 60
        return m * 60 * random.uniform(0.85, 1.25)

    def _watch_load(self) -> list[dict]:
        try:
            if self.watch_path.exists():
                d = json.loads(self.watch_path.read_text(encoding="utf-8") or "{}")
                items = d.get("items") or []
                return [i for i in items if isinstance(i, dict)]
        except Exception:
            pass
        return []

    def _watch_save(self, items: list[dict]) -> None:
        try:
            self.watch_path.parent.mkdir(parents=True, exist_ok=True)
            self.watch_path.write_text(
                json.dumps({"at": time.time(), "items": items[-20:]}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
        except Exception:
            pass

    async def _idle_loop(self) -> None:
        """闲下来就自己刷会儿B站：刷推荐流、挑几个看详情和评论，记下来。"""
        await asyncio.sleep(random.uniform(120, 300))  # 起来后先逛一会儿
        while True:
            try:
                if self.config.get("bili_idle_enable", True):
                    quiet = self._quiet_minutes()
                    # 只有配了「要安静多久」才看聊天活跃度；默认 0 = 不限制
                    if not (quiet > 0 and self._last_active
                            and time.time() - self._last_active < quiet * 60):
                        await self._idle_browse()
                await asyncio.sleep(self._idle_gap())
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[web] 刷B站出错：{e}")
                await asyncio.sleep(300)

    async def _idle_browse(self) -> None:
        feed = await asyncio.to_thread(bili_feed, 10)
        if not feed:
            return
        seen = {str(i.get("bvid") or "") for i in self._watch_load()}
        fresh = [v for v in feed if v.get("bvid") not in seen] or feed
        # 一轮刷 1~3 个，像人一样东看西看，不是打卡式的一个
        n = min(len(fresh), max(1, int(self.config.get("bili_browse_count", 2) or 2)))
        picks = random.sample(fresh[:8], min(n, 8))

        items = self._watch_load()
        for pick in picks:
            try:
                digest = await asyncio.to_thread(bili_digest, pick["bvid"], 3)
                items.append(
                    {
                        "at": time.time(),
                        "bvid": pick["bvid"],
                        "title": pick["title"],
                        "up": pick["up"],
                        "play": pick.get("play"),
                        "url": pick["url"],
                        "note": (digest or "")[:800],
                    }
                )
                logger.info(f"[web] 刷到：{pick['title'][:40]}（UP：{pick['up']}）")
                # 登录了的话，看高兴了点个赞（默认关，配置里开）
                if self.config.get("bili_auto_like", False) and has_session():
                    res = await asyncio.to_thread(bili_like, pick["bvid"], True)
                    logger.info(f"[web] 顺手点赞：{res}")
            except Exception as e:  # noqa: BLE001
                logger.debug(f"[web] 这一个没刷成：{e}")
            await asyncio.sleep(random.uniform(3, 9))  # 别一口气点太快
        self._watch_save(items)

    def _watch_brief(self, limit: int = 2, hours: float = 12) -> str:
        items = [
            i for i in self._watch_load()
            if time.time() - float(i.get("at") or 0) <= hours * 3600
        ]
        if not items:
            return ""
        picked = items[-limit:]
        lines = [f"- 刷到《{i.get('title')}》（UP：{i.get('up')}）" for i in picked]
        return (
            WATCH_MARK
            + "\n她闲着的时候自己在B站刷到了这些：\n"
            + "\n".join(lines)
            + "\n（真想聊再提，别每次都念；不要贴链接）"
        )

    # ---------------- 注入「外面在聊什么」 ----------------
    @filter.on_llm_request(priority=7)
    async def inject_news(self, event: AstrMessageEvent, req) -> None:
        try:
            self._last_active = time.time()
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
            watch = self._watch_brief(2, 12)
            if not brief and not watch:
                return
            # 已经注入过就别重复塞
            if INJECT_MARK in (req.prompt or "") or WATCH_MARK in (req.prompt or ""):
                return
            add = "\n\n".join(p for p in (brief, watch) if p)
            req.prompt = (req.prompt or "") + "\n\n" + add
            self._last_inject = now
            logger.info(f"[web] 已注入提示（{len(add)} 字）")
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

    @llm_tool(name="bili_feed")
    async def tool_bili_feed(self, event: AstrMessageEvent, count: int = 6):
        """像人一样刷一下B站首页推荐，看看现在都推些什么。想随口聊「刚刷到个视频」时用。

        Args:
            count(int): 刷几个，3-8，默认 6
        """

        n = max(3, min(8, int(count or 6)))
        text = await asyncio.to_thread(bili_feed_text, n)
        if not text:
            return "B站推荐流这会儿刷不出来。"
        logger.info(f"[web] B站推荐流 {n} 条")
        return (
            "这是你刚在B站刷到的（标题、UP主、播放量）。"
            "挑一两个有意思的用自己的话讲，别念清单，也别贴链接。\n" + text
        )

    @llm_tool(name="bili_comments")
    async def tool_bili_comments(self, event: AstrMessageEvent, video: str, count: int = 6):
        """看某个B站视频的评论区，别人都在说什么。

        Args:
            video(string): BV号 / av号 / 视频链接都行
            count(int): 看几条最热的，3-10，默认 6
        """

        n = max(3, min(10, int(count or 6)))
        text = await asyncio.to_thread(bili_comments, video, n)
        if not text:
            return "这个视频的评论区拿不到（可能视频号不对，或者接口抽了）。"
        logger.info(f"[web] B站评论 {video[:30]} {n} 条")
        return "评论区是这样的（用你自己的话说大家在聊什么，别一个人一个人念）：\n" + text

    @llm_tool(name="bili_digest")
    async def tool_bili_digest(self, event: AstrMessageEvent, video: str):
        """搞清楚一个B站视频到底在讲什么：视频信息 + 几条热评。

        Args:
            video(string): BV号 / av号 / 视频链接
        """

        text = await asyncio.to_thread(bili_digest, video, 5)
        if not text:
            return "这个视频读不到。"
        logger.info(f"[web] B站解析 {video[:30]}")
        return (
            "这是这个视频的信息和评论区（你看不到画面，只能靠标题、简介、标签和评论判断）。"
            "用你自己的话讲讲它大概在讲什么、大家怎么评价，别照抄。\n" + text
        )

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

    @filter.command("刷", alias={"刷b站", "刷B站"})
    async def cmd_bili_feed(self, event: AstrMessageEvent, count: str = ""):
        """刷一下B站推荐流：/刷 [条数]"""
        try:
            n = max(3, min(8, int(count or 6)))
        except Exception:
            n = 6
        text = await asyncio.to_thread(bili_feed_text, n)
        yield event.plain_result(text or "B站推荐流这会儿刷不出来，过会儿再试。")

    @filter.command("评论", alias={"看评论"})
    async def cmd_bili_comments(self, event: AstrMessageEvent, video: str = ""):
        """看视频评论：/评论 BV1xx411c7mD"""
        v = (video or "").strip()
        if not v:
            yield event.plain_result("用法：/评论 BV号 或 /评论 视频链接")
            return
        text = await asyncio.to_thread(bili_comments, v, 8)
        yield event.plain_result(text or "这个视频的评论区拿不到，检查一下 BV 号。")

    @filter.command("b站登录", alias={"B站登录", "bili登录", "b站扫码"})
    async def cmd_bili_login(self, event: AstrMessageEvent):
        """扫码登录B站（发二维码到聊天里，用手机B站 App 扫）"""
        if has_session():
            info = await asyncio.to_thread(bili_myinfo)
            if info:
                yield event.plain_result(
                    f"已经登录了：{info.get('uname')}（UID {info.get('mid')}）。"
                    "想换号先发 /b站登出。"
                )
                return
        png = self.data_dir / "bili_qr.png"
        key, p = await asyncio.to_thread(_auth.gen_qr, str(png))
        if not key:
            yield event.plain_result("二维码没生成出来，B站通行证接口连不上，稍后再试。")
            return
        yield event.plain_result("用手机B站 App 扫这个码（3 分钟内有效，扫完记得在手机上点确认）：")
        yield event.image_result(p)

        last = 0
        for _ in range(50):
            await asyncio.sleep(3)
            code, cookies = await asyncio.to_thread(_auth.poll_once, key)
            if code == 0:
                info = await asyncio.to_thread(_auth.nav, cookies)
                await asyncio.to_thread(
                    _auth.save_session,
                    self.cookie_path,
                    cookies,
                    {"uname": (info or {}).get("uname"), "mid": (info or {}).get("mid")},
                )
                set_session(cookies)
                logger.info(f"[web] B站登录成功：{(info or {}).get('uname')}")
                yield event.plain_result(
                    f"登录成功：{(info or {}).get('uname')}"
                    f"（UID {(info or {}).get('mid')}）。"
                    "以后推荐流按这个号的口味推，她也能点赞/投币/关注了。"
                )
                return
            if code == 86038:
                yield event.plain_result("二维码过期了，再发一次 /b站登录。")
                return
            if code == 86090 and last != code:
                last = code
                yield event.plain_result("扫到了，在手机上点「确认登录」就行。")
        yield event.plain_result("等了三分钟没等到确认，再发一次 /b站登录 吧。")

    @filter.command("b站登出", alias={"B站登出", "bili登出"})
    async def cmd_bili_logout(self, event: AstrMessageEvent):
        """退出B站登录（删掉本地 cookie）"""
        try:
            if self.cookie_path.exists():
                self.cookie_path.unlink()
        except Exception:
            pass
        set_session(None)
        yield event.plain_result("已经退出登录，cookie 删掉了。")

    @filter.command("b站状态", alias={"B站状态", "bili状态"})
    async def cmd_bili_status(self, event: AstrMessageEvent):
        """看她B站这边什么情况：登录没、闲刷开没、最近刷到啥"""
        lines = []
        if has_session():
            info = await asyncio.to_thread(bili_myinfo)
            if info:
                lines.append(f"登录：{info.get('uname')}（UID {info.get('mid')}，硬币 {info.get('coins')}）")
            else:
                lines.append("登录：cookie 过期了，发 /b站登录 重新扫")
        else:
            lines.append("登录：没登录（发 /b站登录，用手机B站扫一下）")

        idle_on = bool(self.config.get("bili_idle_enable", True))
        m = self._idle_minutes()
        gap = "连续刷（每条之间歇 2~6 分钟）" if m <= 0 else f"每 {m} 分钟左右刷一轮"
        quiet = self._quiet_minutes()
        qtxt = "不限制，有人在聊也照刷" if quiet <= 0 else f"要没聊 {quiet} 分钟才刷"
        lines.append(f"刷B站：{'开' if idle_on else '关'}，{gap}")
        lines.append(f"时间限制：{qtxt}")
        lines.append(f"顺手点赞：{'开' if self.config.get('bili_auto_like', False) else '关'}")

        items = self._watch_load()
        if items:
            lines.append("最近刷到的：")
            for it in items[-3:]:
                ago = int((time.time() - float(it.get("at") or 0)) / 60)
                lines.append(f"- {it.get('title')}（UP：{it.get('up')}，{ago} 分钟前）")
        else:
            lines.append("还没刷到什么，等她自己逛一会儿。")
        yield event.plain_result("\n".join(lines))

    @filter.command("点赞", alias={"赞一下"})
    async def cmd_bili_like(self, event: AstrMessageEvent, video: str = ""):
        """给视频点赞：/点赞 BV号"""
        v = (video or "").strip()
        if not v:
            yield event.plain_result("用法：/点赞 BV号（要先 /b站登录）")
            return
        res = await asyncio.to_thread(bili_like, v, True)
        yield event.plain_result(res)

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
        yield event.plain_result("她正在看外面发生了什么（顺带刷会儿B站），稍等…")
        prov = await self._provider()
        got = await _news.learn(prov, self.news_path, self._ttl(), self._bili_learn())
        if not got:
            yield event.plain_result("这轮没学到东西（可能搜索不通，或者内容都不够新）。")
            return
        yield event.plain_result("学到了：\n" + "\n".join(f"- {g}" for g in got))
