# -*- coding: utf-8 -*-
"""每日对话日志读写（3.0）

data/daily/YYYY-MM-DD.jsonl —— 一行一条消息，人类可读的最终底线备份。
SQLite（core.store）是查询主力，这份 jsonl 是重建源：哪怕 state.db 损坏，
一句 `python -m memory.rebuild` 就能把库恢复回来。

读取时优先 SQLite（快、可筛选），SQLite 为空自动回退 jsonl（迁移期兼容）。
"""

import json
import os
import time

from core.paths import data_dir


def daily_dir():
    return os.path.join(data_dir(), "daily")


def log_path(date_str):
    return os.path.join(daily_dir(), date_str + ".jsonl")


def append(date_str, chat, who, content, is_ai=False, ts=None):
    """追加一条。失败返回 False（日志写不进去不该炸主流程）。"""
    try:
        os.makedirs(daily_dir(), exist_ok=True)
        rec = {"chat": chat, "who": who, "content": content,
               "ts": ts or time.time(), "is_ai": is_ai}
        with open(log_path(date_str), "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            f.flush()
        return True
    except Exception:
        return False


def read_jsonl(date_str, limit=None):
    p = log_path(date_str)
    if not os.path.exists(p):
        return []
    out = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return out
    return out[-limit:] if limit else out


def read_day(date_str, limit=None, chats=None):
    """读某天的消息。优先 SQLite，回退 jsonl。返回标准 dict 列表。"""
    try:
        from core import store
        rows = store.messages(date_str, limit=limit or 500)
        if rows:
            out = [{"chat": r["chat"], "who": r["who"], "content": r["content"],
                    "ts": r["ts"], "is_ai": bool(r["is_ai"])} for r in rows]
            if chats:
                out = [r for r in out if r["chat"] in chats]
            return out[-limit:] if limit else out
    except Exception:
        pass
    rows = read_jsonl(date_str, limit=limit)
    if chats:
        rows = [r for r in rows if r.get("chat") in chats]
    return rows


def list_dates():
    """列出所有有日志的日期，升序。"""
    try:
        from core import store
        ds = [d["date"] for d in store.dates_with_messages(limit=3650)]
        if ds:
            return sorted(ds)
    except Exception:
        pass
    try:
        names = [f[:-6] for f in os.listdir(daily_dir()) if f.endswith(".jsonl")]
        return sorted(names)
    except Exception:
        return []


def count_for(date_str):
    return len(read_jsonl(date_str))


def to_dialog(rows, role_map=None, max_lines=120):
    """把消息列表转成给 AI 看的对话文本。

    role_map: {who: 显示名}，默认直接用 who。
    """
    role_map = role_map or {}
    lines = []
    for r in rows[-max_lines:]:
        who = role_map.get(r.get("who"), r.get("who", "?"))
        c = (r.get("content") or "").replace("\n", " ")
        if not c:
            continue
        lines.append("%s: %s" % (who, c))
    return "\n".join(lines)


def rebuild_from_jsonl(dates=None):
    """把 jsonl 重新灌回 SQLite（state.db 丢失/损坏时的恢复手段）。

    跳过已存在的同 ts + who + content 组合，重复执行安全。
    返回 {"days": n, "rows": m}
    """
    from core import store
    store.init()
    days = dates or list_dates_from_filesystem()
    rows = 0
    for d in days:
        for rec in read_jsonl(d):
            existing = store.messages(d, limit=10000)
            key = (rec.get("ts"), rec.get("who"), rec.get("content"))
            dup = any((abs((e.get("ts") or 0) - (key[0] or 0)) < 1e-6
                       and e.get("who") == key[1] and e.get("content") == key[2])
                      for e in existing)
            if dup:
                continue
            if store.add_message(d, rec.get("chat", ""), rec.get("who", ""),
                                 rec.get("content", ""), rec.get("is_ai", False),
                                 ts=rec.get("ts")):
                rows += 1
    return {"days": len(days), "rows": rows}


def list_dates_from_filesystem():
    try:
        return sorted(f[:-6] for f in os.listdir(daily_dir()) if f.endswith(".jsonl"))
    except Exception:
        return []
