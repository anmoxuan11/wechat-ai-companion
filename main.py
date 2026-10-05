# -*- coding: utf-8 -*-
"""
微信 AI 陪聊机器人 · 主程序 3.0
模仿你的语气，自动回复对象的消息。

3.0 相对 2.0 的关键变化：
  A1/A5  成长记忆复活：独立后台线程补跑历史摘要 + 事实抽取 + 周摘要
  A2     修掉 bot.pid 写不出来的千古 bug（PID_PATH 从未定义）
  A3     schedule.json 走跨进程锁 + 原子写，不再被双进程互相覆盖
  A4/G1  main 全面接入 core（事件线 / SQLite / 护栏 / 成本）
  E1/E2  发送前内容审查 + 免打扰时段 / 冷却 / 冷场收手
  B2     主动发起：她安静久了会自己去关心她
  D1/D5  token 与成本入账、延迟与失败率可查
  D3/D4  崩溃自愈与掉线重连

人工接管（通过"文件传输助手"发命令，即 config.control_chat）：
  接管/暂停   -> 暂停 AI 自动回复
  恢复/启动   -> 重新开启自动回复
  状态        -> 当前状态
  沉淀        -> 立即沉淀昨天的成长记忆
  成本        -> 今日/本月 token 花费

风险提示：仅低频白名单回复，勿群发勿加人；iLink 为官方接口无封号风险，
wxauto 为 UI 自动化存在理论风险，建议对象号测试稳定后再用主力号。
"""

import json
import logging
import os
import random
import sys
import threading
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, BASE_DIR)

from backends import create_backend, BackendError
from ai_client import AIClient, VisionClient
from session import SessionManager, PromptBuilder, MemoryManager

# --- 3.0：接入 core ---
from core import (atomicio, backend_health, chatcfg, cost, events,
                  guard as guard_mod, hotreload, mood as mood_mod, security,
                  store)
from core.lock import FileLock
from memory import daily as mdaily
from memory import summarizer
from memory.worker import MemoryWorker, write_progress

CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CONTROL_PATH = os.path.join(BASE_DIR, "bot_control.json")
SCHEDULE_PATH = os.path.join(BASE_DIR, "data", "schedule.json")
BOT_STATUS_PATH = os.path.join(BASE_DIR, "data", "bot_status.json")
PID_PATH = os.path.join(BASE_DIR, "data", "bot.pid")   # A2：2.0 里这个常量根本没定义
# E3：面板请求切换消息通道的「传话条」（面板和机器人是两个进程）
BACKEND_SWITCH_PATH = os.path.join(BASE_DIR, "data", "backend_switch.json")

# Windows 下重定向 stdout 默认用 ANSI(GBK) 编码，面板按 UTF-8 读日志会乱码/找不到标记。
if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

_log_handlers = [logging.StreamHandler(sys.stdout)]
try:
    _log_handlers.append(logging.FileHandler(os.path.join(BASE_DIR, "bot.log"), encoding="utf-8"))
except Exception:
    try:
        _log_handlers.append(logging.FileHandler(
            os.path.join(BASE_DIR, "bot_%d.log" % os.getpid()), encoding="utf-8"))
    except Exception:
        pass
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=_log_handlers,
)
log = logging.getLogger("main")


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


class Bot:

    def __init__(self):
        # Windows 上用户环境变量常常"设了却读不到"（IMSless 进程拿的是旧环境块），
        # 从注册表补一次，避免"明明配好了却报未配置 API Key"。
        security.refresh_env_from_registry()
        self.cfg = load_config()
        # G2：留一份「开机时」的配置，用来判断哪些改动至今还没真正生效
        # （AI 模型、微信后端这些换不掉的东西，重启前一直挂着）
        self._boot_cfg = json.loads(json.dumps(self.cfg))
        self._pending_cold = []
        os.makedirs(os.path.join(BASE_DIR, "data"), exist_ok=True)
        store.init()

        self._last_msg_ts = {}
        self._last_gap = {}
        self._chat_mood = {}          # B3：chat -> (tags, level, ts)，10 分钟内有效
        self._mood_ttl = 600
        self._buffer = {}
        self._buffer_ts = {}

        # G2：配置热重载——记住文件的修改时间，主循环里比对它就知道要不要重载
        self._cfg_mtime = self._cfg_mtime_now()
        self._cfg_lock = threading.Lock()

        self.daily_count = 0
        self.daily_date = time.strftime("%Y-%m-%d")
        self.wechat_ready = False

        self.paused = (self.cfg.get("safety", {}) or {}).get("pause_by_default", False)
        # B8：listen 的项可能是字符串也可能是对象，统一取名字
        # listen_order 保留原始顺序，兜底选人时才不会变成抽奖
        self.listen_order = chatcfg.names(self.cfg)
        self.listen = set(self.listen_order)

        self.backend = create_backend(self.cfg)
        self.ai = AIClient(self.cfg["ai"])
        self.vision = VisionClient(self.cfg.get("vision", {}))
        style_cfg = self.cfg.get("style", {})
        profile_path = os.path.join(BASE_DIR, style_cfg.get("style_profile_path", "data/style_profile.json"))
        self.memory = MemoryManager(os.path.join(BASE_DIR, "data", "memory.md"), keep_days=3)
        self.prompts = PromptBuilder(self.cfg, profile_path, memory_manager=self.memory)
        self.sessions = SessionManager(
            max_messages=style_cfg.get("max_context_messages", 40),
            timeout_minutes=style_cfg.get("session_timeout_minutes", 240),
        )
        self._daily_dir = os.path.join(BASE_DIR, "data", "daily")

        # --- 3.0 新增组件 ---
        # G2：启动也走「热更新」的同一条路，保证两边行为一致，
        # 不会出现「重启后是 A、热加载后变成 B」这种最难查的问题
        self._apply_hot(self.cfg)
        self.start_ts = 0.0
        self._backend_fail = 0          # 连续后端失败次数（掉线判定）
        self._last_incoming_ts = 0.0    # 对方最后说话时间（主动发起点）
        self._last_error = ""
        self.reply_count = 0
        self.blocked_count = 0
        self.latency_sum = 0.0
        self.latency_n = 0

        self.memory_worker = MemoryWorker(self.ai, self.cfg, on_done=self._on_memory_done)
        self._proactive_cfg = self.cfg.get("proactive", {}) or {}

    # ---------- G2 配置热重载 ----------
    @staticmethod
    def _cfg_mtime_now():
        try:
            return os.path.getmtime(CONFIG_PATH)
        except OSError:
            return 0.0

    @staticmethod
    def _default_media_policy():
        return {
            "sticker": "reply", "voice": "reply", "video": "reply",
            "file": "reply", "location": "reply", "link": "reply",
            "redpacket": "log", "transfer": "log", "recall": "log",
            "card": "log", "other": "log",
        }

    def _apply_hot(self, cfg):
        """把「不用重启就能改」的部分重新套到运行中的机器人身上。

        注意这里**不动 self.paused**：那是你用「接管/恢复」手动控制的，
        每次热重载都拿配置里的默认值盖一遍，等于把你的接管偷偷撤销了。
        """
        safety = cfg.get("safety", {}) or {}
        self.cooldown = safety.get("cooldown_seconds", 20)
        self.daily_limit = safety.get("daily_reply_limit", 200)
        self.reply_groups = safety.get("reply_groups", False)
        self.typing_delay = safety.get("typing_delay_seconds", [2, 6])
        self.settle_seconds = safety.get("settle_seconds", 3)
        self.typing_gap = safety.get("typing_gap_seconds", 2.0)
        self.typing_extra_wait = safety.get("typing_extra_wait_seconds", 3.0)
        self.media_policy = (cfg.get("media") or self._default_media_policy())
        self.control_chat = cfg.get("control_chat", "文件传输助手")
        self._proactive_cfg = cfg.get("proactive", {}) or {}
        self.guard = guard_mod.Guard(cfg)

        # B8：每个监听对象一份独立配置（老的字符串列表也会自动兼容）
        self.chat_cfg = chatcfg.normalize(cfg)
        self._prompts_cache = {}       # chat -> PromptBuilder（只为有覆盖的建）

        # 风格：人设卡 / 关系档案 / 语气滑杆本来就支持热读，重建一次最省心
        try:
            style_cfg = cfg.get("style", {}) or {}
            profile_path = os.path.join(
                BASE_DIR, style_cfg.get("style_profile_path", "data/style_profile.json"))
            self.prompts = PromptBuilder(
                cfg, profile_path, memory_manager=getattr(self, "memory", None))
        except Exception as e:
            log.warning("重新加载风格档案失败（沿用旧的）: %s", str(e)[:120])

        try:
            m = (cfg.get("style", {}) or {}).get("max_context_messages")
            if m and hasattr(self, "sessions"):
                self.sessions.max_messages = int(m)
        except (TypeError, ValueError):
            pass

    def _maybe_reload_config(self):
        """配置变了就热加载。放在主循环里，一秒内能感知到。"""
        m = self._cfg_mtime_now()
        if m == self._cfg_mtime:
            return
        with self._cfg_lock:
            # 读文件时可能正被另一个进程写一半，读坏了就下次再来，别让机器人崩
            try:
                new = load_config()
            except Exception as e:
                self._cfg_mtime = m
                log.warning("配置读取失败，跳过本次重载: %s", str(e)[:120])
                return
            res = hotreload.classify(self.cfg, new)
            if not res["changed"]:
                self._cfg_mtime = m
                return

            old_listen = set(self.listen)
            self.cfg = new
            self._apply_hot(new)

            new_listen = chatcfg.names(new)
            self.listen_order = list(new_listen)
            new_listen = set(new_listen)
            for name in sorted(new_listen - old_listen):
                try:
                    self.backend.add_listener(name)
                    events.emit("config_change", "新增监听：%s" % name,
                                level=events.SUCCESS)
                except Exception as e:
                    log.error("新增监听 [%s] 失败: %s", name, e)
                    events.emit("error", "新增监听 %s 失败" % name,
                                level=events.WARN, error=str(e)[:120])
            dropped = old_listen - new_listen
            if dropped:
                # 后端没有「取消监听」的接口，直接在收消息那一步过滤掉，
                # 效果一样，而且不用重建微信连接。
                events.emit("config_change", "已停止监听：%s" % "、".join(sorted(dropped)))
            self.listen = new_listen
            if getattr(self, "_pending_cold", None) is None:
                self._pending_cold = []
            self._pending_cold = hotreload.classify(self._boot_cfg, self.cfg)["cold"]

            # 主动发起：中途打开也要能开起来（线程只在启动时按开关起过一次）
            if self._proactive_cfg.get("enabled") and not getattr(self, "_proactive_started", False):
                threading.Thread(target=self._proactive_loop, daemon=True).start()
                self._proactive_started = True

            self._cfg_mtime = m
            msg = hotreload.describe(res)
            log.info("配置已重载：%s", msg)
            events.emit("config_change", msg,
                        level=events.WARN if res["cold"] else events.SUCCESS)
            self._write_status()

    # ---------- 生命周期 ----------
    def start(self):
        self.start_ts = time.time()
        log.info("=" * 60)
        log.info("微信 AI 陪聊机器人 3.0 启动")
        log.info("后端: %s | 监听: %s | 控制会话: %s",
                 self.backend.name, ", ".join(self.listen), self.control_chat)
        log.info("风格档案: %s", "已加载" if self.prompts.profile else "未生成（建议先跑 tools/style_builder.py）")
        log.info("=" * 60)
        events.emit("bot_start", "机器人启动（v3.0）", level=events.SUCCESS,
                    backend=self.backend.name, listen=sorted(self.listen))

        ctrl = self._read_control()
        self.paused = bool(ctrl.get("paused", self.paused))
        self._write_control({"paused": self.paused})
        self._write_status()

        # A2：bot.pid 以前因为这个常量不存在而永远写不出来（NameError 被静默吞掉）
        ok, err = atomicio.atomic_write_text(PID_PATH, str(os.getpid()), backup=False)
        if not ok:
            log.warning("写 bot.pid 失败: %s", err)
            events.emit("error", "写 bot.pid 失败", level=events.WARN, error=str(err))

        # 3.0：把历史 jsonl 灌进 SQLite（首次运行 / state.db 丢失后自愈）
        self._backfill_store()

        # C6：开机器的时候顺嘴提醒一句近三天的约定。
        # 纪念日这种事，忘了的代价远大于多看见一行字。
        try:
            from core import reminders
            txt = reminders.today_text()
            if txt:
                log.info("临近的约定：%s", txt)
                events.emit("reminder", txt, level=events.INFO)
        except Exception as e:
            log.debug("提醒加载失败（不影响运行）: %s", e)

        try:
            self.backend.setup(self.cfg)
        except BackendError as e:
            log.error("后端初始化失败: %s", e)
            events.emit("wechat_lost", "后端初始化失败: %s" % str(e)[:120],
                        level=events.ERROR)
            sys.exit(1)

        for name in self.listen | {self.control_chat}:
            try:
                self.backend.add_listener(name)
            except Exception as e:
                log.error("监听 [%s] 初始化失败，已跳过: %s", name, e)
                events.emit("wechat_lost", "监听 [%s] 失败" % name,
                            level=events.ERROR, error=str(e)[:160])

        self.wechat_ready = True
        self.wechat_ready_since_fail = False
        backend_health.set_current(self.backend.name)
        backend_health.note_ok(self.backend.name)
        self._write_status()

        self._notify_control("机器人已启动(v3.0)，当前状态: " + ("暂停(接管中)" if self.paused else "自动回复中"))
        log.info("开始监听... (Ctrl+C 停止)")

        threading.Thread(target=self._scheduler_loop, daemon=True).start()
        threading.Thread(target=self._status_thread, daemon=True).start()
        self.memory_worker.start()
        threading.Thread(target=self._progress_flush_loop, daemon=True).start()
        if self._proactive_cfg.get("enabled"):
            threading.Thread(target=self._proactive_loop, daemon=True).start()
            self._proactive_started = True

        hb = 0
        while True:
            try:
                self._maybe_reload_config()
                self._maybe_switch_backend()

                ctrl = self._read_control()
                new_paused = bool(ctrl.get("paused", self.paused))
                if new_paused != self.paused:
                    self.paused = new_paused
                    self._write_status()
                    events.emit("takeover", "人工接管" if new_paused else "恢复自动回复")

                try:
                    msgs = self.backend.poll_messages()
                    self._backend_fail = 0
                    # E3：健康度记账。每秒都写盘太浪费，一分钟记一次够了
                    now = time.time()
                    if now - getattr(self, "_last_ok_note", 0) > 60:
                        self._last_ok_note = now
                        backend_health.note_ok(self.backend.name)
                    if not self.wechat_ready:
                        self.wechat_ready = True
                        events.emit("wechat_back", "微信连接恢复", level=events.SUCCESS)
                    for msg in msgs:
                        self._handle(msg)
                except BackendError as e:
                    self._on_backend_error(e)

                self._maybe_flush()
                hb += 1
                if hb % 10 == 0:
                    self._write_status()
            except BackendError as e:
                self._on_backend_error(e)
            except Exception as e:
                self._last_error = str(e)[:200]
                log.exception("主循环异常: %s", e)
                events.emit("error", "主循环异常: %s" % str(e)[:120], level=events.ERROR)
            time.sleep(1)

    # ---------- 失败处理（D3/D4） ----------
    def _on_backend_error(self, e):
        """后端异常：计数 → 判定掉线 → 尝试自愈重建。"""
        self._backend_fail += 1
        self._last_error = str(e)[:200]
        log.error("后端错误(%d): %s", self._backend_fail, e)
        backend_health.note_fail(self.backend.name, e)
        events.emit("ai_error" if "timeout" in str(e).lower() else "error",
                    "后端错误: %s" % str(e)[:120], level=events.ERROR,
                    consecutive=self._backend_fail)

        threshold = int((self.cfg.get("guard", {}) or {}).get("reconnect_after_fail", 3))
        if self._backend_fail >= threshold and self.wechat_ready:
            self.wechat_ready = False     # D4：失败要复位，别再假装连接正常
            self._write_status()
            events.emit("wechat_lost", "微信疑似掉线（连续 %d 次失败）" % self._backend_fail,
                        level=events.ERROR)
            try:
                self._notify_control("微信连接似乎断了，我在尝试自愈，稍等一下")
            except Exception:
                pass
            # E3：先试换一条通道；没有可用备份再走原来的原地重建
            before = self.backend.name
            self._maybe_failover()
            if self.backend.name == before:
                self._try_recover()

    def _try_recover(self):
        """D3：崩溃自愈 —— 重建后端并重挂监听。"""
        events.emit("bot_start", "尝试自愈：重建后端连接", level=events.WARN)
        try:
            self.backend = create_backend(self.cfg)
            self.backend.setup(self.cfg)
            for name in self.listen | {self.control_chat}:
                try:
                    self.backend.add_listener(name)
                except Exception as e:
                    log.warning("自愈后监听 [%s] 失败: %s", name, e)
            self.wechat_ready = True
            self._backend_fail = 0
            backend_health.note_ok(self.backend.name)
            backend_health.set_current(self.backend.name)
            events.emit("wechat_back", "自愈成功，连接已恢复", level=events.SUCCESS)
            log.info("自愈成功")
        except Exception as e:
            self._last_error = str(e)[:200]
            log.error("自愈失败: %s", e)
            events.emit("error", "自愈失败: %s" % str(e)[:120], level=events.ERROR)
            time.sleep(10)

    # ---------- B8 多监听对象 ----------
    def _chat_cfg(self, chat):
        """取某个对象的独立配置；没单独配的给一份默认（enabled=True）。"""
        c = getattr(self, "chat_cfg", {}).get(chat)
        if c is None:
            c = chatcfg.ChatConfig(chat, {}, {
                "cooldown": self.cooldown, "daily_limit": self.daily_limit,
                "proactive": bool(self._proactive_cfg.get("enabled", False)),
            })
        return c

    def _cooldown_for(self, chat):
        try:
            return float(self._chat_cfg(chat).cooldown or self.cooldown)
        except (TypeError, ValueError):
            return self.cooldown

    def _prompts_for(self, chat):
        """按对象取 PromptBuilder。

        只有真的给它单独指定了人设 / 关系档案，才另建一个实例——
        大多数情况下全局那个就够，别白白多读文件。
        """
        c = self._chat_cfg(chat)
        if not c.persona_file and not c.relationship_file:
            return self.prompts
        key = "%s|%s" % (c.persona_file, c.relationship_file)
        cached = self._prompts_cache.get(chat)
        if cached and cached[0] == key:
            return cached[1]
        try:
            import copy as _copy
            p = _copy.copy(self.prompts)
            if c.persona_file:
                path = chatcfg.resolve_path(c.persona_file)
                if os.path.exists(path):
                    with open(path, "r", encoding="utf-8") as f:
                        p.persona = f.read().strip()
                else:
                    log.warning("[%s] 指定的人设文件不存在：%s", chat, path)
            if c.relationship_file:
                p.relation_path = chatcfg.resolve_path(c.relationship_file)
            self._prompts_cache[chat] = (key, p)
            return p
        except Exception as e:
            log.warning("[%s] 按对象加载人设失败，用全局的：%s", chat, str(e)[:120])
            return self.prompts

    def _sched_targets(self, job=None):
        """定时任务发给谁。

        修掉 B8 的老 bug：以前 `next(iter(self.listen))` 只取第一个，
        加了第二个对象就会发错人。现在：任务自己指定 target 就按它来，
        否则发给所有启用且允许定时任务的对象。
        """
        want = (job or {}).get("target")
        if want:
            return [want] if isinstance(want, str) else list(want or [])
        targets = [n for n, c in getattr(self, "chat_cfg", {}).items()
                   if c.enabled and c.schedule]
        if targets:
            return targets
        return [next(iter(self.listen), self.control_chat)]

    def _sched_target(self):
        """单条场景（主动发起）用：优先「启用且允许主动发起」的那个。

        兜底不能用 set —— 集合的迭代顺序不确定，一个人时没事，
        两个人的时候「主动发起找谁」就会变成抽奖。
        """
        chats = getattr(self, "chat_cfg", {}) or {}
        for n, c in chats.items():
            if c.enabled and c.proactive:
                return n
        for n, c in chats.items():
            if c.enabled:
                return n
        order = getattr(self, "listen_order", None) or sorted(self.listen)
        return order[0] if order else self.control_chat

    # ---------- E3 后端健康度与降级 ----------
    def _backend_cfg(self):
        return (self.cfg.get("backend_failover", {}) or {})

    def _backend_order(self):
        return backend_health.order_from_cfg(self.cfg, self.backend.name)

    def _swap_backend(self, name):
        """把消息通道换成 name。成功返回 True，失败时**保持原通道不动**。"""
        old = self.backend
        old_name = getattr(old, "name", "?")
        log.warning("E3：尝试把消息通道从 %s 切到 %s", old_name, name)
        events.emit("config_change", "正在切换消息通道：%s → %s" % (old_name, name),
                    level=events.WARN)
        try:
            new = None
            if name == "wechatauto":
                from backends.wechatauto_backend import WechatautoBackend
                new = WechatautoBackend()
            elif name == "wxauto":
                from backends.wxauto_backend import WxautoBackend
                new = WxautoBackend()
            elif name == "ilink":
                from backends.ilink_backend import IlinkBackend
                new = IlinkBackend()
            else:
                raise BackendError("未知后端: %s" % name)
            new.setup(self.cfg)
            for chat in self.listen | {self.control_chat}:
                new.add_listener(chat)
        except Exception as e:
            backend_health.note_fail(name, e)
            self._last_error = str(e)[:200]
            log.error("切换到 %s 失败，继续使用 %s: %s", name, old_name, e)
            events.emit("error", "切换消息通道失败，继续使用 %s" % old_name,
                        level=events.ERROR, error=str(e)[:120])
            return False

        self.backend = new
        try:
            old.close()
        except Exception as e:
            log.debug("关闭旧后端时出错（忽略）: %s", e)
        self._backend_fail = 0
        self.wechat_ready = True
        backend_health.set_current(name)
        backend_health.note_ok(name)
        events.emit("wechat_back", "消息通道已切换到 %s" % name, level=events.SUCCESS)
        self._write_status()
        return True

    def _maybe_failover(self):
        """当前通道连续失败到阈值 → 自动降级到下一个还能用的"""
        fo = self._backend_cfg()
        if not fo.get("enabled", True):
            return
        threshold = int(fo.get("fail_threshold", 3))
        if self._backend_fail < threshold:
            return
        for cand in backend_health.next_candidate(self._backend_order(),
                                                  exclude=self.backend.name):
            if self._swap_backend(cand):
                return
        log.error("所有备用通道都试过了，继续守着 %s", self.backend.name)

    def _maybe_switch_backend(self):
        """面板点「切换通道」：通过文件传话，避免面板直接操作机器人进程。"""
        want = ""
        try:
            if os.path.exists(BACKEND_SWITCH_PATH):
                data = atomicio.read_json(BACKEND_SWITCH_PATH, {}) or {}
                want = (data.get("name") or "").strip()
                try:
                    os.remove(BACKEND_SWITCH_PATH)
                except OSError:
                    pass
        except Exception as e:
            log.debug("读切换请求失败: %s", e)
            return
        if not want or want == self.backend.name:
            return
        if want not in ("wechatauto", "wxauto", "ilink"):
            events.emit("error", "不支持的通道: %s" % want, level=events.WARN)
            return
        self._swap_backend(want)

    # ---------- 消息处理 ----------
    def _handle(self, msg):
        if msg.sender == "self":
            if getattr(msg, "is_manual", False):
                who = self.cfg.get("my_name", "我") + "(真人)"
                log_content = "[图片]" if msg.image_path else (msg.content or "")
                if msg.chat != self.control_chat:
                    self._log_daily(msg.chat, who, log_content, is_ai=False)
                    self.sessions.add(msg.chat, "assistant", log_content)
                log.info("记录主人手动消息 [%s]: %s", msg.chat, log_content[:40])
            return

        if msg.chat == self.control_chat:
            self._handle_command(msg)
            return

        if msg.chat not in self.listen:
            return
        # B8：这个对象被单独关掉了就别回（后端没有「取消监听」，在这里挡）
        if not self._chat_cfg(msg.chat).enabled:
            return
        if msg.is_group and not self.reply_groups:
            return

        self._last_incoming_ts = time.time()
        self.guard.note_incoming()   # 她来了，冷场计数清零

        is_image_skip = False
        if msg.image_path:
            if not self.vision.available:
                is_image_skip = True
            else:
                desc = self.vision.describe_image(
                    msg.image_path,
                    msg.content if msg.content != "[图片]" else "")
                if desc:
                    msg.content = f"[图片: {desc}]"
                else:
                    is_image_skip = True
        elif msg.content == "[图片]":
            is_image_skip = True

        self._log_daily(msg.chat, msg.chat, msg.content)
        log.info("收到 [%s]: %s", msg.chat, msg.content[:60])

        if is_image_skip:
            log.info("图片消息识别失败/不可识别，静默跳过")
            events.emit("skip", "图片识别失败，静默跳过", level=events.WARN, chat=msg.chat)
            return

        if self.paused:
            log.info("AI 已暂停（人工接管中），跳过")
            return

        # B7：红包 / 转账 / 撤回这类只记不回——追着这些说话比不说话尴尬得多
        kind = getattr(msg, "kind", "text") or "text"
        if kind != "text" and self.media_policy.get(kind, "reply") == "log":
            log.info("[%s] 收到 %s，按策略只记录不回复", msg.chat, kind)
            events.emit("skip", "收到 %s，按策略不回复" % kind, level=events.INFO, chat=msg.chat)
            return

        # B3：情绪 / 敏感场景打标，命中就在这轮切保守模式
        tags, level = mood_mod.classify(msg.content)
        if tags:
            self._chat_mood[msg.chat] = (tags, level, time.time())
            log.info("[%s] 场景标签: %s（%s）", msg.chat, "、".join(tags), level)
            events.emit("mood", "识别到 %s：%s" % (level, "、".join(tags)),
                        level=events.WARN if level == "high" else events.INFO,
                        chat=msg.chat, tags=tags)

        now = time.time()
        gap = now - self._last_msg_ts.get(msg.chat, 0)
        self._last_msg_ts[msg.chat] = now

        self._buffer.setdefault(msg.chat, [])
        self._buffer[msg.chat].append(msg.content)
        self._buffer_ts[msg.chat] = now

        # B6：连发很快时，把缓冲时间戳往后推一点，等效于多等一轮
        if 0 < gap < self.typing_gap:
            self._buffer_ts[msg.chat] = now - self.settle_seconds + self.typing_extra_wait
            log.info("[%s] 她在连发（间隔 %.1fs），多等一会儿再接话", msg.chat, gap)
        self._last_gap[msg.chat] = gap

    def _maybe_flush(self):
        now = time.time()
        for chat in list(self._buffer.keys()):
            if now - self._buffer_ts.get(chat, 0) >= self.settle_seconds:
                self._flush(chat)

    def _flush(self, chat):
        msgs = self._buffer.get(chat, [])
        if not msgs:
            self._buffer.pop(chat, None)
            return
        if self.sessions.in_cooldown(chat, self._cooldown_for(chat)):
            log.info("[%s] 冷却中，burst 暂缓回复", chat)
            return
        self._check_daily_reset()
        if self.daily_count >= self.daily_limit:
            log.warning("已达每日回复上限 %d，丢弃缓冲", self.daily_limit)
            events.emit("guard_block", "达到每日回复上限", level=events.WARN, chat=chat)
            self._buffer.pop(chat, None)
            return

        if len(msgs) == 1:
            combined = msgs[0]
        else:
            numbered = " ".join(f"{i+1}){m}" for i, m in enumerate(msgs))
            combined = (
                f"对方刚连发了这几条: {numbered}\n"
                f"请先整体理解她现在的状态/处境/真正想表达什么（不要逐字回、不要反问已说过的事、"
                f"多条之间找联系），再给一个连贯的回应"
            )
        self.sessions.add(chat, "user", combined)
        self._buffer.pop(chat, None)

        delay = random.uniform(*self.typing_delay) if self.typing_delay else 0
        if hasattr(self.backend, "send_typing"):
            try:
                self.backend.send_typing(chat)
            except Exception:
                pass

        # B3：把这一轮的场景标签交给 PromptBuilder（10 分钟后自动失效）
        tags, level = [], None
        mt = self._chat_mood.get(chat)
        if mt:
            tags, level, mts = mt
            if time.time() - mts > self._mood_ttl:
                tags, level = [], None
                self._chat_mood.pop(chat, None)
        prompts = self._prompts_for(chat)
        prompts.mood_tags = tags
        prompts.mood_level = level

        system = prompts.system_prompt()
        context = self.sessions.context(chat)
        messages = [{"role": "system", "content": system}] + context

        t0 = time.time()
        try:
            reply = self.ai.chat(messages)
        except Exception as e:
            self._last_error = str(e)[:200]
            log.error("AI 调用失败: %s", e)
            events.emit("ai_error", "AI 调用失败: %s" % str(e)[:120],
                        level=events.ERROR, chat=chat)
            return
        latency = time.time() - t0
        self.latency_sum += latency
        self.latency_n += 1

        # ---- E1/E2：发送前过护栏 ----
        verdict, reason, usable, bubbles = self.guard.check(
            reply, is_proactive=False,
            strict=bool(self.cfg.get("guard", {}).get("strict", False)))
        if verdict == "block":
            self.blocked_count += 1
            log.warning("回复被护栏拦下：%s | 原文: %s", reason, reply[:60])
            events.emit("guard_block", "回复被拦下: %s" % reason,
                        level=events.ERROR, chat=chat, reason=reason,
                        preview=reply[:120])
            return
        if verdict == "warn":
            events.emit("guard_block", "回复有风险但已放行: %s" % reason,
                        level=events.WARN, chat=chat, preview=usable[:120])

        if not bubbles:
            log.warning("AI 回复为空，跳过")
            return

        if delay:
            time.sleep(min(delay, 8))

        sent = 0
        for i, b in enumerate(bubbles):
            if i > 0:
                time.sleep(random.uniform(0.8, 1.8))
            if self.backend.send(chat, b):
                sent += 1
                self.daily_count += 1

        if sent:
            self.guard.note_sent(sent)
            self.sessions.add(chat, "assistant", usable)
            self._log_daily(chat, self.cfg.get("my_name", "我"), usable, is_ai=True)
            self.sessions.mark_replied(chat)
            self.reply_count += 1
            log.info("回复 [%s]（%d 条气泡，覆盖 %d 条，%.1fs）: %s (今日 %d/%d)",
                     chat, sent, len(msgs), latency, usable[:60],
                     self.daily_count, self.daily_limit)
            events.emit("reply", "回复 [%s] %d 条" % (chat, sent),
                        level=events.SUCCESS, chat=chat, bubbles=sent,
                        latency=round(latency, 2),
                        preview=usable[:120], msg_id=int(time.time() * 1000))

    # ---------- 主动发起（B2） ----------
    def _proactive_loop(self):
        """她很久没说话时，基于记忆主动去关心她。

        配置：config.proactive = {enabled, idle_hours, check_minutes, max_per_day}
        """
        cfg = self._proactive_cfg
        idle_hours = float(cfg.get("idle_hours", 5))
        check_min = float(cfg.get("check_minutes", 30))
        max_per_day = int(cfg.get("max_per_day", 2))
        sent_today = {}
        time.sleep(120)   # 启动两分钟内不打扰

        while True:
            try:
                today = time.strftime("%Y-%m-%d")
                if sent_today.get("date") != today:
                    sent_today = {"date": today, "n": 0}
                idle = time.time() - (self._last_incoming_ts or time.time())
                if (not self.paused and idle >= idle_hours * 3600
                        and sent_today["n"] < max_per_day):
                    if self._proactive_once():
                        sent_today["n"] += 1
            except Exception as e:
                log.warning("主动发起线程异常: %s", e)
            time.sleep(max(60, check_min * 60))

    def _proactive_once(self):
        target = self._sched_target()
        memory_txt = (self.memory.recent_summary() or "")[-800:]
        facts = self.memory.facts(limit=12)
        now = time.localtime()
        prompt = (
            "现在的时间是 %s。她已经有一阵子没找你了。\n"
            "你记得的关于她的事实：\n%s\n"
            "最近的记忆：\n%s\n\n"
            "请以你本人的身份，主动给她发一句话开启话题。"
            "要求：①短，像随手发的微信；②要和她真实的生活/处境有关，别问'在干嘛'这种空话；"
            "③不带压力，她可以不回；④不许暴露 AI 身份；⑤只输出这一句话。"
            % (time.strftime("%H:%M"), facts or "（暂无）", memory_txt or "（暂无）")
        )
        if now.tm_hour < 7 or now.tm_hour >= 23:
            return False
        try:
            text = self.ai.chat([{"role": "system", "content": self.prompts.system_prompt()},
                                 {"role": "user", "content": prompt}])
        except Exception as e:
            log.warning("主动发起生成失败: %s", e)
            return False

        verdict, reason, usable, bubbles = self.guard.check(
            text, is_proactive=True,
            strict=bool(self.cfg.get("guard", {}).get("strict", False)))
        if verdict == "block":
            events.emit("guard_block", "主动发起被拦下: %s" % reason, level=events.WARN)
            return False
        if not bubbles:
            return False
        if self.backend.send(target, bubbles[0]):
            self.guard.note_sent(1)
            self._log_daily(target, self.cfg.get("my_name", "我"), bubbles[0], is_ai=True)
            self.daily_count += 1
            events.emit("reply", "主动发起 [%s]" % target, level=events.SUCCESS,
                        chat=target, preview=bubbles[0][:120], proactive=True)
            log.info("主动发起 [%s]: %s", target, bubbles[0][:60])
            return True
        return False

    # ---------- 日志与记忆 ----------
    def _log_daily(self, chat, who, content, is_ai=False):
        """双写：SQLite（查询主力）+ jsonl（可读备份）。"""
        today = time.strftime("%Y-%m-%d")
        try:
            store.add_message(today, chat, who, content, is_ai=is_ai)
        except Exception:
            pass
        try:
            mdaily.append(today, chat, who, content, is_ai=is_ai)
        except Exception as e:
            log.warning("写每日日志失败: %s", e)

    def _backfill_store(self):
        """首次运行：把历史 jsonl 灌进 SQLite，之后每天懒加载即可。"""
        try:
            if store.count_messages() > 0:
                return
            res = mdaily.rebuild_from_jsonl()
            if res["rows"]:
                log.info("已从历史日志回填 %d 条消息（%d 天）", res["rows"], res["days"])
                events.emit("bot_start", "历史日志回填 SQLite：%d 条" % res["rows"],
                            level=events.SUCCESS)
        except Exception as e:
            log.warning("回填历史日志失败: %s", e)

    def _on_memory_done(self, result):
        """记忆沉淀完成回调：落一条事件 + 刷新进度文件。"""
        try:
            write_progress(self.memory_worker)
        except Exception:
            pass

    def _progress_flush_loop(self):
        while True:
            try:
                write_progress(self.memory_worker)
            except Exception:
                pass
            time.sleep(15)

    # ---------- 控制命令 ----------
    def _handle_command(self, msg):
        cmd = (msg.content or "").strip()
        if cmd in ("接管", "暂停", "pause"):
            self.paused = True
            self._write_control({"paused": True})
            self._write_status()
            self._notify_control("已暂停自动回复，你亲自上吧。发'恢复'重新开启。")
            log.info("人工接管")
        elif cmd in ("恢复", "启动", "resume", "start"):
            self.paused = False
            self._write_control({"paused": False})
            self._write_status()
            self._notify_control("已恢复自动回复。")
            log.info("恢复自动")
        elif cmd in ("状态", "status"):
            self._notify_control(self._status_text())
        elif cmd in ("沉淀", "记忆", "memory"):
            self._notify_control("开始沉淀记忆，稍等…")
            threading.Thread(target=self._cmd_summarize, daemon=True).start()
        elif cmd in ("成本", "cost", "花费"):
            self._notify_control(self._cost_text())
        else:
            if cmd:
                log.info("控制会话收到未识别命令: %s", cmd[:30])

    def _cmd_summarize(self):
        try:
            r = self.memory_worker.run_now(max_days=1)
            if isinstance(r, dict) and r.get("error"):
                self._notify_control("沉淀失败/跳过：%s" % str(r.get("error"))[:60])
            else:
                d = r.get("date", "") if isinstance(r, dict) else ""
                n = len(r.get("facts", [])) if isinstance(r, dict) else 0
                self._notify_control("已沉淀 %s 的记忆（抽取 %d 条事实）" % (d, n))
        except Exception as e:
            self._notify_control("沉淀出错：%s" % str(e)[:60])

    def _status_text(self):
        today = store.usage_total(days=1)
        pending = len(summarizer.pending_dates())
        return ("状态: %s | 今日回复 %d/%d | 今日花费 %s | 待沉淀 %d 天" % (
            "暂停" if self.paused else "运行中",
            self.daily_count, self.daily_limit,
            cost.fmt_cost(today.get("cost", 0)), pending))

    def _cost_text(self):
        today = store.usage_total(days=1)
        month = store.usage_total(days=30)
        return ("今日: %d 次调用 / %s\n近30天: %d 次 / %s" % (
            today.get("calls", 0), cost.fmt_cost(today.get("cost", 0)),
            month.get("calls", 0), cost.fmt_cost(month.get("cost", 0))))

    def _notify_control(self, text):
        try:
            self.backend.send(self.control_chat, text)
        except Exception:
            pass

    # ---------- UI 控制文件 ----------
    def _read_control(self):
        return atomicio.read_json(CONTROL_PATH, {}) or {}

    def _write_control(self, data):
        ok, err = atomicio.atomic_write_json(CONTROL_PATH, data, backup=False)
        if not ok:
            log.warning("写控制文件失败: %s", err)

    def _write_status(self):
        data = {
            "pid": os.getpid(), "paused": self.paused,
            "wechat_ready": getattr(self, "wechat_ready", False),
            "ts": time.time(),
            # 3.0 补充：面板可以直接读这些，不用去猜
            "version": "3.0",
            "daily_count": self.daily_count,
            "daily_limit": self.daily_limit,
            "reply_count": self.reply_count,
            "blocked_count": self.blocked_count,
            "avg_latency": round(self.latency_sum / self.latency_n, 2) if self.latency_n else 0,
            "uptime": round(time.time() - self.start_ts, 1) if self.start_ts else 0,
            "last_error": self._last_error,
            "memory_pending": len(summarizer.pending_dates()),
            # G2：改了但还没生效、必须重启的部分（AI 模型 / 微信后端 / 视觉）
            "pending_cold": getattr(self, "_pending_cold", []),
        }
        ok, err = atomicio.atomic_write_json(BOT_STATUS_PATH, data, backup=False)
        if not ok:
            log.warning("写心跳状态失败: %s", err)

    def _status_thread(self):
        while True:
            try:
                self._write_status()
            except Exception:
                pass
            time.sleep(10)

    # ---------- 定时任务（A3：跨进程锁 + 原子写） ----------
    def _load_schedule(self):
        return atomicio.read_json(SCHEDULE_PATH, {"jobs": []}).get("jobs", []) or []

    def _save_schedule(self, jobs):
        ok, err = atomicio.atomic_write_json(SCHEDULE_PATH, {"jobs": jobs}, backup=False)
        if not ok:
            log.warning("写定时任务失败: %s", err)
        return ok

    # 注：_sched_target / _sched_targets 已在上面的「B8 多监听对象」小节重新定义，
    # 这里原来的单行版本（只取第一个监听对象）已删除——留着会把新的覆盖掉。

    def _scheduler_loop(self):
        while True:
            try:
                now = time.strftime("%H:%M")
                today = time.strftime("%Y-%m-%d")
                for job in self._load_schedule():
                    if not job.get("enabled"):
                        continue
                    if job.get("time") != now:
                        continue
                    if job.get("last_run") == today:
                        continue
                    self._run_job(job, today)
            except Exception as e:
                log.warning("定时任务检查异常: %s", e)
            time.sleep(20)

    def _run_job(self, job, today):
        action = job.get("action")
        log.info("执行定时任务 [%s] %s: %s", job.get("time"), action,
                 (job.get("text") or "")[:30])
        try:
            if action == "message":
                self._run_job_message(job, today)
            elif action == "pause":
                self.paused = True
                self._write_control({"paused": True})
                self._write_status()
                self._notify_control("已按定时任务暂停自动回复")
                events.emit("takeover", "定时任务：暂停", level=events.WARN)
            elif action == "resume":
                self.paused = False
                self._write_control({"paused": False})
                self._write_status()
                self._notify_control("已按定时任务恢复自动回复")
                events.emit("takeover", "定时任务：恢复")
        except Exception as e:
            log.warning("定时任务执行失败: %s", e)
            events.emit("error", "定时任务失败: %s" % str(e)[:120], level=events.ERROR)
        self._mark_job_ran(job, today)

    def _run_job_message(self, job, today):
        # B8：以前只发给第一个监听对象，加了第二个就会发错人
        targets = self._sched_targets(job)
        # 定时任务属于「主动发言」：命中免打扰时段就不发，避免半夜三点发早安
        ok, reason = self.guard.can_send(is_proactive=True)
        if not ok:
            log.warning("定时任务被护栏拦下: %s", reason)
            events.emit("guard_block", "定时任务被拦下: %s" % reason,
                        level=events.WARN, job=job.get("id"))
            return
        for target in targets:
            self._send_job_to(job, target)

    def _send_job_to(self, job, target):
        text = self._gen_timed_text(job, target)
        if not text:
            return
        verdict, why, usable, bubbles = self.guard.check(text, is_proactive=True)
        if verdict == "block":
            events.emit("guard_block", "定时文案被拦下: %s" % why, level=events.WARN)
            return
        if bubbles and self.backend.send(target, bubbles[0]):
            self.guard.note_sent(1)
            self._log_daily(target, self.cfg.get("my_name", "我") + "(定时)",
                            bubbles[0], is_ai=True)
            self.daily_count += 1
            log.info("定时消息已发送 [%s]: %s", target, bubbles[0][:40])
            events.emit("schedule_run", "定时消息已发 [%s]" % target,
                        level=events.SUCCESS, preview=bubbles[0][:120])
            # 主动发了之后别硬聊：等她回应，没回应就收手
            threading.Timer(600, self._check_dry).start()

    def _check_dry(self):
        """发出去的主动/定时消息 10 分钟内没被接话 → 冷场计数 +1。"""
        if time.time() - (self._last_incoming_ts or 0) > 600:
            self.guard.note_dry()
            self.guard.note_dry()
            log.info("主动发言后无人回应，冷场计数 %d", self.guard.dry_streak)
            events.emit("skip", "主动发言无人回应，冷场 +1", level=events.WARN,
                        streak=self.guard.dry_streak)

    def _gen_timed_text(self, job, chat=None):
        """定时文案支持模板：{生成的早安}，或直接发静态 text。"""
        text = (job.get("text") or "").strip()
        if "{生成" not in text:
            return text
        facts = self.memory.facts(limit=10)
        prompt = (
            "请以本人身份写一条%s给对象。要求：①很短，一两句；"
            "②结合你记得的关于她的事实，让她觉得你惦记着她；③自然、不肉麻、不用句号；"
            "④只输出这句话。"
            % ("早安问候" if "早安" in text else ("晚安问候" if "晚安" in text else "问候"))
        )
        if facts:
            prompt += "\n你知道的关于她：\n%s" % facts
        try:
            # B8：按对象取人设，多对象时早安才不会串味
            p = self._prompts_for(chat) if chat else self.prompts
            return self.ai.chat([{"role": "system", "content": p.system_prompt()},
                                 {"role": "user", "content": prompt}])
        except Exception as e:
            log.warning("定时文案生成失败，退回静态文本: %s", e)
            return re_static_tags(text)

    def _mark_job_ran(self, job, today):
        """A3：整个读-改-写过程放在锁里，并用原子写落地。"""
        try:
            with FileLock("schedule", timeout=6.0):
                jobs = self._load_schedule()
                for j in jobs:
                    if j.get("id") == job.get("id"):
                        j["last_run"] = today
                self._save_schedule(jobs)
        except Exception as e:
            log.warning("标记定时任务失败: %s", e)

    def _check_daily_reset(self):
        today = time.strftime("%Y-%m-%d")
        if today != self.daily_date:
            self.daily_date = today
            self.daily_count = 0
            # A1：这里不再直接调用 AI 摘要（那是 worker 的活儿），
            # 避免把一次可能几十秒的 AI 调用塞进回复链路里
            self.memory_worker.wake()


def re_static_tags(text):
    """把 {生成的早安} 之类的模板标记剥掉，返回可发的静态文本。"""
    import re as _re
    t = _re.sub(r"\{生成[^}]*\}", "", text or "").strip()
    return t or "早安呀"


if __name__ == "__main__":
    try:
        Bot().start()
    except KeyboardInterrupt:
        log.info("收到停止信号，退出。")
        events.emit("bot_stop", "收到停止信号退出", level=events.WARN)
    except Exception as e:
        log.exception("机器人异常退出: %s", e)
        events.emit("bot_stop", "异常退出: %s" % str(e)[:160], level=events.ERROR)
