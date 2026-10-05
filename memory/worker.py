# -*- coding: utf-8 -*-
"""记忆沉淀后台线程（3.0 · A1/A5 的落地点）

2.0 的记忆是「主流程的副作用」：只有在正要发回复且刚好跨天时才会跑一次。
机器人暂停、那天没触发回复、跨了两天——记忆就不更新了。这是 data/memory.md
常年 0 字节的根因。

3.0 把它抽成独立后台能力：

  - 机器人启动 → 立刻扫描所有「有对话但没摘要」的日期 → 排队补跑
  - 补跑有节流（默认每次启动最多补 3 天），不会一口气烧掉一堆 token
  - 每天凌晨定点算昨天；任何时候都能手动触发（面板按钮 / 命令行）
  - 日摘要 → 事实抽取 →（凑够一周）→ 周摘要
  - 进度写 agent_state 文件，面板实时可见

关键约束：**任何失败都不能影响机器人主流程**。AI 挂了、网络断了，
worker 记一条事件然后停下，机器人照常陪聊。
"""

import threading
import time

from core import events, store
from . import daily, summarizer


class MemoryWorker:
    """后台沉淀线程。

    ai:      AIClient 实例（复用主线程的，不额外建）
    cfg:     config dict
    on_done: 每次沉淀完成后的回调（可选），签名 fn(result_dict)
    """

    def __init__(self, ai, cfg=None, on_done=None):
        self.ai = ai
        self.cfg = cfg or {}
        self.on_done = on_done

        m = self.cfg.get("memory", {}) or {}
        self.enabled = bool(m.get("enabled", True))
        self.max_per_run = int(m.get("max_days_per_run", 3))
        self.daily_at = str(m.get("daily_at", "03:30"))
        self.extract_facts = bool(m.get("extract_facts", True))
        self.week_enabled = bool(m.get("week_summary", True))
        self.start_delay = float(m.get("start_delay_seconds", 20))
        self.gap = float(m.get("gap_seconds", 3))
        self.chats = None  # 可选：只沉淀指定会话

        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self.state = {
            "running": False, "enabled": self.enabled,
            "phase": "idle",        # idle / backfill / schedule / manual
            "current": "", "queued": [], "done": 0, "failed": 0,
            "last_result": None, "last_ts": 0, "last_error": "",
            "started_at": 0,
        }

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self):
        if not self.enabled:
            events.emit("memory_summary", "记忆沉淀已禁用", level=events.WARN)
            return None
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name="memory-worker")
        self._thread.start()
        return self._thread

    def stop(self):
        self._stop.set()
        self._wake.set()

    def wake(self):
        """手动唤醒一次（面板「立即沉淀」按钮）。"""
        self._wake.set()

    def progress(self):
        """给面板看的进度快照。"""
        with self._lock:
            return dict(self.state)

    # ------------------------------------------------------------------
    # 主循环
    # ------------------------------------------------------------------
    def _loop(self):
        if self.start_delay > 0:
            self._stop.wait(self.start_delay)   # 别跟机器人启动抢资源
        last_daily_date = ""

        while not self._stop.is_set():
            try:
                # 1) 定点：每天沉淀一次「昨天」
                now = time.localtime()
                if (self.daily_at and
                        time.strftime("%H:%M", now) >= self.daily_at and
                        last_daily_date != time.strftime("%Y-%m-%d")):
                    last_daily_date = time.strftime("%Y-%m-%d")
                    self._set(phase="schedule")
                    self._run_daily_once()

                # 2) 补跑历史上缺摘要的日期
                pending = summarizer.pending_dates()
                if pending:
                    self._set(phase="backfill", queued=pending[:50])
                    self._backfill(pending)

            except Exception as e:
                self._set(last_error=str(e)[:200])
                events.emit("memory_summary", "记忆线程异常: %s" % str(e)[:120],
                            level=events.ERROR)

            # 3) 等下一次唤醒（最多 10 分钟自醒一次）
            self._set(phase="idle", running=False)
            self._wake.wait(600)
            self._wake.clear()

    def _run_daily_once(self):
        import datetime
        y = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        if store.has_summary("day", y):
            return
        rows = daily.read_day(y)
        if len(rows) < summarizer.DAY_MIN_LINES:
            return
        self._process(y, rows)

    def _backfill(self, pending):
        n = 0
        for date_str in pending:
            if self._stop.is_set():
                break
            rows = daily.read_day(date_str)
            if len(rows) < summarizer.DAY_MIN_LINES:
                continue
            self._process(date_str, rows)
            n += 1
            if n >= self.max_per_run:
                self._set(last_result="本次补跑已达上限 %d 天，剩余下次启动继续"
                                      % self.max_per_run)
                break
            time.sleep(self.gap)

    # ------------------------------------------------------------------
    # 单日处理
    # ------------------------------------------------------------------
    def _process(self, date_str, rows):
        self._set(running=True, current=date_str, last_error="")
        events.emit("memory_summary", "开始沉淀 %s（%d 条对话）" % (date_str, len(rows)))

        result = {"date": date_str, "lines": len(rows), "summary": False,
                  "facts": 0, "week": "", "error": ""}
        try:
            r = summarizer.summarize_day(
                self.ai, date_str, rows=rows,
                my_name=self.cfg.get("my_name", "我"),
                partner_name=self.cfg.get("partner_name", ""))
            result["summary"] = bool(r.get("ok"))
            result["error"] = r.get("error", "")
            if r.get("ok"):
                self._bump_done()
                events.emit("memory_summary", "%s 日摘要已生成" % date_str,
                            level=events.SUCCESS, days=1)
            else:
                self._bump_failed()
        except Exception as e:
            result["error"] = str(e)[:200]
            self._bump_failed()

        if self.extract_facts and not result["error"]:
            try:
                fr = summarizer.extract_facts(self.ai, date_str, rows=rows)
                result["facts"] = len(fr.get("facts", []))
                if result["facts"]:
                    events.emit("memory_summary",
                                "%s 抽取 %d 条事实" % (date_str, result["facts"]),
                                level=events.SUCCESS, days=1)
            except Exception as e:
                result["error"] = (result["error"] + "; 事实抽取: " + str(e)[:80]).strip("; ")

        # 凑够一周就压周摘要
        if self.week_enabled and result["summary"]:
            try:
                wid = summarizer.iso_week(date_str)
                day_count = len([s for s in store.list_summaries("day", n=400)
                                 if summarizer.iso_week(s["date"]) == wid])
                if day_count >= 3 and not store.has_summary("week", wid):
                    wr = summarizer.summarize_week(self.ai, wid)
                    if wr.get("ok"):
                        result["week"] = wid
                        events.emit("memory_summary", "%s 周摘要已生成" % wid,
                                    level=events.SUCCESS)
            except Exception:
                pass

        self._set(last_result=result, last_ts=time.time(), running=False, current="")
        if self.on_done:
            try:
                self.on_done(result)
            except Exception:
                pass
        return result

    # ------------------------------------------------------------------
    # 手动执行（面板按钮走这条路，同步执行，便于立即反馈）
    # ------------------------------------------------------------------
    def run_now(self, date_str=None, max_days=1):
        """立即沉淀指定日期（或最早未沉淀的一天）。返回结果 dict。"""
        if date_str:
            rows = daily.read_day(date_str)
            return self._process(date_str, rows)
        pending = summarizer.pending_dates()
        if not pending:
            return {"ok": False, "error": "没有需要沉淀的日期", "date": ""}
        out = []
        for d in pending[:max_days]:
            rows = daily.read_day(d)
            if len(rows) < summarizer.DAY_MIN_LINES:
                continue
            out.append(self._process(d, rows))
        return out[0] if out else {"ok": False, "error": "候选日期对话不足", "date": ""}

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------
    def _set(self, **kw):
        with self._lock:
            self.state.update(kw)
            snap = dict(self.state)
        # 进度同步到文件：面板是另一个进程，读不到内存状态
        _write_progress_snapshot(snap)

    def _bump_done(self):
        with self._lock:
            self.state["done"] = self.state.get("done", 0) + 1

    def _bump_failed(self):
        with self._lock:
            self.state["failed"] = self.state.get("failed", 0) + 1


def state_path():
    import os
    from core.paths import data_dir
    return os.path.join(data_dir(), "memory_worker.json")


def _write_progress_snapshot(snap):
    """内部：把快照落盘（每 2 秒最多一次，避免频繁写小文件）。"""
    global _LAST_WRITE
    import json
    import os
    try:
        now = time.time()
        if now - _LAST_WRITE < 2.0 and snap.get("running"):
            return False
        _LAST_WRITE = now
        snap = dict(snap)
        snap["updated"] = now
        p = state_path()
        os.makedirs(os.path.dirname(p), exist_ok=True)
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(snap, f, ensure_ascii=False, indent=2)
            f.flush()
        os.replace(tmp, p)
        return True
    except Exception:
        return False


_LAST_WRITE = 0.0


def write_progress(worker):
    """把进度写到文件，供 ui_server 读取（面板进程和 bot 不同进程）。"""
    globals()["_LAST_WRITE"] = 0.0   # 立即写，不节流
    return _write_progress_snapshot(worker.progress())


def read_progress():
    import json
    import os
    try:
        with open(state_path(), "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"running": False, "enabled": True, "phase": "idle",
                "current": "", "queued": [], "done": 0, "failed": 0,
                "last_result": None, "last_ts": 0, "last_error": ""}
