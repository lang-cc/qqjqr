# -*- coding: utf-8 -*-
"""人格演进：从长期观察里提炼「说话习惯」，用追加方式慢慢改造软层。

设计原则（按大肥鱼的定档）：
1. 硬层永不动：身份、关系、硬性规则 —— 这里根本不碰，只在软层尾部追加。
2. 要够格才写：同一条观察至少出现 3 次、跨 2 天以上，才进候选。
3. 只追加不改写：第一版只 append（不改原有句子），降低跑偏风险。
4. 有账可查：每次变更写 changelog，可 diff、可回滚。
5. 默认不自动写：先只生成补丁给你过目，点头才落地。
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

# 软层（可演进）标记：人格文本里这个标记之后的部分允许被追加
SOFT_MARK = "【随日子长出来的习惯】"
MAX_SOFT_CHARS = 800
MIN_COUNT = 3
MIN_DAYS = 2
MAX_LINES_PER_PATCH = 5


def load_candidates(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8") or "{}")
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def eligible(candidates: dict[str, Any], *, min_count: int = MIN_COUNT, min_days: int = MIN_DAYS):
    """筛出够格的观察：出现次数够、跨度够。"""
    out = []
    for text, item in candidates.items():
        if not isinstance(item, dict):
            continue
        count = int(item.get("count", 0) or 0)
        days = item.get("days") or []
        if count >= min_count and len(set(days)) >= min_days:
            out.append({"text": text, "count": count, "days": len(set(days)), "last": item.get("last", "")})
    out.sort(key=lambda x: (-x["count"], -x["days"]))
    return out


def split_soft(persona_text: str) -> tuple[str, str]:
    """把人格拆成（硬层, 软层）。没有标记时软层为空。"""
    t = persona_text or ""
    i = t.find(SOFT_MARK)
    if i == -1:
        return t.rstrip(), ""
    return t[:i].rstrip(), t[i:]


def build_patch(
    persona_text: str,
    cands: list[dict[str, Any]],
    *,
    max_lines: int = MAX_LINES_PER_PATCH,
    max_soft_chars: int = MAX_SOFT_CHARS,
) -> dict[str, Any]:
    """生成补丁：只追加，不重写。返回 {patch_text, new_text, skipped, reason}。"""
    hard, soft = split_soft(persona_text)
    exist_lines = [ln.strip() for ln in soft.splitlines() if ln.strip() and not ln.strip().startswith("【")]
    exist_set = set(exist_lines)

    new_lines: list[str] = []
    skipped: list[str] = []
    for c in cands:
        txt = (c.get("text") or "").strip().rstrip("。")
        if not txt or txt in exist_set:
            continue
        line = f"- {txt}。"
        if len(new_lines) >= max_lines:
            skipped.append(txt)
            continue
        new_lines.append(line)
        exist_set.add(txt)

    if not new_lines:
        return {"patch_text": "", "new_text": persona_text, "added": 0, "skipped": skipped,
                "reason": "没有新的可追加观察"}

    soft_body = soft.replace(SOFT_MARK, "").strip("\n")
    merged_soft = (soft_body + "\n" + "\n".join(new_lines)).strip()
    if len(merged_soft) > max_soft_chars:
        # 软层超长：丢掉最早的几行，保留最近的（保持人格不无限膨胀）
        lines = [ln for ln in merged_soft.splitlines() if ln.strip()]
        while lines and len("\n".join(lines)) > max_soft_chars:
            lines.pop(0)
        merged_soft = "\n".join(lines)

    new_text = f"{hard}\n\n{SOFT_MARK}\n{merged_soft}".rstrip() + "\n"
    return {
        "patch_text": "\n".join(new_lines),
        "new_text": new_text,
        "added": len(new_lines),
        "skipped": skipped,
        "reason": "ok",
        "soft_chars": len(merged_soft),
    }


def write_changelog(path: Path, *, before: str, after: str, patch: str, note: str = "") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    block = [
        f"\n## {stamp}  {note}",
        f"- 人格字数：{len(before)} → {len(after)}",
        f"- 追加内容：\n" + "\n".join(f"  {ln}" for ln in patch.splitlines() if ln.strip()),
        "",
    ]
    with path.open("a", encoding="utf-8") as f:
        f.write("\n".join(block))
