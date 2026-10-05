# -*- coding: utf-8 -*-
"""单实例锁（2.0，P0-1）

1.0 的问题：面板绑固定端口 8765，第二个实例发现端口被占用就「静默退出 + 开浏览器」。
结果：桌面副本和 dist 打包版并存时，你打开的可能是 A 部署的页面，却在操作 B 部署的 bot。

2.0 做法：
  1. 启动时写 <项目目录>/.panel.lock（含 PID + BASE_DIR + 写入时间）
  2. 端口被占用时，先问对方 /api/identity 拿 BASE_DIR
     - 一致 → 认为是自己（或同目录重启），直接开浏览器
     - 不一致 → 明确提示「另一个部署正在占用」，不静默
  3. 锁文件里的 PID 若已死，视为陈旧锁，接管
"""

import json
import os
import socket
import time

LOCK_NAME = ".panel.lock"
IDENTITY_TIMEOUT = 2.0


def lock_path(base_dir):
    return os.path.join(base_dir, LOCK_NAME)


def write_lock(base_dir, port):
    """写锁文件。返回锁内容 dict。"""
    data = {
        "pid": os.getpid(),
        "base_dir": base_dir,
        "port": port,
        "ts": time.time(),
        "time_str": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    p = lock_path(base_dir)
    try:
        tmp = p + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, p)
    except OSError:
        pass
    return data


def read_lock(base_dir):
    p = lock_path(base_dir)
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def remove_lock(base_dir):
    """清理锁文件。

    优先 os.remove；删不掉时退一步**改名**（Windows 上偶尔会遇到句柄没释放
    干净、或安全软件不允许删除），改名的语义同样满足「这个目录不再持有锁」
    ——后续启动只会去找 .panel.lock，找不到就认为没有实例在跑。
    """
    p = lock_path(base_dir)
    try:
        os.remove(p)
        return True
    except OSError:
        pass
    try:
        if not os.path.exists(p):
            return True
        os.replace(p, p + ".released")
        return True
    except OSError:
        return False


def port_in_use(port, host="127.0.0.1"):
    s = socket.socket()
    s.settimeout(0.8)
    try:
        return s.connect_ex((host, port)) == 0
    finally:
        s.close()


def bind_free(port, host="127.0.0.1"):
    """端口是否可绑定（未被占用）。"""
    s = socket.socket()
    try:
        s.bind((host, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


def ask_identity(port, timeout=IDENTITY_TIMEOUT):
    """向已在运行的面板询问身份。返回 dict 或 None。"""
    try:
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:%d/api/identity" % port)
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def resolve_conflict(base_dir, port, alive_fn=None):
    """启动时判断端口冲突性质。

    返回 dict:
      {"action": "start"|"reuse"|"warn", "other": {...}|None, "msg": str}
      - start: 可以自己起服务
      - reuse: 已有同目录实例，直接开浏览器
      - warn : 有不同目录的实例占用，需要提示用户
    """
    if not port_in_use(port):
        return {"action": "start", "other": None, "msg": "端口空闲"}

    info = ask_identity(port)
    if info is None:
        # 端口被占但对方不认识（别的程序占用了 8765）
        return {
            "action": "warn",
            "other": None,
            "msg": "端口 %d 被其他程序占用，无法启动控制面板。\n"
                   "请关闭占用该端口的程序，或修改 ui_server.py 的 PORT。" % port,
        }

    other_dir = (info.get("base_dir") or "").rstrip("\\/").lower()
    my_dir = base_dir.rstrip("\\/").lower()

    if other_dir == my_dir:
        return {"action": "reuse", "other": info,
                "msg": "已有控制面板在运行（同一目录），直接打开浏览器"}

    return {
        "action": "warn",
        "other": info,
        "msg": "端口 %d 已被另一个部署占用：\n\n"
               "  运行中：%s\n  本次启动：%s\n\n"
               "两个部署会互相干扰（可能操作到错误的机器人）。\n"
               "建议先关闭另一个实例，或只保留一份部署。"
               % (port, info.get("base_dir"), base_dir),
    }


def stale_lock(base_dir, alive_fn):
    """返回陈旧锁信息（PID 已死但文件还在），否则 None。"""
    data = read_lock(base_dir)
    if not data:
        return None
    pid = data.get("pid")
    if pid and alive_fn and alive_fn(pid):
        return None
    return data
