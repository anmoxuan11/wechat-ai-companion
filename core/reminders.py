# -*- coding: utf-8 -*-
"""C6：约定与纪念日提醒

为什么需要：relationship.md 里写着「8.29 飞B城」这种约定，但那对模型来说
只是一行文字——它不一定记得住，你也没有任何提醒。而约定是恋爱里最不该忘的
东西，漏一次很伤。

做法：从关系档案和长期事实里正则抽带日期的句子，存成结构化事件
（data/reminders.json），到期前在面板和事件时间线里提醒你。

只做提醒，不替你说话——要不要让 AI 当天自然提起，由你自己决定（默认关）。
"""

import datetime as dt
import hashlib
import json
import os
import re
import time

from core import atomicio, paths

# 支持的日期写法：8.29 / 8月29日 / 2026-08-29 / 08-29
_DATE_PATTERNS = [
    re.compile(r"(?P<y>20\d{2})\s*[-/.]\s*(?P<m>\d{1,2})\s*[-/.]\s*(?P<d>\d{1,2})"),
    re.compile(r"(?P<m>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*[日号]?"),
    re.compile(r"(?<![\d.])(?P<m>\d{1,2})\s*[.。]\s*(?P<d>\d{1,2})(?![\d.])"),
    re.compile(r"(?<![\d-])(?P<m>\d{1,2})\s*-\s*(?P<d>\d{1,2})(?![\d-])"),
]

# 这些词出现时，这行更可能是一次性约定而不是纪念日
_ONE_OFF_HINTS = ("飞", "去", "回", "出发", "见面", "考试", "面试", "报到",
                  "搬家", "聚餐", "预约", "订", "取", "截止", "还", "交")
_REPEAT_HINTS = ("纪念", "生日", "周年", "恋爱", "在一起", "认识")


def _reminders_path():
    return os.path.join(paths.data_dir(), "reminders.json")


def _read_text(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _sources():
    base = paths.base_dir()
    out = [os.path.join(base, "data", "relationship.md")]
    try:
        from core import store
        for f in store.list_facts(category="relation", limit=50):
            out.append(("fact", f.get("value", "")))
        for f in store.list_facts(category="plan", limit=50):
            out.append(("fact", f.get("value", "")))
    except Exception:
        pass
    return out


def _parse_date(m, d, y=None, yearly=False):
    """把抽到的月日补全成年-月-日。

    没写年份且日期已过时分两种：
      - 年度重复（生日/纪念日）→ 顺延到明年，它每年都来，本该如此；
      - 一次性约定（飞B城/开学）→ 就留在今年，让它自然过期，
        别把 8.29 已经飞完的事硬编成"明年 8 月 29 日"骗你。
    """
    try:
        m, d = int(m), int(d)
    except Exception:
        return None
    if not (1 <= m <= 12 and 1 <= d <= 31):
        return None
    now = time.localtime()
    if y:
        year = int(y)
    else:
        year = now.tm_year
        if (m, d) < (now.tm_mon, now.tm_mday) and yearly:
            year += 1
    try:
        return time.mktime(time.strptime("%04d-%02d-%02d" % (year, m, d), "%Y-%m-%d"))
    except Exception:
        return None


def extract():
    """从关系档案 / 事实卡片里抽带日期的句子，返回提醒列表（不落盘）"""
    items, seen = [], set()
    for src in _sources():
        if isinstance(src, tuple):
            text, origin = src[1], src[0]
        else:
            text = _read_text(src)
            origin = "relationship"
        for line in text.splitlines():
            line = line.strip().lstrip("-*• ").strip()
            if len(line) < 4 or len(line) > 120:
                continue
            # 先判断是「每年都来」还是「就这一次」，因为它决定日期怎么补年份
            repeat = "yearly" if any(h in line for h in _REPEAT_HINTS) else (
                "none" if any(h in line for h in _ONE_OFF_HINTS) else "yearly")
            # 一行里可能有多个日期（「8.29 飞B城；9.1 开学」），全都要，
            # 别只取第一个——漏掉的往往才是要紧的那个。
            found = []
            for pat in _DATE_PATTERNS:
                for m in pat.finditer(line):
                    ts = _parse_date(m.group("m"), m.group("d"),
                                     m.groupdict().get("y"), yearly=(repeat == "yearly"))
                    if ts and ts not in found:
                        found.append(ts)
            for ts in found:
                key = (line, ts)
                if key in seen:
                    continue
                seen.add(key)
                items.append({
                    "id": "r%d" % int(ts),
                    "title": line,
                    "ts": ts,
                    "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
                    "repeat": repeat,
                    "passed": ts < time.time() - 86400,
                    "source": origin,
                })
    items.sort(key=lambda x: x["ts"])
    return items


def _load():
    data = atomicio.read_json(_reminders_path(), {}) or {}
    items = data.get("reminders")
    return items if isinstance(items, list) else None


def refresh():
    """重新抽取，但**手填的提醒不能跟着一起没**——那是你自己敲进去的。"""
    old = _load() or []
    manual = [it for it in old if it.get("source") == "manual"]
    items = extract()
    keys = {(it.get("title"), it.get("date")) for it in items}
    for it in manual:
        if (it.get("title"), it.get("date")) not in keys:
            items.append(it)
    items.sort(key=lambda x: x.get("ts", 0))
    ok, err = atomicio.atomic_write_json(_reminders_path(), {"reminders": items}, backup=False)
    if not ok:
        return {"ok": False, "msg": "写入失败: %s" % err, "reminders": []}
    return {"ok": True, "count": len(items), "manual": len(manual), "reminders": items}


def add(title, date_str, repeat="none"):
    """手填一条提醒。date_str 支持 2026-10-31 / 10-31 / 10.31 / 10月31日。"""
    title = (title or "").strip()
    if not title:
        return {"ok": False, "msg": "内容不能为空"}
    if len(title) > 120:
        title = title[:120]
    ts = None
    ds = (date_str or "").strip()
    for pat in _DATE_PATTERNS:
        m = pat.search(ds)
        if m:
            ts = _parse_date(m.group("m"), m.group("d"), m.groupdict().get("y"),
                             yearly=(repeat == "yearly"))
            if ts:
                break
    if not ts:
        try:
            ts = time.mktime(time.strptime(ds, "%Y-%m-%d"))
        except Exception:
            return {"ok": False, "msg": "日期看不懂，写成 2026-10-31 或 10-31 吧"}
    items = _load()
    if items is None:
        items = extract()
    rid = "m" + hashlib.md5(("%s|%d" % (title, int(ts))).encode("utf-8")).hexdigest()[:10]
    items = [it for it in items if it.get("id") != rid]
    items.append({
        "id": rid, "title": title, "ts": ts,
        "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
        "repeat": "yearly" if repeat == "yearly" else "none",
        "passed": ts < time.time() - 86400, "source": "manual",
    })
    items.sort(key=lambda x: x.get("ts", 0))
    ok, err = atomicio.atomic_write_json(_reminders_path(), {"reminders": items}, backup=False)
    if not ok:
        return {"ok": False, "msg": "写入失败: %s" % err}
    return {"ok": True, "id": rid, "date": time.strftime("%Y-%m-%d", time.localtime(ts)),
            "msg": "已记住：%s（%s）" % (title, time.strftime("%Y-%m-%d", time.localtime(ts)))}


def delete(rid):
    items = _load()
    if items is None:
        return {"ok": False, "msg": "还没有提醒"}
    left = [it for it in items if it.get("id") != rid]
    if len(left) == len(items):
        return {"ok": False, "msg": "没找到这条提醒"}
    ok, err = atomicio.atomic_write_json(_reminders_path(), {"reminders": left}, backup=False)
    if not ok:
        return {"ok": False, "msg": "写入失败: %s" % err}
    return {"ok": True, "msg": "已删除", "count": len(left)}


def list_reminders(days_ahead=60, include_past=False):
    """列出即将到来的提醒（默认 60 天内），顺带更新已过期的重复项年份

    include_past=True 时会把刚过去 7 天内的也带上——「昨天她说的事」你也想看见。
    """
    items = _load()
    if items is None:
        items = extract()
        atomicio.atomic_write_json(_reminders_path(), {"reminders": items}, backup=False)
    now = time.time()
    changed = False
    out = []
    for it in items:
        ts = it.get("ts", 0)
        # 过去的一次性约定保留 7 天后丢弃；年度重复的顺延到明年
        if ts < now - 7 * 86400:
            if it.get("repeat") == "yearly":
                lt = time.localtime(ts)
                # while 而不是 if：停了半年没开面板的话，一次只补一年补不回来
                while ts < now - 7 * 86400:
                    lt = time.localtime(ts)
                    try:
                        ts = time.mktime(time.strptime(
                            "%04d-%02d-%02d" % (lt.tm_year + 1, lt.tm_mon, lt.tm_mday),
                            "%Y-%m-%d"))
                    except Exception:
                        break
                it["ts"] = ts
                it["date"] = time.strftime("%Y-%m-%d", time.localtime(ts))
                it["passed"] = False
                changed = True
            else:
                continue
        # 按「日历天」算差值：23:50 收到一条 00:10 到期的提醒，
        # 按秒差算出来是 0 天但人眼看是明天，这种错位会让"今天"变成谎言。
        days = (dt.date.fromtimestamp(ts) - dt.date.fromtimestamp(now)).days
        if days < 0 and not include_past:
            continue
        if days <= days_ahead:
            r = dict(it)
            r["days_left"] = days
            r["today"] = days == 0
            out.append(r)
    if changed:
        atomicio.atomic_write_json(_reminders_path(), {"reminders": items}, backup=False)
    out.sort(key=lambda x: x["ts"])
    return out


def today_text():
    """给 AI / 事件时间线用的一句话提醒（今天或 3 天内到期的）"""
    rows = [r for r in list_reminders(days_ahead=3)]
    if not rows:
        return ""
    parts = []
    for r in rows:
        if r["days_left"] <= 0:
            parts.append("今天：%s" % r["title"])
        else:
            parts.append("%d 天后：%s" % (r["days_left"], r["title"]))
    return "；".join(parts)
