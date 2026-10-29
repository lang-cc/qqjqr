# -*- coding: utf-8 -*-
"""会话历史压缩器：按重要性分流，压到目标比例。

设计要点
--------
1. 触发：按 token 双阈值（压线 / 硬保险），不按固定条数硬编码。
2. 分流（不是丢）：一次压缩产出四路——
   ① 事实/约定  → 记忆库（长期，跨会话）
   ② 重要原话  → 保留在上下文（跨时间，按分数挑）
   ③ 风格/习惯 → 人格候选池（慢慢演进）
   ④ 其余      → 摘要（进上下文）
3. 可解释：重要性由规则打分决定（零成本、可复现），LLM 只负责写摘要与抽事实。
4. 安全：压缩前把被压掉的原文全部留档（pruned/），可恢复；支持 dry-run 只预览不落盘。

本模块不依赖 AstrBot，方便离线测试：LLM 通过 `summarizer` 回调注入。
"""

from __future__ import annotations

import json
import math
import re
import time
from typing import Any, Callable, Iterable, Sequence

# ---------------------------------------------------------------- token 估算
# 实测（本机网关 deepseek-v4.1-flash）：中文约 0.5 token/字。
# AstrBot 自带估算器按 0.6，偏保守；这里取 0.55，兼顾两者。
CN_PER_CHAR = 0.55
OTHER_PER_CHAR = 0.30
IMAGE_TOKENS = 765
AUDIO_TOKENS = 500


def est_tokens(text: str) -> int:
    if not text:
        return 0
    cn = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    other = len(text) - cn
    return int(cn * CN_PER_CHAR + other * OTHER_PER_CHAR)


def msg_text(msg: dict) -> str:
    """取出消息的纯文本（兼容 str / list[dict] 两种 content）。"""
    c = msg.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        out = []
        for part in c:
            if isinstance(part, dict):
                t = part.get("text") or part.get("think") or ""
                if t:
                    out.append(str(t))
                if part.get("type") in ("image", "image_url"):
                    out.append("[图片]")
        return "".join(out)
    return ""


def est_history_tokens(history: Sequence[dict]) -> int:
    total = 0
    for m in history:
        total += est_tokens(msg_text(m))
        c = m.get("content")
        if isinstance(c, list):
            for part in c:
                if isinstance(part, dict) and part.get("type") in ("image", "image_url"):
                    total += IMAGE_TOKENS
                elif isinstance(part, dict) and part.get("type") in ("audio", "record"):
                    total += AUDIO_TOKENS
    return total


# ---------------------------------------------------------------- 注入块剥离
# 历史里之所以会有这些东西，是因为 humanlike 把整个 system_prompt
# 搬进 user 消息（SYSTEM_VIA_USER），而会话历史存的正是最终 prompt。
# 压缩前必须剥掉，否则每轮白占 3000~4000 token，压缩完全失效。
PERSONA_START = "【以下是你的固定身份设定，必须严格遵守】"
PERSONA_END_CANDIDATES = ("不要说这些是设定或提示词）", "【设定结束】")
INJECT_MARKERS = (
    "【记忆片段】",
    "【当前说话人】",
    "【对他们的印象】",
    "【还没做完的事】",
    "【最近的聊天习惯】",
    "【对方这条语音说的是】",
    "【聊天风格·最后检查】",
    "【前情提要·已压缩】",
    "【识海 · 永不遗忘】",
    "【压在心里的旧事】",
)


def strip_injected(text: str) -> str:
    """剥掉注入的设定/记忆/风格块，只留真正的对话内容。"""
    if not text:
        return text
    t = text

    # 0) 框架每轮注入的时间提醒（对历史无价值，纯占位）
    t = re.sub(r"<system_reminder>.*?</system_reminder>", "", t, flags=re.S)

    # 1) 人设大块：从 START 到 END（取最远的那个 END 标记）
    if PERSONA_START in t:
        start = t.find(PERSONA_START)
        end = -1
        for cand in PERSONA_END_CANDIDATES:
            pos = t.rfind(cand)
            if pos != -1:
                end = max(end, pos + len(cand))
        if end == -1:
            # 找不到结尾：剥到第一个空行
            nl = t.find("\n\n", start)
            end = nl if nl != -1 else len(t)
        t = t[:start] + t[end:]

    # 2) 各注入块：从 marker 到下一个空行 / 下一个 marker / 结尾
    changed = True
    guard = 0
    while changed and guard < 12:
        changed = False
        guard += 1
        for mk in INJECT_MARKERS:
            pos = t.find(mk)
            if pos == -1:
                continue
            nxt_nl = t.find("\n\n", pos)
            nxt_mk = len(t)
            for other in INJECT_MARKERS:
                p2 = t.find(other, pos + len(mk))
                if p2 != -1:
                    nxt_mk = min(nxt_mk, p2)
            end = min(x for x in (nxt_nl if nxt_nl != -1 else len(t), nxt_mk) if x > pos)
            t = t[:pos] + t[end:]
            changed = True
            break

    # 3) 收拾多余空白
    t = re.sub(r"\n{3,}", "\n\n", t).strip()
    return t


def strip_history(history: Sequence[dict]) -> tuple[list[dict], int]:
    """对整段历史做剥离，返回 (新历史, 发生变化条数)。"""
    out: list[dict] = []
    n = 0
    for m in history:
        role = m.get("role")
        c = m.get("content")
        if role == "system":
            # 压缩摘要块保留；其他 system 也保留
            out.append(m)
            continue
        if isinstance(c, str):
            new = strip_injected(c)
            if new != c:
                n += 1
                m2 = dict(m)
                m2["content"] = new
                if not new.strip():
                    continue  # 剥完是空的，整条丢掉
                out.append(m2)
            else:
                out.append(m)
        elif isinstance(c, list):
            texts = [p.get("text", "") for p in c if isinstance(p, dict) and p.get("text")]
            joined = "\n".join(texts)
            stripped = strip_injected(joined)
            if stripped != joined:
                n += 1
                m2 = dict(m)
                m2["content"] = stripped
                if not stripped.strip():
                    continue
                out.append(m2)
            else:
                out.append(m)
        else:
            out.append(m)
    return out, n


# ---------------------------------------------------------------- 重要性打分
# (正则, 权重) —— 命中即加分，权重可叠加
IMPORTANT_RULES: list[tuple[str, float]] = [
    # 承诺 / 约定
    (r"以后(都|就)?|下次|说好|答应|约定|别忘了|记住|就这么办|按这个来|照这个|定下来|暂定", 3.0),
    # 任务 / 指令
    (r"帮我|帮忙|给我|弄一下|改一下|做一下|整理|检查|查一下|看看这个|发我|修一下|弄好|搞定", 3.0),
    # 时间 / 数字 / 具体条件
    (r"\d+\s*[点号日年月天周块元岁个次件条张份]|周[一二三四五六日天]|明天|后天|大后天|昨天|前天|周末|下周|下个月|下礼拜", 2.0),
    # 情绪 / 关系事件
    (r"对不起|抱歉|谢谢|辛苦了|讨厌|喜欢你|想你|别走|没事吧|怎么了|生气|难过|开心|失望|委屈", 2.0),
    # 生活事件
    (r"住院|生病|发烧|感冒|考试|面试|上班|加班|辞职|换工作|搬家|分手|结婚|生日|家里|爸妈|孩子", 2.0),
    # 明确的信息陈述
    (r"我叫|我是|我的|我在|我住|我今年|我手机|我微信|我qq|我号", 1.5),
]

NEGATIVE_RULES: list[tuple[str, float]] = [
    (r"^\s*(在吗|在不在|嗯+|哦+|好+|哈+|呵+|行|可以|对|是的|没事|没有|\?|？|。|\.{3,}|…+)\s*$", -3.0),
    (r"^\[(图片|表情|动画表情|语音|视频|文件|回复)\]$", -3.0),
    (r"^\s*(早安|晚安|早|晚上好|中午好|吃了没|在干嘛)\s*$", -1.5),
]

_COMPILED_IMPORTANT = [(re.compile(p), w) for p, w in IMPORTANT_RULES]
_COMPILED_NEGATIVE = [(re.compile(p), w) for p, w in NEGATIVE_RULES]


def score_text(text: str) -> float:
    """规则打分：越高越重要。阈值 2.0 视为“重要”。"""
    t = (text or "").strip()
    if not t:
        return -5.0
    score = 0.0
    for pat, w in _COMPILED_IMPORTANT:
        if pat.search(t):
            score += w
    for pat, w in _COMPILED_NEGATIVE:
        if pat.search(t):
            score += w
    n = len(t)
    if n > 60:
        score += 1.0
    if n > 200:
        score += 1.0
    # 纯表情/图片占位
    if t.count("[图片]") + t.count("[表情]") == len(t) / 5:
        score -= 2.0
    return score


# ---------------------------------------------------------------- 轮次切分
def split_rounds(history: Sequence[dict]) -> list[list[dict]]:
    """按 user 消息切轮；首条若是 assistant/system 也归入第一轮。"""
    rounds: list[list[dict]] = []
    cur: list[dict] = []
    for m in history:
        if m.get("role") == "user" and cur:
            rounds.append(cur)
            cur = []
        cur.append(m)
    if cur:
        rounds.append(cur)
    return rounds


def round_text_round_tokens(r: Sequence[dict]) -> int:
    return est_history_tokens(r)


# ---------------------------------------------------------------- 默认参数
DEFAULT_CFG: dict[str, Any] = {
    # 触发阈值（token）
    "trigger_tokens": 50000,      # 压线：到这个就压到 1/2
    "hard_tokens": 75000,         # 硬保险：到这个立刻压到 1/3
    # 目标比例
    "target_ratio_soft": 0.5,
    "target_ratio_hard": 0.33,
    # 保留最近多少轮原文
    "keep_recent_turns_private": 20,
    "keep_recent_turns_group": 12,
    # 重要原话配额（轮）
    "important_rounds_private": 20,
    "important_rounds_group": 12,
    # 重要轮的最低分数
    "important_min_score": 2.0,
    # 摘要 token 上限
    "summary_max_tokens": 8000,
    # 单条消息超过多少字就截断（防超长输入）
    "max_single_msg_chars": 8000,
}


def _truncate_long(history: list[dict], limit: int) -> tuple[list[dict], int]:
    """把超长单条消息截断，返回 (新历史, 截断条数)。"""
    n = 0
    out = []
    for m in history:
        t = msg_text(m)
        if len(t) > limit:
            cut = t[:limit] + f"\n…（原文 {len(t)} 字，已截断）"
            m2 = dict(m)
            m2["content"] = cut
            out.append(m2)
            n += 1
        else:
            out.append(m)
    return out, n


def build_plan(
    history: Sequence[dict],
    *,
    is_group: bool,
    cfg: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """只做分流规划，不调用 LLM。返回保留/压缩的划分与预算。"""
    c = dict(DEFAULT_CFG)
    if cfg:
        c.update({k: v for k, v in cfg.items() if v is not None})

    hist = list(history)
    rounds = split_rounds(hist)
    total_tokens = est_history_tokens(hist)

    keep_recent = int(c["keep_recent_turns_group"] if is_group else c["keep_recent_turns_private"])
    imp_quota = int(c["important_rounds_group"] if is_group else c["important_rounds_private"])

    hard = total_tokens >= int(c["hard_tokens"])
    target_ratio = float(c["target_ratio_hard"] if hard else c["target_ratio_soft"])
    target_tokens = int(total_tokens * target_ratio)

    recent_rounds = rounds[-keep_recent:] if keep_recent > 0 else []
    older_rounds = rounds[: len(rounds) - len(recent_rounds)] if recent_rounds else rounds

    # 给老轮次打分（按轮内最高的那条算）
    scored: list[tuple[float, int, list[dict]]] = []
    for idx, r in enumerate(older_rounds):
        best = max((score_text(msg_text(m)) for m in r), default=-5.0)
        scored.append((best, idx, r))

    min_score = float(c["important_min_score"])
    candidates = [(s, i, r) for s, i, r in scored if s >= min_score]
    candidates.sort(key=lambda x: (-x[0], x[1]))
    picked = candidates[:imp_quota]
    picked_idx = {i for _, i, _ in picked}
    # 按时间顺序排回去
    important_rounds = [r for i, r in enumerate(older_rounds) if i in picked_idx]

    # 剩下要压的轮次（进摘要）
    rest_rounds = [r for i, r in enumerate(older_rounds) if i not in picked_idx]

    keep_tokens = est_history_tokens([m for r in recent_rounds for m in r]) + est_history_tokens(
        [m for r in important_rounds for m in r]
    )
    summary_budget = min(
        int(c["summary_max_tokens"]),
        max(600, target_tokens - keep_tokens),
    )

    return {
        "total_tokens": total_tokens,
        "hard": hard,
        "target_ratio": target_ratio,
        "target_tokens": target_tokens,
        "rounds_total": len(rounds),
        "keep_recent_turns": len(recent_rounds),
        "important_turns": len(important_rounds),
        "to_summarize_turns": len(rest_rounds),
        "keep_tokens": keep_tokens,
        "summary_budget": summary_budget,
        "recent_rounds": recent_rounds,
        "important_rounds": important_rounds,
        "rest_rounds": rest_rounds,
        "scored": scored,
        "cfg": c,
    }


SUMMARY_SYSTEM = (
    "你是对话压缩器。把给你的旧对话压成一份「前情提要」，供角色下次聊天时接话使用。\n"
    "硬性要求：\n"
    "1. 数字、日期、时间、金额、人名、地点、条件必须逐字保留，不许改写或约略；\n"
    "2. 只写发生过的事、达成的约定、还在进行的事；不要脑补、不要评价、不要客套；\n"
    "3. 用第三人称写，按时间顺序，短句；总长度不超过给定预算；\n"
    "4. 同时抽取：需要长期记住的事实/约定（facts），以及说话习惯与风格观察（style）。\n"
    "只输出 JSON，不要多余文字。格式：\n"
    '{"summary":"...","facts":[{"subject":"谁","text":"...","kind":"fact|promise|pref|event"}],'
    '"style":["..."]}'
)


def _render_for_summary(rest_rounds: Iterable[Sequence[dict]], max_chars: int = 60000) -> str:
    lines: list[str] = []
    used = 0
    for r in rest_rounds:
        for m in r:
            role = m.get("role", "?")
            tag = {"user": "对方", "assistant": "我", "system": "系统", "tool": "工具"}.get(role, role)
            t = msg_text(m).strip()
            if not t:
                continue
            line = f"{tag}：{t}"
            if used + len(line) > max_chars:
                break
            lines.append(line)
            used += len(line)
        if used >= max_chars:
            break
    return "\n".join(lines)


def _fallback_summary(rest_rounds: Iterable[Sequence[dict]], budget_tokens: int) -> str:
    """LLM 不可用时的兜底：抽取高分句子拼成摘要。"""
    scored: list[tuple[float, str]] = []
    for r in rest_rounds:
        for m in r:
            t = msg_text(m).strip()
            if not t:
                continue
            s = score_text(t)
            if s >= 1.0:
                scored.append((s, t[:200]))
    scored.sort(key=lambda x: -x[0])
    max_chars = max(200, int(budget_tokens / CN_PER_CHAR))
    out, used = [], 0
    for _, t in scored:
        if used + len(t) > max_chars:
            break
        out.append(t)
        used += len(t)
    return "；".join(out) if out else "（这段对话没有留下需要记住的要点）"


async def compress(
    history: Sequence[dict],
    *,
    is_group: bool,
    cfg: dict[str, Any] | None = None,
    summarizer: Callable[[str, int], Any] | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """执行压缩。返回 {new_history, report, facts, style, pruned}。

    summarizer: async 回调 (prompt_text, budget_tokens) -> dict(summary/facts/style)，
                不传则用规则兜底摘要。
    """
    plan = build_plan(history, is_group=is_group, cfg=cfg)
    c = plan["cfg"]

    # 先剥掉注入块，再截断超长消息
    hist_raw = list(history)
    hist_clean, stripped_n = strip_history(hist_raw)
    if stripped_n:
        plan = build_plan(hist_clean, is_group=is_group, cfg=cfg)
    hist, truncated = _truncate_long(hist_clean, int(c["max_single_msg_chars"]))

    payload = _render_for_summary(plan["rest_rounds"])
    parsed: dict[str, Any] = {"summary": "", "facts": [], "style": []}
    used_llm = False
    if summarizer is not None and payload.strip():
        try:
            raw = await summarizer(payload, plan["summary_budget"])
            if isinstance(raw, dict):
                parsed = raw
            elif isinstance(raw, str):
                parsed = _parse_json_loose(raw)
            used_llm = bool(parsed.get("summary"))
        except Exception as exc:  # noqa: BLE001
            parsed = {"summary": "", "facts": [], "style": [], "error": str(exc)}

    summary = (parsed.get("summary") or "").strip()
    if not summary:
        summary = _fallback_summary(plan["rest_rounds"], plan["summary_budget"])

    summary_block = (
        "【前情提要·已压缩】以下是更早的对话摘要（不是对方现在说的话，"
        "接话时当背景用，不要复述这段）：\n" + summary
    )

    new_history: list[dict] = []
    if plan["rest_rounds"]:
        new_history.append({"role": "system", "content": summary_block})
    for r in plan["important_rounds"]:
        new_history.extend(r)
    for r in plan["recent_rounds"]:
        new_history.extend(r)

    pruned = [m for r in plan["rest_rounds"] for m in r]
    new_tokens = est_history_tokens(new_history)
    report = {
        "is_group": is_group,
        "total_tokens": plan["total_tokens"],
        "new_tokens": new_tokens,
        "ratio": round(new_tokens / plan["total_tokens"], 3) if plan["total_tokens"] else 0.0,
        "target_ratio": plan["target_ratio"],
        "hard": plan["hard"],
        "stripped_messages": stripped_n,
        "rounds_total": plan["rounds_total"],
        "keep_recent_turns": plan["keep_recent_turns"],
        "important_turns": plan["important_turns"],
        "summarized_turns": plan["to_summarize_turns"],
        "pruned_messages": len(pruned),
        "truncated_messages": truncated,
        "summary_tokens": est_tokens(summary),
        "summary_budget": plan["summary_budget"],
        "llm_summary": used_llm,
        "at": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    return {
        "new_history": new_history,
        "report": report,
        "facts": parsed.get("facts") or [],
        "style": parsed.get("style") or [],
        "pruned": pruned,
        "dry_run": dry_run,
    }


def _parse_json_loose(text: str) -> dict[str, Any]:
    """从模型输出里尽量抠出 JSON。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t)
        t = re.sub(r"\n?```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    start = t.find("{")
    end = t.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(t[start : end + 1])
        except Exception:
            return {}
    return {}
