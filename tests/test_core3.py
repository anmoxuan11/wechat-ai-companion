# -*- coding: utf-8 -*-
"""3.0 新增内核模块的单元测试

隔离原则：core.paths.base_dir() 指向真实项目目录，测试**不能**污染真数据。
统一 monkeypatch store.db_path / events.events_path / 锁目录到 tmp_path。

覆盖：
  core.privacy   敏感信息识别 / 脱敏 / 事实体检
  core.guard     内容审查 / 免打扰 / 冷场 / 限流
  core.cost      用量估算 / 入库
  core.store     消息 / 用量 / 反馈 / 事实 / 摘要
  core.lock      跨进程互斥
  core.events    事件写入与聚合
  memory         事实解析 / 待沉淀日期
"""

import json
import os
import sys
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import cost, events, privacy, store  # noqa: E402
from core import guard as guard_mod  # noqa: E402
from core.lock import FileLock  # noqa: E402


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    """把 store / events / 锁全部重定向到临时目录。

    注意：store 的连接是 **thread-local** 的，换了 db_path 之后如果不清掉旧
    连接，所有测试会继续写进上一个用例的库里（表现为「数据莫名其妙变多」）。
    """
    conn = getattr(store._TLS, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
        store._TLS.conn = None
    monkeypatch.setattr(store, "db_path", lambda: str(tmp_path / "state.db"))
    monkeypatch.setattr(events, "events_path", lambda: str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(cost, "_PRICES", dict(cost.DEFAULT_PRICES))
    yield tmp_path


# ---------------------------------------------------------------------------
# privacy
# ---------------------------------------------------------------------------

def test_privacy_detects_card_and_blocks():
    text = "她的招商银行卡号是6214 8329 8600 8352"
    hits = privacy.detect(text)
    assert any(h["name"] == "银行卡号" for h in hits)
    assert privacy.is_blocked(text) is True


def test_privacy_allows_normal_sentence():
    assert privacy.detect("我们明天去商场配眼镜") == []
    assert privacy.is_blocked("她在准备考研，还有100天") is False


def test_privacy_redact_keeps_head_tail():
    out = privacy.redact("手机号 19522431962")
    assert "19522431962" not in out
    assert "1962" in out


def test_privacy_filter_fact_drops_sensitive():
    item = {"key": "银行账户", "value": "她的卡号是6214 8329 8600 8352"}
    ok, reason = privacy.filter_fact_dict(item)
    assert ok is False and "高敏" in reason


def test_privacy_filter_fact_redacts_phone():
    item = {"key": "手机号", "value": "她有一个195开头的手机号，号码是19522431962"}
    ok, reason = privacy.filter_fact_dict(item)
    assert ok is True and "19522431962" not in item["value"]


# ---------------------------------------------------------------------------
# guard
# ---------------------------------------------------------------------------

def test_guard_blocks_identity_leak():
    v, reason, _fixed = guard_mod.review("我是人工智能助手，请问有什么可以帮您？")
    assert v == "block"
    assert "身份" in reason
    # 被拦下的回复不会进入气泡列表
    _verdict, _why, _used, bubbles = guard_mod.Guard({}).check(
        "我是人工智能助手，请问有什么可以帮您？")
    assert bubbles == []


def test_guard_warns_on_markdown():
    v, reason, fixed = guard_mod.review("## 标题\n**粗体**\n- 列表")
    assert v == "warn"
    assert "**" not in fixed          # 粗体标记被清掉
    assert not fixed.startswith("#")  # 标题符号被清掉
    assert "列表" in fixed            # 正文内容保留


def test_guard_passes_natural_reply():
    v, reason, fixed = guard_mod.review("今天累了吧 早点睡")
    assert v == "ok"
    assert fixed == "今天累了吧 早点睡"


def test_guard_strips_periods():
    assert guard_mod.clean_format("好的。收到。") == "好的收到"


def test_guard_splits_bubbles_and_dedupes():
    b = guard_mod.split_bubbles("心疼你三秒\n剩下的时间用来笑你\n心疼你三秒")
    assert b == ["心疼你三秒", "剩下的时间用来笑你"]


@pytest.mark.parametrize("hhmm,expected", [
    ((23, 30), True), ((3, 0), True), ((12, 0), False), ((8, 0), False),
])
def test_guard_quiet_hours(hhmm, expected):
    g = guard_mod.Guard({"guard": {"quiet_hours": [[23, 8]]}})
    t = time.struct_time((2026, 9, 14, hhmm[0], hhmm[1], 0, 0, 0, 0))
    got, _ = g.in_quiet_hours(now=t)
    assert got is expected


def test_guard_dry_streak_stops_proactive():
    g = guard_mod.Guard({"guard": {"quiet_hours": [], "dry_streak_limit": 2}})
    for _ in range(2):
        g.note_dry()
    ok, reason = g.can_send(is_proactive=True)
    assert ok is False and "无人回应" in reason
    g.note_incoming()                       # 她开口了 → 计数清零
    ok, _ = g.can_send(is_proactive=True)
    assert ok is True


def test_guard_hourly_limit():
    g = guard_mod.Guard({"guard": {"quiet_hours": [], "max_per_hour": 2}})
    g.note_sent(2)
    ok, reason = g.can_send()
    assert ok is False and "上限" in reason


def test_guard_disabled_passes_everything():
    g = guard_mod.Guard({"guard": {"enabled": False, "quiet_hours": [[0, 23]]}})
    ok, _ = g.can_send()
    assert ok is True


# ---------------------------------------------------------------------------
# cost
# ---------------------------------------------------------------------------

def test_cost_estimate_math():
    # deepseek-chat: 输入 2元/百万，缓存命中 0.5，输出 8
    est = cost.estimate("deepseek-chat", prompt_tokens=1_000_000,
                        completion_tokens=0, cached_tokens=1_000_000)
    assert est["cost"] == pytest.approx(0.5)
    est = cost.estimate("deepseek-chat", prompt_tokens=1_000_000,
                        completion_tokens=1_000_000)
    assert est["cost"] == pytest.approx(10.0)


def test_cost_unknown_model_estimated():
    est = cost.estimate("some-random-model", 1000, 1000)
    assert est["estimated"] is True


def test_cost_record_lands_in_store(store_ready=None):
    store.init()
    r = cost.record("deepseek-chat", "chat", 1000, 200)
    assert r["in_db"] is True
    total = store.usage_total(days=1)
    assert total["calls"] == 1 and total["cost"] > 0


# ---------------------------------------------------------------------------
# store
# ---------------------------------------------------------------------------

def test_store_message_roundtrip():
    store.init()
    assert store.add_message("2026-09-01", "伴侣示例", "她", "今天好累")
    rows = store.messages("2026-09-01")
    assert len(rows) == 1
    assert rows[0]["content"] == "今天好累"
    assert rows[0]["is_ai"] == 0
    assert store.count_messages() == 1


def test_store_exclude_ai_messages():
    store.init()
    store.add_message("2026-09-01", "伴侣示例", "她", "你好")
    store.add_message("2026-09-01", "我", "我", "hi", is_ai=True)
    assert store.count_messages() == 2
    assert store.count_messages(include_ai=False) == 1


def test_store_facts_upsert_and_delete():
    store.init()
    store.upsert_fact("宠物", "她养了一只叫元宝的橘猫", "life", 0.9, "2026-09-01")
    store.upsert_fact("宠物", "她养了一只叫元宝的橘猫", "life", 0.9, "2026-09-02")
    rows = store.list_facts(limit=10)
    assert len(rows) == 1          # 同 key+value 不重复
    assert rows[0]["hits"] == 2    # 重复出现累加
    assert store.delete_fact(rows[0]["id"]) is True
    assert store.list_facts(limit=10) == []


def test_store_summaries_scope_separated():
    store.init()
    store.save_summary("day", "2026-09-01", "第一天")
    store.save_summary("week", "2026-W36", "这一周")
    assert store.get_summary("day", "2026-09-01") == "第一天"
    assert store.get_summary("week", "2026-W36") == "这一周"
    assert store.has_summary("day", "2026-09-02") is False
    store.save_summary("day", "2026-09-01", "改过了")
    assert store.get_summary("day", "2026-09-01") == "改过了"


def test_store_feedback_stats():
    store.init()
    store.add_feedback(1, chat="伴侣示例", reply="哄她")
    store.add_feedback(1, chat="伴侣示例", reply="又哄她")
    store.add_feedback(-1, chat="伴侣示例", reply="这次踩了")
    st = store.feedback_stats(days=30)
    assert st["up"] == 2 and st["down"] == 1 and st["total"] == 3
    assert st["rate"] == pytest.approx(0.667, abs=1e-3)


# ---------------------------------------------------------------------------
# lock
# ---------------------------------------------------------------------------

def test_lock_mutual_exclusion(tmp_path):
    p = FileLock("unit", base=str(tmp_path))
    p.acquire(timeout=1)
    try:
        assert os.path.exists(os.path.join(str(tmp_path), ".lock_unit"))
        q = FileLock("unit", base=str(tmp_path), timeout=0.2)
        with pytest.raises(Exception):
            q.acquire()
    finally:
        p.release()


def test_lock_steal_after_release(tmp_path):
    a = FileLock("unit2", base=str(tmp_path))
    a.acquire(timeout=1)
    a.release()
    b = FileLock("unit2", base=str(tmp_path), timeout=1)
    assert b.acquire() is True
    b.release()


def test_lock_context_manager(tmp_path):
    with FileLock("unit3", base=str(tmp_path)):
        pass
    assert not os.path.exists(os.path.join(str(tmp_path), ".lock_unit3"))


# ---------------------------------------------------------------------------
# events
# ---------------------------------------------------------------------------

def test_events_emit_and_read():
    events.emit("reply", "回复了她", level=events.SUCCESS, chat="伴侣示例")
    events.emit("ai_error", "AI 超时", level=events.ERROR)
    rows = events.recent(n=10)
    assert len(rows) == 2
    assert rows[-1]["kind"] == "ai_error"
    assert len(events.recent(n=1)) == 1


def test_events_filter_by_kind():
    events.emit("reply", "a")
    events.emit("guard_block", "b")
    assert len(events.recent(n=10, kinds={"reply"})) == 1
    assert len(events.recent(n=10, level=events.ERROR)) == 0


def test_events_aggregation():
    events.emit("reply", "1", level=events.SUCCESS)
    events.emit("reply", "2", level=events.SUCCESS)
    events.emit("ai_error", "x", level=events.ERROR)
    agg = {d["kind"]: d for d in events.summary_by_kind(days=1)}
    assert agg["reply"]["count"] == 2
    assert agg["ai_error"]["errors"] == 1


def test_events_clear():
    events.emit("reply", "x")
    assert events.clear() is True
    assert events.recent(n=10) == []


# ---------------------------------------------------------------------------
# memory.summarizer 纯函数
# ---------------------------------------------------------------------------

def test_parse_facts_from_fenced_json():
    from memory.summarizer import parse_facts
    raw = '```json\n[{"key":"宠物","value":"她养了只猫","category":"life","confidence":0.9}]\n```'
    got = parse_facts(raw)
    assert len(got) == 1 and got[0]["key"] == "宠物"


def test_parse_facts_tolerates_garbage():
    from memory.summarizer import parse_facts
    assert parse_facts("") == []
    assert parse_facts("我没看懂你的意思") == []
    assert parse_facts('可能吧 [{"key":"a","value":"b"}] 就这样') == [
        {"key": "a", "value": "b"}]


def test_iso_week_mapping():
    from memory.summarizer import iso_week
    assert iso_week("2026-09-01") == iso_week("2026-09-04")   # 同一周
    assert iso_week("2026-09-01") != iso_week("2026-09-10")   # 不同周


def test_pending_dates_excludes_summarized(tmp_path, monkeypatch):
    from memory import daily, summarizer
    monkeypatch.setattr(daily, "daily_dir", lambda: str(tmp_path))
    store.init()
    store.save_summary("day", "2026-08-01", "已有摘要")
    for d in ("2026-08-01", "2026-08-02"):
        with open(os.path.join(str(tmp_path), d + ".jsonl"), "w", encoding="utf-8") as f:
            f.write(json.dumps({"chat": "伴侣示例", "who": "她",
                                "content": "hi", "ts": 1.0}) + "\n")
    pending = summarizer.pending_dates(today="2026-08-03")
    assert pending == ["2026-08-02"]


def test_to_dialog_renders_lines():
    from memory.daily import to_dialog
    rows = [{"who": "她", "content": "累死了"}, {"who": "你", "content": "抱抱"}]
    assert to_dialog(rows) == "她: 累死了\n你: 抱抱"
    assert to_dialog(rows, role_map={"她": "她", "你": "我"}) == "她: 累死了\n我: 抱抱"
