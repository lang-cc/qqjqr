"""小年工具箱：提醒、签到积分、运势、诗句（本地优先，尽量不依赖外网）。

指令一览：
  /提醒 +30m 喝水          30 分钟后提醒
  /提醒 18:30 开会         今天/明天 18:30 提醒
  /提醒列表  /提醒删 3
  /签到  /积分  /排行
  /运势  /诗句  /帮助

设计原则：能不联网就不联网（这台机器上大部分第三方 API 都被掐了），
只有「/诗句」走网络，失败就退回内置句子。
"""

import asyncio
import json
import os
import random
import time
from datetime import datetime, timedelta

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.star import Context, Star, register

DATA_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data.json")
TICK_SECONDS = 5  # 提醒轮询间隔

FORTUNES = [
    ("大吉", "今天开口就有人接话，想办的事顺得离谱。"),
    ("中吉", "小顺。有人会主动找你，别端着。"),
    ("小吉", "平平淡淡，但没坑。适合摸鱼。"),
    ("吉", "有点小摩擦，最后都过去了。"),
    ("末吉", "慢半拍反而稳妥，别急着定事。"),
    ("凶", "今天少说话，多喝水，别跟人较劲。"),
]

FALLBACK_POEMS = [
    "晚来天欲雪，能饮一杯无。",
    "人间烟火气，最抚凡人心。",
    "风起于青萍之末，浪成于微澜之间。",
    "年年岁岁花相似，岁岁年年人不同。",
]


def _load() -> dict:
    try:
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"reminders": [], "scores": {}, "last_checkin": {}, "next_id": 1}


def _save(d: dict) -> None:
    tmp = DATA_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, indent=2)
    os.replace(tmp, DATA_FILE)


def _parse_due(arg: str) -> float | None:
    """支持 +30s/+10m/+2h 或 18:30"""
    arg = arg.strip()
    now = datetime.now()
    if arg.startswith("+"):
        body = arg[1:].lower()
        try:
            num = float(body[:-1])
        except ValueError:
            return None
        unit = body[-1]
        delta = {
            "s": timedelta(seconds=num),
            "m": timedelta(minutes=num),
            "h": timedelta(hours=num),
        }.get(unit)
        if not delta:
            return None
        return (now + delta).timestamp()
    if ":" in arg:
        try:
            hh, mm = arg.split(":")
            target = now.replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
        except ValueError:
            return None
        if target <= now:
            target += timedelta(days=1)
        return target.timestamp()
    return None


def _fmt_due(ts: float) -> str:
    return datetime.fromtimestamp(ts).strftime("%m-%d %H:%M:%S")


@register(
    "astrbot_plugin_xiaonian_toolbox",
    "qqjqr",
    "小年工具箱：提醒/签到/运势/诗句",
    "0.1.0",
)
class XiaonianToolbox(Star):
    def __init__(self, context: Context):
        super().__init__(context)
        self.data = _load()
        self._task: asyncio.Task | None = None

    async def initialize(self) -> None:
        self._task = asyncio.create_task(self._scheduler())
        logger.info("[xiaonian_toolbox] 提醒调度已启动")

    async def terminate(self) -> None:
        if self._task:
            self._task.cancel()

    # ---------------- 提醒调度 ----------------
    async def _scheduler(self) -> None:
        while True:
            try:
                await asyncio.sleep(TICK_SECONDS)
                now = time.time()
                due = [r for r in self.data["reminders"] if r["due"] <= now]
                for r in due:
                    ok = await self._send(r["umo"], f"⏰ 提醒：{r['text']}")
                    logger.info(
                        f"[xiaonian_toolbox] 提醒 #{r['id']} 到点 -> {r['umo']} ok={ok}"
                    )
                    self.data["reminders"].remove(r)
                if due:
                    _save(self.data)
            except asyncio.CancelledError:
                return
            except Exception as e:  # 调度线程不能死
                logger.warning(f"[xiaonian_toolbox] 调度出错: {e}")

    async def _send(self, umo: str, text: str) -> bool:
        try:
            from astrbot.core.message.message_event_result import MessageChain

            chain = MessageChain([Plain(text)])
            return await self.context.send_message(umo, chain)
        except Exception as e:
            logger.warning(f"[xiaonian_toolbox] 主动发送失败: {e}")
            return False

    # ---------------- 指令 ----------------
    @filter.command("提醒", alias={"remind"})
    async def add_reminder(self, event: AstrMessageEvent):
        """用法：/提醒 +30m 喝水 ｜ /提醒 18:30 开会"""
        raw = (event.message_str or "").strip()
        for prefix in ("/提醒", "提醒", "/remind", "remind"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        parts = raw.split(maxsplit=1)
        if len(parts) < 2:
            yield event.plain_result(
                "咋写：/提醒 +30m 喝水（30 分钟后），或 /提醒 18:30 开会（到点提醒）"
            )
            return
        due = _parse_due(parts[0])
        if due is None:
            yield event.plain_result("时间没看懂，写成 +30m / +2h / 18:30 这样。")
            return
        rid = self.data.get("next_id", 1)
        self.data["next_id"] = rid + 1
        self.data["reminders"].append(
            {
                "id": rid,
                "umo": event.unified_msg_origin,
                "user": str(event.get_sender_id()),
                "text": parts[1],
                "due": due,
            }
        )
        _save(self.data)
        yield event.plain_result(f"行，#{rid} 记下了，{_fmt_due(due)} 叫你。")

    @filter.command("提醒列表")
    async def list_reminders(self, event: AstrMessageEvent):
        rs = self.data["reminders"]
        if not rs:
            yield event.plain_result("这会儿没有待办提醒。")
            return
        lines = [f"#{r['id']} {_fmt_due(r['due'])} {r['text']}" for r in rs[:20]]
        yield event.plain_result("待提醒：\n" + "\n".join(lines))

    @filter.command("提醒删")
    async def del_reminder(self, event: AstrMessageEvent, rid: str = ""):
        try:
            target = int(rid.strip())
        except ValueError:
            yield event.plain_result("给个编号，比如 /提醒删 3")
            return
        before = len(self.data["reminders"])
        self.data["reminders"] = [r for r in self.data["reminders"] if r["id"] != target]
        _save(self.data)
        yield event.plain_result(
            "删掉了。" if len(self.data["reminders"]) < before else "没找到这个编号。"
        )

    @filter.command("签到")
    async def checkin(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id())
        today = datetime.now().strftime("%Y-%m-%d")
        if self.data["last_checkin"].get(uid) == today:
            yield event.plain_result(
                f"今天已经签过了，现在 {self.data['scores'].get(uid, 0)} 分。"
            )
            return
        gain = random.randint(3, 12)
        self.data["last_checkin"][uid] = today
        self.data["scores"][uid] = self.data["scores"].get(uid, 0) + gain
        _save(self.data)
        yield event.plain_result(
            f"签到 +{gain}，现在一共 {self.data['scores'][uid]} 分。"
        )

    @filter.command("积分")
    async def score(self, event: AstrMessageEvent):
        uid = str(event.get_sender_id())
        yield event.plain_result(f"你有 {self.data['scores'].get(uid, 0)} 分。")

    @filter.command("排行")
    async def rank(self, event: AstrMessageEvent):
        items = sorted(self.data["scores"].items(), key=lambda kv: kv[1], reverse=True)[:10]
        if not items:
            yield event.plain_result("还没人签到。")
            return
        lines = [f"{i + 1}. {uid} — {pts} 分" for i, (uid, pts) in enumerate(items)]
        yield event.plain_result("积分榜：\n" + "\n".join(lines))

    @filter.command("运势")
    async def fortune(self, event: AstrMessageEvent):
        level, text = random.choice(FORTUNES)
        yield event.plain_result(f"今日{level}：{text}")

    @filter.command("诗句")
    async def poem(self, event: AstrMessageEvent):
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    "https://v2.jinrishici.com/one.json",
                    timeout=aiohttp.ClientTimeout(total=6),
                ) as r:
                    data = await r.json(content_type=None)
                    d = data["data"]
                    content = d["content"]
                    origin = d.get("origin") or {}
                    if isinstance(origin, dict):
                        title = origin.get("title") or ""
                        author = origin.get("author") or ""
                        dynasty = origin.get("dynasty") or ""
                        credit = " ".join(x for x in (dynasty, author, title) if x)
                    else:
                        credit = str(origin)
                    yield event.plain_result(f"{content}\n—— {credit}" if credit else content)
                    return
        except Exception as e:
            logger.info(f"[xiaonian_toolbox] 诗句接口不可用，用内置: {e}")
        yield event.plain_result(random.choice(FALLBACK_POEMS))

    @filter.command("天气")
    async def weather(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        for prefix in ("/天气", "天气"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        city = raw or "北京"
        code = await self._city_code(city)
        if not code:
            yield event.plain_result(f"没找着「{city}」这个地名，换个说法试试。")
            return
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(
                    f"http://t.weather.itboy.net/api/weather/city/{code}",
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    d = await r.json(content_type=None)
            data = d["data"]
            info = d["cityInfo"]
            today = data["forecast"][0]
            yield event.plain_result(
                f"{info['city']} 现在 {data['wendu']}℃，{today['type']}，"
                f"{today['low']} ~ {today['high']}，"
                f"{today['fx']}{today['fl']}，空气 {data.get('quality', '未知')}。"
            )
        except Exception as e:
            logger.info(f"[xiaonian_toolbox] 天气接口失败: {e}")
            yield event.plain_result("天气那边现在没响应，过会儿再问我。")

    async def _city_code(self, city: str) -> str:
        cache = self.data.setdefault("city_codes", {})
        if city in cache:
            return cache[city]
        try:
            import urllib.parse

            async with aiohttp.ClientSession() as s:
                url = (
                    "http://toy1.weather.com.cn/search?cityname="
                    + urllib.parse.quote(city)
                )
                async with s.get(
                    url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Referer": "http://www.weather.com.cn/",
                    },
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    text = await r.text()
            payload = json.loads(text[1:-1]) if text.startswith("(") else json.loads(text)
            if not payload:
                return ""
            code = payload[0]["ref"].split("~")[0]
            cache[city] = code
            _save(self.data)
            return code
        except Exception as e:
            logger.info(f"[xiaonian_toolbox] 城市查询失败: {e}")
            return ""

    @filter.command("点歌")
    async def music(self, event: AstrMessageEvent):
        raw = (event.message_str or "").strip()
        for prefix in ("/点歌", "点歌"):
            if raw.startswith(prefix):
                raw = raw[len(prefix):].strip()
                break
        if not raw:
            yield event.plain_result("点啥歌？发 /点歌 晴天 这样。")
            return
        try:
            import urllib.parse

            async with aiohttp.ClientSession() as s:
                url = "https://music.163.com/api/search/get/web?" + urllib.parse.urlencode(
                    {"s": raw, "type": 1, "limit": 3}
                )
                async with s.get(
                    url,
                    headers={"Referer": "https://music.163.com/"},
                    timeout=aiohttp.ClientTimeout(total=8),
                ) as r:
                    d = await r.json(content_type=None)
            songs = (d.get("result") or {}).get("songs") or []
            if not songs:
                yield event.plain_result(f"没搜到「{raw}」，换个关键词。")
                return
            lines = []
            for sg in songs[:3]:
                artist = "/".join(a["name"] for a in sg.get("artists", [])[:2])
                lines.append(
                    f"{sg['name']} — {artist}\nhttps://music.163.com/song?id={sg['id']}"
                )
            yield event.plain_result("找到了：\n\n" + "\n\n".join(lines))
        except Exception as e:
            logger.info(f"[xiaonian_toolbox] 点歌失败: {e}")
            yield event.plain_result("搜歌那边没响应，等会儿再试。")

    @filter.command("帮助")
    async def help_cmd(self, event: AstrMessageEvent):
        yield event.plain_result(
            "能用的：\n"
            "/提醒 +30m 喝水 · /提醒 18:30 开会 · /提醒列表 · /提醒删 3\n"
            "/签到 · /积分 · /排行\n"
            "/运势 · /诗句 · /天气 北京 · /点歌 晴天\n"
            "/帮助"
        )
