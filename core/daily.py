# -*- coding: utf-8 -*-
"""C5：每日回顾卡片

为什么需要：面板的「今日聊天记录」是一坨原始流水，想回答「今天她心情怎么样、
聊了什么」，你得自己一行行读。而一天结束的时候，人想要的其实就四件事：
她情绪怎么样、聊了什么、记住了什么新东西、有没有没兑现的约定。

做法：把当天的数据压成一张卡片。全部本地计算，不额外调一次 AI——
回顾是为了省时间，不是再制造一笔花费和一个等待。

可翻历史：有聊天记录的日期都能翻回去看。
"""

import time

from core import mood as mood_mod
from core import reminders as reminders_mod
from core import store

_HOURS = 24


def _dates_asc(limit=3650):
    """有聊天记录的日期，升序。store 返回的是 dict 且已按日期倒序。"""
    return sorted(d["date"] for d in store.dates_with_messages(limit=limit))


def _day_range(date_str):
    """返回当天 [起, 止) 的 unix 时间戳"""
    t0 = time.mktime(time.strptime(date_str, "%Y-%m-%d"))
    return t0, t0 + 86400


def _mood_curve(her_msgs):
    """按小时聚合她的情绪。

    同一小时里取最重的那一档——她先抱怨后撒娇，这一小时该记成"不太好"，
    取平均会把真正的情绪抹平。
    """
    buckets = {}
    for m in her_msgs:
        hour = time.localtime(m.get("ts") or 0).tm_hour
        tags, level = mood_mod.classify(m.get("content") or "")
        b = buckets.setdefault(hour, {"hour": hour, "level": None, "tags": [], "n": 0})
        b["n"] += 1
        if level and mood_mod.severity_rank(level) > mood_mod.severity_rank(b["level"]):
            b["level"] = level
            b["tags"] = tags
    curve = []
    for h in range(_HOURS):
        b = buckets.get(h)
        if not b:
            curve.append({"hour": h, "level": "none", "tags": [], "n": 0})
        else:
            curve.append({"hour": h, "level": b["level"] or "calm",
                          "tags": b["tags"], "n": b["n"]})
    return curve


def _mood_summary(curve):
    """把 24 格压成一句话：今天整体怎么样"""
    talk = [c for c in curve if c["n"] > 0]
    if not talk:
        return "这天没有聊天记录"
    counts = {}
    for c in talk:
        counts[c["level"]] = counts.get(c["level"], 0) + 1
    top = max(counts.items(), key=lambda kv: (mood_mod.severity_rank(kv[0]), kv[1]))[0]
    worst = max(talk, key=lambda c: mood_mod.severity_rank(c["level"]))
    if mood_mod.severity_rank(worst["level"]) >= 2:
        return "有 %d 个小时情绪不太好，最重的一次在 %d 点左右（%s）" % (
            counts.get("high", 0), worst["hour"],
            "、".join(worst["tags"][:3]) or "情绪波动")
    if counts.get("mid"):
        return "整体平稳，有 %d 个小时略显波动" % counts["mid"]
    return "整体平稳"


def _facts_of_day(t0, t1, date_str):
    """当天新沉淀的事实。

    两个口径都要认：ts 是写入时间（补跑记忆时可能是几天后才写），
    source_date 才是它真正属于的那天。只看 ts 的话，补跑出来的事实会全部算在
    补跑那天头上，回顾卡片就成了假的。
    """
    out, seen = [], set()
    for f in store.list_facts(limit=2000):
        ts = f.get("ts") or 0
        if f.get("id") in seen:
            continue
        if t0 <= ts < t1 or f.get("source_date") == date_str:
            seen.add(f.get("id"))
            out.append(f)
    out.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    return out


def review(date_str=None):
    """生成某一天的回顾卡片。date_str 为空表示今天。"""
    date_str = date_str or time.strftime("%Y-%m-%d")
    t0, t1 = _day_range(date_str)

    store.init()
    her = store.messages(date_str=date_str, include_ai=False, limit=2000)
    allm = store.messages(date_str=date_str, include_ai=True, limit=4000)
    ai = [m for m in allm if m.get("is_ai")]

    curve = _mood_curve(her)
    cost_row = None
    for d in store.usage_by_day(days=40):
        if d.get("date") == date_str:
            cost_row = d
            break

    facts = _facts_of_day(t0, t1, date_str)
    summary = store.get_summary("day", date_str) or ""

    # 当天的约定：只挑正好落在这一天的
    due = []
    try:
        for r in reminders_mod.list_reminders(days_ahead=3650, include_past=True):
            if r.get("date") == date_str:
                due.append(r)
    except Exception:
        due = []

    # 可翻的日期（前后各给一个，翻页按钮才知道该不该亮）
    dates = _dates_asc()
    prev_d = next_d = None
    for i, d in enumerate(dates):
        if d == date_str:
            if i > 0:
                prev_d = dates[i - 1]
            if i < len(dates) - 1:
                next_d = dates[i + 1]
            break

    her_len = sum(len(m.get("content") or "") for m in her)
    ai_len = sum(len(m.get("content") or "") for m in ai)
    return {
        "ok": True,
        "date": date_str,
        "has_data": bool(her or ai),
        "stats": {
            "her_msgs": len(her),
            "ai_replies": len(ai),
            "her_chars": her_len,
            "ai_chars": ai_len,
            "avg_her": round(her_len / len(her), 1) if her else 0,
            "avg_ai": round(ai_len / len(ai), 1) if ai else 0,
            "cost": (cost_row or {}).get("cost", 0.0),
            "calls": (cost_row or {}).get("calls", 0),
            "new_facts": len(facts),
        },
        "mood_curve": curve,
        "mood_text": _mood_summary(curve),
        "peak_hour": max((c["hour"] for c in curve if c["n"] > 0), default=None),
        "summary": summary,
        "facts": [{"id": f.get("id"), "key": f.get("key"),
                   "value": f.get("value"), "category": f.get("category")}
                  for f in facts[:20]],
        "reminders_due": due,
        "prev_date": prev_d,
        "next_date": next_d,
        "recent_dates": dates[-14:],
    }


def recent_list(n=14):
    """最近有聊天记录的 n 天（新的在前），供快速跳转"""
    return list(reversed(_dates_asc()[-n:]))
