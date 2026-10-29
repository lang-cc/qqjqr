# -*- coding: utf-8 -*-
"""清理会话历史：剥掉思考块与注入残留。

只保留真正说出口的话，避免模型下轮读到自己的英文反思。
"""

import json
import re
import sqlite3
import sys

DB = "data/data_v4.db"

INJ = [
    re.compile(r"【外面最近在聊】.*?(?=\n\n|\Z)", re.S),
    re.compile(r"【以下是你的固定身份设定，必须严格遵守】.*?【设定结束】\n?", re.S),
    re.compile(r"【你此刻的心情】[^\n]*", re.S),
]
TAG = re.compile(r"</?image_caption>|\[Image Attachment:[^\]]*\]")


def segs_of(ct):
    if isinstance(ct, str):
        try:
            return json.loads(ct)
        except Exception:
            return ct
    return ct


def clean_assistant(ct):
    segs = segs_of(ct)
    if isinstance(segs, list):
        parts = []
        for s in segs:
            if isinstance(s, dict) and s.get("type") == "text":
                parts.append(str(s.get("text") or ""))
        out = "".join(parts).strip()
        return out or None
    return ct


def clean_user(ct):
    s = str(ct)
    for p in INJ:
        s = p.sub("", s)
    s = TAG.sub("", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def main(apply: bool) -> None:
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT rowid AS rid, user_id, content FROM conversations").fetchall()
    for r in rows:
        hist = json.loads(r["content"])
        out = []
        dropped = 0
        for m in hist:
            if not isinstance(m, dict):
                continue
            if m.get("role") == "assistant":
                v = clean_assistant(m.get("content"))
                if v is None:
                    dropped += 1
                    continue
                m2 = dict(m)
                m2["content"] = v
                out.append(m2)
            else:
                m2 = dict(m)
                m2["content"] = clean_user(m.get("content"))
                out.append(m2)
        before = len(r["content"])
        new = json.dumps(out, ensure_ascii=False)
        print(f"{str(r['user_id'])[:46]:46} {len(hist):3}->{len(out):3} 条  {before:6}->{len(new):6} 字符  丢弃 {dropped}")
        if apply:
            conn.execute("UPDATE conversations SET content=? WHERE rowid=?", (new, r["rid"]))
    if apply:
        conn.commit()
        print("已写入")
    else:
        print("（预演，未写入）")
    conn.close()


if __name__ == "__main__":
    main("--apply" in sys.argv)
