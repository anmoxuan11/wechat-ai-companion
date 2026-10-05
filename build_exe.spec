# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 打包配置：微信AI陪聊控制面板.exe

产出：dist/微信AI陪聊控制面板.exe（单文件，双击即用）

打包内容：
  - launcher.py 作为入口
  - ui_server.py / assets/ 作为数据文件嵌进去（exe 旁有则优先用同级的）
  - comtypes 的 COM 类型库源码（关键！uiautomation 依赖它，
    PyInstaller 默认会把 gen/*.py 当源码处理掉，必须显式当数据文件带上）

不打包：main.py 及其依赖（bot 本体仍由项目目录里的 Python 跑）。
       这样 exe 保持轻量（~15MB），且 bot 出问题时好排查。
"""

import os
import sys
from PyInstaller.utils.hooks import collect_submodules, collect_data_files

PROJECT = os.path.abspath(os.path.dirname(SPEC))  # noqa: F821
SP = os.path.join(sys.prefix, "Lib", "site-packages")

datas = [
    # 面板后端脚本：让 exe 能独立跑（项目目录缺失时兜底）
    (os.path.join(PROJECT, "ui_server.py"), "."),
    # 前端资源统一在 assets/：骨架页 + 三个静态资源
    # （3.0 新增 extra.js：洞察 / 记忆库 / 调教三个标签页）
    (os.path.join(PROJECT, "assets", "dashboard.html"), "assets"),
    (os.path.join(PROJECT, "assets", "style.css"), "assets"),
    (os.path.join(PROJECT, "assets", "app.js"), "assets"),
    (os.path.join(PROJECT, "assets", "extra.js"), "assets"),
    # 3.0：面板自己要能跑「回复预演」和「记忆沉淀」，离不开这两个模块
    (os.path.join(PROJECT, "ai_client.py"), "."),
    (os.path.join(PROJECT, "session.py"), "."),
]

# 2.0：core/ 与 web/ 是新的公共模块包
for pkg in ("core", "web"):
    pkg_dir = os.path.join(PROJECT, pkg)
    if os.path.isdir(pkg_dir):
        for fn in os.listdir(pkg_dir):
            if fn.endswith(".py"):
                datas.append((os.path.join(pkg_dir, fn), pkg))

# 3.0：memory/ 子系统（摘要 / 事实 / 后台补跑）
mem_dir = os.path.join(PROJECT, "memory")
if os.path.isdir(mem_dir):
    for fn in os.listdir(mem_dir):
        if fn.endswith(".py"):
            datas.append((os.path.join(mem_dir, fn), "memory"))

# comtypes 的 COM 类型库生成源码：uiautomation 初始化 UIAutomationCore 时要用。
# 若只带 __pycache__ 而不带 .py，comtypes 会走「内存生成」分支——本机实测可用，
# 但带上源码是更稳的做法（省去每次启动重新解析类型库）。
comtypes_gen = os.path.join(SP, "comtypes", "gen")
if os.path.isdir(comtypes_gen):
    for fn in os.listdir(comtypes_gen):
        if fn.endswith(".py"):
            datas.append((os.path.join(comtypes_gen, fn), os.path.join("comtypes", "gen")))

# uiautomation 自带的 UIAutomationClient DLL（64/32 位各一份）
uia_bin = os.path.join(SP, "uiautomation", "bin")
if os.path.isdir(uia_bin):
    for fn in os.listdir(uia_bin):
        datas.append((os.path.join(uia_bin, fn), os.path.join("uiautomation", "bin")))

hiddenimports = [
    "comtypes",
    "comtypes.client",
    "comtypes.gen",
    "comtypes.stream",
    "comtypes.typeinfo",
    "comtypes.automation",
    "comtypes.persist",
    "webbrowser",
    # 2.0 新增模块（ui_server 运行时动态 import，PyInstaller 静态分析看不到）
    "core", "core.paths", "core.atomicio", "core.proclog",
    "core.security", "core.instance",
    "web", "web.state",
]

# 2.0：core/web 下的子模块显式收集，避免漏打
for _pkg in ("core", "web"):
    if os.path.isdir(os.path.join(PROJECT, _pkg)):
        hiddenimports += collect_submodules(_pkg)

# comtypes.gen 下的生成模块名字是动态的（UIAutomationClient 等），显式收集
hiddenimports += collect_submodules("comtypes.gen")


a = Analysis(  # noqa: F821
    [os.path.join(PROJECT, "launcher.py")],
    pathex=[PROJECT],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # 明确排除 bot 本体与重型科学计算库，控制体积
    excludes=[
        "numpy", "pandas", "matplotlib", "scipy",
        "PyQt5", "PySide2", "tkinter", "test", "unittest",
    ],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure)  # noqa: F821

# onedir 模式（COLLECT）：不做自解压，避开 PyInstaller 6.x 在本机的
# onefile 解压缺陷（实测只解出 unicodedata.pyd 就停了）。
# 代价是产物是一个文件夹，双击里面的 exe 即可，同样零依赖。
exe = EXE(  # noqa: F821
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="微信AI陪聊控制面板",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,          # 无控制台窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=os.path.join(PROJECT, "panel.ico") if os.path.isfile(
        os.path.join(PROJECT, "panel.ico")) else None,
)

coll = COLLECT(  # noqa: F821
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="微信AI陪聊控制面板",
)
