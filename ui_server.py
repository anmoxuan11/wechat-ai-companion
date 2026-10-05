# -*- coding: utf-8 -*-
"""
微信 AI 陪聊机器人 · 本地控制面板后端

纯标准库实现（http.server），无需 pip 安装任何依赖。
启动：python ui_server.py  →  浏览器打开 http://127.0.0.1:8765

功能：
  - 开启 / 关闭 / 重启 机器人进程
  - 暂停 / 继续（实时写 bot_control.json，bot 每轮轮询读取，无需重启）
  - 切换监听对象（改写 config.json 的 listen 后重启）
  - 实时状态（运行中 / 暂停 / 监听对象 / 运行时长 / 微信连接状态）
  - 实时日志（读取 bot.log 末尾）
  - 清空成长记忆（清空 data/daily 与 data/memory.md）

注意：机器人进程由本服务以子进程方式拉起，进程信息记在 bot.pid；
即使关闭本控制面板，机器人仍会在后台继续运行（除非点「关闭」）。
"""

import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

# ---- 2.0：引入核心公共模块（路径解析/原子写/进程查询/安全/单实例）----
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
if getattr(sys, "frozen", False):
    sys.path.insert(0, os.path.dirname(os.path.abspath(sys.executable)))

from core import (  # noqa: E402
    alive as _alive2,
    atomic_write_json as _awjson,
    atomic_write_text as _awtext,
    cleanup_temp_ps1 as _cleanup_ps1,
    find_python as _find_python,
    kill_tree as _kill_tree2,
    port_in_use as _port_in_use2,
    prune_dir as _prune_dir,
    read_json as _rjson,
    read_text as _rtext,
    resolve_conflict as _resolve_conflict,
    rotate_if_needed as _rotate,
    tail_text as _tail_text,
    proc_tree_mem_mb as _ptree_mem,
    proc_start_time as _pstart,
    write_lock as _write_lock,
    remove_lock as _remove_lock,
)
from core.security import (  # noqa: E402
    TokenGuard, TOKEN_HEADER, PROTECTED_POST, DESTRUCTIVE,
    require_confirm, resolve_api_key, ENV_API_KEY, audit_config_secrets,
)

try:  # 3.0：扩展 API（事件线/成本/记忆/预演/护栏/备份）。缺失不影响老功能
    from web import api_extra as _apiextra
except Exception:  # pragma: no cover
    _apiextra = None

# 3.0 新增的写端点：与 core.security.PROTECTED_POST 同等对待。
# 单独维护一份，避免在 core 层写死面板路由（职责不同）。
EXTRA_PROTECTED_POST = frozenset({
    "/api/preview", "/api/feedback", "/api/tone_dials", "/api/memory/run",
    "/api/fact/delete", "/api/fact/add", "/api/guard/save", "/api/guard/test",
    "/api/backup/create", "/api/backup/restore", "/api/events/clear",
    "/api/proposals/batch", "/api/proposals/dedupe", "/api/proposals/archive_stale",
    "/api/persona/redistill", "/api/persona/apply",
    "/api/reminders/refresh", "/api/reminders/add", "/api/reminders/delete",
    "/api/backend/switch",
    "/api/chats/update", "/api/chats/add",
})

# 3.0 里同样「撤不回来」的操作：改之前必须显式带 confirm=true
EXTRA_DESTRUCTIVE_POST = frozenset({
    "/api/fact/delete", "/api/events/clear", "/api/backup/restore",
    "/api/proposals/batch", "/api/proposals/dedupe", "/api/proposals/archive_stale",
    "/api/persona/apply", "/api/reminders/delete",
})

def _resolve_base_dir():
    """项目目录解析（2.0）。

    三种运行形态：
      1. 源码运行         → 本文件所在目录
      2. 打包成 exe       → exe 所在目录
      3. exe 拉起本脚本   → 脚本在 <exe目录>/_internal 下，真实项目目录由
                            启动器通过环境变量 WECHAT_BOT_DIR 明确指定

    第 3 种最关键：不处理的话面板会把 _internal 当成项目目录，
    于是读不到 config.json、数据和日志也全写错地方（1.0 就有这个隐患）。
    """
    env_dir = os.environ.get("WECHAT_BOT_DIR", "").strip().strip('"')
    if env_dir and os.path.isdir(env_dir):
        return os.path.abspath(env_dir)

    here = os.path.dirname(os.path.abspath(__file__))
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))

    # 脚本被放在 _internal 里（PyInstaller onedir）：向上找到真正的项目目录
    if os.path.basename(here).lower() == "_internal":
        cur = os.path.dirname(here)
        for _ in range(3):
            if os.path.isfile(os.path.join(cur, "config.json")):
                return cur
            cur = os.path.dirname(cur)
    return here


VERSION = "3.0"

BASE_DIR = _resolve_base_dir()
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
CONTROL_PATH = os.path.join(BASE_DIR, "bot_control.json")
PID_PATH = os.path.join(BASE_DIR, "bot.pid")
BOT_STATUS_PATH = os.path.join(BASE_DIR, "data", "bot_status.json")
LOG_PATH = os.path.join(BASE_DIR, "bot.log")
CONSOLE_LOG_PATH = os.path.join(BASE_DIR, "bot_console.log")
DAILY_DIR = os.path.join(BASE_DIR, "data", "daily")
MEMORY_MD = os.path.join(BASE_DIR, "data", "memory.md")
PERSONA_PATH = os.path.join(BASE_DIR, "data", "persona.md")
PROFILE_PATH = os.path.join(BASE_DIR, "data", "style_profile.json")
RELATIONSHIP_PATH = os.path.join(BASE_DIR, "data", "relationship.md")
SCHEDULE_PATH = os.path.join(BASE_DIR, "data", "schedule.json")

# 2.0：不再硬编码机器专属路径（1.0 这里是写死的 C:\Users\you\...）
# 每次解析，保证"换目录/换电脑/拷 U 盘"都能找到解释器
def _resolve_python():
    py, _src = _find_python()
    return py or sys.executable


VENV_PYTHON = _resolve_python()
MAIN_SCRIPT = os.path.join(BASE_DIR, "main.py")

PORT = 8765
_NO_WINDOW = 0x08000000  # CREATE_NO_WINDOW：子进程不弹黑框（exe 无控制台时尤其关键）
_ui_start_ts = 0.0
_lock = threading.Lock()


# ---------- 进程管理 ----------
def _proc_tree_mem_mb(root_pid):
    """机器人是 venv python.exe 重定向器（小进程）+ 真解释器子进程：
    把根进程和它的直接子进程内存合计，才是真实占用"""
    if not root_pid:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        TH32CS_SNAPPROCESS = 0x00000002
        snapshot = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snapshot or snapshot == 2 ** 64 - 1:
            return None
        children = []
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while ok:
            if entry.th32ParentProcessID == root_pid:
                children.append(entry.th32ProcessID)
            ok = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
        kernel32.CloseHandle(snapshot)
        total = (_proc_mem_mb(root_pid) or 0)
        for c in children:
            total += (_proc_mem_mb(c) or 0)
        return round(total, 1) if total else None
    except Exception:
        return None


def _alive(pid):
    """判断进程是否存活：枚举进程表快照（Toolhelp32），与任务管理器一致。

    不能只用 OpenProcess：对已终止/僵尸态的 PID 它可能仍返回句柄造成误判
    （曾导致"杀了 bot 却以为没杀掉→重启失败"）。快照枚举是权威的。
    """
    if not pid:
        return False
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

        class PROCESSENTRY32W(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(wintypes.ULONG)),
                ("th32ModuleID", wintypes.DWORD),
                ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260),
            ]

        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        TH32CS_SNAPPROCESS = 0x00000002
        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == 2 ** 64 - 1:
            return False
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            if entry.th32ProcessID == pid:
                kernel32.CloseHandle(snap)
                return True
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        kernel32.CloseHandle(snap)
        return False
    except Exception:
        return False


def _proc_mem_mb(pid):
    """读取进程内存占用（MB）——纯 ctypes 调 psapi，瞬时、不弹窗"""
    if not pid:
        return None
    try:
        import ctypes
        from ctypes import wintypes
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi")
        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        class PMC(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]

        h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            return None
        try:
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            if psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), ctypes.sizeof(PMC)):
                return round(pmc.WorkingSetSize / 1024.0 / 1024.0, 1)
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        pass
    return None


def _run_ps(script):
    """2.0：已废弃，保留空实现仅为兼容旧调用点。

    原实现会生成临时 .ps1 再调 powershell（冷启动 300~800ms，且临时文件
    在异常路径下会残留——1.0 项目目录里累积了 80+ 个 tmp*.ps1）。
    现在全部改用 core.proclog 的纯 ctypes 实现：
      - _find_bot_pid()  → 扫描 bot.pid / 心跳文件（不再扫进程命令行）
      - uptime_str()     → GetProcessTimes（不再 Get-CimInstance）
    """
    return ""


def _find_bot_pid():
    """2.0：不再调 PowerShell 扫进程命令行（慢且脆）。

    改为从 bot 自己的心跳文件里读真实 PID——bot 每轮都会写
    data/bot_status.json，里面有它自己的 os.getpid()。
    拿不到再退回 bot.pid 文件。两者都没有就是没在跑。
    """
    st = _bot_status()
    pid = st.get("pid")
    if pid and _alive2(pid):
        return pid
    try:
        pid = int(_rtext(PID_PATH).strip())
        if _alive2(pid):
            return pid
    except (ValueError, AttributeError, OSError):
        pass
    return None


def _bot_status():
    """读取 bot 自报的心跳状态（真实 PID + 暂停状态 + 时间戳）"""
    try:
        with open(BOT_STATUS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def bot_pid():
    """优先用 bot 自报的心跳（新鲜+进程表确认）；心跳失效才扫描兜底。

    不再盲信 pidfile：venv python.exe 是重定向器，pidfile 里的外壳 PID 可能已死
    或已被复用，OpenProcess 还会误报存活——只有心跳（bot 亲自上报 os.getpid()）
    或进程表扫描才可信。
    """
    st = _bot_status()
    pid = st.get("pid")
    # 心跳 120 秒内 + 进程表确认 → 真实运行（bot 有独立心跳线程，长任务也不会超时）
    if pid and time.time() - st.get("ts", 0) < 120 and _alive(pid):
        return pid
    # 兜底扫描（不写 pidfile：扫描可能命中外壳进程，写进去反而误导）
    d = _find_bot_pid()
    if d and _alive(d):
        return d
    return None


def is_running():
    return bot_pid() is not None


def _write_pid(pid):
    try:
        with open(PID_PATH, "w", encoding="utf-8") as f:
            f.write(str(pid))
    except Exception:
        pass


def _wait_status_fresh(timeout, expect_paused=None):
    """等待 bot 心跳新鲜（bot 启动完成后会写 bot_status.json）"""
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = _bot_status()
        if st.get("pid") and time.time() - st.get("ts", 0) < 15:
            if expect_paused is None or st.get("paused") == expect_paused:
                return st
        time.sleep(1)
    return None


def start_bot():
    with _lock:
        if is_running():
            return {"ok": True, "running": True, "pid": bot_pid(), "msg": "机器人已在运行"}
        # 清掉旧心跳/旧 pid，保证 _wait_status_fresh 只认新 bot 的
        try:
            if os.path.exists(BOT_STATUS_PATH):
                os.remove(BOT_STATUS_PATH)
            if os.path.exists(PID_PATH):
                os.remove(PID_PATH)
        except Exception:
            pass
        # 重定向子进程输出到 bot_console.log，同时 main.py 自己写 bot.log
        try:
            f = open(CONSOLE_LOG_PATH, "ab", buffering=0)
        except Exception:
            f = open(os.devnull, "wb")
        try:
            cmd = [VENV_PYTHON, MAIN_SCRIPT]
            proc = subprocess.Popen(
                cmd, cwd=BASE_DIR, stdout=f, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                creationflags=0x08000000,  # CREATE_NO_WINDOW：不弹黑框
            )
        finally:
            f.close()  # 释放句柄，避免泄漏
        global _ui_start_ts
        _ui_start_ts = time.time()
        # 等 bot 自报真实 PID（最多 30 秒；微信库初始化较慢）
        st = _wait_status_fresh(30)
        if st:
            real_pid = st.get("pid")
            _write_pid(real_pid)
            return {"ok": True, "running": True, "pid": real_pid, "msg": "已开启"}
        # 启动失败：尝试清理刚拉起的进程
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                           creationflags=_NO_WINDOW)
        except Exception:
            pass
        # 一次性瞬时锁/竞态自愈：清状态后重试一轮
        _logf("首次启动未就绪，5 秒后重试一次")
        time.sleep(5)
        try:
            if os.path.exists(BOT_STATUS_PATH):
                os.remove(BOT_STATUS_PATH)
        except Exception:
            pass
        try:
            f2 = open(CONSOLE_LOG_PATH, "ab", buffering=0)
        except Exception:
            f2 = open(os.devnull, "wb")
        try:
            proc2 = subprocess.Popen(
                [VENV_PYTHON, MAIN_SCRIPT], cwd=BASE_DIR, stdout=f2, stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL, creationflags=0x08000000,
            )
        finally:
            f2.close()
        st2 = _wait_status_fresh(30)
        if st2:
            real_pid = st2.get("pid")
            _write_pid(real_pid)
            return {"ok": True, "running": True, "pid": real_pid, "msg": "已开启（重试成功）"}
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc2.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                           creationflags=_NO_WINDOW)
        except Exception:
            pass
        return {"ok": False, "running": False, "msg": "启动后心跳未就绪，请查看 bot_console.log"}


def _kill_pid(pid):
    """taskkill /T 连树杀；仍活着则 PowerShell 兜底。返回是否已死"""
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                       creationflags=_NO_WINDOW)
    except Exception:
        pass
    # 等死透（最多 10 秒）
    for _ in range(10):
        if not _alive(pid):
            return True
        time.sleep(1)
    # 2.0 兜底：taskkill /T 杀整棵树，再逐个杀子进程（不再调 PowerShell）
    return _kill_tree2(pid)


def stop_bot():
    with _lock:
        pid = bot_pid()
        if not pid:
            # 心跳与 pidfile 都没有：确认是否残留真解释器（扫描兜底）
            d = _find_bot_pid()
            if d and _alive(d):
                pid = d
            else:
                return {"ok": True, "running": False, "msg": "机器人未在运行"}
        dead = _kill_pid(pid)
        try:
            if os.path.exists(PID_PATH):
                os.remove(PID_PATH)
        except Exception:
            pass
        if not dead:
            return {"ok": False, "running": True, "pid": pid, "msg": "关闭失败，进程仍存活"}
        return {"ok": True, "running": False, "msg": "已关闭"}


def restart_bot():
    with _lock:
        pid = bot_pid()
        if pid:
            dead = _kill_pid(pid)
            try:
                if os.path.exists(PID_PATH):
                    os.remove(PID_PATH)
            except Exception:
                pass
            if not dead:
                return {"ok": False, "running": True, "pid": pid, "msg": "重启失败：旧进程未杀死"}
        time.sleep(1)
        # 清掉旧心跳/旧 pid，保证 _wait_status_fresh 只认新 bot 的
        try:
            if os.path.exists(BOT_STATUS_PATH):
                os.remove(BOT_STATUS_PATH)
            if os.path.exists(PID_PATH):
                os.remove(PID_PATH)
        except Exception:
            pass
        # 重新启动
        try:
            f = open(CONSOLE_LOG_PATH, "ab", buffering=0)
        except Exception:
            f = open(os.devnull, "wb")
        proc = subprocess.Popen(
            [VENV_PYTHON, MAIN_SCRIPT], cwd=BASE_DIR, stdout=f, stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL, creationflags=0x08000000,
        )
        global _ui_start_ts
        _ui_start_ts = time.time()
        st = _wait_status_fresh(30)
        if st:
            real_pid = st.get("pid")
            _write_pid(real_pid)
            return {"ok": True, "running": True, "pid": real_pid, "msg": "已重启"}
        try:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                           creationflags=_NO_WINDOW)
        except Exception:
            pass
        return {"ok": False, "running": False, "msg": "重启后心跳未就绪，请查看 bot_console.log"}


# ---------- 控制文件 / 配置 ----------
def read_control():
    try:
        with open(CONTROL_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def set_paused(val):
    """2.0：原子写。控制文件只是个 paused 开关，不做备份（避免 .bak 噪音）。"""
    ctrl = read_control()
    ctrl["paused"] = bool(val)
    ok, err = _awjson(CONTROL_PATH, ctrl, backup=False)
    if not ok:
        _logf("set_paused 原子写失败: %r (%s)" % (err, CONTROL_PATH))
    return ok


def _set_paused_verified(val):
    """暂停/继续：写入后等 bot 心跳确认生效，返回 verified 结果"""
    ok = set_paused(val)
    if not ok:
        return {"ok": False, "paused": bool(val), "msg": "写控制文件失败", "verified": False}
    # 等 bot 心跳同步（最多 6 秒）
    deadline = time.time() + 6
    verified = False
    while time.time() < deadline:
        st = _bot_status()
        if st.get("pid") and st.get("paused") == bool(val):
            verified = True
            break
        time.sleep(0.5)
    msg = ("已暂停，bot 已确认生效" if val else "已继续，bot 已确认生效") if verified else \
          ("已写入，但 bot 未确认（未运行？）" if not _bot_status().get("pid") else "已写入，但 bot 心跳尚未同步")
    return {"ok": True, "paused": bool(val), "msg": msg, "verified": verified}


def read_config():
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def set_listen(target):
    cfg = read_config()
    cfg["listen"] = [target]
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except Exception:
        return False


def clear_memory():
    removed = []
    # 清空每日对话日志
    if os.path.isdir(DAILY_DIR):
        for fn in os.listdir(DAILY_DIR):
            if fn.endswith(".jsonl"):
                try:
                    os.remove(os.path.join(DAILY_DIR, fn))
                    removed.append(fn)
                except Exception:
                    pass
    # 清空成长记忆
    if os.path.exists(MEMORY_MD):
        try:
            os.remove(MEMORY_MD)
            removed.append("memory.md")
        except Exception:
            pass
    return removed


# ---------- 人设 & 记忆面板 ----------
def _read_text(p):
    try:
        with open(p, "r", encoding="utf-8") as f:
            return f.read()
    except Exception:
        return ""


def _write_text(p, text):
    """2.0：原子写（临时文件 → os.replace）+ 自动备份最近 5 份。

    1.0 是直接 open(p,"w") 覆盖：
      - 写到一半被杀/断电 → 文件半截，人设卡直接损坏
      - 覆盖即永久丢失，没有回溯可能
    人设卡/关系背景/成长记忆是不可再生资产（靠蒸馏聊天记录生成），
    所以这里强制备份。
    """
    ok, err = _awtext(p, text)
    if not ok:
        _logf("原子写失败: %r | path=%s" % (err, p))
    return ok


def get_panel():
    """人设卡 + 关系背景 + 成长记忆 + 风格统计 + 完整 system prompt 预览"""
    persona = _read_text(PERSONA_PATH)
    memory = _read_text(MEMORY_MD)
    relationship = _read_text(RELATIONSHIP_PATH)
    profile = {}
    try:
        with open(PROFILE_PATH, "r", encoding="utf-8") as f:
            profile = json.load(f)
    except Exception:
        pass
    # 与 bot 完全一致的 system prompt（复用 session.py 的 PromptBuilder）
    preview = ""
    try:
        from session import PromptBuilder, MemoryManager
        cfg = read_config()
        mm = MemoryManager(MEMORY_MD, keep_days=3)
        pb = PromptBuilder(cfg, PROFILE_PATH, memory_manager=mm)
        preview = pb.system_prompt()
    except Exception as e:
        preview = "（预览失败: %s）" % e
    profile_stats = {
        "avg_length": profile.get("avg_message_length"),
        "catchphrases": profile.get("catchphrases", [])[:8],
        "emojis": profile.get("top_emojis", [])[:6],
        "fewshot_pairs": len(profile.get("fewshot_pairs", [])),
        "total_messages": profile.get("total_messages_from_me"),
        "generated_at": profile.get("generated_at"),
    }
    return {
        "persona": persona,
        "memory": memory,
        "relationship": relationship,
        "profile": profile_stats,
        "prompt_preview": preview,
    }


def get_daily(date=None):
    """格式化某天的聊天记录：'HH:MM who: content'"""
    date = date or time.strftime("%Y-%m-%d")
    p = os.path.join(DAILY_DIR, date + ".jsonl")
    out = []
    try:
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    hhmm = time.strftime("%H:%M", time.localtime(d.get("ts", 0)))
                    who = d.get("who", "?")
                    if d.get("is_ai"):
                        who = who + "（AI代发）"
                    out.append("%s %s: %s" % (hhmm, who, d.get("content", "")))
                except Exception:
                    out.append(line)
    except Exception:
        pass
    return "\n".join(out[-200:])


# ---------- 自检 ----------
def self_test():
    """一键自检：逐项验证控制链路是否真的有效（面板 <-> bot <-> 微信）。

    每项附 fix 字段：异常时可执行的一键修复动作标识（None=无需/不可修复）。
    动作由 /api/fix 分发执行。
    """
    items = []
    running = is_running()
    st = _bot_status()
    pid = bot_pid()

    # 1. 进程与心跳
    if running:
        age = int(time.time() - st.get("ts", 0)) if st.get("pid") else -1
        items.append({"name": "bot 进程存活", "ok": True,
                      "detail": "PID %s（心跳 %s 秒前）" % (pid, age), "fix": None})
    else:
        items.append({"name": "bot 进程存活", "ok": False, "detail": "未检测到运行中的 bot",
                      "fix": "start"})

    # 2. 心跳与 PID 一致性
    if st.get("pid"):
        ok = bool(pid) and pid == st.get("pid")
        items.append({"name": "PID 一致性（心跳/检测）", "ok": ok,
                      "detail": ("心跳 %s = 检测 %s" % (st.get("pid"), pid)) if ok
                      else ("心跳 %s ≠ 检测 %s" % (st.get("pid"), pid)),
                      "fix": None if ok else "restart"})
    else:
        items.append({"name": "PID 一致性（心跳/检测）", "ok": not running,
                      "detail": "无心跳文件" if not running else "bot 在跑但无心跳（初始化期？）",
                      "fix": None if not running else "restart"})

    # 3. 控制文件读写往返
    try:
        before = read_control().get("paused", False)
        set_paused(before)  # 写回同值，验证可写
        after = read_control().get("paused", False)
        ok = before == after
        items.append({"name": "控制文件读写", "ok": ok,
                      "detail": "读→写→读 往返一致（paused=%s）" % before,
                      "fix": None if ok else "rebuild_control"})
    except Exception as e:
        items.append({"name": "控制文件读写", "ok": False, "detail": "异常: %r" % e,
                      "fix": "rebuild_control"})

    # 4. 配置可读
    cfg = read_config()
    items.append({"name": "config.json", "ok": bool(cfg),
                  "detail": "监听: %s" % "、".join(cfg.get("listen", [])) if cfg else "读取失败",
                  "fix": None if cfg else "rebuild_config"})

    # 5. 日志心跳（bot 活跃度：bot 自报心跳优先，日志 mtime 仅作参考）
    #    bot 暂停/空闲时本就不写日志，只看日志 mtime 会误报——以 bot 心跳为准。
    heartbeat_age = int(time.time() - st.get("ts", 0)) if st.get("ts") else -1
    try:
        log_age = int(time.time() - os.path.getmtime(CONSOLE_LOG_PATH))
    except Exception:
        log_age = -1
    if running and 0 <= heartbeat_age < 120:
        items.append({"name": "日志心跳", "ok": True,
                      "detail": "bot 心跳 %d 秒前（日志 %s）" % (
                          heartbeat_age, ("%d 秒前" % log_age) if log_age >= 0 else "无"),
                      "fix": None})
    else:
        ok = (0 <= log_age < 180)
        items.append({"name": "日志心跳", "ok": ok,
                      "detail": ("日志 %d 秒前有更新（bot 心跳未更新，可能卡死）" % log_age) if ok
                      else ("日志已 %d 秒未更新" % log_age if log_age >= 0 else "无法读取日志"),
                      "fix": None if ok else "restart"})

    # 6. 微信连接（从日志尾部推断）
    wc = wechat_status() if running else "未运行"
    wc_ok = (wc == "已连接")
    items.append({"name": "微信连接", "ok": wc_ok, "detail": wc,
                  "fix": None if wc_ok else ("start" if not running else "restart")})

    # 7. 暂停状态一致性（bot 心跳 vs 控制文件）
    ctrl_paused = bool(read_control().get("paused", False))
    bot_paused = bool(st.get("paused", False)) if st.get("pid") else None
    if bot_paused is None:
        items.append({"name": "暂停状态同步", "ok": not running, "detail": "bot 未运行，无同步对象",
                      "fix": None})
    else:
        ok = ctrl_paused == bot_paused
        items.append({"name": "暂停状态同步", "ok": ok,
                      "detail": "控制文件=%s 心跳=%s" % (ctrl_paused, bot_paused),
                      "fix": None if ok else "sync_pause"})

    passed = sum(1 for i in items if i["ok"])
    return {"ok": passed == len(items), "passed": passed, "total": len(items), "items": items}


def fix_issue(action):
    """一键修复：分发到具体修复动作。返回 {ok, msg}（附 running/pid 便于前端刷新）。"""
    if action == "start":
        r = start_bot()
        return {"ok": bool(r.get("ok")), "msg": r.get("msg", "已开启"),
                "running": r.get("running"), "pid": r.get("pid")}
    if action == "restart":
        r = restart_bot()
        return {"ok": bool(r.get("ok")), "msg": r.get("msg", "已重启"),
                "running": r.get("running"), "pid": r.get("pid")}
    if action == "rebuild_control":
        # 2.0：统一走原子写（先写临时文件再替换），不再手写重试循环
        ok, err = _awjson(CONTROL_PATH, {"paused": False})
        return ({"ok": True, "msg": "已重建控制文件（paused=false），下次回复生效"}
                if ok else {"ok": False, "msg": "重建控制文件失败: %r" % (err,)})
    if action == "rebuild_config":
        if read_config():
            return {"ok": True, "msg": "config.json 可读，无需重建"}
        default = {"listen": ["伴侣示例"], "control_chat": "文件传输助手",
                   "partner_name": "", "my_name": "我", "ai": {}}
        ok, err = _awjson(CONFIG_PATH, default)
        return ({"ok": True,
                 "msg": "已重建最小 config.json（listen=伴侣示例），请核对后重启"}
                if ok else {"ok": False, "msg": "重建 config 失败: %r" % (err,)})
    if action == "sync_pause":
        ctrl_paused = bool(read_control().get("paused", False))
        ok = set_paused(ctrl_paused)
        return {"ok": ok, "msg": ("已重写控制文件 paused=%s，bot 下轮同步" % ctrl_paused) if ok
                else "重写控制文件失败"}
    return {"ok": False, "msg": "未知修复动作: %s" % action}


# ---------- AI 参谋 & 提案中心（自进化） ----------
PROPOSALS_PATH = os.path.join(BASE_DIR, "data", "proposals.json")
_advisor_history = []              # 参谋对话上下文（内存，重启即清）
_ADVISOR_MAX_HISTORY = 20
_proposals_lock = threading.Lock()


def _load_proposals():
    try:
        with open(PROPOSALS_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("proposals", [])
    except Exception:
        return []


def _save_proposals(proposals):
    try:
        os.makedirs(os.path.dirname(PROPOSALS_PATH), exist_ok=True)
        with open(PROPOSALS_PATH, "w", encoding="utf-8") as f:
            json.dump({"proposals": proposals}, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def _new_proposal(ptype, content, reason, source):
    return {
        "id": "p%d" % int(time.time() * 1000),
        "type": ptype,
        "content": content,
        "reason": reason,
        "source": source,
        "kind": "suggestion",   # 问=question（需主人回答）| 议=suggestion（AI 建议）
        "status": "pending",
        "created_at": time.time(),
    }


_PLACEHOLDER_MARKS = ("待确认", "待补充", "待定", "待她", "待我", "未定", "TBD")


def _is_placeholder(content):
    """占位提案（'她的生日是（待确认）'这类）服务端直接丢弃，防写垃圾进档案"""
    return any(m in content for m in _PLACEHOLDER_MARKS)


def _parse_proposal_line(line):
    """解析一行提案：支持新格式 【提案】类别|文件类型|内容|原因 与旧格式 【提案】类型|内容|原因

    类别：问（question，需主人补充信息）或 议（suggestion，AI 的确定建议）。
    旧格式自动推断：内容含问号视为问询类。
    返回 {kind,type,content,reason} 或 None。
    """
    line = line.replace("【提案】", "", 1).strip()
    parts = [p.strip() for p in line.split("|")]
    if len(parts) < 2:
        return None
    kind_raw, second = parts[0], parts[1]
    if kind_raw in ("question", "suggestion", "问", "议", "q", "s"):
        ptype = second
        content = parts[2].strip() if len(parts) > 2 else ""
        reason = "|".join(parts[3:]).strip() if len(parts) > 3 else ""
        kind = "question" if kind_raw in ("question", "问", "q") else "suggestion"
    else:
        ptype = kind_raw
        content = second
        reason = "|".join(parts[2:]).strip() if len(parts) > 2 else ""
        kind = "question" if ("？" in content or "?" in content) else "suggestion"
    if ptype not in ("relationship", "persona", "memory", "advice") or not content:
        return None
    # 问询类必须像问题；不像问题的问询降级丢弃
    if kind == "question" and not any(m in content for m in ("？", "?")):
        return None
    return {"kind": kind, "type": ptype, "content": content, "reason": reason}


def _add_proposals(items):
    """items: [{kind,type,content,reason,source}] → 追加到提案库（按 type+content 去重 pending）

    占位过滤只作用于提议类；问询类本身就是要人回答的问题，不过滤。
    """
    with _proposals_lock:
        proposals = _load_proposals()
        existing = {(p.get("type"), p.get("content")) for p in proposals if p.get("status") == "pending"}
        added = []
        for it in items:
            key = (it.get("type", ""), it.get("content", ""))
            kind = it.get("kind", "suggestion")
            if key in existing or not key[1]:
                continue
            if kind != "question" and _is_placeholder(it.get("content", "")):
                continue
            p = _new_proposal(it["type"], it["content"], it.get("reason", ""), it.get("source", "advisor"))
            p["kind"] = kind
            proposals.append(p)
            existing.add(key)
            added.append(p)
        _save_proposals(proposals)
        return added


def _apply_proposal(p):
    """批准：按类型把内容追加到对应档案文件"""
    content = (p.get("content") or "").strip()
    if not content:
        return False
    target = {
        "relationship": RELATIONSHIP_PATH,
        "persona": PERSONA_PATH,
        "memory": MEMORY_MD,
    }.get(p.get("type"))
    if target:
        base = _read_text(target).rstrip()
        if content in base:
            return True  # 内容已存在，视为完成
        return _write_text(target, (base + "\n" + content + "\n") if base else content + "\n")
    return True  # advice 类型不需要落盘


def _proposal_act(pid, status, edited_content=None):
    with _proposals_lock:
        proposals = _load_proposals()
        found = None
        for p in proposals:
            if p.get("id") == pid and p.get("status") == "pending":
                found = p
                break
        if not found:
            return {"ok": False, "msg": "提案不存在或已处理"}
        if status == "approved":
            if edited_content is not None:
                found["content"] = (edited_content or "").strip()
            if not found.get("content"):
                return {"ok": False, "msg": "内容为空，未写入"}
            # 问询类不落盘（它只是要主人回答的问题）；提议类才写入档案
            if found.get("kind") != "question":
                if not _apply_proposal(found):
                    return {"ok": False, "msg": "写入档案失败"}
        found["status"] = status
        _save_proposals(proposals)
        if status == "approved":
            return {"ok": True, "msg": "已批准并写入档案（立即生效）" if found.get("kind") != "question" else "已标记解决"}
        if status == "resolved":
            return {"ok": True, "msg": "已标记解决"}
        return {"ok": True, "msg": "已拒绝"}


def get_proposals():
    props = _load_proposals()
    props.sort(key=lambda p: (p.get("status") != "pending", -p.get("created_at", 0)))
    return props


# ---------------------------------------------------------------------------
# C4 提案中心减负：批量处理 / 相似合并 / 过期自动归档
#
# 背景：提案一多就没人看了，自进化机制等于失效。这里的三件事都是为了
# 让收件箱能清空：一次点掉一批、重复的自动合掉、躺了 30 天的自己归档。
# ---------------------------------------------------------------------------
_PROPOSAL_ARCHIVE_DAYS = 30


def _proposal_fingerprint(content):
    """内容指纹：去掉标点与空白后取 md5，用来认出「换了个说法的同一件事」"""
    import re
    import hashlib
    s = re.sub(r"[\s，。！？、,.!?；;：:\"'#~()（）\[\]【】{}<>\-—_|/\\]+",
               "", (content or "").lower())
    return hashlib.md5(s.encode("utf-8")).hexdigest()[:12]


def _archive_stale_proposals(days=_PROPOSAL_ARCHIVE_DAYS):
    """超过 N 天仍未处理的 pending 自动归档，返回归档条数"""
    cutoff = time.time() - days * 86400
    with _proposals_lock:
        proposals = _load_proposals()
        n = 0
        for p in proposals:
            if p.get("status") == "pending" and p.get("created_at", 0) < cutoff:
                p["status"] = "archived"
                p["archived_at"] = time.time()
                n += 1
        if n:
            _save_proposals(proposals)
        return n


def _dedupe_proposals():
    """pending 里指纹相同的合并：保留最早一条，其余归档"""
    with _proposals_lock:
        proposals = _load_proposals()
        pend = [p for p in proposals if p.get("status") == "pending"]
        pend.sort(key=lambda p: p.get("created_at", 0))
        seen, n = {}, 0
        for p in pend:
            fp = _proposal_fingerprint(p.get("content", ""))
            if fp in seen:
                p["status"] = "archived"
                p["archived_at"] = time.time()
                p["merged_into"] = seen[fp]
                n += 1
            else:
                seen[fp] = p.get("id")
        if n:
            _save_proposals(proposals)
        return n


def _proposal_batch(ids, status, all_pending=False):
    """批量处理提案。status: approved / rejected / archived / resolved

    问询类（kind=question）只是要主人回答的问题，批准时不写档案。
    逐条处理，单条写档案失败不影响其余，最后返回成功/失败计数。
    """
    if status not in ("approved", "rejected", "archived", "resolved"):
        return {"ok": False, "msg": "不支持的处理结果: %s" % status}
    idset = set(ids or [])
    with _proposals_lock:
        proposals = _load_proposals()
        targets = [p for p in proposals
                   if p.get("status") == "pending" and (all_pending or p.get("id") in idset)]
        ok, fail = 0, 0
        for p in targets:
            if status == "approved" and p.get("kind") != "question":
                if not _apply_proposal(p):
                    fail += 1
                    continue
            p["status"] = status
            p["handled_at"] = time.time()
            ok += 1
        if targets:
            _save_proposals(proposals)
        return {"ok": True, "done": ok, "failed": fail,
                "msg": ("已处理 %d 条" % ok) + ("，%d 条写入失败" % fail if fail else "")}


def get_proposal_stats():
    """提案中心概览：待处理 / 重复组 / 过期数，给减负工具条用"""
    props = _load_proposals()
    pend = [p for p in props if p.get("status") == "pending"]
    cutoff = time.time() - _PROPOSAL_ARCHIVE_DAYS * 86400
    stale = sum(1 for p in pend if p.get("created_at", 0) < cutoff)
    fps = {}
    for p in pend:
        fps.setdefault(_proposal_fingerprint(p.get("content", "")), 0)
        fps[_proposal_fingerprint(p.get("content", ""))] += 1
    dup = sum(v - 1 for v in fps.values() if v > 1)
    return {"ok": True, "pending": len(pend), "stale": stale, "dup": dup,
            "total": len(props), "archive_days": _PROPOSAL_ARCHIVE_DAYS}


def _advisor_system():
    return (
        "你是微信 AI 陪聊机器人的「参谋助手」，正在帮主人管理这个机器人。你了解以下资料：\n\n"
        "--- 关系背景档案 ---\n" + (_read_text(RELATIONSHIP_PATH).strip() or "(空)") + "\n\n"
        "--- 成长记忆 ---\n" + (_read_text(MEMORY_MD).strip() or "(空)") + "\n\n"
        "--- 人设卡 ---\n" + (_read_text(PERSONA_PATH).strip() or "(空)") + "\n\n"
        "你的职责：解答主人的问题，主动发现缺漏的信息，帮主人完善机器人的记忆与回复质量。\n"
        "当你有要补充/修改的内容时，在回复末尾单独输出一行，格式严格为：\n"
        "【提案】类别|文件类型|内容|原因\n"
        "类别：问（你需要主人告诉你信息，内容写成一句完整的问题，比如'她生日是几号？'）\n"
        "　　　议（你有确定的具体建议，内容可直接写入档案，比如'她生日是 3 月 14 日'）。\n"
        "文件类型：relationship（关系档案）、persona（人设卡）、memory（成长记忆）、advice（改进建议，不写档案）。\n"
        "重要：当你需要主人补充信息时，**除了在对话里问，还必须在末尾输出对应的'问'类提案行**，方便主人跟进处理；"
        "当你确定具体内容时，输出'议'类提案行。一次最多输出 2 条，没需要就别输出。\n"
        "对话风格：像朋友一样自然，直接说重点，别啰嗦。"
    )


def advisor_chat(msg):
    global _advisor_history
    if not (msg or "").strip():
        return {"reply": "说点什么吧", "proposals": []}
    from ai_client import AIClient
    cfg = read_config()
    ai = AIClient(cfg.get("ai", {}))
    _advisor_history.append({"role": "user", "content": msg.strip()[:2000]})
    _advisor_history = _advisor_history[-_ADVISOR_MAX_HISTORY:]
    messages = [{"role": "system", "content": _advisor_system()}] + _advisor_history
    try:
        reply = ai.chat(messages)
    except Exception as e:
        return {"reply": "调用 AI 失败: %s" % e, "proposals": []}
    _advisor_history.append({"role": "assistant", "content": reply})
    _advisor_history = _advisor_history[-_ADVISOR_MAX_HISTORY:]
    # 解析提案行并剥离
    proposals = []
    kept = []
    for line in reply.splitlines():
        line = line.strip()
        if line.startswith("【提案】"):
            parsed = _parse_proposal_line(line)
            if parsed:
                parsed["source"] = "advisor"
                proposals.append(parsed)
                continue
        kept.append(line)
    added = _add_proposals(proposals)
    # 结构化抽取：无论参谋怎么说，都让它把"要补充的（问）+ 有把握的建议（议）"整理成提案
    # 对话式回复与结构化抽取分离，保证问询永不遗漏（即使它没输出提案行）
    try:
        extract = (
            "你是参谋助手。根据上面的对话和你掌握的档案，输出提案，每条一行，严格格式：\n"
            "【提案】类别|文件类型|内容|原因\n"
            "类别：问（需要主人补充的信息，内容写成完整问题，如'她生日是几号？'）\n"
            "　　　议（你有把握的具体建议，内容可直接写入档案，如'她生日是 3 月 14 日'）。\n"
            "文件类型：relationship（关系档案）/persona（人设卡）/memory（成长记忆）/advice（改进建议，不写档案）。\n"
            "需要主人补充的用'问'，你确定的用'议'。最多 3 条；没有就只输出（无）两个字。\n\n"
            "对话：\n主人：" + (msg or "") + "\n参谋：" + reply
        )
        q2 = ai.chat([{"role": "user", "content": extract}])
        qproposals = []
        for line in q2.splitlines():
            line = line.strip()
            if line.startswith("【提案】"):
                parsed = _parse_proposal_line(line)
                if parsed:
                    parsed["source"] = "advisor"
                    qproposals.append(parsed)
        if qproposals:
            added += _add_proposals(qproposals)
    except Exception:
        pass
    return {"reply": "\n".join(kept), "proposals": added}


def self_check():
    """记忆体检：看档案+最近聊天，输出缺漏/改进提案。约 5-15 秒。"""
    import datetime as _dt
    from ai_client import AIClient
    cfg = read_config()
    ai = AIClient(cfg.get("ai", {}))
    today = _dt.date.today().isoformat()
    yesterday = (_dt.date.today() - _dt.timedelta(days=1)).isoformat()
    logs = (get_daily(yesterday) + "\n" + get_daily(today))[-3000:]
    prompt = (
        "你是微信 AI 陪聊机器人的「记忆体检官」。请检查以下资料，找出缺漏或可改进之处：\n\n"
        "--- 关系背景档案 ---\n" + (_read_text(RELATIONSHIP_PATH).strip() or "(空)") + "\n\n"
        "--- 成长记忆 ---\n" + (_read_text(MEMORY_MD).strip() or "(空)") + "\n\n"
        "--- 人设卡 ---\n" + (_read_text(PERSONA_PATH).strip() or "(空)") + "\n\n"
        "--- 最近聊天记录（昨天+今天）---\n" + (logs or "(无)") + "\n\n"
        "注意：聊天记录里'（AI代发）'结尾的行是机器人 AI 代发的回复，不代表主人真实说话习惯，"
        "分析人设时不要参考；'（真人）'结尾的行是主人真实发言，可以参考。"
        "判断依据以对方的消息和上面档案为主。\n"
        "请输出 0-5 条提案，每条单独一行，严格格式：\n"
        "【提案】类别|文件类型|内容|原因\n"
        "类别：问（档案里缺、需要主人告诉你的信息，内容写成问题）或 议（你从聊天中总结的确定建议，内容可直接写入档案）。\n"
        "文件类型：relationship（关系档案）、persona（人设卡）、memory（成长记忆）、advice（回复质量改进建议，不写档案）。\n"
        "只输出有意义、有把握的信息，宁可少而准；不要编造档案和聊天里没有的事实；不要输出'待补充'这类占位内容。"
    )
    try:
        reply = ai.chat([{"role": "user", "content": prompt}])
    except Exception as e:
        return {"ok": False, "msg": "体检失败: %s" % e, "added": []}
    proposals = []
    for line in reply.splitlines():
        line = line.strip()
        if line.startswith("【提案】"):
            parsed = _parse_proposal_line(line)
            if parsed:
                parsed["source"] = "selfcheck"
                proposals.append(parsed)
    added = _add_proposals(proposals)
    return {"ok": True, "msg": "体检完成，新增 %d 条提案" % len(added), "added": added}


_last_check_date = ""


def _daily_watch():
    """跨天自动体检：每天零点后第一次醒来时跑一次"""
    global _last_check_date
    while True:
        try:
            today = time.strftime("%Y-%m-%d")
            if _last_check_date and today != _last_check_date:
                _logf("跨天，自动体检")
                try:
                    r = self_check()
                    _logf("自动体检: %s" % r.get("msg"))
                except Exception as e:
                    _logf("自动体检异常: %r" % e)
            _last_check_date = today
        except Exception:
            pass
        time.sleep(60)


# ---------- 状态聚合 ----------
_uptime_cache = {"val": None, "ts": 0.0}


def uptime_str():
    """2.0：纯 ctypes 读进程创建时间（毫秒级），不再需要缓存，也不再调 PowerShell。

    1.0 为了缓解 PowerShell 冷启动（300~800ms）才加了 30 秒缓存，
    现在源头变快了，缓存反而会导致"刚重启 bot 后时长显示滞后"，故移除。
    """
    pid = bot_pid()
    if not pid:
        return "—"
    start = _pstart(pid)
    if not start:
        return "运行中"
    secs = max(0.0, time.time() - start)
    d, s = divmod(int(secs), 86400)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if d:
        return "%d天%d小时%d分" % (d, h, m)
    if h:
        return "%d小时%d分" % (h, m)
    return "%d分" % m


def _uptime_compute():
    """兼容旧调用点。"""
    return uptime_str()


def wechat_status():
    """判断微信连接状态：优先用 bot 心跳自报的 wechat_ready（bot 亲自上报，可靠），
    日志尾部推断仅作兜底 + 补充'发送受限'提示（微信窗口被遮挡/锁屏时）。
    """
    st = _bot_status()
    # bot 自报就绪（心跳里的 wechat_ready）→ 最可靠，不再依赖日志里的启动标记
    if st.get("pid") and st.get("wechat_ready"):
        try:
            text = tail_log(100)
            if "微信窗口不可见" in text or "发送失败" in text:
                return "已连接（发送受限：微信窗口不可见）"
        except Exception:
            pass
        return "已连接"
    try:
        text = tail_log(300)
        if "后端初始化失败" in text:
            return "异常"
        connected = ("UIA 驱动已启用" in text or "UIA 树不可用" in text
                     or "消息监听线程已启动" in text or "后端就绪" in text)
        if not connected:
            return "未连接/启动中"
        if "微信窗口不可见" in text or "发送失败" in text:
            return "已连接（发送受限：微信窗口不可见）"
        return "已连接"
    except Exception:
        return "未知"


def get_status():
    """2.0：状态聚合统一走 web.state.build_status（含解释器来源、密钥脱敏）。"""
    from web.state import build_status
    cfg = read_config()
    ctrl = read_control()
    pid = bot_pid()
    st = _bot_status()
    try:
        return build_status(cfg, ctrl, st, pid, [LOG_PATH, CONSOLE_LOG_PATH])
    except Exception as e:
        # 状态聚合绝不能让面板崩，退回最小可用字段
        _logf("build_status 失败，退回精简状态: %r" % e)
        return {
            "running": is_running(),
            "paused": bool(ctrl.get("paused", False)),
            "pid": pid,
            "listen": cfg.get("listen", []),
            "python": VENV_PYTHON,
            "version": VERSION,
        }


def tail_log(n=200):
    """合并 bot.log 与 bot_console.log（真实日志在 console，bot.log 常为空）。

    只解码尾部字节：优先严格 UTF-8（新版 bot 强制 UTF-8），失败退回 GBK
    （Windows 重定向 stdout 的旧格式），避免旧乱码干扰状态判断。
    """
    texts = []
    for p in (LOG_PATH, CONSOLE_LOG_PATH):
        try:
            data = open(p, "rb").read()
            if not data:
                continue
            tail = data[-4096:]
            try:
                text = tail.decode("utf-8")  # 严格模式
            except UnicodeDecodeError:
                text = tail.decode("gbk", errors="replace")
            texts.append(text)
        except Exception:
            pass
    if not texts:
        return ""
    merged = "\n".join(texts)
    return "\n".join(merged.splitlines()[-n:])


# ---------- 定时任务管理 ----------
def get_schedule():
    try:
        with open(SCHEDULE_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("jobs", [])
    except Exception:
        return []


def save_schedule(jobs):
    """2.0：原子写 + 自动备份，取代 1.0 的手写重试循环。"""
    ok, err = _awjson(SCHEDULE_PATH, {"jobs": jobs})
    if not ok:
        _logf("save_schedule 写入失败: %r" % (err,))
    return ok


def sched_add(time_str, action, text):
    import re
    if not re.match(r"^\d{2}:\d{2}$", time_str or ""):
        return {"ok": False, "msg": "时间格式应为 HH:MM"}
    if action not in ("message", "pause", "resume"):
        return {"ok": False, "msg": "未知动作"}
    if action == "message" and not (text or "").strip():
        return {"ok": False, "msg": "消息内容不能为空"}
    jobs = get_schedule()
    jobs.append({
        "id": "s%d" % int(time.time() * 1000),
        "time": time_str,
        "action": action,
        "text": (text or "").strip(),
        "enabled": True,
        "last_run": "",
    })
    save_schedule(jobs)
    return {"ok": True, "msg": "已添加定时任务 %s" % time_str}


def sched_toggle(jid):
    jobs = get_schedule()
    for j in jobs:
        if j.get("id") == jid:
            j["enabled"] = not j.get("enabled", True)
            if save_schedule(jobs):
                return {"ok": True, "msg": "已" + ("开启" if j["enabled"] else "停用")}
            return {"ok": False, "msg": "写入失败（文件被占用？），状态未改变"}
    return {"ok": False, "msg": "任务不存在"}


def sched_delete(jid):
    before = get_schedule()
    jobs = [j for j in before if j.get("id") != jid]
    if len(jobs) == len(before):
        return {"ok": False, "msg": "任务不存在"}
    if save_schedule(jobs):
        return {"ok": True, "msg": "已删除"}
    return {"ok": False, "msg": "写入失败（文件被占用？），未删除"}


# ---------- HTTP 处理 ----------
_GUARD = TokenGuard(enabled=True)      # 2.0：面板访问令牌（防 CSRF / 本机误操作）
_SERVER_HOLDER = {"srv": None}
_PANEL = {"password": "", "allow_lan": False}   # F1：局域网访问与口令（main() 里填充）


def _panel_cookie_value(password):
    import hashlib
    return hashlib.sha256(("wbpanel:" + password).encode("utf-8")).hexdigest()[:32]


def _panel_password_ok(headers):
    """F1：开了局域网就必须过口令。未设口令时永远放行（本机模式）。"""
    pw = _PANEL.get("password") or ""
    if not pw:
        return True
    want = _panel_cookie_value(pw)
    cookie = headers.get("Cookie") or ""
    for part in cookie.split(";"):
        k, _, v = part.strip().partition("=")
        if k == "wb_panel" and v == want:
            return True
    return False


_LOGIN_PAGE = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1.0"><title>面板登录</title>
<style>body{margin:0;background:#0f1419;color:#e6edf3;display:flex;align-items:center;
justify-content:center;height:100vh;font-family:-apple-system,"PingFang SC","Microsoft YaHei",sans-serif;}
.box{background:#1a212b;border:1px solid #2c3744;border-radius:14px;padding:28px 30px;width:320px;}
h2{margin:0 0 6px;font-size:17px;}p{margin:0 0 18px;font-size:12.5px;color:#8b98a5;line-height:1.6;}
input{width:100%%;background:#212b38;color:#e6edf3;border:1px solid #2c3744;border-radius:10px;
padding:11px 12px;font-size:14px;box-sizing:border-box;margin-bottom:12px;}
button{width:100%%;background:#4f9cff;border:none;color:#fff;padding:11px;border-radius:10px;
font-size:14px;font-weight:600;cursor:pointer;}.err{color:#f85149;font-size:12.5px;margin-bottom:10px;}</style>
</head><body><form class="box" method="post" action="/login">
<h2>微信 AI 陪聊 · 面板</h2><p>面板已开放到局域网，请输入访问口令。</p>
%s<input type="password" name="pw" placeholder="访问口令" autofocus>
<button type="submit">进入面板</button></form></body></html>"""


def _shutdown_soon(delay=0.3):
    """延时关掉 HTTP 服务，让当前请求先把响应发完。

    退出面板是「只关面板，不动 bot」：bot 由 start_bot 以独立子进程拉起，
    父进程退出后仍在后台跑（与原有设计一致）。
    """
    def _do():
        time.sleep(delay)
        srv = _SERVER_HOLDER.get("srv")
        try:
            if srv is not None:
                srv.shutdown()
        except Exception:
            pass
    threading.Thread(target=_do, daemon=True).start()


class Handler(BaseHTTPRequestHandler):
    def _send(self, code, obj=None, body=None, ctype="application/json; charset=utf-8"):
        if obj is not None:
            body = json.dumps(obj, ensure_ascii=False)
            ctype = "application/json; charset=utf-8"
        data = body.encode("utf-8") if isinstance(body, str) else body
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _serve_html(self):
        # exe 模式：优先读 exe 旁的 assets/dashboard.html（可自定义），
        # 再退回打包进 exe 的那份。前端资源统一放 assets/
        candidates = [os.path.join(BASE_DIR, "assets", "dashboard.html"),
                      os.path.join(BASE_DIR, "dashboard.html")]
        if getattr(sys, "frozen", False):
            try:
                candidates.append(os.path.join(sys._MEIPASS, "assets", "dashboard.html"))
            except Exception:
                pass
        html = ""
        for p in candidates:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    html = f.read()
                    break
            except Exception:
                continue
        if not html:
            self._send(500, body="assets/dashboard.html 缺失，请确认项目文件完整",
                       ctype="text/plain; charset=utf-8")
            return
        # 2.0：把面板令牌注入 HTML，前端所有写请求必须带回它
        meta = _GUARD.meta_tag()
        if meta:
            if "<head>" in html:
                html = html.replace("<head>", "<head>\n" + meta, 1)
            else:
                html = meta + html
        self._send(200, body=html, ctype="text/html; charset=utf-8")

    def _serve_stream(self):
        """D7：Server-Sent Events。

        只推「有新事件」这一件事，状态与日志仍由前端自行拉取——避免在一条
        长连接里塞进所有数据模型。连接最长 5 分钟，到点自然断开由前端重连，
        防止线程被挂死的客户端永久占住。
        """
        try:
            from core import events as _ev
        except Exception:
            self._send(500, obj={"ok": False, "msg": "事件模块不可用"})
            return
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache, no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
        except Exception:
            return

        last_ts = time.time()
        deadline = time.time() + 300
        try:
            while time.time() < deadline:
                try:
                    items = _ev.recent(30, since=last_ts)
                except Exception:
                    items = []
                if items:
                    last_ts = max(float(e.get("ts", last_ts)) for e in items) + 0.001
                    payload = json.dumps({"events": items}, ensure_ascii=False)
                    self.wfile.write(("event: events\ndata: " + payload + "\n\n").encode("utf-8"))
                else:
                    self.wfile.write(b": keepalive\n\n")
                self.wfile.flush()
                time.sleep(1.5)
        except Exception:
            pass

    # ---- 2.0 静态资源 ----
    _ASSET_TYPES = {
        ".css": "text/css; charset=utf-8",
        ".js": "application/javascript; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".ico": "image/x-icon",
        ".woff2": "font/woff2",
    }

    def _serve_asset(self, rel):
        # 目录穿越防护：只允许 assets/ 下的普通文件名
        rel = unquote(rel).split("?")[0]
        if not rel or ".." in rel or rel.startswith(("/", "\\")):
            self._send(403, obj={"error": "bad asset path"})
            return
        if "\\" in rel:
            rel = rel.replace("\\", "/")
        cands = [os.path.join(BASE_DIR, "assets", rel)]
        if getattr(sys, "frozen", False):
            try:
                cands.append(os.path.join(sys._MEIPASS, "assets", rel))
            except Exception:
                pass
        for p in cands:
            try:
                with open(p, "rb") as f:
                    data = f.read()
                ctype = self._ASSET_TYPES.get(
                    os.path.splitext(rel)[1].lower(),
                    "application/octet-stream")
                self._send(200, body=data, ctype=ctype)
                return
            except Exception:
                continue
        self._send(404, obj={"error": "asset not found"})

    def do_GET(self):
        try:
            self._do_get()
        except Exception as e:
            # 单个请求异常绝不能让服务断连
            try:
                self._send(500, obj={"ok": False, "error": "GET 处理失败", "detail": str(e)})
            except Exception:
                pass

    def _do_get(self):
        parsed = urlparse(self.path)
        # F1：开了局域网就要求口令（/api/identity 与 /login 本身放行）
        if parsed.path != "/login" and not _panel_password_ok(self.headers):
            self._send(200, body=_LOGIN_PAGE % "", ctype="text/html; charset=utf-8")
            return
        # 2.0：身份接口（无鉴权，供单实例锁判断"占用端口的是不是自己"）
        if parsed.path == "/api/identity":
            self._send(200, obj={
                "version": VERSION,
                "base_dir": BASE_DIR,
                "pid": os.getpid(),
                "wechat_app": "wechat-ai-companion",
            })
            return
        # D7：SSE 实时推送。前端连上后由服务端主动推事件，不再 3 秒一问。
        if parsed.path == "/api/stream":
            self._serve_stream()
            return
        if parsed.path in ("/", "/index.html"):
            self._serve_html()
            return
        # 2.0：前端已拆分为 assets/style.css + assets/app.js，这里做静态分发
        if parsed.path.startswith("/assets/"):
            self._serve_asset(parsed.path[len("/assets/"):])
            return
        if parsed.path == "/api/status":
            self._send(200, obj=get_status())
            return
        if parsed.path == "/api/log":
            qs = parse_qs(parsed.query)
            n = int(qs.get("lines", ["200"])[0])
            self._send(200, obj={"log": tail_log(n)})
            return
        if parsed.path == "/api/panel":
            self._send(200, obj=get_panel())
            return
        if parsed.path == "/api/daily":
            qs = parse_qs(parsed.query)
            date = qs.get("date", [None])[0]
            self._send(200, obj={"log": get_daily(date)})
            return
        if parsed.path == "/api/proposals":
            self._send(200, obj={"proposals": get_proposals()})
            return
        if parsed.path == "/api/proposals/stats":
            self._send(200, obj=get_proposal_stats())
            return
        if parsed.path == "/api/schedule":
            self._send(200, obj={"jobs": get_schedule()})
            return
        # 3.0：扩展 GET（事件线 / 成本 / 记忆 / 事实 / 护栏 / 备份）
        if _apiextra is not None:
            res = _apiextra.handle_get(parsed.path, parse_qs(parsed.query))
            if res is not None:
                self._send(200, obj=res)
                return
        self._send(404, obj={"error": "not found"})

    def do_POST(self):
        try:
            self._do_post()
        except Exception as e:
            try:
                self._send(500, obj={"ok": False, "error": "POST 处理失败", "detail": str(e)})
            except Exception:
                pass

    def _do_post(self):
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        if length > 1_000_000:  # 2.0：请求体上限，避免无界读取
            self._send(413, obj={"ok": False, "msg": "请求体过大"})
            return
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        try:
            payload = json.loads(raw) if raw else {}
        except Exception:
            payload = {}

        # F1：登录（局域网口令）
        if parsed.path == "/login":
            import urllib.parse as _up
            fields = _up.parse_qs(raw)
            pw = (fields.get("pw") or [""])[0]
            if pw and _PANEL.get("password") and pw == _PANEL["password"]:
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Set-Cookie",
                                 "wb_panel=%s; Path=/; HttpOnly; SameSite=Lax"
                                 % _panel_cookie_value(_PANEL["password"]))
                self.end_headers()
            else:
                self._send(200, body=_LOGIN_PAGE % '<div class="err">口令不对，再试一次</div>',
                           ctype="text/html; charset=utf-8")
            return

        # 2.0 安全校验：写操作必须带面板令牌 + 同源；破坏性操作另需显式确认
        # 3.0：新增的写端点同样受令牌保护（即便核心集合里没有它们）
        if parsed.path in PROTECTED_POST or parsed.path in EXTRA_PROTECTED_POST:
            ok, reason = _GUARD.check(
                self.headers, origin_host=self.headers.get("Origin"))
            if not ok:
                self._send(403, obj={"ok": False, "msg": "请求被拒绝：" + reason})
                return
            need = (not require_confirm(parsed.path, payload) or
                    (parsed.path in EXTRA_DESTRUCTIVE_POST and not payload.get("confirm")))
            if need:
                self._send(400, obj={
                    "ok": False,
                    "msg": "这是破坏性操作，请带 confirm=true 显式确认"})
                return

        if parsed.path == "/api/start":
            self._send(200, obj=start_bot())
        elif parsed.path == "/api/stop":
            self._send(200, obj=stop_bot())
        elif parsed.path == "/api/restart":
            self._send(200, obj=restart_bot())
        elif parsed.path == "/api/self_test":
            self._send(200, obj=self_test())
        elif parsed.path == "/api/fix":
            action = payload.get("action", "")
            self._send(200, obj=fix_issue(action))
        elif parsed.path == "/api/pause":
            self._send(200, obj=_set_paused_verified(True))
        elif parsed.path == "/api/resume":
            self._send(200, obj=_set_paused_verified(False))
        elif parsed.path == "/api/set_listen":
            target = payload.get("target", "")
            if not target:
                self._send(400, obj={"ok": False, "msg": "缺少 target"})
                return
            ok = set_listen(target)
            if not ok:
                self._send(500, obj={"ok": False, "msg": "写 config 失败"})
                return
            # 切换监听对象需要重启才能生效
            res = restart_bot()
            res["msg"] = (res.get("msg", "") + f"；已将监听对象切到「{target}」并重启").strip("；")
            self._send(200, obj=res)
        elif parsed.path == "/api/clear_memory":
            removed = clear_memory()
            self._send(200, obj={"ok": True, "removed": removed, "msg": f"已清空 {len(removed)} 个文件"})
        elif parsed.path == "/api/save_persona":
            content = payload.get("content", "")
            ok = _write_text(PERSONA_PATH, content)
            self._send(200, obj={"ok": ok, "msg": "人设卡已保存（下次回复立即生效）" if ok else "保存失败"})
        elif parsed.path == "/api/save_relationship":
            content = payload.get("content", "")
            ok = _write_text(RELATIONSHIP_PATH, content)
            self._send(200, obj={"ok": ok, "msg": "关系背景已保存（下次回复立即生效）" if ok else "保存失败"})
        elif parsed.path == "/api/save_memory":
            content = payload.get("content", "")
            ok = _write_text(MEMORY_MD, content)
            self._send(200, obj={"ok": ok, "msg": "成长记忆已保存（立即生效）" if ok else "保存失败"})
        elif parsed.path == "/api/advisor_chat":
            msg = payload.get("msg", "")
            self._send(200, obj=advisor_chat(msg))
        elif parsed.path == "/api/self_check":
            self._send(200, obj=self_check())
        elif parsed.path == "/api/proposal/approve":
            self._send(200, obj=_proposal_act(payload.get("id", ""), "approved", payload.get("content")))
        elif parsed.path == "/api/proposal/reject":
            self._send(200, obj=_proposal_act(payload.get("id", ""), "rejected"))
        elif parsed.path == "/api/proposal/resolve":
            self._send(200, obj=_proposal_act(payload.get("id", ""), "resolved"))
        elif parsed.path == "/api/proposals/batch":
            # C4：批量通过 / 拒绝 / 归档。all=true 时处理全部待处理提案
            self._send(200, obj=_proposal_batch(
                payload.get("ids") or [], payload.get("status", "rejected"),
                bool(payload.get("all"))))
        elif parsed.path == "/api/proposals/dedupe":
            n = _dedupe_proposals()
            self._send(200, obj={"ok": True, "merged": n,
                                 "msg": ("合并了 %d 条重复提案" % n) if n else "没有发现重复提案"})
        elif parsed.path == "/api/proposals/archive_stale":
            days = int(payload.get("days") or _PROPOSAL_ARCHIVE_DAYS)
            n = _archive_stale_proposals(days)
            self._send(200, obj={"ok": True, "archived": n,
                                 "msg": ("归档了 %d 条超过 %d 天的提案" % (n, days)) if n
                                 else "没有过期提案"})
        elif parsed.path == "/api/schedule/add":
            self._send(200, obj=sched_add(payload.get("time", ""), payload.get("action", ""), payload.get("text", "")))
        elif parsed.path == "/api/schedule/toggle":
            self._send(200, obj=sched_toggle(payload.get("id", "")))
        elif parsed.path == "/api/schedule/delete":
            self._send(200, obj=sched_delete(payload.get("id", "")))
        elif parsed.path == "/api/shutdown":
            self._send(200, obj={"ok": True, "msg": "控制面板正在退出"})
            _shutdown_soon()
        else:
            # 3.0：扩展 POST（预演 / 反馈 / 滑杆 / 沉淀 / 事实 / 护栏 / 备份）
            if _apiextra is not None:
                res = _apiextra.handle_post(parsed.path, payload)
                if res is not None:
                    self._send(200, obj=res)
                    return
            self._send(404, obj={"error": "not found"})

    def log_message(self, fmt, *args):
        pass  # 安静


def _port_in_use(port):
    import socket
    s = socket.socket()
    try:
        s.bind(("127.0.0.1", port))
        return False
    except OSError:
        return True
    finally:
        s.close()


def _logf(msg):
    """exe 无控制台，启动/异常写文件方便排查"""
    try:
        with open(os.path.join(BASE_DIR, "ui_server_startup.log"), "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + str(msg) + "\n")
    except Exception:
        pass


def _open_browser(url):
    time.sleep(1.0)
    try:
        webbrowser.open(url)
    except Exception as e:
        _logf("open browser: %r" % e)


def _housekeeping():
    """2.0：启动后做一次清理（日志轮转 / 旧日志 / 残留临时脚本）。"""
    try:
        # 日志超过 2MB 就切分，保留 3 份
        for p in (CONSOLE_LOG_PATH, LOG_PATH):
            if _rotate(p):
                _logf("日志轮转: %s" % os.path.basename(p))
        # wechatauto 日志按天累积，只留最近 7 天 / 最多 10 个
        wl = os.path.join(BASE_DIR, "wechatauto_logs")
        if os.path.isdir(wl):
            n = _prune_dir(wl, ".log", keep=10, max_age_days=7)
            if n:
                _logf("清理 wechatauto 日志 %d 个" % n)
        # 1.0 遗留的 tmp*.ps1（异常路径下没删掉）
        n = _cleanup_ps1(BASE_DIR)
        if n:
            _logf("清理残留临时脚本 %d 个" % n)
    except Exception as e:
        _logf("清理任务异常: %r" % e)


def _notify_conflict(msg):
    """端口冲突提示：exe（无控制台）弹窗，源码模式写日志+打印。"""
    _logf("启动中止: %s" % msg.replace("\n", " | "))
    if getattr(sys, "frozen", False):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                0, msg, "微信AI陪聊控制面板 v%s" % VERSION, 0x30 | 0x40000)
        except Exception:
            pass
    else:
        print(msg)


def _panel_cfg():
    """F1：面板监听配置。

    默认只绑 127.0.0.1。开局域网必须同时设 password——否则面板上全是真实聊天
    记录和人设档案，暴露到局域网等于裸奔。没设密码就拒绝，回落本机。
    """
    cfg = {}
    try:
        cfg = read_config().get("panel") or {}
    except Exception:
        cfg = {}
    allow_lan = bool(cfg.get("allow_lan", False))
    password = str(cfg.get("password") or "")
    host = "0.0.0.0" if allow_lan else "127.0.0.1"
    if allow_lan and not password:
        _logf("panel.allow_lan=true 但未设 password，已回落到仅本机监听")
        host = "127.0.0.1"
        allow_lan = False
    return {"host": host, "allow_lan": allow_lan, "password": password}


def main():
    global _ui_start_ts
    _ui_start_ts = time.time()
    pcfg = _panel_cfg()
    _PANEL["password"] = pcfg["password"]
    _PANEL["allow_lan"] = pcfg["allow_lan"]
    host = pcfg["host"]
    url = "http://127.0.0.1:%d/" % PORT
    try:
        # 2.0：先判断端口冲突的性质，不再"静默退出 + 开浏览器"
        verdict = _resolve_conflict(BASE_DIR, PORT, alive_fn=_alive2)
        if verdict["action"] == "reuse":
            _logf("复用已有实例（同目录），打开浏览器")
            try:
                webbrowser.open(url)
            except Exception:
                pass
            return
        if verdict["action"] == "warn":
            _notify_conflict(verdict["msg"])
            return

        server = ThreadingHTTPServer((host, PORT), Handler)
        _SERVER_HOLDER["srv"] = server
        _write_lock(BASE_DIR, PORT)          # 2.0：写单实例锁
        _logf("控制面板 v%s 已启动: %s（BASE_DIR=%s）" % (VERSION, url, BASE_DIR))
        if pcfg["allow_lan"]:
            try:
                import socket
                lan = socket.gethostbyname(socket.gethostname())
                _logf("局域网访问已开启：http://%s:%d/ （手机连同一个 WiFi 即可打开）" % (lan, PORT))
            except Exception:
                pass
        _logf("Python 解释器: %s" % VENV_PYTHON)
        # 浏览器放到后台线程打开，绝不影响服务启动
        threading.Thread(target=_open_browser, args=(url,), daemon=True).start()
        # 启动后做一次清理（日志轮转/旧日志/临时文件）
        threading.Thread(target=_housekeeping, daemon=True).start()
        # 跨天自动体检（自进化）
        threading.Thread(target=_daily_watch, daemon=True).start()
        try:
            server.serve_forever()
        finally:
            # 先释放监听套接字，避免端口短时间内处于 TIME_WAIT 占位
            try:
                server.server_close()
            except Exception:
                pass
            _remove_lock(BASE_DIR)
            _logf("控制面板已退出，实例锁已清理")
    except Exception as e:
        _logf("启动异常: %r" % e)
        raise


def run_self_test_cli():
    """--self-test：命令行出文本报告，不启动服务（P2-10）。

    适合远程排查或直接把结果贴给别人看。
    """
    cfg = read_config()
    ctrl = read_control()
    st = _bot_status()
    pid = bot_pid()

    def _roundtrip():
        before = read_control().get("paused", False)
        ok = set_paused(before)
        after = read_control().get("paused", False)
        return ok and before == after

    # 复用 web.state 的统一自检实现
    try:
        from web.state import self_test_items, build_status, text_report
        result = self_test_items(
            cfg, ctrl, st, pid, [LOG_PATH, CONSOLE_LOG_PATH], _roundtrip)
        status = build_status(cfg, ctrl, st, pid, [LOG_PATH, CONSOLE_LOG_PATH])
        print(text_report(result, status))
    except Exception as e:
        # web.state 不可用时退回内建自检
        print("（web.state 不可用，使用内建自检：%r）\n" % e)
        result = self_test()
        print("自检 %d/%d 通过" % (result["passed"], result["total"]))
        for i in result["items"]:
            print("  %s %-12s %s" % ("OK  " if i["ok"] else "FAIL",
                                     i["name"], i["detail"]))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    args = [a.lower() for a in sys.argv[1:]]
    if "--self-test" in args or "--selftest" in args:
        sys.exit(run_self_test_cli())
    if "--version" in args or "-v" in args:
        print("微信 AI 陪聊控制面板 v%s" % VERSION)
        print("项目目录：%s" % BASE_DIR)
        print("解释器  ：%s" % VENV_PYTHON)
        sys.exit(0)
    main()
