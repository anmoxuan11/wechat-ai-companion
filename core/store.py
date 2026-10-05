# -*- coding: utf-8 -*-
"""SQLite 结构化存储（3.0）

为什么要它：
  2.0 全靠 jsonl + json 存数据。统计「本月花了多少钱」「最近一周她的情绪走势」
  要把 11 个 jsonl 全量扫一遍；跑一年就是几万条，每次查询都全量读，而且无从
  去重（join 不了、排序不了）。

现在落一份 data/state.db（messages / usage / feedback / facts / summaries / events），
jsonl 仍然照写，作为**可读可从零恢复的文本备份**——双写，互为兜底。

线程模型：每个线程一条独立连接（thread-local），写走 WAL + busy_timeout，
并发读写不会互相把对方踢掉。
"""

import json
import os
import sqlite3
import threading
import time

from .paths import data_dir

_TLS = threading.local()

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    ts       REAL    NOT NULL,
    date     TEXT    NOT NULL,
    chat     TEXT    NOT NULL,
    who      TEXT    NOT NULL,
    content  TEXT,
    is_ai    INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_msg_date  ON messages(date);
CREATE INDEX IF NOT EXISTS idx_msg_chat  ON messages(chat, ts);

CREATE TABLE IF NOT EXISTS usage (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ts                REAL NOT NULL,
    date              TEXT NOT NULL,
    kind              TEXT NOT NULL DEFAULT 'chat',
    model             TEXT,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cached_tokens     INTEGER NOT NULL DEFAULT 0,
    cost              REAL    NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_usage_date ON usage(date);

CREATE TABLE IF NOT EXISTS feedback (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    ts      REAL NOT NULL,
    date    TEXT NOT NULL,
    chat    TEXT,
    prompt  TEXT,
    reply   TEXT,
    score   INTEGER NOT NULL,
    note    TEXT
);
CREATE INDEX IF NOT EXISTS idx_fb_date ON feedback(date);

CREATE TABLE IF NOT EXISTS facts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    key         TEXT NOT NULL,
    value       TEXT NOT NULL,
    category    TEXT NOT NULL DEFAULT 'other',
    confidence  REAL NOT NULL DEFAULT 0.7,
    source_date TEXT,
    ts          REAL NOT NULL,
    hits        INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0,
    UNIQUE(key, value)
);

CREATE TABLE IF NOT EXISTS summaries (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    date  TEXT NOT NULL,
    text  TEXT NOT NULL,
    ts    REAL NOT NULL,
    UNIQUE(scope, date)
);

CREATE TABLE IF NOT EXISTS events (
    id    INTEGER PRIMARY KEY AUTOINCREMENT,
    ts    REAL NOT NULL,
    kind  TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    msg   TEXT,
    data  TEXT
);
CREATE INDEX IF NOT EXISTS idx_ev_ts ON events(ts);
"""


def db_path():
    return os.path.join(data_dir(), "state.db")


def connect():
    """取当前线程的连接（不存在就建）。"""
    conn = getattr(_TLS, "conn", None)
    if conn is not None:
        return conn
    os.makedirs(data_dir(), exist_ok=True)
    conn = sqlite3.connect(db_path(), timeout=8.0, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=8000")
        conn.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    _TLS.conn = conn
    return conn


def init():
    """建表（幂等）。返回 True/False。"""
    try:
        conn = connect()
        conn.executescript(SCHEMA)
        conn.commit()
        return True
    except sqlite3.Error:
        return False


# ---------------------------------------------------------------------------
# 消息
# ---------------------------------------------------------------------------

def add_message(date_str, chat, who, content, is_ai=False, ts=None):
    try:
        init()
        connect().execute(
            "INSERT INTO messages(ts,date,chat,who,content,is_ai) VALUES(?,?,?,?,?,?)",
            (ts or time.time(), date_str, chat, who, content or "", 1 if is_ai else 0))
        connect().commit()
        return True
    except sqlite3.Error:
        return False


def messages(date_str=None, chat=None, limit=200, include_ai=True):
    """查消息。默认最新 limit 条按时间正序返回。"""
    try:
        init()
        sql = "SELECT * FROM messages WHERE 1=1"
        args = []
        if date_str:
            sql += " AND date=?"
            args.append(date_str)
        if chat:
            sql += " AND chat=?"
            args.append(chat)
        if not include_ai:
            sql += " AND is_ai=0"
        sql += " ORDER BY ts DESC LIMIT ?"
        args.append(limit)
        rows = connect().execute(sql, args).fetchall()
        return [dict(r) for r in reversed(rows)]
    except sqlite3.Error:
        return []


def count_messages(days=None, chat=None, include_ai=True):
    try:
        init()
        sql = "SELECT COUNT(*) c FROM messages WHERE 1=1"
        args = []
        if days:
            sql += " AND ts >= ?"
            args.append(time.time() - days * 86400)
        if chat:
            sql += " AND chat=?"
            args.append(chat)
        if not include_ai:
            sql += " AND is_ai=0"
        return int(connect().execute(sql, args).fetchone()["c"])
    except sqlite3.Error:
        return 0


def dates_with_messages(limit=60):
    try:
        init()
        rows = connect().execute(
            "SELECT date, COUNT(*) c FROM messages GROUP BY date ORDER BY date DESC LIMIT ?",
            (limit,)).fetchall()
        return [{"date": r["date"], "count": int(r["c"])} for r in rows]
    except sqlite3.Error:
        return []


# ---------------------------------------------------------------------------
# 用量 / 成本
# ---------------------------------------------------------------------------

def add_usage(kind, model, prompt_tokens=0, completion_tokens=0,
              cached_tokens=0, cost=0.0, ts=None):
    try:
        init()
        ts = ts or time.time()
        connect().execute(
            "INSERT INTO usage(ts,date,kind,model,prompt_tokens,"
            "completion_tokens,cached_tokens,cost) VALUES(?,?,?,?,?,?,?,?)",
            (ts, time.strftime("%Y-%m-%d", time.localtime(ts)), kind, model,
             int(prompt_tokens or 0), int(completion_tokens or 0),
             int(cached_tokens or 0), float(cost or 0.0)))
        connect().commit()
        return True
    except sqlite3.Error:
        return False


def usage_by_day(days=7):
    try:
        init()
        since = time.time() - days * 86400
        rows = connect().execute(
            "SELECT date, SUM(prompt_tokens) p, SUM(completion_tokens) c, "
            "SUM(cached_tokens) k, SUM(cost) cost, COUNT(*) n "
            "FROM usage WHERE ts>=? GROUP BY date ORDER BY date",
            (since,)).fetchall()
        return [{"date": r["date"], "prompt": int(r["p"] or 0),
                 "completion": int(r["c"] or 0), "cached": int(r["k"] or 0),
                 "cost": round(float(r["cost"] or 0.0), 6),
                 "calls": int(r["n"])} for r in rows]
    except sqlite3.Error:
        return []


def usage_total(days=None):
    try:
        init()
        sql = ("SELECT SUM(prompt_tokens) p, SUM(completion_tokens) c, "
               "SUM(cost) cost, COUNT(*) n FROM usage")
        args = []
        if days:
            sql += " WHERE ts>=?"
            args.append(time.time() - days * 86400)
        r = connect().execute(sql, args).fetchone()
        return {"prompt": int(r["p"] or 0), "completion": int(r["c"] or 0),
                "cost": round(float(r["cost"] or 0.0), 6), "calls": int(r["n"] or 0)}
    except sqlite3.Error:
        return {"prompt": 0, "completion": 0, "cost": 0.0, "calls": 0}


# ---------------------------------------------------------------------------
# 反馈（赞 / 踩）
# ---------------------------------------------------------------------------

def add_feedback(score, chat=None, prompt=None, reply=None, note=None):
    """score: 1 赞 / -1 踩"""
    try:
        init()
        connect().execute(
            "INSERT INTO feedback(ts,date,chat,prompt,reply,score,note) "
            "VALUES(?,?,?,?,?,?,?)",
            (time.time(), time.strftime("%Y-%m-%d"), chat, prompt, reply,
             int(score), note))
        connect().commit()
        return True
    except sqlite3.Error:
        return False


def feedback_stats(days=30):
    try:
        init()
        since = time.time() - days * 86400
        rows = connect().execute(
            "SELECT score, COUNT(*) c FROM feedback WHERE ts>=? GROUP BY score",
            (since,)).fetchall()
        out = {"up": 0, "down": 0}
        for r in rows:
            if int(r["score"]) > 0:
                out["up"] = int(r["c"])
            else:
                out["down"] = int(r["c"])
        tot = out["up"] + out["down"]
        out["total"] = tot
        out["rate"] = round(out["up"] / tot, 3) if tot else 0.0
        return out
    except sqlite3.Error:
        return {"up": 0, "down": 0, "total": 0, "rate": 0.0}


def feedback_recent(n=50):
    try:
        init()
        rows = connect().execute(
            "SELECT * FROM feedback ORDER BY ts DESC LIMIT ?", (n,)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


# ---------------------------------------------------------------------------
# 事实卡片（C2：从流水账总结升级为结构化事实）
# ---------------------------------------------------------------------------

def upsert_fact(key, value, category="other", confidence=0.7, source_date=None):
    """写入/更新一条事实。相同 (key,value) 只保留一条，重复出现累加置信度。

    注意：这里的调用方都应是「已过 privacy 闸门」的上层。本函数不做内容过滤，
    否则会掩盖调用方没过滤的事实。要体检请用 core.privacy.filter_fact_dict。
    """
    try:
        init()
        conn = connect()
        row = conn.execute(
            "SELECT id, confidence, hits FROM facts WHERE key=? AND value=?",
            (key, value)).fetchone()
        if row:
            hits = int(row["hits"]) + 1
            conf = min(0.98, float(row["confidence"]) + 0.05)
            conn.execute(
                "UPDATE facts SET hits=?, confidence=?, deleted=0, ts=?, "
                "category=?, source_date=? WHERE id=?",
                (hits, conf, time.time(), category, source_date, row["id"]))
        else:
            # 首次出现即记作 1 次（hits 的含义是「被提及/被验证过几次」）
            conn.execute(
                "INSERT INTO facts(key,value,category,confidence,source_date,ts,hits) "
                "VALUES(?,?,?,?,?,?,1)",
                (key, value, category, float(confidence), source_date, time.time()))
        conn.commit()
        return True
    except sqlite3.Error:
        return False


def list_facts(category=None, limit=100, min_confidence=0.0):
    try:
        init()
        sql = "SELECT * FROM facts WHERE deleted=0"
        args = []
        if category:
            sql += " AND category=?"
            args.append(category)
        if min_confidence:
            sql += " AND confidence>=?"
            args.append(min_confidence)
        sql += " ORDER BY hits DESC, ts DESC LIMIT ?"
        args.append(limit)
        rows = connect().execute(sql, args).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


def delete_fact(fid):
    try:
        init()
        connect().execute("UPDATE facts SET deleted=1 WHERE id=?", (fid,))
        connect().commit()
        return True
    except sqlite3.Error:
        return False


def facts_text(limit=40, min_confidence=0.5):
    """给 prompt 用的纯文本事实清单。"""
    out = []
    for f in list_facts(limit=limit, min_confidence=min_confidence):
        out.append("- %s: %s" % (f["key"], f["value"]))
    return "\n".join(out)


# ---------------------------------------------------------------------------
# 摘要（日 / 周 / 长期）
# ---------------------------------------------------------------------------

def save_summary(scope, date_str, text):
    try:
        init()
        connect().execute(
            "INSERT INTO summaries(scope,date,text,ts) VALUES(?,?,?,?) "
            "ON CONFLICT(scope,date) DO UPDATE SET text=excluded.text, ts=excluded.ts",
            (scope, date_str, text, time.time()))
        connect().commit()
        return True
    except sqlite3.Error:
        return False


def get_summary(scope, date_str):
    try:
        init()
        r = connect().execute(
            "SELECT text FROM summaries WHERE scope=? AND date=?",
            (scope, date_str)).fetchone()
        return r["text"] if r else ""
    except sqlite3.Error:
        return ""


def has_summary(scope, date_str):
    return bool(get_summary(scope, date_str))


def list_summaries(scope, n=10):
    try:
        init()
        rows = connect().execute(
            "SELECT * FROM summaries WHERE scope=? ORDER BY date DESC LIMIT ?",
            (scope, n)).fetchall()
        return [dict(r) for r in rows]
    except sqlite3.Error:
        return []


# ---------------------------------------------------------------------------
# 事件镜像（便于跨天聚合查询）
# ---------------------------------------------------------------------------

def add_event(ts, kind, level, msg, data=None):
    try:
        init()
        connect().execute(
            "INSERT INTO events(ts,kind,level,msg,data) VALUES(?,?,?,?,?)",
            (ts, kind, level, msg,
             json.dumps(data, ensure_ascii=False) if data else None))
        connect().commit()
        return True
    except sqlite3.Error:
        return False


def stats():
    """面板首页概览用。"""
    try:
        init()
        return {
            "ok": True,
            "messages": count_messages(),
            "msg_days": len(dates_with_messages(limit=3650)),
            "cost_total": usage_total()["cost"],
            "calls": usage_total()["calls"],
            "facts": len(list_facts(limit=1000)),
            "day_summaries": len(list_summaries("day", n=3650)),
            "feedback": feedback_stats(days=3650)["total"],
        }
    except Exception:
        return {"ok": False}
