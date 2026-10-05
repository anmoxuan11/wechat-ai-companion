@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem ============================================================
rem  微信AI陪聊 · 一键安装（生成桌面快捷方式）
rem
rem  双击本文件即可：自动找到启动目标，在桌面创建
rem  「微信AI陪聊」快捷方式，图标用 exe 自带的那个。
rem ============================================================

set "HERE=%~dp0"
set "TARGET="

if exist "%HERE%微信AI陪聊控制面板.exe" set "TARGET=%HERE%微信AI陪聊控制面板.exe"
if not defined TARGET if exist "%HERE%dist\微信AI陪聊控制面板\微信AI陪聊控制面板.exe" set "TARGET=%HERE%dist\微信AI陪聊控制面板\微信AI陪聊控制面板.exe"
if not defined TARGET if exist "%HERE%一键启动.bat" set "TARGET=%HERE%一键启动.bat"

if not defined TARGET (
    echo.
    echo [!] 没找到可启动的文件
    echo.
    echo     当前目录：%HERE%
    echo     请确认存在以下任一文件：
    echo       - 微信AI陪聊控制面板.exe
    echo       - dist\微信AI陪聊控制面板\微信AI陪聊控制面板.exe
    echo       - 一键启动.bat
    echo.
    pause
    exit /b 1
)

echo.
echo   正在创建桌面快捷方式...
echo   目标：%TARGET%
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$t='%TARGET%'; $d=Split-Path $t -Parent; $lnk=Join-Path ([Environment]::GetFolderPath('Desktop')) '微信AI陪聊.lnk'; $sh=New-Object -ComObject WScript.Shell; $sc=$sh.CreateShortcut($lnk); $sc.TargetPath=$t; $sc.WorkingDirectory=$d; $sc.IconLocation='{0},0' -f $t; $sc.Description='启动微信 AI 陪聊控制面板'; $sc.Save(); Write-Host ('   已创建: ' + $lnk)"

if %errorlevel% neq 0 (
    echo.
    echo [!] 创建失败，请手动把 exe 发送到桌面快捷方式
    pause
    exit /b 1
)

echo.
echo   完成！桌面已生成「微信AI陪聊」，双击即可启动。
echo.
timeout /t 3 >nul
exit /b 0
