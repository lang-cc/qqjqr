# -*- coding: utf-8 -*-
"""时事与热梗：让她知道外面在发生什么。

每天定时搜一次热搜/热梗/新闻，让模型提炼成几条「一句话」，
存进 news.json（不进记忆库，避免挤掉真正重要的事），3 天自动过期。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from astrbot.api import logger

from .web import (
    bili_bangumi,
    bili_comments,
    bili_feed,
    bili_hot_words,
    bili_popular_items,
    bing_search,
)

# (类型, 搜索词)
TOPICS = (
    ("热搜", "今日微博热搜 热点话题"),
    ("热梗", "最近流行的网络热梗 梗是什么意思"),
    ("时事", "今天重要新闻 头条"),
)

EXTRACT_PROMPT = """下面是搜索得到的网页片段，以及她在B站刷到的东西。请挑出 3 条**最近确实在发生或正在流行**的内容。

硬要求：
- 每条一行，格式：类型|一句话
- 类型只能是：热搜 / 热梗 / 时事 / B站
- 一句话 25 字以内
- B站那类要写成「B站上在火什么」，比如「B站都在刷XX挑战」「B站热搜是XX」
- 只要真正新鲜的：广告、"2023年""去年"这类过期内容、无关的百科介绍，全部丢掉
- 判断不出是不是最近的，就跳过
- 宁少勿滥，凑不满 3 条就少给

素材：
{body}"""


def bili_material(n_feed: int = 6, n_comments: int = 3) -> str:
    """把B站此刻的东西收成素材：推荐流、热搜词、热门榜、番剧榜，外加一条视频的热评。"""
    lines: list[str] = []
    try:
        feed = bili_feed(n_feed)
        for r in feed:
            lines.append(
                f"[B站推荐流] {r['title']}（UP：{r['up']}，播放 {r['play']}，{r['duration']}）"
            )
        for w in bili_hot_words(8):
            lines.append(f"[B站热搜] {w}")
        for r in bili_popular_items(5):
            lines.append(f"[B站热门] {r['title']}（UP：{r['up']}，播放 {r['play']}）")
        ban = bili_bangumi(3)
        if ban:
            for ln in ban.splitlines()[1:]:
                ln = ln.strip()
                if ln:
                    lines.append(f"[B站番剧] {ln}")
        # 挑推荐流第一条，看它的热评——这才叫真在刷
        for r in feed[:2]:
            cm = bili_comments(r["bvid"], n_comments)
            if cm:
                for ln in cm.splitlines()[1:]:
                    ln = ln.strip()
                    if ln:
                        lines.append(f"[B站评论区] {ln}")
                break
    except Exception as e:  # noqa: BLE001
        logger.debug(f"[news] B站素材抓取失败：{e}")
    return "\n".join(lines)


def _load(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text(encoding="utf-8") or "{}")
    except Exception:
        pass
    return {"at": 0, "items": []}


def _save(path: Path, data: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def alive_items(data: dict, ttl_days: int = 3, now: float | None = None) -> list[dict]:
    """还没过期的条目。"""
    now = now or time.time()
    ttl = max(1, int(ttl_days)) * 86400
    out = []
    for it in data.get("items") or []:
        if not isinstance(it, dict):
            continue
        if now - float(it.get("at") or 0) <= ttl and str(it.get("text") or "").strip():
            out.append(it)
    return out


def _parse(text: str) -> list[str]:
    out = []
    for line in (text or "").splitlines():
        ln = line.strip().lstrip("-•*0123456789.、) ").strip()
        if not ln or "|" not in ln:
            continue
        kind, _, body = ln.partition("|")
        body = body.strip()
        if len(body) < 4 or len(body) > 60:
            continue
        out.append(f"{kind.strip()}|{body}")
    return out[:4]


async def learn(provider, path: Path, ttl_days: int = 3, with_bili: bool = True) -> list[str]:
    """搜一轮 + 刷一轮B站、提炼一轮、存进去。返回这次学到的东西。"""
    if provider is None:
        logger.warning("[news] 没有可用模型，跳过这次学习")
        return []

    # 抓素材（每类取前 4 条摘要）
    chunks = []
    for kind, query in TOPICS:
        rows = bing_search(query, 4)
        if not rows:
            continue
        part = "\n".join(f"[{kind}] {r['title']} —— {r['snippet']}" for r in rows)
        chunks.append(part)

    # B站：推荐流 + 热搜 + 热门榜 + 一条视频的热评
    if with_bili:
        mat = bili_material()
        if mat:
            chunks.append(mat)

    if not chunks:
        logger.warning("[news] 搜索和B站都没有结果，跳过")
        return []

    body = "\n\n".join(chunks)[:4000]
    try:
        resp = await provider.text_chat(
            prompt=EXTRACT_PROMPT.format(body=body),
            system_prompt="你是一个只做信息提炼的助手。严格按要求输出，不要解释。",
        )
        raw = getattr(resp, "completion_text", "") or ""
    except Exception as e:  # noqa: BLE001
        logger.warning(f"[news] 提炼失败：{e}")
        return []

    got = _parse(raw)
    if not got:
        logger.info("[news] 这轮没提炼出有效内容")
        return []

    now = time.time()
    data = _load(path)
    items = alive_items(data, ttl_days, now)
    seen = {str(i.get("text") or "") for i in items}
    for line in got:
        kind, _, text = line.partition("|")
        if text in seen:
            continue
        seen.add(text)
        items.append({"kind": kind, "text": text, "at": now})

    # 保留最近的 12 条，别越堆越多
    items = items[-12:]
    _save(path, {"at": now, "items": items})
    logger.info(f"[news] 本轮学到 {len(got)} 条，池内共 {len(items)} 条")
    return [i["text"] for i in items[-len(got):]]


def brief_for_prompt(data: dict, ttl_days: int = 3, limit: int = 3) -> str:
    """给模型看的一小段「外面在聊什么」。"""
    items = alive_items(data, ttl_days)
    if not items:
        return ""
    picked = items[-limit:]
    lines = [f"- {it['text']}" for it in picked]
    return (
        "【外面最近在聊】\n"
        + "\n".join(lines)
        + "\n（你知道这些就行，不用硬提；聊到相关话题时自然带一句，别像播新闻）"
    )
