"""小年表情包口袋 —— 只留大肥鱼看着顺眼的那几张，限额 200 张。

做什么：
  · 看得到但先不收：群里/私聊里别人发的表情包（含 QQ 商城表情 mface），
    不需要 @ 机器人、不需要唤醒词，先丢进内存里的「候选池」。
  · 由人挑：看上了就引用那张图发 /收（或 /收 编号 / /收 链接），才真正入库。
    只有 owner_ids 里的人（默认大肥鱼）和面板管理员能挑，别人只能 /表情 用。
  · 分类整理：按会话丢进 data/sticker_pocket/group_<群号>/ 或 private_<QQ号>/，
    条目录在 data/sticker_pocket/index.json（sha1 去重、使用次数、最后使用时间）。
  · 限额：默认最多 200 张（config: max_count），满了不再收，
    到 180 张（config: warn_at）开始在 /表情库 里报警。
  · 自己用：/表情 随机发一张；/表情 3 连发 3 张。
  · 腾位置：/表情清理 20 —— 按「用得最少 + 最久没发」删 20 张。
  · 想省事可把 config 的 auto_collect 打开，变成来者不拒自动全收。

为什么不用 @filter.event_message_type(ALL) 收图：
  AstrBot 的 waking_check 阶段会把未唤醒消息 stop_event，插件 handler 根本跑不到；
  而且 mface（QQ 表情包）在 aiocqhttp 适配器里被 `continue` 直接丢弃了。
  所以这里直接订阅 aiocqhttp 的原始事件总线（bot.subscribe("message", ...)），
  在框架转换之前就把原始消息段拿下来。

指令一览（前缀 /）：
  /收             引用那张表情包发，收进库（也支持 /收 编号、/收 链接）
  /收列表         这个会话刚才有哪些图可以收
  /表情 [张数]    随机发（张数 ≤ send_limit）
  /表情库         库存状态：x/200、来源分布、最常用、候选池还有多少
  /表情列表 [N]   按收录时间倒序列出，带 id 和使用次数
  /表情删 12 15   按 id 删
  /表情清理 [N]   删用得最少的 N 张（默认 20）
  /表情诊断       收不到图时看处理流水
  /表情帮助
"""

import asyncio
import base64
import hashlib
import json
import os
import random
from datetime import datetime
from urllib.parse import unquote, urlparse

import aiohttp

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image
from astrbot.api.star import Context, Star, get_astrbot_data_path, register

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
# 库放在 data/sticker_pocket/ 而不是插件目录：插件更新/重装不会把表情包带走
STICKER_ROOT = os.path.join(get_astrbot_data_path(), "sticker_pocket")
INDEX_FILE = os.path.join(STICKER_ROOT, "index.json")

DEFAULTS = {
    "enabled": True,
    "auto_collect": False,
    "recent_keep": 60,
    "owner_ids": ["3862642627"],
    "max_count": 200,
    "warn_at": 180,
    "max_file_kb": 1024,
    "max_side": 1200,
    "text_message_ok": False,
    "send_limit": 5,
}

EXT_BY_FORMAT = {
    "PNG": "png",
    "JPEG": "jpg",
    "GIF": "gif",
    "WEBP": "webp",
    "BMP": "bmp",
}

DEFAULT_CLEAN = 20


def _now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _human_kb(kb: float) -> str:
    return f"{kb / 1024:.1f}MB" if kb >= 1024 else f"{kb:.0f}KB"


def _short_time(ts: str | None) -> str:
    if not ts:
        return "—"
    return ts[5:16] if len(ts) >= 16 else ts


@register(
    "astrbot_plugin_xiaonian_sticker",
    "qqjqr",
    "小年表情包口袋：自动收别人发的表情包，限额 200，可随机发出去",
    "0.1.0",
)
class StickerPocket(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context, config)
        self.conf = dict(DEFAULTS)
        if isinstance(config, dict):
            for k, v in config.items():
                if k in DEFAULTS and v is not None:
                    self.conf[k] = v
        os.makedirs(STICKER_ROOT, exist_ok=True)
        self.index = self._load_index()
        self._lock = asyncio.Lock()
        self._tasks: set[asyncio.Task] = set()
        self._hooked: dict[int, tuple] = {}
        self._watch_task: asyncio.Task | None = None
        self._debug: list[str] = []  # 最近的处理流水，给 /表情诊断 看
        self._full_noted = False
        self._recent: list[dict] = []  # 候选池：看到但还没收的图
        self._recent_seq = 0

    # ---------------- 生命周期 ----------------
    async def initialize(self) -> None:
        self._watch_task = asyncio.create_task(self._watch_platforms())
        logger.info(
            f"[sticker] 表情包口袋已开张：{len(self.index['items'])}/{self.conf['max_count']} 张"
        )

    async def terminate(self) -> None:
        if self._watch_task:
            self._watch_task.cancel()
        for task in list(self._tasks):
            task.cancel()
        for bot, handler in list(self._hooked.values()):
            try:
                bot.unsubscribe("message", handler)
            except Exception:
                pass
        self._hooked.clear()

    # ---------------- 索引读写 ----------------
    def _load_index(self) -> dict:
        try:
            with open(INDEX_FILE, encoding="utf-8") as f:
                data = json.load(f)
            data.setdefault("items", [])
            data.setdefault("next_id", 1)
            data.setdefault("warned_at", 0)
            return data
        except FileNotFoundError:
            return {"items": [], "next_id": 1, "warned_at": 0}
        except Exception as e:
            logger.warning(f"[sticker] index.json 读取失败，改用空库：{e}")
            return {"items": [], "next_id": 1, "warned_at": 0}

    def _save_index(self) -> None:
        os.makedirs(STICKER_ROOT, exist_ok=True)
        tmp = INDEX_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self.index, f, ensure_ascii=False, indent=1)
        os.replace(tmp, INDEX_FILE)

    # ---------------- 原始消息订阅 ----------------
    async def _watch_platforms(self) -> None:
        """平台可能比插件晚加载 / 中途重连，所以定期扫一遍。"""
        while True:
            try:
                insts = getattr(self.context.platform_manager, "platform_insts", []) or []
                for inst in insts:
                    bot = getattr(inst, "bot", None)
                    if bot is None or not hasattr(bot, "subscribe"):
                        continue
                    key = id(bot)
                    if key in self._hooked:
                        continue
                    handler = self._make_raw_handler(bot)
                    bot.subscribe("message", handler)
                    self._hooked[key] = (bot, handler)
                    logger.info(
                        f"[sticker] 已挂上表情包口袋监听：{getattr(inst.meta(), 'id', '?')}"
                    )
            except Exception as e:
                logger.warning(f"[sticker] 扫描平台失败：{e}")
            await asyncio.sleep(10)

    def _make_raw_handler(self, bot):
        async def _handler(event) -> None:
            # 原始事件回调必须秒回，下载/写盘丢到后台任务里做
            try:
                payload = {k: event[k] for k in event.keys()}
            except Exception:
                return
            task = asyncio.create_task(self._ingest_raw(payload))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)

        return _handler

    # ---------------- 收图主流程 ----------------
    def _note(self, text: str) -> None:
        """记一条处理流水（/表情诊断 用），同时写 info 日志。"""
        line = f"{_now()[11:]} {text}"
        self._debug.append(line)
        if len(self._debug) > 40:
            del self._debug[: len(self._debug) - 40]
        logger.info(f"[sticker] {text}")

    async def _ingest_raw(self, payload: dict) -> None:
        if not self.conf["enabled"]:
            return
        self_id = str(payload.get("self_id") or "")
        user_id = str(payload.get("user_id") or "")
        if not user_id or user_id == self_id:
            return  # 自己发的不要

        segments = payload.get("message")
        if not isinstance(segments, list):
            self._note(f"跳过：消息不是数组格式（{type(segments).__name__}）")
            return  # 只处理数组格式的消息段

        has_text = False
        candidates: list[tuple[str, str]] = []
        media_brief: list[str] = []
        for seg in segments:
            if not isinstance(seg, dict):
                continue
            seg_type = str(seg.get("type") or "")
            data = seg.get("data") or {}
            if seg_type in ("text", "plain"):
                if str(data.get("text") or "").strip():
                    has_text = True
            elif seg_type == "mface":
                # QQ 商城表情 / 收藏表情，框架会丢掉，但原始段里有 url
                ref = str(data.get("url") or data.get("file") or "")
                media_brief.append(
                    f"mface(keys={','.join(sorted(data))},ref={ref[:40] or '空'})"
                )
                if ref:
                    candidates.append(("mface", ref))
            elif seg_type == "image":
                summary = str(data.get("summary") or "")
                sub_type = str(data.get("sub_type") or "")
                ref = str(data.get("url") or data.get("file") or "")
                media_brief.append(
                    f"image(summary={summary or '空'},sub_type={sub_type or '空'},"
                    f"size={data.get('file_size') or '?'},keys={','.join(sorted(data))},"
                    f"ref={ref[:40] or '空'})"
                )
                looks_sticker = ("表情" in summary) or sub_type == "1"
                if not ref:
                    continue
                if has_text and not looks_sticker and not self.conf["text_message_ok"]:
                    continue  # 带字的聊天图，先当作不是表情包
                candidates.append(("image", ref))

        if media_brief:
            self._note(
                f"收到图片消息 ← {user_id} @ "
                f"{'group_' + str(payload.get('group_id')) if payload.get('group_id') else 'private'}"
                f"：{' | '.join(media_brief)}（带文字={has_text}）"
            )

        if has_text and not self.conf["text_message_ok"]:
            # 整条消息带字：只留明确标了是表情的（用户引用回复 /收 仍可强行收）
            candidates = [c for c in candidates if c[0] == "mface"]
            if not candidates:
                self._note("没进候选池：消息里带文字，且不是明确的表情包")
                return

        if not candidates:
            return

        group_id = str(payload.get("group_id") or "")
        sender = payload.get("sender") or {}
        user_name = str(
            sender.get("card") or sender.get("nickname") or user_id
        ).strip() or user_id

        # 先丢进候选池：默认不自动入库，等你 /收 挑了再说
        self._remember(
            refs=candidates,
            group_id=group_id,
            user_id=user_id,
            user_name=user_name,
            msg_id=str(payload.get("message_id") or ""),
        )

        if not self.conf["auto_collect"]:
            self._note(
                f"进候选池（未入库）：{len(candidates)} 张 ← {user_name}，"
                f"想要就引用它发 /收"
            )
            return

        for kind, ref in candidates[:3]:
            try:
                entry, reason = await self._save_sticker(
                    ref=ref,
                    kind=kind,
                    group_id=group_id,
                    user_id=user_id,
                    user_name=user_name,
                )
                if entry is None:
                    self._note(f"没存下来：{reason}")
            except Exception as e:
                self._note(f"收录出错（{kind}, {ref[:40]}）：{e}")

    # ---------------- 候选池 ----------------
    def _remember(
        self,
        refs: list[tuple[str, str]],
        group_id: str,
        user_id: str,
        user_name: str,
        msg_id: str,
    ) -> None:
        self._recent_seq += 1
        self._recent.append(
            {
                "seq": self._recent_seq,
                "refs": refs[:3],
                "group_id": group_id,
                "user_id": user_id,
                "user_name": user_name,
                "msg_id": msg_id,
                "ts": _now(),
                "saved": False,
            }
        )
        keep = max(10, int(self.conf["recent_keep"]))
        if len(self._recent) > keep:
            del self._recent[: len(self._recent) - keep]

    @staticmethod
    def _session_key(group_id: str, user_id: str) -> str:
        return f"group_{group_id}" if group_id else f"private_{user_id}"

    def _item_key(self, item: dict) -> str:
        return self._session_key(item.get("group_id") or "", item.get("user_id") or "")

    def _session_pool(self, key: str) -> list[dict]:
        return [x for x in self._recent if self._item_key(x) == key]

    @staticmethod
    def _refs_from_components(components) -> list[tuple[str, str]]:
        """从引用消息的段里抠出图片引用。"""
        out: list[tuple[str, str]] = []
        for comp in components or []:
            if type(comp).__name__ != "Image":
                continue
            ref = str(getattr(comp, "url", "") or getattr(comp, "file", "") or "")
            if ref:
                out.append(("image", ref))
        return out

    async def _save_sticker(
        self,
        ref: str,
        kind: str,
        group_id: str,
        user_id: str,
        user_name: str,
    ) -> tuple[dict | None, str]:
        """存一张表情包。返回 (条目 or None, 原因说明)。"""
        max_count = int(self.conf["max_count"])
        if len(self.index["items"]) >= max_count:
            if not self._full_noted:
                self._full_noted = True
                logger.warning(
                    f"[sticker] 已满（{max_count} 张），停止收录；/表情清理 可腾位置"
                )
            return None, f"库已满（{max_count} 张上限）"

        data = await self._fetch_bytes(ref)
        if not data:
            return None, "图下载不下来（链接失效或超过体积上限）"

        size, fmt = self._image_size(data)
        if size:
            w, h = size
            if max(w, h) > int(self.conf["max_side"]):
                return None, f"图太大（{w}x{h}，上限 {self.conf['max_side']}px）"
        else:
            w = h = 0

        sha1 = hashlib.sha1(data).hexdigest()

        async with self._lock:
            for it in self.index["items"]:
                if it.get("sha1") == sha1:
                    return None, f"已经有了（#{it['id']}，同一张图不重复存）"
            if len(self.index["items"]) >= max_count:
                return None, f"库已满（{max_count} 张上限）"

            sid = int(self.index["next_id"])
            ext = EXT_BY_FORMAT.get(fmt, self._guess_ext(ref))
            folder = f"group_{group_id}" if group_id else f"private_{user_id}"
            rel_name = f"{sid:04d}_{user_id}.{ext}"
            abs_dir = os.path.join(STICKER_ROOT, folder)
            os.makedirs(abs_dir, exist_ok=True)
            abs_path = os.path.join(abs_dir, rel_name)
            with open(abs_path, "wb") as f:
                f.write(data)

            entry = {
                "id": sid,
                "file": f"{folder}/{rel_name}",
                "sha1": sha1,
                "kb": round(len(data) / 1024, 1),
                "w": w,
                "h": h,
                "kind": kind,
                "group_id": group_id,
                "user_id": user_id,
                "user_name": user_name,
                "saved_at": _now(),
                "uses": 0,
                "last_used": None,
            }
            self.index["items"].append(entry)
            self.index["next_id"] = sid + 1
            self._save_index()

            total = len(self.index["items"])
            warn_at = int(self.conf["warn_at"])
            if total == max_count:
                self._full_noted = True
                logger.warning(
                    f"[sticker] 表情包库已满（{total}/{max_count}），新表情包不再入库，"
                    f"用 /表情清理 腾位置"
                )
            elif total >= warn_at and total > int(self.index.get("warned_at") or 0):
                self.index["warned_at"] = total
                self._save_index()
                logger.warning(
                    f"[sticker] 表情包库快满了（{total}/{max_count}），"
                    f"建议 /表情清理 {DEFAULT_CLEAN}"
                )
            self._note(
                f"收录 #{sid} {kind} {w}x{h} {_human_kb(len(data) / 1024)}"
                f" ← {user_name}({user_id}) @ {folder}"
            )
            return entry, "已收录"

    async def _fetch_bytes(self, ref: str) -> bytes | None:
        limit = int(self.conf["max_file_kb"]) * 1024
        ref = ref.strip()
        if not ref:
            return None
        if ref.startswith("base64://"):
            try:
                return base64.b64decode(ref[len("base64://") :])
            except Exception:
                return None
        if ref.startswith("data:"):
            try:
                return base64.b64decode(ref.split(",", 1)[1])
            except Exception:
                return None
        if ref.startswith("http://") or ref.startswith("https://"):
            try:
                timeout = aiohttp.ClientTimeout(total=15)
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.get(
                        ref,
                        headers={"User-Agent": "Mozilla/5.0"},
                    ) as resp:
                        if resp.status != 200:
                            return None
                        if resp.content_length and resp.content_length > limit:
                            return None
                        blob = await resp.content.read(limit + 1)
                if len(blob) > limit:
                    return None
                return blob or None
            except Exception as e:
                logger.debug(f"[sticker] 下载失败 {ref[:80]}：{e}")
                return None

        # 本地路径（file:/// 或裸路径）
        path = ref
        if path.startswith("file://"):
            path = unquote(urlparse(path).path)
            if os.name == "nt" and len(path) > 2 and path[0] == "/" and path[2] == ":":
                path = path[1:]
        try:
            if os.path.isfile(path) and os.path.getsize(path) <= limit:
                with open(path, "rb") as f:
                    return f.read() or None
        except OSError:
            return None
        return None

    @staticmethod
    def _guess_ext(ref: str) -> str:
        base = ref.split("?", 1)[0]
        ext = os.path.splitext(base)[1].lower().lstrip(".")
        return ext if ext in ("jpg", "jpeg", "png", "gif", "webp", "bmp") else "jpg"

    @staticmethod
    def _image_size(data: bytes) -> tuple[tuple[int, int] | None, str]:
        try:
            import io

            from PIL import Image as PILImage

            with PILImage.open(io.BytesIO(data)) as im:
                return im.size, (im.format or "").upper()
        except Exception:
            return None, ""

    # ---------------- 使用记录 ----------------
    def _mark_used(self, ids: list[int]) -> None:
        now = _now()
        changed = False
        target = set(ids)
        for it in self.index["items"]:
            if it.get("id") in target:
                it["uses"] = int(it.get("uses") or 0) + 1
                it["last_used"] = now
                changed = True
        if changed:
            try:
                self._save_index()
            except Exception as e:
                logger.warning(f"[sticker] 写索引失败：{e}")

    def _abs(self, entry: dict) -> str:
        return os.path.join(STICKER_ROOT, entry["file"].replace("/", os.sep))

    # ---------------- 指令 ----------------
    @staticmethod
    def _arg_after(message_str: str, name: str) -> str:
        s = (message_str or "").strip()
        for cand in (name, "/" + name):
            if s.startswith(cand):
                return s[len(cand) :].strip()
        return s

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        return str(getattr(event, "role", "")) == "admin"

    def _is_owner(self, event: AstrMessageEvent) -> bool:
        """谁能决定收不收：面板管理员，或 owner_ids 里指定的号（默认大肥鱼）。"""
        sid = str(event.get_sender_id())
        owners = [str(x) for x in (self.conf.get("owner_ids") or [])]
        return sid in owners or self._is_admin(event)

    @filter.command("收")
    async def cmd_collect(self, event: AstrMessageEvent):
        """把看着顺眼的那张收进库：引用它发 /收，或 /收 编号，或 /收 图片链接。"""
        if not self._is_owner(event):
            yield event.plain_result("收不收由大肥鱼说了算，你发 /表情 用就行。")
            return

        arg = self._arg_after(event.message_str, "收").strip()
        group_id = str(getattr(event.message_obj, "group_id", "") or "")
        sender_id = str(event.get_sender_id())
        key = self._session_key(group_id, sender_id)
        refs: list[tuple[str, str]] = []
        src_group, src_user, src_name = group_id, sender_id, sender_id
        target = None

        if arg.startswith("http://") or arg.startswith("https://"):
            refs = [("image", arg)]
        elif arg.isdigit():
            target = next((x for x in self._recent if x["seq"] == int(arg)), None)
            if target is None:
                yield event.plain_result(
                    f"候选池里没有 #{arg}。发 /收列表 看看现在有哪些。"
                )
                return
        else:
            # 引用回复优先：被引用消息里若带图，直接从里面拿
            reply = next(
                (
                    c
                    for c in (event.message_obj.message or [])
                    if type(c).__name__ == "Reply"
                ),
                None,
            )
            if reply is not None:
                refs = self._refs_from_components(getattr(reply, "chain", None))
                if refs:
                    src_user = str(getattr(reply, "sender_id", "") or src_user)
                    src_name = str(getattr(reply, "sender_nickname", "") or src_user)
                else:
                    target = next(
                        (
                            x
                            for x in self._recent
                            if x["msg_id"] == str(getattr(reply, "id", ""))
                        ),
                        None,
                    )
            if not refs and target is None:
                # 没引用：就收这个会话里最近一张还没收的
                pool = self._session_pool(key)
                target = next((x for x in reversed(pool) if not x["saved"]), None) or (
                    pool[-1] if pool else None
                )

        if target is not None:
            refs = target["refs"]
            src_group = target["group_id"] or src_group
            src_user = target["user_id"] or src_user
            src_name = target["user_name"] or src_name

        if not refs:
            yield event.plain_result(
                "没找到要收的表情包：引用那条表情包再发 /收，"
                "或者先 /收列表 看编号，也可以 /收 图片链接。"
            )
            return

        saved: list[dict] = []
        problems: list[str] = []
        for kind, ref in refs[:3]:
            entry, reason = await self._save_sticker(
                ref=ref,
                kind=kind,
                group_id=src_group,
                user_id=src_user,
                user_name=src_name,
            )
            if entry:
                saved.append(entry)
            else:
                problems.append(reason)

        if target is not None and saved:
            target["saved"] = True
        if not saved:
            yield event.plain_result("没收下来：" + "；".join(problems[:2]))
            return

        ids = "、".join("#" + str(e["id"]) for e in saved)
        lines = [
            f"收下了 {len(saved)} 张：{ids}。现在 "
            f"{len(self.index['items'])}/{self.conf['max_count']} 张。"
        ]
        if problems:
            lines.append("另外没存：" + "；".join(problems[:2]))
        yield event.plain_result("\n".join(lines))

    @filter.command("收列表")
    async def cmd_pool(self, event: AstrMessageEvent):
        """看看刚才聊天里有哪些图可以收。"""
        group_id = str(getattr(event.message_obj, "group_id", "") or "")
        key = self._session_key(group_id, str(event.get_sender_id()))
        pool = self._session_pool(key)
        if not pool:
            yield event.plain_result(
                "这个会话里还没看到图片消息。等群友发表情包，再用 /收列表 看。"
            )
            return
        lines = [f"这个会话最近看到 {len(pool)} 条图片消息："]
        for item in reversed(pool[-8:]):
            flag = "已收" if item["saved"] else "没收"
            lines.append(
                f"#{item['seq']} {item['ts'][11:16]} {item['user_name']} "
                f"{len(item['refs'])}张 [{flag}]"
            )
        lines.append("看上了就引用那张图发 /收，或者 /收 编号")
        yield event.plain_result("\n".join(lines))

    @filter.command("表情")
    async def cmd_send(self, event: AstrMessageEvent):
        arg = self._arg_after(event.message_str, "表情")
        count = 1
        if arg.isdigit():
            count = min(max(int(arg), 1), max(1, int(self.conf["send_limit"])))
        items = [it for it in self.index["items"] if os.path.exists(self._abs(it))]
        if not items:
            yield event.plain_result(
                "库里还空着。先去群里让人发表情包，我看到了会自己收；"
                "或者看看 /表情帮助。"
            )
            return
        picks = random.sample(items, min(count, len(items)))
        chain = [Image.fromFileSystem(self._abs(it)) for it in picks]
        self._mark_used([it["id"] for it in picks])
        yield event.chain_result(chain)

    @filter.command("表情库")
    async def cmd_status(self, event: AstrMessageEvent):
        items = self.index["items"]
        total = len(items)
        mx = int(self.conf["max_count"])
        warn_at = int(self.conf["warn_at"])
        group_id = str(getattr(event.message_obj, "group_id", "") or "")
        key = self._session_key(group_id, str(event.get_sender_id()))
        pool = self._session_pool(key)
        pool_hint = (
            f"这个会话还有 {len(pool)} 条图片消息在候选池里，看上了引用它发 /收"
            if pool
            else "这个会话还没有候选图，等群友发表情包"
        )

        if not total:
            yield event.plain_result(
                f"0/{mx} 张，一张都没收。\n{pool_hint}（/收列表 看清单）。"
            )
            return

        by_dir: dict[str, int] = {}
        for it in items:
            key_dir = it["file"].split("/")[0]
            by_dir[key_dir] = by_dir.get(key_dir, 0) + 1
        top = sorted(items, key=lambda x: int(x.get("uses") or 0), reverse=True)[:3]
        idle = sum(1 for it in items if not int(it.get("uses") or 0))
        size_mb = sum(float(it.get("kb") or 0) for it in items) / 1024

        lines = [f"表情包库：{total}/{mx} 张（{size_mb:.1f}MB），从没发过的有 {idle} 张"]
        lines.append("来源：" + "、".join(f"{k} {v}张" for k, v in sorted(by_dir.items())[:6]))
        if top and int(top[0].get("uses") or 0) > 0:
            lines.append(
                "最常用："
                + "、".join(f"#{it['id']}({it['uses']}次)" for it in top if int(it.get("uses") or 0))
            )
        if total >= mx:
            lines.append(f"⚠ 满了，新的收不进来。发 /表情清理 {DEFAULT_CLEAN} 腾位置。")
        elif total >= warn_at:
            lines.append(
                f"⚠ 快满了（还差 {mx - total} 张）。想清就 /表情清理 {DEFAULT_CLEAN}，"
                "按用得最少删。"
            )
        lines.append(pool_hint)
        lines.append("管理：/表情列表 · /表情删 12 · /表情清理 20")
        yield event.plain_result("\n".join(lines))

    @filter.command("表情列表")
    async def cmd_list(self, event: AstrMessageEvent):
        arg = self._arg_after(event.message_str, "表情列表")
        limit = int(arg) if arg.isdigit() else 15
        limit = min(max(limit, 1), 50)
        items = self.index["items"]
        if not items:
            yield event.plain_result("库里还是空的。")
            return
        rows = list(reversed(items))[:limit]
        lines = [f"最近收录的 {len(rows)} 张（共 {len(items)} 张）："]
        for it in rows:
            lines.append(
                f"#{it['id']} {it.get('w')}x{it.get('h')} {_human_kb(float(it.get('kb') or 0))}"
                f" 用{int(it.get('uses') or 0)}次 最近{_short_time(it.get('last_used'))}"
                f" ← {it.get('user_name')} @ {it['file'].split('/')[0]}"
            )
        yield event.plain_result("\n".join(lines))

    @filter.command("表情删")
    async def cmd_delete(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("删表情包得大肥鱼自己来，你发 /表情 用就行。")
            return
        arg = self._arg_after(event.message_str, "表情删")
        ids = {int(x) for x in arg.replace(",", " ").split() if x.isdigit()}
        if not ids:
            yield event.plain_result("给我编号，比如 /表情删 12 或者 /表情删 12 15。")
            return
        removed = self._drop(ids)
        if not removed:
            yield event.plain_result("这几个编号没找到。")
            return
        yield event.plain_result(
            f"删了 {len(removed)} 张：{'、'.join('#' + str(it['id']) for it in removed)}。"
            f"现在 {len(self.index['items'])}/{self.conf['max_count']} 张。"
        )

    @filter.command("表情清理")
    async def cmd_cleanup(self, event: AstrMessageEvent):
        if not self._is_owner(event):
            yield event.plain_result("清库得大肥鱼自己来。")
            return
        arg = self._arg_after(event.message_str, "表情清理")
        count = int(arg) if arg.isdigit() else DEFAULT_CLEAN
        count = min(max(count, 1), 100)
        items = self.index["items"]
        if not items:
            yield event.plain_result("库是空的，没什么好清的。")
            return
        victims = self._least_used(count)
        removed = self._drop({it["id"] for it in victims})
        rest = len(self.index["items"])
        lines = [
            f"按「用得最少 + 最久没发」清了 {len(removed)} 张，现在 {rest}/{self.conf['max_count']} 张。"
        ]
        preview = "、".join(
            f"#{it['id']}({int(it.get('uses') or 0)}次)" for it in removed[:8]
        )
        if preview:
            lines.append("清掉的：" + preview + ("…" if len(removed) > 8 else ""))
        if rest < int(self.conf["warn_at"]):
            lines.append("这下松快了。")
        yield event.plain_result("\n".join(lines))

    @filter.command("表情诊断")
    async def cmd_debug(self, event: AstrMessageEvent):
        """看最近 10 条图片消息的处理流水，收不到图时用这个排查。"""
        hooked = "、".join(str(getattr(b, "self_id", "?")) for b, _ in self._hooked.values())
        lines = [
            f"收集开关：{'开' if self.conf['enabled'] else '关'}｜"
            f"模式：{'自动全收' if self.conf['auto_collect'] else '手动挑（/收）'}｜"
            f"库：{len(self.index['items'])}/{self.conf['max_count']} 张｜"
            f"候选池：{len(self._recent)} 条｜"
            f"监听平台：{len(self._hooked)} 个（{hooked or '无'}）"
        ]
        if not self._debug:
            lines.append("还没有处理过任何图片消息。发张图试试，或者确认机器人真在这个群里。")
        else:
            lines.append("最近的处理流水：")
            lines.extend(reversed(self._debug[-10:]))
        yield event.plain_result("\n".join(lines))

    @filter.command("表情帮助")
    async def cmd_help(self, event: AstrMessageEvent):
        mx = self.conf["max_count"]
        mode = "自动全收（来者不拒）" if self.conf["auto_collect"] else "只收我看着顺眼的"
        yield event.plain_result(
            f"表情包口袋（当前模式：{mode}）：\n"
            "/收 —— 引用那张表情包发，收进库\n"
            "/收列表 —— 这个会话刚才有哪些图可以收\n"
            "/表情 —— 随机发一张（/表情 3 连发，最多 "
            f"{self.conf['send_limit']} 张）\n"
            "/表情库 —— 现在多少张、哪个群收得多、快满没\n"
            "/表情列表 15 —— 最近收的，带编号\n"
            f"/表情清理 {DEFAULT_CLEAN} —— 删用得最少的 {DEFAULT_CLEAN} 张（大肥鱼）\n"
            "/表情删 12 —— 按编号删（大肥鱼）\n"
            f"别人发的表情包我只放进候选池，上限 {mx} 张，"
            "想留哪张就引用它发 /收。"
        )

    # ---------------- 删除 ----------------
    def _least_used(self, count: int) -> list[dict]:
        items = list(self.index["items"])
        items.sort(
            key=lambda it: (
                int(it.get("uses") or 0),
                it.get("last_used") or "0",
                it.get("saved_at") or "0",
            )
        )
        return items[:count]

    def _drop(self, ids: set[int]) -> list[dict]:
        removed: list[dict] = []
        keep: list[dict] = []
        for it in self.index["items"]:
            if it.get("id") in ids:
                removed.append(it)
            else:
                keep.append(it)
        if not removed:
            return []
        for it in removed:
            try:
                path = self._abs(it)
                if os.path.isfile(path):
                    os.remove(path)
            except OSError as e:
                logger.warning(f"[sticker] 删文件失败 {it.get('file')}：{e}")
        self.index["items"] = keep
        if len(keep) < int(self.conf["warn_at"]):
            self.index["warned_at"] = 0
        try:
            self._save_index()
        except Exception as e:
            logger.warning(f"[sticker] 写索引失败：{e}")
        return removed
