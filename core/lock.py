# -*- coding: utf-8 -*-
"""跨进程文件锁（3.0）

解决的问题（2.0 真实踩到的）：
  data/schedule.json、data/proposals.json 被**两个进程**（面板 ui_server / 机器人 main）
  外加各自的多线程同时读-改-写。经典后果：定时任务刚把 last_run 标成今天，
  面板改个开关整文件覆盖回去 → 同一条早安一天发两次。

做法：O_EXCL 独占创建锁文件 + 陈旧锁自动抢占（持锁进程已死则接管）。
纯标准库，Windows / Linux 通用。不做 flock/msvcrt 的平台分支，保证行为一致。
"""

import os
import random
import time

STALE_SECONDS = 30.0


class Timeout(Exception):
    pass


class FileLock:
    """简单的互斥锁。

    用法：
        with FileLock("schedule"):
            jobs = load(); mutate(); save()

    注意：锁只保护**遵守同一约定**的调用方。第三方代码直接写文件锁不住，
    但本项目所有写口都收口到 atomicio / store，足够覆盖。
    """

    def __init__(self, name, timeout=5.0, stale=STALE_SECONDS, base=None):
        if base is None:
            from .paths import data_dir
            base = data_dir()
        os.makedirs(base, exist_ok=True)
        self.path = os.path.join(base, ".lock_%s" % name)
        self.timeout = timeout
        self.stale = stale
        self.held = False

    # ---- 内部 ----
    def _try_acquire(self):
        try:
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            try:
                os.write(fd, ("%d\n" % os.getpid()).encode("ascii"))
            finally:
                os.close(fd)
            return True
        except FileExistsError:
            return False
        except OSError:
            return False

    def _steal_if_stale(self):
        """持锁进程已死（或锁文件超过 stale 秒没人动）→ 抢占，避免死锁卡死整条链路。"""
        try:
            age = time.time() - os.path.getmtime(self.path)
        except OSError:
            return False
        if age < self.stale:
            return False
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                pid = int((f.read().strip() or "0").split("\n")[0])
        except Exception:
            pid = 0
        if pid and self._alive(pid) and age < self.stale * 4:
            return False  # 进程还活着，即使有点慢也别抢
        try:
            os.remove(self.path)
        except OSError:
            return False
        return True

    @staticmethod
    def _alive(pid):
        # 进程存活性判断（放在 Core 层，避免与 core.proclog 形成循环依赖）
        try:
            import ctypes
            h = ctypes.windll.kernel32.OpenProcess(0x1000, False, int(pid))
            if h:
                ctypes.windll.kernel32.CloseHandle(h)
                return True
            return False
        except Exception:
            try:
                os.kill(int(pid), 0)
                return True
            except Exception:
                return False

    # ---- 公开 ----
    def acquire(self, timeout=None):
        t = self.timeout if timeout is None else timeout
        deadline = time.time() + t
        while True:
            if self._try_acquire():
                self.held = True
                return True
            if time.time() >= deadline:
                raise Timeout("获取锁超时: %s" % os.path.basename(self.path))
            self._steal_if_stale()
            time.sleep(random.uniform(0.02, 0.06))

    def release(self):
        if not self.held:
            return
        try:
            os.remove(self.path)
        except OSError:
            pass
        self.held = False

    def __enter__(self):
        self.acquire()
        return self

    def __exit__(self, *exc):
        self.release()
        return False


def schedule_lock(timeout=5.0):
    return FileLock("schedule", timeout=timeout)


def proposals_lock(timeout=5.0):
    return FileLock("proposals", timeout=timeout)


def summary_lock(timeout=5.0):
    return FileLock("summary", timeout=timeout)
