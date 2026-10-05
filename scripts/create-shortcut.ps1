# ============================================================
#  微信AI陪聊 · 创建桌面快捷方式
#
#  用法：右键本文件 → 「使用 PowerShell 运行」
#        或在 PowerShell 里执行：
#          powershell -ExecutionPolicy Bypass -File 创建桌面快捷方式.ps1
# ============================================================

$ErrorActionPreference = "Stop"

$here = Split-Path -Parent $MyInvocation.MyCommand.Path

# 按优先级挑启动目标
$candidates = @(
    (Join-Path $here "微信AI陪聊控制面板.exe"),
    (Join-Path $here "dist\微信AI陪聊控制面板\微信AI陪聊控制面板.exe"),
    (Join-Path $here "一键启动.bat")
)

$target = $null
foreach ($c in $candidates) {
    if (Test-Path -LiteralPath $c) { $target = $c; break }
}

if (-not $target) {
    Write-Host ""
    Write-Host "[!] 没找到可启动的文件" -ForegroundColor Red
    Write-Host ""
    Write-Host "    当前目录：$here"
    Write-Host "    请确认存在以下任一文件："
    Write-Host "      - 微信AI陪聊控制面板.exe"
    Write-Host "      - dist\微信AI陪聊控制面板\微信AI陪聊控制面板.exe"
    Write-Host "      - 一键启动.bat"
    Write-Host ""
    Read-Host "按回车退出"
    exit 1
}

$workdir = Split-Path -Parent $target
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "微信AI陪聊.lnk"

Write-Host ""
Write-Host "  正在创建桌面快捷方式..." -ForegroundColor Cyan
Write-Host "  目标：$target"
Write-Host ""

$shell = New-Object -ComObject WScript.Shell
$sc = $shell.CreateShortcut($lnkPath)
$sc.TargetPath       = $target
$sc.WorkingDirectory = $workdir
$sc.IconLocation     = "$target,0"
$sc.Description      = "启动微信 AI 陪聊控制面板"
$sc.WindowStyle      = 1
$sc.Save()

if (Test-Path -LiteralPath $lnkPath) {
    Write-Host "  完成！桌面已生成「微信AI陪聊」" -ForegroundColor Green
    Write-Host "  $lnkPath"
} else {
    Write-Host "  [!] 创建失败" -ForegroundColor Red
}

Write-Host ""
Start-Sleep -Seconds 2
