# -*- coding: utf-8 -*-
"""按「干净规则」重新导入记忆：只留中文对话，去掉模型英文思考。

之前的导入把「对方：xxx ｜ 我：<英文推理> + 中文回复」整段塞进记忆，
导致她检索到一堆英文思考，还学会了「我是AI」「我做不到」。

这个脚本的规则：
1. 只保留中文部分，英文思考整段丢掉；
2. 走 engine.is_garbage_memory 过滤；
3. 只保留有信息量的轮次（规则打分 ≥ 1.0）；
4. 群聊记忆标 group，私聊标 private。
"""

from __future__ import annotations

import io
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
PLUGIN_DIR = HERE.parents[1]
DATA_DIR = PLUGIN_DIR.parents[1]
DB = DATA_DIR / "data_v4.db"

sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))

import compressor as cmp  # noqa: E402
from astrbot_plugin_xiaonian_memory.engine import MemoryEngine, is_garbage_memory  # noqa: E402
from astrbot_plugin_xiaonian_memory.store import Store  # noqa: E402

CJK = re.compile(r"[\u4e00-\u9fff]")
# 把一串文本里的英文句子砍掉，只留中文片段
EN_WORD = re.compile(r"[A-Za-z]{2,}")
CJK = re.compile(r"[一-鿿]")
# 只要出现成串英文，就判定为「带着思考残留」，这条我方发言整句不要
HAS_EN = re.compile(r"[A-Za-z]{3,}")


def cn_only(text: str) -> str:
    """去掉标签，砍掉明显英文句子，只留中文。"""
    t = text or ""
    t = re.sub(r"<[^>]{1,200}>", "", t)
    t = re.sub(r"[A-Za-z][A-Za-z0-9'\"“”‘’.,!?;:\-()\[\] ]{6,}", " ", t)
    t = re.sub(r"\s{2,}", " ", t).strip(" |｜-—_")
    return t


def reply_ok(text: str) -> bool:
    """我方的回复：只要掺了成串英文（思考残留）就整句丢掉。"""
    return not HAS_EN.search(text or "")


def informative(text: str) -> bool:
    """有没有实际信息：中文够多、不是纯应答。"""
    cn = CJK.findall(text)
    if len(cn) < 6:
        return False
    if re.fullmatch(r"[\s，。！？、,.!?~～…\-—|｜:：]*", text):
        return False
    return True


def main() -> int:
    apply = "--apply" in sys.argv
    brain = DATA_DIR / "plugin_data" / "astrbot_plugin_xiaonian_memory" / "brain.db"
    store = Store(brain)
    engine = MemoryEngine(store)

    conn = sqlite3.connect(DB)
    rows = conn.execute("select conversation_id, user_id, content from conversations").fetchall()

    added = skipped = 0
    for cid, uid, content in rows:
        try:
            hist = json.loads(content or "[]")
        except Exception:
            continue
        if not isinstance(hist, list) or not hist:
            continue
        is_group = "GroupMessage" in str(uid)
        vis = "group" if is_group else "private"
        cleaned, _ = cmp.strip_history(hist)
        for r in cmp.split_rounds(cleaned):
            parts = []
            best = -9.0
            for m in r:
                raw = cmp.msg_text(m)
                if not raw.strip():
                    continue
                t = cn_only(raw)
                if not informative(t):
                    continue
                if m.get("role") != "user" and not reply_ok(raw):
                    continue  # 带英文思考的回复，整条丢弃
                best = max(best, cmp.score_text(t))
                role = "对方" if m.get("role") == "user" else "我"
                parts.append(f"{role}：{t[:260]}")
            if not parts or best < 1.0:
                skipped += 1
                continue
            text = "｜".join(parts)[:800]
            if is_garbage_memory(text):
                skipped += 1
                continue
            if apply:
                try:
                    mid = engine.remember(
                        text=text,
                        kind="episode",
                        importance=max(0.35, min(0.9, 0.35 + best / 12.0)),
                        source="reimport",
                        meta={"from": uid, "conv": cid, "at": time.strftime("%Y-%m-%d %H:%M")},
                        visibility=vis,
                    )
                    if mid == -1:
                        skipped += 1
                        continue
                    added += 1
                except Exception:
                    skipped += 1
                    continue
            else:
                added += 1

    conn.close()
    print(f"{'已写入' if apply else '将写入'} {added} 条，跳过 {skipped} 条")
    if apply:
        print("记忆库:", engine.stats())
    elif added:
        print("（dry-run，加 --apply 才真正写）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
