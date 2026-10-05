# -*- coding: utf-8 -*-
"""进程查询与日志读取（2.0）

取代 1.0 里两处「生成临时 .ps1 文件 → 调 powershell -Bypass」的做法。

理由：
  - PowerShell 冷启动 300~800ms，还要写/删临时文件，失败模式多
  - 1.0 作者已经为 _alive()/_proc_mem_mb() 写了纯 ctypes 的 Toolhelp32 快照实现，
    只是 _find_bot_pid()/uptime 这两处没改过来；这里统一补齐
"""

import ctypes
import os
import time
from ctypes import wintypes

TH32CS_SNAPPROCESS = 0x00000002
INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * 260),
    ]


def _snapshot_procs():
    """返回 [(pid, ppid, name), ...]；失败返回空列表。"""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
        kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(PROCESSENTRY32W)]
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

        snap = kernel32.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return []
        out = []
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            out.append((entry.th32ProcessID, entry.th32ParentProcessID, entry.szExeFile))
            entry = PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
        kernel32.CloseHandle(snap)
        return out
    except Exception:
        return []


def alive(pid):
    """进程是否存活（快照枚举，权威；不用 OpenProcess 避免僵尸态误判）。"""
    if not pid:
        return False
    for p, _pp, _n in _snapshot_procs():
        if p == pid:
            return True
    return False


def proc_name(pid):
    for p, _pp, name in _snapshot_procs():
        if p == pid:
            return name
    return None


def children_of(pid):
    return [p for p, pp, _n in _snapshot_procs() if pp == pid]


def proc_mem_mb(pid):
    """工作集内存（MB）。失败返回 None。"""
    if not pid:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi")

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

        PROCESS_QUERY_INFORMATION = 0x0400
        PROCESS_VM_READ = 0x0010
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        h = kernel32.OpenProcess(PROCESS_QUERY_INFORMATION | PROCESS_VM_READ, False, pid)
        if not h:
            return None
        try:
            pmc = PMC()
            pmc.cb = ctypes.sizeof(PMC)
            if not psapi.GetProcessMemoryInfo(h, ctypes.byref(pmc), pmc.cb):
                return None
            return round(pmc.WorkingSetSize / (1024 * 1024), 1)
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return None


def proc_tree_mem_mb(pid):
    """进程及其直接子进程的内存合计。"""
    if not pid:
        return None
    total = proc_mem_mb(pid) or 0
    for c in children_of(pid):
        total += proc_mem_mb(c) or 0
    return round(total, 1) if total else None


def proc_start_time(pid):
    """进程创建时间（Unix 秒）。失败返回 None。

    用于替代 uptime 查询里的 PowerShell/Get-CimInstance。
    """
    if not pid:
        return None
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return None
        try:
            ft_create = wintypes.FILETIME()
            ft_exit = wintypes.FILETIME()
            ft_kernel = wintypes.FILETIME()
            ft_user = wintypes.FILETIME()
            ok = kernel32.GetProcessTimes(
                h, ctypes.byref(ft_create), ctypes.byref(ft_exit),
                ctypes.byref(ft_kernel), ctypes.byref(ft_user))
            if not ok:
                return None
            v = (ft_create.dwHighDateTime << 32) | ft_create.dwLowDateTime
            if not v:
                return None
            # FILETIME: 1601-01-01 起的 100ns 计数
            return v / 10_000_000.0 - 11644473600.0
        finally:
            kernel32.CloseHandle(h)
    except Exception:
        return None


def kill_tree(pid, timeout=10):
    """taskkill /F /T 杀进程树；失败尝试逐个杀子进程。返回是否已死。"""
    import subprocess
    NO_WINDOW = 0x08000000
    try:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=timeout, creationflags=NO_WINDOW)
    except Exception:
        pass
    time.sleep(0.5)
    if not alive(pid):
        return True
    for c in children_of(pid):
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(c)],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=timeout, creationflags=NO_WINDOW)
        except Exception:
            pass
    time.sleep(0.5)
    return not alive(pid)


# ---------------------------------------------------------------------------
# 日志读取
# ---------------------------------------------------------------------------

_TAIL_CACHE = {}


def tail_text(path, n=200, max_bytes=8192):
    """读日志尾部：只解尾部字节，UTF-8 失败退回 GBK。

    复用 ui_server 1.0 的成熟做法（避免旧日志乱码干扰状态判断）。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return ""
    if not data:
        return ""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("gbk", errors="replace")
    return "\n".join(text.splitlines()[-n:])


def rotate_if_needed(path, max_bytes=2 * 1024 * 1024, backups=3):
    """简易日志轮转：超过 max_bytes 就切成 .1/.2/.3，再新建。

    取代 1.0 的「日志无限增长」。main.py 侧建议用 RotatingFileHandler，
    这里给已经存在的大日志做兜底。
    """
    try:
        if not os.path.exists(path) or os.path.getsize(path) < max_bytes:
            return False
        for i in range(backups, 0, -1):
            src = path if i == 1 else "%s.%d" % (path, i - 1)
            dst = "%s.%d" % (path, i)
            if os.path.exists(src):
                if os.path.exists(dst):
                    try:
                        os.remove(dst)
                    except OSError:
                        pass
                try:
                    os.rename(src, dst)
                except OSError:
                    pass
        return True
    except OSError:
        return False


def prune_dir(d, pattern_suffix, keep, max_age_days=None):
    """清理目录：按后缀筛选，保留最近 keep 个，可选删除超过 N 天的。

    用于 wechatauto_logs/ 保留最近 7 天。
    """
    try:
        names = [f for f in os.listdir(d) if f.endswith(pattern_suffix)]
    except OSError:
        return 0
    now = time.time()
    removed = 0
    for name in names:
        p = os.path.join(d, name)
        try:
            mt = os.path.getmtime(p)
        except OSError:
            continue
        if max_age_days and (now - mt) > max_age_days * 86400:
            try:
                os.remove(p)
                removed += 1
            except OSError:
                pass
    # 再按数量裁剪
    try:
        rest = sorted(
            (f for f in os.listdir(d) if f.endswith(pattern_suffix)),
            key=lambda f: os.path.getmtime(os.path.join(d, f)),
            reverse=True)
        for old in rest[keep:]:
            try:
                os.remove(os.path.join(d, old))
                removed += 1
            except OSError:
                pass
    except OSError:
        pass
    return removed


def cleanup_temp_ps1(directory, max_age_seconds=3600):
    """清理残留的 tmp*.ps1（1.0 里累积了 80+ 个）。"""
    import glob
    removed = 0
    now = time.time()
    for p in glob.glob(os.path.join(directory, "tmp*.ps1")):
        try:
            if now - os.path.getmtime(p) > max_age_seconds:
                os.remove(p)
                removed += 1
        except OSError:
            pass
    return removed
