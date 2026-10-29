# -*- coding: utf-8 -*-
"""历史清洗 + 记忆导入（一次性工具，可反复跑）。

用法（在 AstrBot 根目录）：
    .venv/Scripts/python.exe data/plugins/astrbot_plugin_xiaonian_memory/tools/import_history.py --dry-run
    .venv/Scripts/python.exe data/plugins/astrbot_plugin_xiaonian_memory/tools/import_history.py --apply

它做两件事：
1. 清洗：把会话历史里被 humanlike 搬进去的人设副本、记忆块、时间戳剥掉
   （这是上下文膨胀 87% 的元凶）。--apply 才会写回，写前自动备份 DB。
2. 导入：把清洗后的对话按「高价值句子」写成 episode 记忆进 brain.db，
   这样新记忆库第一天就有内容，不会失忆。
"""

from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
PLUGIN_DIR = HERE.parents[1]
DATA_DIR = PLUGIN_DIR.parents[1]          # .../data
ASTRBOT_ROOT = DATA_DIR.parent            # .../
DB = DATA_DIR / "data_v4.db"

sys.path.insert(0, str(PLUGIN_DIR))
sys.path.insert(0, str(PLUGIN_DIR.parent))  # data/plugins，让插件能按包名导入
import compressor as cmp  # noqa: E402


def load_plugin_data_dir() -> Path:
    """和插件一致的插件数据目录。"""
    d = DATA_DIR / "plugin_data" / "astrbot_plugin_xiaonian_memory"
    d.mkdir(parents=True, exist_ok=True)
    return d


def iter_conversations(conn: sqlite3.Connection):
    cur = conn.execute("select conversation_id, user_id, content from conversations")
    for cid, uid, content in cur.fetchall():
        try:
            hist = json.loads(content or "[]")
        except Exception:
            continue
        if isinstance(hist, list) and hist:
            yield cid, uid, hist


def clean_mode(conn, apply: bool) -> dict:
    stats = {"conversations": 0, "msg_before": 0, "msg_after": 0, "tok_before": 0, "tok_after": 0}
    updates = []
    for cid, uid, hist in iter_conversations(conn):
        cleaned, changed = cmp.strip_history(hist)
        t0, t1 = cmp.est_history_tokens(hist), cmp.est_history_tokens(cleaned)
        stats["conversations"] += 1
        stats["msg_before"] += len(hist)
        stats["msg_after"] += len(cleaned)
        stats["tok_before"] += t0
        stats["tok_after"] += t1
        print(
            f"  {uid[-40:]:<42} {len(hist):>4}→{len(cleaned):<4} 条"
            f"  {t0:>7}→{t1:<7} token  剥{changed}条"
        )
        if changed:
            updates.append((json.dumps(cleaned, ensure_ascii=False), cid))

    print(
        f"\n合计：{stats['conversations']} 个会话，"
        f"{stats['msg_before']}→{stats['msg_after']} 条，"
        f"{stats['tok_before']}→{stats['tok_after']} token"
        f"（降 {100 * (1 - stats['tok_after'] / max(1, stats['tok_before'])):.1f}%）"
    )
    if apply and updates:
        backup = DB.with_suffix(f".db.before_clean_{time.strftime('%Y%m%d_%H%M%S')}")
        shutil.copy2(DB, backup)
        print(f"已备份：{backup.name}")
        conn.executemany("update conversations set content=? where conversation_id=?", updates)
        conn.commit()
        print(f"已写回 {len(updates)} 个会话")
    elif updates:
        print(f"（dry-run，未写回；加 --apply 才会改动 {len(updates)} 个会话）")
    return stats


def import_mode(conn, apply: bool) -> dict:
    """把清洗后的对话写成 episode 记忆。"""
    brain_db = load_plugin_data_dir() / "brain.db"
    print(f"记忆库：{brain_db}")

    from astrbot_plugin_xiaonian_memory.engine import MemoryEngine  # noqa: WPS433
    from astrbot_plugin_xiaonian_memory.store import Store  # noqa: WPS433

    store = Store(brain_db)
    engine = MemoryEngine(store)

    added = 0
    total_scored = 0
    for cid, uid, hist in iter_conversations(conn):
        cleaned, _ = cmp.strip_history(hist)
        is_group = "GroupMessage" in uid
        visibility = "group" if is_group else "private"
        rounds = cmp.split_rounds(cleaned)
        for r in rounds:
            lines = []
            best = -9.0
            for m in r:
                t = cmp.msg_text(m).strip()
                if not t:
                    continue
                s = cmp.score_text(t)
                best = max(best, s)
                role = "对方" if m.get("role") == "user" else "我"
                lines.append(f"{role}：{t[:300]}")
            if not lines or best < 1.0:
                continue
            text = "｜".join(lines)
            if len(text) < 8:
                continue
            total_scored += 1
            if not apply:
                continue
            try:
                engine.remember(
                    text=text[:1200],
                    kind="episode",
                    importance=max(0.35, min(0.95, 0.35 + best / 12.0)),
                    source="import_history",
                    meta={"from": uid, "conversation": cid, "imported_at": time.strftime("%Y-%m-%d %H:%M")},
                    visibility=visibility,
                )
                added += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  写记忆失败：{exc}")
    print(f"候选记忆 {total_scored} 条" + (f"，已写入 {added} 条" if apply else "（dry-run 未写入）"))
    if apply:
        st = engine.stats()
        print(f"记忆库现状：{st['active']} 条活跃 / {st['archived']} 条归档")
    store.close() if hasattr(store, "close") else None
    return {"candidates": total_scored, "added": added}


def main() -> int:
    ap = argparse.ArgumentParser(description="会话历史清洗 + 记忆导入")
    ap.add_argument("--dry-run", action="store_true", help="只看结果不改动")
    ap.add_argument("--apply", action="store_true", help="真正写回/写入")
    ap.add_argument("--skip-clean", action="store_true", help="跳过清洗，只导入")
    ap.add_argument("--skip-import", action="store_true", help="只清洗，不导入")
    args = ap.parse_args()
    if not args.apply:
        args.dry_run = True

    if not DB.exists():
        print(f"找不到数据库：{DB}")
        return 2
    print(f"数据库：{DB}")
    print(f"模式：{'APPLY（会改动）' if args.apply else 'DRY-RUN（只看）'}\n")

    conn = sqlite3.connect(DB)
    try:
        if not args.skip_clean:
            print("== 一、清洗历史注入块 ==")
            clean_mode(conn, args.apply)
            print()
        if not args.skip_import:
            print("== 二、导入记忆库 ==")
            import_mode(conn, args.apply)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
