# -*- coding: utf-8 -*-
"""E3：消息通道（后端）健康度与自动降级

为什么需要：config 里写 backend: auto，选完就完了——没有健康度跟踪，
也没有失败时的切换。而 README 自己就写着「iLink 是官方接口无封号风险、
wxauto 是 UI 自动化有理论风险」，这份认知却从来没有变成代码里的行为。

做法：给每个后端记一份健康档案（成功/失败次数、连续失败、最近可用时间），
算出 0-100 分；当前通道连续失败到阈值就自动切到下一个可用的，
并把这件事写进事件时间线让你看见。

两条刻意的克制：
  1. **只自动降级，不自动升回**。探测一个后端是否恢复，意味着要真的把它拉起来
     （wxauto 系会去抢微信窗口），这本身就是一次干扰。所以「切回首选」做成
     面板上的按钮，由你决定什么时候试。
  2. **顺序可配，默认不改现状**。ilink 目前是技术储备（会显示 bot 身份），
     贸然排到第一位会在下次启动时把你的通道换掉。想让它优先，
     在 config.json 的 backend_failover.order 里把它写前面。
"""

import os
import time

from core import atomicio, paths

# 风险等级只用于面板提示，不参与打分（打分只看它实际表现得好不好）
RISK = {
    "ilink": ("low", "官方接口，无封号风险"),
    "wechatauto": ("low", "读本地数据库，低风险"),
    "wxauto": ("high", "UI 自动化，理论上有风险"),
    "auto": ("mid", "自动选择"),
}

DEFAULT_ORDER = ["wechatauto", "wxauto", "ilink"]


def _path():
    return os.path.join(paths.data_dir(), "backend_health.json")


def _load():
    data = atomicio.read_json(_path(), {}) or {}
    return data.get("backends") or {}, data.get("current") or ""


def _save(backends, current):
    atomicio.atomic_write_json(
        _path(), {"backends": backends, "current": current,
                  "ts": time.time()}, backup=False)


def _rec(backends, name):
    r = backends.get(name)
    if not isinstance(r, dict):
        r = {"ok": 0, "fail": 0, "consec_fail": 0,
             "last_ok": 0, "last_fail": 0, "last_error": ""}
        backends[name] = r
    return r


def score(rec):
    """0-100。只看两件事：最近是否连续在失败、失败离现在有多近。"""
    s = 100
    s -= 30 * min(int(rec.get("consec_fail", 0)), 3)
    if rec.get("last_fail"):
        age = time.time() - float(rec["last_fail"])
        if age < 3600:
            s -= 20
        elif age < 6 * 3600:
            s -= 8
    return max(0, min(100, int(s)))


def note_ok(name):
    backends, current = _load()
    r = _rec(backends, name)
    r["ok"] = int(r.get("ok", 0)) + 1
    r["consec_fail"] = 0
    r["last_ok"] = time.time()
    _save(backends, current or name)
    return score(r)


def note_fail(name, err=""):
    backends, current = _load()
    r = _rec(backends, name)
    r["fail"] = int(r.get("fail", 0)) + 1
    r["consec_fail"] = int(r.get("consec_fail", 0)) + 1
    r["last_fail"] = time.time()
    r["last_error"] = str(err)[:200]
    _save(backends, current or name)
    return score(r)


def set_current(name):
    backends, _ = _load()
    _rec(backends, name)
    _save(backends, name)


def current_name():
    return _load()[1]


def snapshot(order=None):
    """面板用：每个后端的健康度 + 风险提示 + 当前是哪一个"""
    backends, current = _load()
    order = list(order or DEFAULT_ORDER)
    for n in backends:
        if n not in order:
            order.append(n)
    rows = []
    for n in order:
        r = _rec(backends, n)
        risk, note = RISK.get(n, ("mid", ""))
        rows.append({
            "name": n, "score": score(r), "ok": r.get("ok", 0),
            "fail": r.get("fail", 0), "consec_fail": r.get("consec_fail", 0),
            "last_ok": r.get("last_ok", 0), "last_fail": r.get("last_fail", 0),
            "last_error": r.get("last_error", ""),
            "risk": risk, "risk_note": note, "current": n == current,
        })
    return {"ok": True, "backends": rows, "current": current or "",
            "order": order}


def next_candidate(order, exclude=None):
    """按配置顺序挑下一个还能试的后端（排除当前的）"""
    backends, _ = _load()
    for n in (order or DEFAULT_ORDER):
        if n == exclude:
            continue
        if score(_rec(backends, n)) <= 0:
            continue      # 已经连续挂了很多次，别再拿它救场
        yield n


def order_from_cfg(cfg, current_backend_name=None):
    """从配置读降级顺序；没配就按「当前 + 默认顺序」排。

    当前正在用的排第一位，保证没配过的人重启后行为不变。
    """
    fo = (cfg or {}).get("backend_failover", {}) or {}
    order = [n for n in (fo.get("order") or []) if n in RISK]
    if not order:
        order = list(DEFAULT_ORDER)
    if current_backend_name and current_backend_name in RISK:
        order = [current_backend_name] + [n for n in order
                                          if n != current_backend_name]
    return order
