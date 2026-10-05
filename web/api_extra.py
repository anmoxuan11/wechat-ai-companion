# -*- coding: utf-8 -*-
"""3.0 扩展 API（面板）

2.0 的 ui_server.py 已经 1600 多行，再往里塞新端点会让它彻底不可维护。
这里放 3.0 新增的全部接口实现，ui_server 只在路由表末尾挂一层转发：

    web.api_extra.handle_get(path, query)  -> dict | None
    web.api_extra.handle_post(path, body)  -> dict | None

返回 None 表示「不是我的路由」，交给 ui_server 原有的 404 逻辑。

涵盖：
  D2 事件时间线 / D1 成本 / D5 健康度
  A1 记忆沉淀状态与手动触发 / C2 事实卡片管理
  B1 回复预演（干跑）/ B4 赞踩反馈 / B5 语气滑杆
  E1/E2 护栏配置与审查测试
  D6 备份与恢复
"""

import json
import os
import time

from core import atomicio, cost, events, guard as guard_mod, paths, store
from core.security import refresh_env_from_registry

DATA_DIR = paths.data_dir()
CONFIG_PATH = os.path.join(paths.base_dir(), "config.json")
BACKUP_DIR = os.path.join(paths.base_dir(), "backups")


def load_cfg():
    """读配置。顺手把用户环境变量补齐（避免「明明设了却读不到」的玄学）。"""
    refresh_env_from_registry()
    return atomicio.read_json(CONFIG_PATH, {}) or {}


def save_cfg(cfg):
    return atomicio.atomic_write_json(CONFIG_PATH, cfg, backup=True)[0]


# ---------------------------------------------------------------------------
# GET
# ---------------------------------------------------------------------------

def get_events(qs):
    limit = int(qs.get("limit", ["200"])[0])
    kind = qs.get("kind", [None])[0]
    level = qs.get("level", [None])[0]
    kinds = set(kind.split(",")) if kind else None
    rows = events.recent(n=max(1, min(limit, 1000)), kinds=kinds, level=level)
    return {"ok": True, "events": rows, "kinds": events.kinds_seen(),
            "agg": events.summary_by_kind(days=1)}


def get_cost(qs):
    days = int(qs.get("days", ["7"])[0])
    today = store.usage_total(days=1)
    month = store.usage_total(days=30)
    return {"ok": True,
            "today": today, "month": month,
            "by_day": store.usage_by_day(days=days),
            "today_text": cost.fmt_cost(today.get("cost", 0)),
            "month_text": cost.fmt_cost(month.get("cost", 0))}


def get_insights(qs):
    """首页「概览」卡片：今天发生了多少事，拦了多少条，平均多快。"""
    day = events.summary_by_kind(days=1)
    agg = {d["kind"]: d for d in day}
    replies = store.messages(chat=None, limit=5000)
    return {
        "ok": True,
        "events_today": sum(d["count"] for d in day),
        "replies_today": agg.get("reply", {}).get("count", 0),
        "blocked_today": agg.get("guard_block", {}).get("count", 0),
        "errors_today": sum(d["errors"] for d in day),
        "msgs_stored": len(replies),
        "days": len(store.dates_with_messages(limit=3650)),
        "cost_today": cost.fmt_cost(store.usage_total(days=1).get("cost", 0)),
        "usage": store.usage_total(days=1),
    }


def get_health(qs):
    """E3 健康度评分：把散落的信号压成一个 0-100 分 + 可读理由。"""
    reasons, score = [], 100
    agg = {d["kind"]: d for d in events.summary_by_kind(days=1)}
    err = agg.get("ai_error", {}).get("count", 0)
    if err >= 5:
        score -= 30
        reasons.append("AI 调用失败 %d 次" % err)
    elif err:
        score -= 10
        reasons.append("AI 调用失败 %d 次" % err)
    lost = agg.get("wechat_lost", {}).get("count", 0)
    if lost:
        score -= 15 * min(lost, 3)
        reasons.append("微信掉线 %d 次（已自动重试）" % lost)
    blocked = agg.get("guard_block", {}).get("count", 0)
    replies = agg.get("reply", {}).get("count", 0)
    if replies and blocked / max(replies, 1) > 0.2:
        score -= 20
        reasons.append("拦截率偏高 %d/%d" % (blocked, replies))
    c = store.usage_total(days=1).get("cost", 0)
    limit = (load_cfg().get("cost", {}) or {}).get("daily_alert_yuan", 5.0)
    if c > limit:
        score -= 15
        reasons.append("今日花费 %s 超过预警线 %s" % (cost.fmt_cost(c), limit))
    if not reasons:
        reasons.append("各项指标正常")
    score = max(0, min(100, score))

    # G2：改了但重启才生效的部分（bot 自己写进心跳状态里）
    st = atomicio.read_json(os.path.join(DATA_DIR, "bot_status.json"), {}) or {}
    restart = st.get("pending_cold") or []
    return {"ok": True, "score": score, "reasons": reasons,
            "restart_required": restart,
            "level": "good" if score >= 80 else ("warn" if score >= 55 else "bad")}


def get_memory_status(qs):
    """A1：记忆沉淀的状态面板数据。"""
    store.init()
    try:
        from memory import summarizer
        from memory.worker import read_progress
        worker_state = read_progress()
        pending = summarizer.pending_dates()
    except Exception as e:
        worker_state, pending = {"phase": "unknown"}, []
        _ = e
    days = store.list_summaries("day", n=30)
    weeks = store.list_summaries("week", n=8)
    return {"ok": True,
            "worker": worker_state,
            "pending": pending,
            "pending_count": len(pending),
            "recent_days": [{"date": d["date"], "text": d["text"]} for d in days],
            "recent_weeks": [{"date": w["date"], "text": w["text"]} for w in weeks],
            "total_days": len(days), "total_weeks": len(weeks)}


def get_facts(qs):
    cat = qs.get("category", [None])[0]
    rows = store.list_facts(category=cat, limit=300)
    return {"ok": True, "facts": rows, "total": len(rows),
            "text": "\n".join("- %s：%s" % (r["key"], r["value"]) for r in rows)}


def get_persona_status(qs):
    """C3：人设卡再蒸馏状态（当前卡 / 待采纳草案 / 可用样本数）"""
    from core import persona
    return persona.status()


def post_persona_redistill(payload):
    """C3：重新蒸馏，产出草案。只写 data/persona_proposal.md，不碰现卡。"""
    from core import persona
    try:
        limit = int(payload.get("limit") or persona.DEFAULT_SAMPLE_LIMIT)
    except Exception:
        limit = persona.DEFAULT_SAMPLE_LIMIT
    r = persona.propose(limit=limit)
    if r.get("ok"):
        r["msg"] = "草案已生成（%d 条真人样本），看过了再决定要不要覆盖" % r.get("samples", 0)
        try:
            events.emit("config_change", "人设卡已重新蒸馏出草案，等待主人确认",
                        level=events.INFO)
        except Exception:
            pass
    return r


def post_persona_apply(payload):
    """C3：采纳草案（自动备份旧卡）"""
    from core import persona
    r = persona.apply_proposal()
    if r.get("ok"):
        try:
            events.emit("config_change", "人设卡已更新（旧卡已备份）", level=events.SUCCESS)
        except Exception:
            pass
    return r


def get_chats(qs):
    """B8：多监听对象各自独立的配置。老的字符串列表也照常显示。"""
    from core import chatcfg
    cfg = load_cfg()
    chats = chatcfg.normalize(cfg)
    safety = cfg.get("safety", {}) or {}
    return {"ok": True,
            "chats": [c.to_dict() for c in chats.values()],
            "defaults": {"cooldown": safety.get("cooldown_seconds", 20),
                         "daily_limit": safety.get("daily_reply_limit", 200)},
            "count": len(chats),
            "enabled": len([c for c in chats.values() if c.enabled])}


def post_chats_update(payload):
    """B8：改某个对象的独立配置（enabled / cooldown / 人设文件 …）。"""
    from core import chatcfg
    name = (payload.get("name") or "").strip()
    patch = payload.get("patch") or {}
    cfg = load_cfg()
    ok, msg = chatcfg.update(cfg, name, patch)
    if not ok:
        return {"ok": False, "msg": msg}
    if not save_cfg(cfg):
        return {"ok": False, "msg": "写配置失败"}
    events.emit("config_change", "监听对象设置已改：%s" % msg, level=events.SUCCESS)
    return {"ok": True, "msg": msg + "（机器人会自动热加载）"}


def post_chats_add(payload):
    from core import chatcfg
    cfg = load_cfg()
    ok, msg = chatcfg.add(cfg, (payload.get("name") or "").strip())
    if not ok:
        return {"ok": False, "msg": msg}
    if not save_cfg(cfg):
        return {"ok": False, "msg": "写配置失败"}
    events.emit("config_change", "新增监听对象：%s" % msg, level=events.SUCCESS)
    return {"ok": True, "msg": msg + "（机器人下一轮挂上监听）"}


def get_backend_health(qs):
    """E3：各消息通道的健康度、风险提示，以及现在走的是哪一条。"""
    from core import backend_health
    cfg = load_cfg()
    order = backend_health.order_from_cfg(cfg)
    snap = backend_health.snapshot(order)
    running = False
    try:
        import ui_server as ui  # 延迟导入：启动期 ui_server 正在 import 本模块
        running = bool((ui.get_status() or {}).get("running"))
    except Exception:
        pass
    fo = (cfg.get("backend_failover", {}) or {})
    snap["running"] = running
    snap["failover_enabled"] = fo.get("enabled", True)
    snap["fail_threshold"] = fo.get("fail_threshold", 3)
    return snap


def post_backend_switch(payload):
    """E3：手动切换消息通道。

    面板和机器人是两个进程，这里不直接操作机器人，只放一张「传话条」，
    由机器人主循环自己来取——它才知道什么时候切最安全。
    """
    name = (payload.get("name") or "").strip()
    if name not in ("wechatauto", "wxauto", "ilink"):
        return {"ok": False, "msg": "只能切到 wechatauto / wxauto / ilink"}
    path = os.path.join(paths.base_dir(), "data", "backend_switch.json")
    ok, err = atomicio.atomic_write_json(
        path, {"name": name, "ts": time.time()}, backup=False)
    if not ok:
        return {"ok": False, "msg": "写入切换请求失败: %s" % err}
    events.emit("config_change", "已请求切换消息通道 → %s（机器人下一轮生效）" % name,
                level=events.WARN)
    return {"ok": True, "name": name,
            "msg": "切换请求已发出，机器人下一轮就会换过去"}


def get_daily(qs):
    """C5 每日回顾卡片：把一天的流水压成「她怎么样 / 聊了什么 / 记住了什么」。"""
    from core import daily
    date_str = (qs.get("date", [None])[0] or "").strip() or None
    r = daily.review(date_str)
    r["recent_dates"] = daily.recent_list(14)
    return r


def get_reminders(qs):
    """C6 约定与纪念日：把 relationship.md 里散着的日期变成会提醒你的条目。"""
    from core import reminders
    days = int(qs.get("days", ["60"])[0])
    past = qs.get("past", ["0"])[0] in ("1", "true", "yes")
    up = reminders.list_reminders(days_ahead=max(1, min(days, 3650)),
                                  include_past=past)
    return {"ok": True, "reminders": up, "count": len(up),
            "today_text": reminders.today_text(),
            "next": up[0] if up else None}


def post_reminders_refresh(payload):
    from core import reminders
    r = reminders.refresh()
    if r.get("ok"):
        r["msg"] = "已从关系档案重新扫描出 %d 条" % r.get("count", 0)
        try:
            events.emit("config_change", "提醒已刷新", level=events.INFO)
        except Exception:
            pass
    return r


def post_reminders_add(payload):
    from core import reminders
    r = reminders.add(payload.get("title"), payload.get("date"),
                      payload.get("repeat", "none"))
    if r.get("ok"):
        try:
            events.emit("config_change", "新增提醒：%s" % payload.get("title"),
                        level=events.SUCCESS)
        except Exception:
            pass
    return r


def post_reminders_delete(payload):
    from core import reminders
    return reminders.delete(payload.get("id"))


def get_onboarding(qs):
    """F4 新手指引：把「换台电脑重装一遍」的四步压成一个可勾选的清单。

    每一步都带一个直达动作（open: 面板页面 / api: 直接调的接口），
    全部完成后前端自动隐藏这张卡片。
    """
    import ui_server as ui  # 延迟导入：启动期 ui_server 正在 import 本模块
    refresh_env_from_registry()

    st = {}
    try:
        st = ui.get_status() or {}
    except Exception:
        st = {}
    running = bool(st.get("running"))

    cfg = load_cfg()
    has_key = bool(os.environ.get("DEEPSEEK_API_KEY")
                   or (cfg.get("ai", {}) or {}).get("api_key"))
    sp_path = os.path.join(DATA_DIR, "style_profile.json")
    try:
        has_style = os.path.exists(sp_path) and os.path.getsize(sp_path) > 32
    except Exception:
        has_style = False

    steps = [
        {
            "key": "backend",
            "title": "装好微信后端依赖",
            "desc": "按你的微信版本装 wxauto / wxauto4 / wechatauto 之一",
            "ok": running or (st.get("wechat") == "已连接"),
            "open": "console",
        },
        {
            "key": "key",
            "title": "配好 DeepSeek 密钥",
            "desc": "推荐存进环境变量 DEEPSEEK_API_KEY，不落盘最安全",
            "ok": has_key,
            "open": "console",
        },
        {
            "key": "style",
            "title": "生成风格档案",
            "desc": "跑一次风格提炼，AI 才知道你平时怎么说话",
            "ok": has_style,
            "open": "memory",
        },
        {
            "key": "running",
            "title": "把机器人开起来",
            "desc": "开启后它才会自动接她的话",
            "ok": running,
            "open": "console",
        },
    ]
    done = sum(1 for s in steps if s["ok"])
    return {"ok": True, "steps": steps, "done": done,
            "total": len(steps), "finished": done == len(steps)}


def get_guard_config(qs):
    cfg = load_cfg()
    g = dict(cfg.get("guard", {}) or {})
    g.setdefault("quiet_hours", [[23, 8]])
    g.setdefault("max_per_hour", 30)
    g.setdefault("dry_streak_limit", 3)
    g.setdefault("enabled", True)
    g.setdefault("strict", False)
    return {"ok": True, "guard": g,
            "now_quiet": guard_mod.Guard(cfg).in_quiet_hours()}


def get_backups(qs):
    rows = []
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        for f in sorted(os.listdir(BACKUP_DIR), reverse=True):
            if f.endswith(".zip"):
                p = os.path.join(BACKUP_DIR, f)
                rows.append({"name": f, "size": os.path.getsize(p),
                             "ts": os.path.getmtime(p),
                             "time": time.strftime("%Y-%m-%d %H:%M",
                                                   time.localtime(os.path.getmtime(p)))})
    except Exception:
        pass
    return {"ok": True, "backups": rows[:30]}


def get_tone_dials(qs):
    """B5：读当前语气滑杆值 + 翻译后的描述文本。"""
    cfg = load_cfg()
    style = cfg.get("style", {}) or {}
    dials = dict(style.get("tone_dials", {}) or {})
    enabled = bool(style.get("tone_dials_enabled", True))
    labels = {}
    if enabled and dials:
        try:
            from session import DIAL_HINT
        except Exception:
            DIAL_HINT = {}
        for k, v in dials.items():
            labels[k] = DIAL_HINT.get(k, "") if isinstance(DIAL_HINT, dict) else ""
    return {"ok": True, "dials": dials, "enabled": enabled, "labels": labels}


_GET_ROUTES = {
    "/api/events": get_events,
    "/api/cost": get_cost,
    "/api/insights": get_insights,
    "/api/health": get_health,
    "/api/memory_status": get_memory_status,
    "/api/facts": get_facts,
    "/api/guard": get_guard_config,
    "/api/onboarding": get_onboarding,
    "/api/persona/status": get_persona_status,
    "/api/reminders": get_reminders,
    "/api/backend/health": get_backend_health,
    "/api/chats": get_chats,
    # 注意：不能叫 /api/daily——那个是 2.0 的「聊天记录」，app.js 在用
    "/api/daily_review": get_daily,
    "/api/backups": get_backups,
    "/api/tone_dials": get_tone_dials,
}


def handle_get(path, qs):
    fn = _GET_ROUTES.get(path)
    if not fn:
        return None
    try:
        return fn(qs or {})
    except Exception as e:
        return {"ok": False, "error": str(e)[:200]}


# ---------------------------------------------------------------------------
# POST
# ---------------------------------------------------------------------------

def post_preview(payload):
    """B1 回复预演（干跑）：改完人设卡先看它会怎么说，**不发给对方**。"""
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "msg": "先输入她可能说的话"}
    cfg = load_cfg()
    refresh_env_from_registry()

    try:
        from ai_client import AIClient
        from core import cost as cost_mod
        from session import PromptBuilder, SessionManager
    except Exception as e:
        return {"ok": False, "msg": "依赖加载失败: %s" % str(e)[:120]}

    try:
        ai = AIClient(cfg["ai"])
    except Exception as e:
        return {"ok": False, "msg": "AI 客户端初始化失败: %s" % str(e)[:120]}

    style_cfg = cfg.get("style", {}) or {}
    profile_path = os.path.join(paths.base_dir(),
                                style_cfg.get("style_profile_path", "data/style_profile.json"))
    try:
        from session import MemoryManager
        prompts = PromptBuilder(cfg, profile_path,
                               memory_manager=MemoryManager(
                                   os.path.join(DATA_DIR, "memory.md")))
        sessions = SessionManager(max_messages=8, timeout_minutes=60)
        chat = "__preview__"
        sessions.add(chat, "user", text)
        messages = ([{"role": "system", "content": prompts.system_prompt()}]
                    + sessions.context(chat))
    except Exception as e:
        return {"ok": False, "msg": "构造 prompt 失败: %s" % str(e)[:120]}

    t0 = time.time()
    try:
        raw = ai.chat(messages)
    except Exception as e:
        return {"ok": False, "msg": "AI 调用失败: %s" % str(e)[:160]}
    latency = round(time.time() - t0, 2)

    # 用真实的护栏跑一遍：让你看到的，就是真会发出去的东西
    gd = guard_mod.Guard(cfg)
    verdict, reason, usable, bubbles = gd.check(raw, strict=False)
    return {
        "ok": True, "raw": raw, "used": usable, "bubbles": bubbles,
        "verdict": verdict, "reason": reason, "latency": latency,
        "cost": cost.fmt_cost((ai.last_cost or {}).get("cost", 0)),
        "model": ai.model,
        "msg": ("预览已生成" if verdict != "block" else "这条会被护栏拦下：%s" % reason),
    }


def post_feedback(payload):
    """B4 赞/踩：把「这条回复好不好」变成可积累的数据。"""
    score = int(payload.get("score", 0) or 0)
    if score not in (1, -1):
        return {"ok": False, "msg": "score 只能是 1（赞）或 -1（踩）"}
    ok = store.add_feedback(
        score,
        chat=payload.get("chat"),
        prompt=(payload.get("prompt") or "")[:500],
        reply=(payload.get("reply") or "")[:1000],
        note=(payload.get("note") or "")[:300])
    events.emit("feedback", "收到%s反馈" % ("赞" if score > 0 else "踩"),
                level=events.SUCCESS if score > 0 else events.WARN)
    st = store.feedback_stats(days=30)
    return {"ok": ok, "stats": st,
            "msg": "已记录，好评率 %.0f%%" % (st["rate"] * 100)}


def post_tone_dials(payload):
    """B5 语气滑杆：不写 md，拖一下就改质感。"""
    dials = payload.get("dials") or {}
    if not isinstance(dials, dict):
        return {"ok": False, "msg": "dials 应为对象"}
    clean = {}
    for k, v in dials.items():
        try:
            clean[str(k)] = max(0.0, min(1.0, float(v)))
        except (TypeError, ValueError):
            continue
    cfg = load_cfg()
    cfg.setdefault("style", {})
    cfg["style"]["tone_dials"] = clean
    cfg["style"]["tone_dials_enabled"] = bool(payload.get("enabled", True))
    ok = save_cfg(cfg)
    events.emit("config_change", "语气滑杆已更新", level=events.SUCCESS, dials=clean)
    return {"ok": ok, "dials": clean,
            "msg": "已保存，下一次回复立即生效"}


def post_memory_run(payload):
    """A1/A5：立即沉淀记忆（手动触发一天或全部）。"""
    all_days = bool(payload.get("all"))
    date_str = payload.get("date")
    try:
        from memory import summarizer
        from memory.worker import MemoryWorker, write_progress
    except Exception as e:
        return {"ok": False, "msg": "记忆模块加载失败: %s" % str(e)[:120]}

    cfg = load_cfg()
    refresh_env_from_registry()
    try:
        from ai_client import AIClient
        ai = AIClient(cfg["ai"])
    except Exception as e:
        return {"ok": False, "msg": "AI 初始化失败: %s" % str(e)[:120]}

    worker = MemoryWorker(ai, cfg)
    max_days = 99 if all_days else 1
    res = worker.run_now(date_str=date_str, max_days=max_days)
    write_progress(worker)
    if isinstance(res, dict) and res.get("error") and not res.get("date"):
        return {"ok": False, "msg": str(res.get("error"))[:200]}
    if isinstance(res, list):
        ok_n = sum(1 for r in res if r.get("summary"))
        facts_n = sum(len(r.get("facts", [])) for r in res)
        return {"ok": True, "msg": "已处理 %d 天（成功 %d），抽到 %d 条事实"
                                  % (len(res), ok_n, facts_n)}
    return {"ok": True, "date": res.get("date"), "summary": res.get("summary"),
            "facts": len(res.get("facts", [])),
            "msg": "已沉淀 %s" % res.get("date", "")}


def post_fact_delete(payload):
    fid = payload.get("id")
    ok = store.delete_fact(int(fid))
    events.emit("config_change", "删除事实 #%s" % fid)
    return {"ok": ok, "msg": "已删除" if ok else "删除失败"}


def post_fact_add(payload):
    key = (payload.get("key") or "").strip()[:30]
    val = (payload.get("value") or "").strip()[:200]
    if not key or not val:
        return {"ok": False, "msg": "key 和 value 都不能为空"}
    cat = (payload.get("category") or "other").strip()[:16]
    ok = store.upsert_fact(key, val, category=cat, confidence=0.95,
                           source_date=time.strftime("%Y-%m-%d"))
    events.emit("config_change", "手填事实：%s" % key, level=events.SUCCESS)
    return {"ok": ok, "msg": "已加入长期事实"}


def post_guard_save(payload):
    g = payload.get("guard") or {}
    if not isinstance(g, dict):
        return {"ok": False, "msg": "guard 应为对象"}
    cfg = load_cfg()
    cur = dict(cfg.get("guard", {}) or {})
    for k in ("enabled", "strict", "max_per_hour", "dry_streak_limit",
              "reconnect_after_fail", "quiet_hours"):
        if k in g:
            cur[k] = g[k]
    cfg["guard"] = cur
    ok = save_cfg(cfg)
    events.emit("config_change", "护栏配置已更新", level=events.SUCCESS)
    gd = guard_mod.Guard(cfg)
    quiet, span = gd.in_quiet_hours()
    return {"ok": ok, "guard": cur, "quiet_now": quiet, "span": span,
            "msg": "已保存（立即生效）"}


def post_guard_test(payload):
    """拿一段文字试护栏，看看哪些模式会被拦。

    now_hour：可选，用来模拟「如果是下午 3 点发这句会怎样」——
    免打扰时段的结果会完全不同，测试和时间点相关的规则离不开它。
    """
    text = (payload.get("text") or "").strip()
    if not text:
        return {"ok": False, "msg": "输入一段文字测试"}
    cfg = load_cfg()
    gd = guard_mod.Guard(cfg)
    now = None
    hour = payload.get("now_hour")
    if hour is not None:
        try:
            h = max(0, min(23, int(hour)))
            now = time.struct_time((2026, 1, 1, h, 0, 0, 0, 1, 0))
        except (TypeError, ValueError):
            now = None
    v, why, fixed, bubbles = gd.check(text, strict=bool(payload.get("strict")),
                                      is_proactive=bool(payload.get("proactive")),
                                      now=now)
    quiet, span = gd.in_quiet_hours(now)
    return {"ok": True, "verdict": v, "reason": why, "fixed": fixed,
            "bubbles": bubbles, "disabled": not gd.enabled,
            "quiet_now": quiet, "quiet_span": span}


def post_backup_create(payload):
    """D6：把配置和记忆打包成 zip。"""
    import zipfile
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamps = time.strftime("%Y%m%d_%H%M%S")
        name = "backup_%s.zip" % stamps
        out = os.path.join(BACKUP_DIR, name)
        root = paths.base_dir()
        includes = ["config.json", "bot_control.json"]
        for rel in ("data/memory.md", "data/persona.md", "data/relationship.md",
                    "data/style_profile.json", "data/schedule.json",
                    "data/proposals.json", "data/state.db", "data/chat_history.json"):
            if os.path.exists(os.path.join(root, rel)):
                includes.append(rel)
        ddir = os.path.join(root, "data")
        if os.path.isdir(ddir):
            for f in sorted(os.listdir(ddir)):
                if f.endswith(".jsonl"):
                    includes.append(os.path.join("data", f))
        n = 0
        with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
            for rel in includes:
                p = os.path.join(root, rel)
                if os.path.exists(p):
                    z.write(p, rel)
                    n += 1
        events.emit("backup", "创建快照 %s" % name, level=events.SUCCESS, files=n)
        return {"ok": True, "name": name, "files": n,
                "size": os.path.getsize(out), "msg": "备份完成，共 %d 个文件" % n}
    except Exception as e:
        return {"ok": False, "msg": "备份失败: %s" % str(e)[:160]}


def post_backup_restore(payload):
    """D6：从快照恢复（会先给当前 data 打个「还原前」备份）。"""
    import zipfile
    name = (payload.get("name") or "").strip()
    if not name or "/" in name or "\\" in name or not name.endswith(".zip"):
        return {"ok": False, "msg": "快照名不合法"}
    src = os.path.join(BACKUP_DIR, name)
    if not os.path.exists(src):
        return {"ok": False, "msg": "快照不存在"}
    post_backup_create({"auto": True})
    root = paths.base_dir()
    try:
        n = 0
        with zipfile.ZipFile(src, "r") as z:
            for item in z.namelist():
                target = os.path.join(root, item)
                os.makedirs(os.path.dirname(target), exist_ok=True)
                with z.open(item) as f, open(target, "wb") as o:
                    o.write(f.read())
                n += 1
        events.emit("backup", "从 %s 恢复 %d 个文件" % (name, n), level=events.WARN)
        return {"ok": True, "files": n, "msg": "已恢复 %d 个文件，建议重启机器人" % n}
    except Exception as e:
        return {"ok": False, "msg": "恢复失败: %s" % str(e)[:160]}


def post_events_clear(payload):
    ok = events.clear()
    return {"ok": ok, "msg": "事件时间线已清空"}


_POST_ROUTES = {
    "/api/preview": post_preview,
    "/api/feedback": post_feedback,
    "/api/tone_dials": post_tone_dials,
    "/api/memory/run": post_memory_run,
    "/api/fact/delete": post_fact_delete,
    "/api/fact/add": post_fact_add,
    "/api/guard/save": post_guard_save,
    "/api/guard/test": post_guard_test,
    "/api/backup/create": post_backup_create,
    "/api/backup/restore": post_backup_restore,
    "/api/events/clear": post_events_clear,
    "/api/persona/redistill": post_persona_redistill,
    "/api/persona/apply": post_persona_apply,
    "/api/backend/switch": post_backend_switch,
    "/api/chats/update": post_chats_update,
    "/api/chats/add": post_chats_add,
    "/api/reminders/refresh": post_reminders_refresh,
    "/api/reminders/add": post_reminders_add,
    "/api/reminders/delete": post_reminders_delete,
}


def handle_post(path, payload):
    fn = _POST_ROUTES.get(path)
    if not fn:
        return None
    try:
        store.init()
        return fn(payload or {})
    except Exception as e:
        return {"ok": False, "msg": "处理失败: %s" % str(e)[:200]}
