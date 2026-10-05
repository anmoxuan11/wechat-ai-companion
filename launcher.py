# -*- coding: utf-8 -*-
"""
微信 AI 陪聊控制面板 · 一键启动器（打包成 exe 用）

双击 exe 后干这几件事：
  1. 定位「项目目录」（优先找本 exe 同级的 ui_server.py；找不到就提示）
  2. 用同目录下的内嵌 Python 解释器（或系统 Python）拉起 ui_server.py
  3. 等端口就绪，打开浏览器
  4. 失败时弹窗并把日志尾部贴出来（绝不静默失败）

设计原则：无论怎么打包，这个文件都只用标准库，且不假设任何绝对路径。
"""

import os
import socket
import subprocess
import sys
import time
import webbrowser

PANEL_PORT = 8765
EXE_NAME = "微信AI陪聊控制面板"


def _script_dir():
    """exe（或 py）所在目录；PyInstaller onefile 下是 exe 真实位置。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def _meipass():
    base = getattr(sys, "_MEIPASS", None)
    return base if base and os.path.isdir(base) else None


def _candidate_dirs(start):
    """候选项目目录，按顺序去重。

    打包成 onedir 后 exe 在 <项目>/dist/名字/ 下，脚本在 _internal 里。
    只看 exe 同级会把「_internal」当成项目目录 —— 那里没有 config.json，
    面板就会读写到错误的地方。所以这里向上也找两级。
    """
    cands = []
    cur = start
    for _ in range(3):                 # 自身 + 上两级
        if cur and cur not in cands:
            cands.append(cur)
        cur = os.path.dirname(cur) if cur else None

    # 同级子目录（排除 PyInstaller 的 _internal，避免误判）
    for base in list(cands):
        try:
            for n in sorted(os.listdir(base)):
                p = os.path.join(base, n)
                if os.path.isdir(p) and os.path.basename(p) != "_internal":
                    if p not in cands:
                        cands.append(p)
        except OSError:
            pass

    mp = _meipass()                    # 兜底：打包资源目录
    if mp and mp not in cands:
        cands.append(mp)
    return cands


def _find_project_dir(start):
    """找真正的项目目录。

    判定标准优先看 config.json（_internal 里没有它），
    其次才看 ui_server.py。
    """
    cands = _candidate_dirs(start)
    for c in cands:
        if os.path.isfile(os.path.join(c, "config.json")):
            return c
    for c in cands:
        if os.path.isfile(os.path.join(c, "ui_server.py")):
            return c
    return None


def _find_python(project_dir):
    """找 Python 解释器（2.0：不再硬编码任何用户目录）。

    顺序：环境变量 → 内嵌 Python → 项目 venv → 常见安装位置 → 系统 PATH
    """
    import glob
    import shutil

    cands = []

    # 1) 显式指定（排障/多版本共存最好用）
    env_py = os.environ.get("WECHAT_BOT_PYTHON", "").strip().strip('"')
    if env_py:
        cands.append(env_py)

    # 2) 内嵌 Python（随发行包一起分发时最理想）
    for sub in ("python", "runtime"):
        cands.append(os.path.join(project_dir, sub, "python.exe"))
    cands.append(os.path.join(project_dir, "python.exe"))

    # 3) 项目内 venv
    for sub in (".venv", "venv", "env"):
        cands.append(os.path.join(project_dir, sub, "Scripts", "python.exe"))

    # 4) 常见安装位置（用户级 / 系统级），避免依赖某个具体用户名
    local = os.environ.get("LOCALAPPDATA", "")
    for base in (local, r"C:\Python314", r"C:\Python313", r"C:\Python312"):
        if not base:
            continue
        cands.append(os.path.join(base, "Programs", "Python", "Python3*", "python.exe"))

    for c in list(cands):
        if "*" in c:
            cands.extend(sorted(glob.glob(c), reverse=True))

    for c in cands:
        try:
            if c and "*" not in c and os.path.isfile(c):
                return c
        except OSError:
            continue

    # 5) 系统 PATH
    for name in ("python.exe", "python3.exe", "py.exe"):
        hit = shutil.which(name)
        if hit:
            return hit
    return None


def _port_busy(port):
    s = socket.socket()
    s.settimeout(0.6)
    try:
        return s.connect_ex(("127.0.0.1", port)) == 0
    finally:
        s.close()


def _wait_port(port, timeout=25):
    t0 = time.time()
    while time.time() - t0 < timeout:
        if _port_busy(port):
            return True
        time.sleep(0.4)
    return False


def _msgbox(text, title=EXE_NAME, style=0x40):
    """0x40 信息图标 / 0x10 错误图标"""
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, text, title, style | 0x40000)
    except Exception:
        print(text)


def _panel_identity(port):
    """端口被占用时，确认占用者是不是自家面板（2.0：避免误连别的程序）。"""
    import json
    import urllib.request
    try:
        with urllib.request.urlopen(
                "http://127.0.0.1:%d/api/identity" % port, timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception:
        return None


def _tail(path, n=2000):
    try:
        data = open(path, "rb").read()
        if not data:
            return ""
        tail = data[-8000:]
        try:
            return tail.decode("utf-8")
        except UnicodeDecodeError:
            return tail.decode("gbk", errors="replace")
    except Exception:
        return ""


def main():
    # exe 模式下 cwd 可能很怪（比如 C:\Windows\System32），先切成 exe 所在目录
    here = _script_dir()
    try:
        os.chdir(here)
    except OSError:
        pass

    # 已有面板在跑：直接开浏览器，不再起第二个
    if _port_busy(PANEL_PORT):
        ident = _panel_identity(PANEL_PORT)
        if ident and ident.get("wechat_app") == "wechat-ai-companion":
            webbrowser.open("http://127.0.0.1:%d/" % PANEL_PORT)
            return 0
        _msgbox(
            "端口 %d 已被别的程序占用，面板无法启动。\n\n"
            "请先关掉占用该端口的程序，或改端口后重试。\n"
            "（如需改端口，编辑 ui_server.py 顶部的 PORT 常量）" % PANEL_PORT,
            style=0x10)
        return 1

    project = _find_project_dir(here)
    if not project:
        _msgbox(
            "没找到项目文件 ui_server.py。\n\n"
            "请把本程序放在「微信AI陪聊」文件夹里，\n"
            "或与 ui_server.py 放在同一目录。\n\n"
            "当前目录：\n%s" % here,
            style=0x10)
        return 1

    py = _find_python(project)
    if not py:
        _msgbox(
            "没找到可用的 Python 解释器。\n\n"
            "请把内嵌 Python 放到项目下的 python\\python.exe，\n"
            "或在项目里建 venv，或安装 Python 到系统 PATH。\n\n"
            "项目目录：\n%s" % project,
            style=0x10)
        return 1

    ui = os.path.join(project, "ui_server.py")
    # _internal 里也有一份 ui_server.py（打包兜底）；项目目录里没有时用它
    if not os.path.isfile(ui):
        mp = _meipass()
        if mp and os.path.isfile(os.path.join(mp, "ui_server.py")):
            ui = os.path.join(mp, "ui_server.py")
    if not os.path.isfile(ui):
        _msgbox("项目目录里没有 ui_server.py：\n%s" % project, style=0x10)
        return 1
    startup_log = os.path.join(project, "ui_server_startup.log")

    # 明确告诉面板「项目目录是哪个」，避免它把 _internal 当成项目目录
    env = dict(os.environ)
    env["WECHAT_BOT_DIR"] = project

    # 隐藏窗口拉起面板（pythonw 优先，避免黑框）
    runner = py
    pyw = os.path.join(os.path.dirname(py), "pythonw.exe")
    if os.path.isfile(pyw):
        runner = pyw

    CREATE_NO_WINDOW = 0x08000000
    try:
        subprocess.Popen(
            [runner, ui],
            cwd=project,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=CREATE_NO_WINDOW,
        )
    except Exception as e:
        _msgbox("启动失败：%r\n\n项目目录：\n%s" % (e, project), style=0x10)
        return 1

    if _wait_port(PANEL_PORT, timeout=25):
        webbrowser.open("http://127.0.0.1:%d/" % PANEL_PORT)
        return 0

    # 启动失败：把日志尾部摆出来，不让用户猜
    log = _tail(startup_log)
    _msgbox(
        "控制面板启动超时（端口 %d 没起来）。\n\n"
        "解释器：%s\n项目目录：%s\n\n"
        "启动日志尾部：\n%s" % (PANEL_PORT, py, project, log[-1200:] or "（空）"),
        style=0x10)
    return 1


if __name__ == "__main__":
    sys.exit(main())
