@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  微信AI陪聊 · 一键启动控制面板
rem
rem  优先级：
rem    1. 打包好的 exe（dist\...\微信AI陪聊控制面板.exe）
rem    2. launcher.py + 项目 Python
rem    3. 面板脚本 ui_server.py
rem  找不到就报错，绝不静默失败。
rem ============================================================

set "HERE=%~dp0"
set "EXE="

rem --- 1) 找 exe ---
for %%F in (
    "%HERE%微信AI陪聊控制面板.exe"
    "%HERE%dist\微信AI陪聊控制面板\微信AI陪聊控制面板.exe"
) do (
    if not defined EXE if exist "%%~F" set "EXE=%%~F"
)

if defined EXE (
    start "" "%EXE%"
    exit /b
)

rem --- 2) 找 Python 跑 launcher.py（2.0：不再写死任何用户目录）---
set "PY="
if defined WECHAT_BOT_PYTHON if exist "%WECHAT_BOT_PYTHON%" set "PY=%WECHAT_BOT_PYTHON%"
if not defined PY if exist "%HERE%python\pythonw.exe" set "PY=%HERE%python\pythonw.exe"
if not defined PY if exist "%HERE%python\python.exe" set "PY=%HERE%python\python.exe"
if not defined PY if exist "%HERE%.venv\Scripts\pythonw.exe" set "PY=%HERE%.venv\Scripts\pythonw.exe"
if not defined PY if exist "%HERE%.venv\Scripts\python.exe" set "PY=%HERE%.venv\Scripts\python.exe"
if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
)

if defined PY if exist "%HERE%launcher.py" (
    start "" "%PY%" "%HERE%launcher.py"
    exit /b
)

rem --- 3) 兜底：直接跑 ui_server.py ---
if defined PY if exist "%HERE%ui_server.py" (
    start "" "%PY%" "%HERE%ui_server.py"
    exit /b
)

echo.
echo [!] 启动失败：没找到可执行文件或 Python 解释器
echo.
echo     当前目录：%HERE%
echo.
echo     请确认以下任一条件成立：
echo       1. dist\微信AI陪聊控制面板\ 下有 exe
echo       2. 目录下有 launcher.py 且能找到 Python
echo       3. 目录下有 ui_server.py 且能找到 Python
echo.
pause
exit /b 1
