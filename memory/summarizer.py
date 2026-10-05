# -*- coding: utf-8 -*-
"""记忆沉淀：日摘要 / 周摘要 / 事实抽取（3.0）

2.0 的问题回顾（这就是 A1 存在的理由）：
  `_summarize_yesterday()` 只挂在 `_check_daily_reset()` 上，而后者只在
  `_flush()` 里被调用 —— 也就是「正要发一条回复、且刚好跨天」这一瞬间。
  机器人一暂停就再也不沉淀了，data/memory.md 常年 0 字节。

3.0 的做法：
  - 沉淀逻辑从「主流程的副作用」里彻底抽出来，成为**独立能力**，由 worker 调度
  - 「昨天」改成「任何缺摘要的历史日期」，可补跑、可重跑、可手动触发
  - 除了流水账摘要，额外做**结构化事实抽取**（她养了只叫 XX 的猫 / 她周四有考试），
    这类事实能被精确检索、能被单独修正，比一段自然语言摘要有用得多

三级金字塔（C1）：
  day   → 当天对话Summary（每天一条）
  week  → 把 7 条日摘要压成周记（每周一条）
  facts → 从对话里抽出的长期事实（持续累加、可去重、可人工改）
"""

import json
import re
import time

from core import privacy, store

DAY_MIN_LINES = 3     # 少于这么几句不值得总结
MAX_INPUT_LINES = 160


# ---------------------------------------------------------------------------
# 提示词
# ---------------------------------------------------------------------------

DAY_SYSTEM = """你是两个人的聊天记录分析助手。你要为其中一方整理"今天发生了什么"的备忘，
让他明天醒来能在三秒内想起昨天聊过什么、对方现在什么状态。

要求：
1. 只从记录里能推出的内容，绝不脑补、绝不虚构
2. 用中文，第三人称写"她"，第二人称写"你"（他是男，她是女）
3. 分四块输出，每块一行，没有内容就写"无"：
   心情：<她今天的情绪状态，用一两句具体描述，别写"挺好的"这种空话>
   事情：<她今天经历/正在忙的关键事件>
   约定：<双方约定或承诺的事，含时间地点>
   惦记：<接下来几天需要记得/关心的事>
4. 每块控制在 60 字以内，宁精勿全
5. 只输出这四行，不要标题、不要解释、不要客套话"""

FACT_SYSTEM = """你负责从对话里提取"值得长期记住的事实"。

提取规则：
- 只提取**明确的、具体的、未来还有用**的事实。例：
  她的宠物名字、常去的地方、固定作息、正在备考的科目、讨厌的食物、
  身体上的毛病、重要的纪念日、家庭关系、口头约定
- 不要提取一次性的情绪、不要把猜测当事实、不要重复已有的 key
- 每条事实写成 {"key": 类别, "value": 具体事实}，value 是一句完整的话
- category 只能是：life(生活) / work(学业工作) / pref(喜好厌恶) / health(身体) / relation(关系约定) / plan(计划日程) / other
- confidence 用 0-1 表示你有多大把握（明确说了=0.9，推测=0.4）
- 最多 6 条，宁缺毋滥
- 只输出 JSON 数组，不要 markdown 代码块、不要任何解释

输出格式：
[{"key":"宠物","value":"她养了一只叫元宝的橘猫","category":"life","confidence":0.9}]"""

WEEK_SYSTEM = """你要把一周的每日备忘压成一份周记，给下个星期用。

要求：
1. 找出这一周的主线（她在忙什么大事、情绪的主基调、关系的变化）
2. 合并重复信息，扔掉琐碎的一次性小事
3. 保留所有仍然有效的约定和承诺
4. 中文输出，分两块：
   主线：<3-4 句话概括这一周>
   待办：<仍然有效的约定/承诺/需要跟进的事，一条一行，无则写"无">
5. 只输出这两块，不要客套话"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _as_text(reply):
    """AIClient.chat 可能返回 str 或 dict，统一取文本。"""
    if isinstance(reply, dict):
        return str(reply.get("text", "") or "")
    return str(reply or "")


def _strip_code_fence(text):
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    return t.strip()


def parse_facts(raw):
    """把模型输出解析成事实列表。容错：抽第一段 JSON 数组。"""
    t = _strip_code_fence(_as_text(raw))
    if not t:
        return []
    try:
        obj = json.loads(t)
        if isinstance(obj, list):
            return obj
    except Exception:
        pass
    m = re.search(r"\[.*\]", t, re.S)
    if m:
        try:
            obj = json.loads(m.group(0))
            if isinstance(obj, list):
                return obj
        except Exception:
            pass
    return []


# ---------------------------------------------------------------------------
# 日摘要
# ---------------------------------------------------------------------------

def summarize_day(ai, date_str, rows=None, my_name="我", partner_name="她"):
    """生成某天的日摘要。返回 dict {ok, date, text, lines, error}。"""
    from . import daily
    rows = rows if rows is not None else daily.read_day(date_str)
    # 双方姓名映射：把会话名换成「她」，主人和 AI 都换成「你」
    role_map = {}
    for r in rows:
        role_map.setdefault(r.get("who"), "你")
    if partner_name and any(r.get("chat") == partner_name for r in rows):
        for r in rows:
            if r.get("who") == partner_name and not r.get("is_ai"):
                role_map[r.get("who")] = "她"
    dialog = daily.to_dialog(rows, role_map=role_map, max_lines=MAX_INPUT_LINES)

    if len(rows) < DAY_MIN_LINES:
        return {"ok": False, "date": date_str, "text": "",
                "lines": len(rows), "error": "对话不足 %d 条，跳过" % DAY_MIN_LINES}

    messages = [
        {"role": "system", "content": DAY_SYSTEM},
        {"role": "user", "content": "日期：%s\n\n%s" % (date_str, dialog)},
    ]
    try:
        text = _as_text(ai.chat(messages)).strip()
    except Exception as e:
        return {"ok": False, "date": date_str, "text": "",
                "lines": len(rows), "error": str(e)[:200]}
    if not text:
        return {"ok": False, "date": date_str, "text": "",
                "lines": len(rows), "error": "AI 返回为空"}
    store.save_summary("day", date_str, text)
    _append_md(date_str, text)
    return {"ok": True, "date": date_str, "text": text, "lines": len(rows), "error": ""}


def get_day(date_str):
    return store.get_summary("day", date_str)


# ---------------------------------------------------------------------------
# 事实抽取
# ---------------------------------------------------------------------------

def extract_facts(ai, date_str, rows=None):
    """从当天对话抽事实，写入 SQLite。返回 {"ok","facts","date","error"}。"""
    from . import daily
    rows = rows if rows is not None else daily.read_day(date_str)
    if len(rows) < DAY_MIN_LINES:
        return {"ok": False, "facts": [], "date": date_str, "error": "对话不足"}
    dialog = daily.to_dialog(rows, max_lines=MAX_INPUT_LINES)

    known = store.list_facts(limit=200)
    known_txt = "\n".join("- %s：%s" % (f["key"], f["value"]) for f in known[:60]) or "（暂无）"

    messages = [
        {"role": "system", "content": FACT_SYSTEM},
        {"role": "user", "content": "已经知道的事实（不要重复提取）：\n%s\n\n今天的对话：\n%s"
                                    % (known_txt, dialog)},
    ]
    try:
        raw = _as_text(ai.chat(messages))
    except Exception as e:
        return {"ok": False, "facts": [], "date": date_str, "error": str(e)[:200]}

    items = parse_facts(raw)
    saved, dropped = [], []
    for it in items[:6]:
        if not isinstance(it, dict):
            continue
        key = str(it.get("key", "")).strip()[:30]
        val = str(it.get("value", "")).strip()[:200]
        if not key or not val:
            continue
        cat = str(it.get("category", "other")).strip()[:16] or "other"
        try:
            conf = float(it.get("confidence", 0.7))
        except Exception:
            conf = 0.7

        # E4 隐私闸门：卡号/身份证/验证码/密码这类，宁可这条记忆不要
        allowed, reason = privacy.filter_fact_dict({"key": key, "value": val})
        if not allowed:
            dropped.append({"key": key, "reason": reason})
            continue
        if reason.startswith("已脱敏"):
            val = privacy.redact(val)

        if store.upsert_fact(key, val, category=cat, confidence=conf,
                             source_date=date_str):
            saved.append({"key": key, "value": val, "category": cat,
                          "confidence": conf})
    if dropped:
        import logging
        logging.getLogger("memory").warning(
            "%s 有 %d 条事实因含高敏信息被丢弃", date_str, len(dropped))
    return {"ok": True, "facts": saved, "dropped": dropped, "date": date_str, "error": ""}


# ---------------------------------------------------------------------------
# 周摘要
# ---------------------------------------------------------------------------

def iso_week(date_str):
    import datetime
    d = datetime.date.fromisoformat(date_str)
    y, w, _ = d.isocalendar()
    return "%d-W%02d" % (y, w)


def summarize_week(ai, week_id):
    """把属于该 ISO 周的日摘要压成周记。"""
    days = [s for s in store.list_summaries("day", n=400)
            if iso_week(s["date"]) == week_id]
    if len(days) < 2:
        return {"ok": False, "week": week_id, "text": "", "error": "该周摘要不足 2 天"}
    body = "\n\n".join("【%s】\n%s" % (d["date"], d["text"]) for d in sorted(days, key=lambda x: x["date"]))
    messages = [
        {"role": "system", "content": WEEK_SYSTEM},
        {"role": "user", "content": "周次：%s\n\n%s" % (week_id, body)},
    ]
    try:
        text = _as_text(ai.chat(messages)).strip()
    except Exception as e:
        return {"ok": False, "week": week_id, "text": "", "error": str(e)[:200]}
    if not text:
        return {"ok": False, "week": week_id, "text": "", "error": "AI 返回为空"}
    store.save_summary("week", week_id, text)
    return {"ok": True, "week": week_id, "text": text, "error": ""}


# ---------------------------------------------------------------------------
# memory.md 兼容层（老格式保留，便于人工直接看/改）
# ---------------------------------------------------------------------------

def _append_md(date_str, text):
    """同步写一份到老格式 data/memory.md（去重）。

    保留它是有意的：markdown 是主人的资产，可以随时手改。
    真相以 SQLite 为准，手改后可用 `python -m memory.sync_md` 回灌。
    """
    import os
    from core.paths import data_dir
    p = os.path.join(data_dir(), "memory.md")
    try:
        os.makedirs(os.path.dirname(p), exist_ok=True)
        old = ""
        if os.path.exists(p):
            with open(p, "r", encoding="utf-8") as f:
                old = f.read()
        marker = "【%s】" % date_str
        if marker in old:
            # 已存在：替换该段
            blocks = [b for b in old.split("\n\n") if b.strip()]
            blocks = [b for b in blocks if not b.startswith(marker)]
            blocks.append("%s%s" % (marker, text.strip()))
            body = "\n\n".join(blocks) + "\n"
        else:
            body = old.rstrip("\n") + ("\n\n" if old.strip() else "") + \
                   "%s%s\n" % (marker, text.strip())
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(body)
            f.flush()
        import os as _os
        _os.replace(tmp, p)
        return True
    except Exception:
        return False


def sync_md_to_db():
    """把人工改过的 memory.md 回灌到 SQLite。返回写入天数。"""
    import os
    from core.paths import data_dir
    p = os.path.join(data_dir(), "memory.md")
    if not os.path.exists(p):
        return 0
    with open(p, "r", encoding="utf-8") as f:
        content = f.read()
    n = 0
    for block in content.split("\n\n"):
        b = block.strip()
        m = re.match(r"【(\d{4}-\d{2}-\d{2})】(.*)", b, re.S)
        if not m:
            continue
        date_str, text = m.group(1), m.group(2).strip()
        existing = store.get_summary("day", date_str)
        if existing != text:
            store.save_summary("day", date_str, text)
            n += 1
    return n


def pending_dates(today=None, exclude_today=True, limit_days=400):
    """找出所有「有对话但没摘要」的日期（含历史，不限于昨天）。"""
    from . import daily
    today = today or time.strftime("%Y-%m-%d")
    out = []
    for d in daily.list_dates():
        if exclude_today and d >= today:
            continue          # 今天还在进行中，等明天再总结
        if len(d) != 10:
            continue
        if not store.has_summary("day", d):
            out.append(d)
        if len(out) >= limit_days:
            break
    return sorted(out)
