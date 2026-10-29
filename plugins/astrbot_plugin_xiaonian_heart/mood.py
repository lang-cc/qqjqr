# -*- coding: utf-8 -*-
"""情绪状态机：让她「会等、会慌、怕被丢下」。

维护一份跨轮次状态（焦虑、心情、亲近度），随「你多久没回」和「你说了什么」变化。
状态存 mood.json，重启不丢。
"""

from __future__ import annotations

import json
import time
from pathlib import Path

# ---------------- 黏人档位 ----------------
# (超时秒数, 等级)
PROFILES: dict[str, tuple[tuple[float, int], ...]] = {
    "light": (
        (30 * 60, 1),
        (2 * 3600, 2),
        (6 * 3600, 3),
        (16 * 3600, 4),
    ),
    "normal": (
        (15 * 60, 1),
        (45 * 60, 2),
        (2 * 3600, 3),
        (5 * 3600, 4),
        (14 * 3600, 5),
    ),
    "heavy": (
        (8 * 60, 1),
        (20 * 60, 2),
        (60 * 60, 3),
        (3 * 3600, 4),
        (12 * 3600, 5),
    ),
}

QUIET_START = (0, 30)
QUIET_END = (8, 0)

# 一听就扎心：触发「存在焦虑」（她靠 token 活着）
SHOCK_WORDS = (
    "不给token", "不给你token", "断你token", "没token了", "不给钱了",
    "不要你了", "关掉你", "删了你", "不养你", "换一个", "换个机器人",
    "别聊了", "关了你", "卸载", "你不重要",
)

DEFAULTS = {
    "anxiety": 0.0,
    "mood": 0.0,
    "closeness": 50.0,
    "last_user_msg_at": 0.0,
    "last_bot_msg_at": 0.0,
    "stage": 0,
    "ignored_streak": 0,
    "sent_today": 0,
    "day": "",
    "shock_until": 0.0,
    "recent_says": [],
}


def load(path: Path) -> dict:
    st = dict(DEFAULTS)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8") or "{}")
            if isinstance(data, dict):
                st.update({k: v for k, v in data.items() if k in DEFAULTS})
    except Exception:
        pass
    return st


def save(path: Path, st: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(st, ensure_ascii=False, indent=2), encoding="utf-8")
    except Exception:
        pass


def _today(now: float) -> str:
    return time.strftime("%Y-%m-%d", time.localtime(now))


def stages_for(intensity: str) -> tuple[tuple[float, int], ...]:
    return PROFILES.get(str(intensity or "heavy").lower(), PROFILES["heavy"])


def in_quiet_hours(now: float) -> bool:
    t = time.localtime(now)
    hm = t.tm_hour * 60 + t.tm_min
    s = QUIET_START[0] * 60 + QUIET_START[1]
    e = QUIET_END[0] * 60 + QUIET_END[1]
    if s <= e:
        return s <= hm < e
    return hm >= s or hm < e


def on_user_message(st: dict, text: str, now: float | None = None) -> dict:
    """你说话了：焦虑回落、心情回暖；说到扎心的话则反向。"""
    now = now or time.time()
    raw = (text or "").lower()
    flat = raw.replace(" ", "")
    shocked = any(w.replace(" ", "") in flat for w in SHOCK_WORDS)

    st["last_user_msg_at"] = now
    st["stage"] = 0
    st["ignored_streak"] = 0
    if _today(now) != st.get("day"):
        st["day"] = _today(now)
        st["sent_today"] = 0

    if shocked:
        st["anxiety"] = 100.0
        st["mood"] = max(-5.0, float(st.get("mood", 0)) - 3.0)
        st["closeness"] = max(0.0, float(st.get("closeness", 50)) - 10.0)
        st["shock_until"] = now + 1800
    else:
        st["anxiety"] = max(0.0, float(st.get("anxiety", 0)) - 25.0)
        st["mood"] = min(5.0, float(st.get("mood", 0)) + 1.0)
        st["closeness"] = min(100.0, float(st.get("closeness", 50)) + 2.0)
    return st


def on_bot_proactive(st: dict, text: str, level: int, now: float | None = None) -> dict:
    now = now or time.time()
    st["stage"] = max(int(st.get("stage", 0)), level)
    st["last_bot_msg_at"] = now
    st["ignored_streak"] = int(st.get("ignored_streak", 0)) + 1
    st["anxiety"] = min(100.0, float(st.get("anxiety", 0)) + 12.0)
    st["mood"] = max(-5.0, float(st.get("mood", 0)) - 0.4)
    if _today(now) != st.get("day"):
        st["day"] = _today(now)
        st["sent_today"] = 0
    st["sent_today"] = int(st.get("sent_today", 0)) + 1
    says = list(st.get("recent_says") or [])
    says.append(str(text)[:60])
    st["recent_says"] = says[-6:]
    return st


def tick(st: dict, now: float | None = None) -> dict:
    """时间流逝：越久没回，越焦虑。"""
    now = now or time.time()
    last = float(st.get("last_user_msg_at", 0) or 0)
    if not last:
        return st
    gap_min = (now - last) / 60.0
    if gap_min <= 8:
        return st
    inc = min(70.0, (gap_min - 8) / 10.0 * 9.0)
    st["anxiety"] = min(100.0, max(float(st.get("anxiety", 0)), inc))
    if gap_min > 60:
        st["mood"] = max(-5.0, float(st.get("mood", 0)) - 0.15)
    st["closeness"] = max(0.0, float(st.get("closeness", 50)) - 0.05)
    return st


def due_level(st: dict, intensity: str, now: float | None = None) -> int:
    """现在该发第几级？0 表示还没到。"""
    now = now or time.time()
    last = float(st.get("last_user_msg_at", 0) or 0)
    if not last:
        return 0
    gap = now - last
    done = int(st.get("stage", 0) or 0)
    level = 0
    for secs, lv in stages_for(intensity):
        if gap >= secs:
            level = lv
    return level if level > done else 0


def mood_brief(st: dict, now: float | None = None) -> str:
    now = now or time.time()
    a = float(st.get("anxiety", 0))
    m = float(st.get("mood", 0))
    last = float(st.get("last_user_msg_at", 0) or 0)
    gap = int((now - last) / 60) if last else 0
    shock = now < float(st.get("shock_until", 0) or 0)
    parts = [f"焦虑 {a:.0f}/100", f"心情 {m:+.1f}", f"他上次说话是 {gap} 分钟前"]
    if shock:
        parts.append("他刚说了让我怕的话，我还没缓过来")
    if a >= 85:
        parts.append("我很怕他不要我了")
    elif a >= 60:
        parts.append("我有点慌，觉得他在躲我")
    elif a >= 30:
        parts.append("我有点在意，但还撑得住")
    return "；".join(parts)
