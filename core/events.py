# -*- coding: utf-8 -*-
"""事件时间线（3.0）

解决 2.0 的「黑盒感」：机器人跑了一晚上，出了什么事只能翻 bot.log 猜。
现在所有关键动作统一落一条结构化事件：

    bot_start / bot_stop / reply / skip / ai_error / wechat_lost / wechat_back
    takeover / memory_summary / schedule_run / guard_block / config_change / cost_alert

落盘为 data/events.jsonl（追加写 + 滚动裁剪），面板可查、可做筛选、可画趋势。
jsonl 每行自带时间戳，即使进程被强杀也不会损坏已有记录。

设计要点：
  - **只追加**：不重写整个文件，避免和别的进程抢写
  - **永不抛异常**：埋点失败绝不能影响主流程
  - **结构化**：kind 用于筛选，level 决定颜色，data 放明细
"""

import json
import os
import threading
import time

from .paths import base_dir

_LOCK = threading.Lock()

# 事件级别
INFO = "info"
WARN = "warn"
ERROR = "error"
SUCCESS = "success"

# 最大保留条数（超出后滚动裁剪，防止无限增长）
MAX_KEEP = 5000


def events_path():
    return os.path.join(base_dir(), "data", "events.jsonl")


def emit(kind, msg, level=INFO, **data):
    """写一条事件。任何异常都吞掉（埋点不应该炸掉机器人）。

    返回写入的 dict（失败返回 None）。
    """
    rec = {
        "ts": round(time.time(), 3),
        "time": time.strftime("%Y-%m-%d %H:%M:%S"),
        "kind": str(kind),
        "level": level if level in (INFO, WARN, ERROR, SUCCESS) else INFO,
        "msg": str(msg)[:500],
    }
    if data:
        rec["data"] = data
    try:
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        p = events_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with _LOCK:
            with open(p, "a", encoding="utf-8") as f:
                f.write(line)
                f.flush()
    except Exception:
        return None
    return rec


def recent(n=200, kinds=None, level=None, since=None, until=None):
    """读最近事件（倒序返回最新的 n 条，结果本身按时间正序）。

    kinds: 允许的 kind 集合；level: 最低级别过滤不在此处理，精确匹配；
    since/until: unix 时间戳边界。
    """
    p = events_path()
    if not os.path.exists(p):
        return []
    try:
        with open(p, "r", encoding="utf-8") as f:
            raw = f.readlines()[-max(n * 4, 400):]
    except Exception:
        return []
    out = []
    for line in raw:
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
        except Exception:
            continue
        if kinds and r.get("kind") not in kinds:
            continue
        if level and r.get("level") != level:
            continue
        if since is not None and r.get("ts", 0) < since:
            continue
        if until is not None and r.get("ts", 0) > until:
            continue
        out.append(r)
    return out[-n:]


def count_recent_window(seconds, kind=None):
    """统计最近 seconds 秒内某类事件的条数（用于频率判断，如连续 AI 失败）。"""
    since = time.time() - seconds
    n = 0
    for r in recent(n=500, since=since):
        if kind is None or r.get("kind") == kind:
            n += 1
    return n


def kinds_seen():
    """返回出现过的所有 kind（供面板筛选下拉）。"""
    ks = set()
    for r in recent(n=MAX_KEEP):
        ks.add(r.get("kind"))
    return sorted(ks)


def summary_by_kind(days=1):
    """最近 N 天按 kind 聚合计数，面板用来画「今天发生了什么」。"""
    since = time.time() - days * 86400
    agg = {}
    for r in recent(n=MAX_KEEP, since=since):
        k = r.get("kind", "unknown")
        b = agg.setdefault(k, {"kind": k, "count": 0, "last": "", "errors": 0})
        b["count"] += 1
        b["last"] = r.get("time", "")
        if r.get("level") == ERROR:
            b["errors"] += 1
    return sorted(agg.values(), key=lambda x: -x["count"])


def clear():
    """清空事件线（面板「清理历史」用）。"""
    try:
        p = events_path()
        if os.path.exists(p):
            os.remove(p)
            return True
    except Exception:
        pass
    return False


def rotate(max_keep=MAX_KEEP):
    """裁剪到最近 max_keep 条。避免长期运行后文件过大拖慢读取。"""
    p = events_path()
    try:
        if not os.path.exists(p) or os.path.getsize(p) < max_keep * 120:
            return False
        with open(p, "r", encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) <= max_keep:
            return False
        keep = lines[-max_keep:]
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.writelines(keep)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
        return True
    except Exception:
        return False
