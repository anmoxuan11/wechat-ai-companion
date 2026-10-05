@echo off
chcp 65001 >nul
cd /d "%~dp0\.."

rem 本地面板启动（GitHub 公开版）：优先用项目 venv，其次用 PATH 里的 python
set "PY="
if exist "%~dp0\..\.venv\Scripts\pythonw.exe" set "PY=%~dp0\..\.venv\Scripts\pythonw.exe"
if not defined PY if exist "%~dp0\..\.venv\Scripts\python.exe" set "PY=%~dp0\..\.venv\Scripts\python.exe"
if not defined PY (
    for /f "delims=" %%P in ('where python 2^>nul') do if not defined PY set "PY=%%P"
)
if not defined PY (
    echo [!] 找不到 Python，请先安装 Python 3.11 并加入 PATH
    pause
    exit /b 1
)

start "" "%PY%" "%~dp0\..\ui_server.py"
exit
