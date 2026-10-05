# -*- coding: utf-8 -*-
"""路径与解释器解析（2.0）

原则：**不硬编码任何机器专属路径**。所有路径要么从当前文件位置推导，
要么从环境变量读取，要么按候选列表探测。

这样 2.0 的 exe / 项目副本可以整体拷到任意目录、任意电脑上运行。
"""

import os
import shutil
import sys

# ---------------------------------------------------------------------------
# 目录定位
# ---------------------------------------------------------------------------


def base_dir():
    """项目根目录。

    - 源码运行：本文件所在目录的上一级（core/ 的父目录）
    - 打包成 exe：exe 所在目录（数据文件跟 exe 走，便于用户就地改配置）
    """
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    here = os.path.dirname(os.path.abspath(__file__))
    return os.path.dirname(here)


def meipass():
    """PyInstaller 解压目录（onedir 下通常为空）。"""
    p = getattr(sys, "_MEIPASS", None)
    return p if p and os.path.isdir(p) else None


def resource(*parts):
    """查找内置资源：优先项目目录，其次打包解压目录。"""
    bd = base_dir()
    p = os.path.join(bd, *parts)
    if os.path.exists(p):
        return p
    mp = meipass()
    if mp:
        p2 = os.path.join(mp, *parts)
        if os.path.exists(p2):
            return p2
    return p  # 返回首选路径，调用方自行处理不存在的情况


def data_dir():
    return os.path.join(base_dir(), "data")


def log_dir():
    return os.path.join(base_dir(), "logs")


def ensure_dir(path):
    try:
        os.makedirs(path, exist_ok=True)
    except OSError:
        pass
    return path


# ---------------------------------------------------------------------------
# Python 解释器解析（取代硬编码的 VENV_PYTHON）
# ---------------------------------------------------------------------------

_PY_CANDIDATES = (
    # 1) 随发行包分发的内嵌 Python（推荐：真正做到免安装）
    os.path.join("python", "python.exe"),
    os.path.join("runtime", "python.exe"),
    "python.exe",
    # 2) 项目内虚拟环境
    os.path.join(".venv", "Scripts", "python.exe"),
    os.path.join("venv", "Scripts", "python.exe"),
    os.path.join("env", "Scripts", "python.exe"),
)

# 环境变量名：用户想指定解释器时设它即可
ENV_PYTHON = "WECHAT_BOT_PYTHON"


def find_python(prefer_windowless=True):
    """按优先级解析 Python 解释器路径，返回 (路径, 来源说明)。

    顺序：
      1. 环境变量 WECHAT_BOT_PYTHON
      2. 项目内内嵌 python / venv
      3. 打包场景：exe 自己就是解释器入口（但 exe 跑不了 bot，见下）
      4. 系统 PATH

    注：exe（frozen）不能直接当 bot 解释器用——bot 需要 wechatauto 等第三方包，
    exe 里没有。所以要显式找外部解释器。
    """
    bd = base_dir()

    # 1) 环境变量
    env = os.environ.get(ENV_PYTHON, "").strip().strip('"')
    if env and os.path.isfile(env):
        return env, "环境变量 %s" % ENV_PYTHON

    # 2) 项目内候选
    for rel in _PY_CANDIDATES:
        p = os.path.join(bd, rel)
        if os.path.isfile(p):
            if prefer_windowless:
                pw = os.path.join(os.path.dirname(p), "pythonw.exe")
                if os.path.isfile(pw):
                    return pw, "项目内 %s" % rel
            return p, "项目内 %s" % rel

    # 3) 已知托管环境（本机历史路径，作为兼容兜底，不作为首选）
    legacy = os.path.join(
        os.path.expanduser("~"), ".workbuddy", "binaries", "python",
        "envs", "wechat", "Scripts", "python.exe")
    if os.path.isfile(legacy):
        pw = os.path.join(os.path.dirname(legacy), "pythonw.exe")
        return (pw if os.path.isfile(pw) else legacy), "WorkBuddy 托管环境"

    # 4) 系统 PATH
    for name in ("pythonw.exe", "python.exe"):
        hit = shutil.which(name)
        if hit:
            return hit, "系统 PATH"

    return None, "未找到"


def python_hint():
    """找不到解释器时给用户的排障提示。"""
    return (
        "未找到可用的 Python 解释器。\n\n"
        "请任选一种方式解决：\n"
        "  1. 把内嵌 Python 放到  %s\\python\\python.exe\n"
        "  2. 在项目里建虚拟环境  .venv\n"
        "  3. 设置环境变量 %s 指向 python.exe\n"
        "  4. 安装 Python 并加入系统 PATH\n" % (base_dir(), ENV_PYTHON)
    )
