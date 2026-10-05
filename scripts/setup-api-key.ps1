# 微信 AI 陪聊 · 2.0 —— API 密钥迁移工具
# 作用：把 config.json 里的明文密钥搬到「用户级环境变量」，
#       之后 config.json 只留空值，密钥不会随文件/截图/exe 泄露。
#
# 用法：右键 -> 使用 PowerShell 运行（或双击本文件）
# 重复运行是安全的：已经是占位符时会让你手动输入新密钥。

$ErrorActionPreference = "Stop"
$here = Split-Path -Parent $MyInvocation.MyCommand.Path
$cfgPath = Join-Path $here "config.json"

function Set-UserEnv($name, $value) {
    [Environment]::SetEnvironmentVariable($name, $value, "User")
    # 同时在当前会话生效，方便立刻验证
    [Environment]::SetEnvironmentVariable($name, $value, "Process")
}

Write-Host "==============================================" -ForegroundColor Cyan
Write-Host " 微信 AI 陪聊 2.0 · API 密钥迁移到环境变量" -ForegroundColor Cyan
Write-Host "==============================================" -ForegroundColor Cyan
Write-Host ""

if (-not (Test-Path $cfgPath)) {
    Write-Host "找不到 config.json：$cfgPath" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

$cfg = Get-Content $cfgPath -Raw -Encoding UTF8 | ConvertFrom-Json

function Is-Real($k) {
    if ([string]::IsNullOrWhiteSpace($k)) { return $false }
    $k = $k.Trim()
    if ($k.StartsWith("sk-xxxx") -or $k.StartsWith("YOUR_API_KEY")) { return $false }
    return $true
}

$aiKey  = $cfg.ai.api_key
$visKey = $cfg.vision.api_key

# 视觉 Key 如果和对话 Key 一样，就共用同一个环境变量
$migrated = @()

if (Is-Real $aiKey) {
    Set-UserEnv "DEEPSEEK_API_KEY" $aiKey
    $migrated += "DEEPSEEK_API_KEY"
    Write-Host "[OK] 对话 Key  -> 环境变量 DEEPSEEK_API_KEY" -ForegroundColor Green
} else {
    Write-Host "[--] config.json 里没有可用的对话 Key，跳过" -ForegroundColor Yellow
}

if ((Is-Real $visKey) -and ($visKey -ne $aiKey)) {
    Set-UserEnv "DEEPSEEK_VISION_KEY" $visKey
    $migrated += "DEEPSEEK_VISION_KEY"
    Write-Host "[OK] 视觉 Key  -> 环境变量 DEEPSEEK_VISION_KEY" -ForegroundColor Green
} elseif (Is-Real $visKey) {
    Write-Host "[--] 视觉 Key 与对话 Key 相同，共用 DEEPSEEK_API_KEY" -ForegroundColor Yellow
} else {
    Write-Host "[--] config.json 里没有可用的视觉 Key，跳过" -ForegroundColor Yellow
}

if ($migrated.Count -eq 0) {
    Write-Host ""
    Write-Host "没有可迁移的密钥。若要手动设置，请运行：" -ForegroundColor Yellow
    Write-Host '  setx DEEPSEEK_API_KEY "sk-你的密钥"' -ForegroundColor Gray
    Read-Host "按回车退出"
    exit 0
}

# 从注册表回读校验，确保真的写进去了
Write-Host ""
Write-Host "校验写入结果..." -ForegroundColor Cyan
$okAll = $true
foreach ($n in $migrated) {
    $v = [Environment]::GetEnvironmentVariable($n, "User")
    if ($v) {
        $mask = $v.Substring(0, [Math]::Min(6, $v.Length)) + "..." + $v.Substring($v.Length - 4)
        Write-Host "  $n = $mask" -ForegroundColor Green
    } else {
        Write-Host "  $n 写入失败！" -ForegroundColor Red
        $okAll = $false
    }
}

if (-not $okAll) {
    Write-Host ""
    Write-Host "校验未通过，config.json 保持原样（未改动）。" -ForegroundColor Red
    Read-Host "按回车退出"
    exit 1
}

# 校验通过 -> 把 config.json 里的明文清空
Write-Host ""
Write-Host "清空 config.json 中的明文密钥（原文件备份为 config.json.bak）..." -ForegroundColor Cyan
Copy-Item $cfgPath "$cfgPath.bak" -Force
$cfg.ai.api_key = ""
$cfg.vision.api_key = ""
$json = $cfg | ConvertTo-Json -Depth 8
[System.IO.File]::WriteAllText($cfgPath, $json, (New-Object System.Text.UTF8Encoding($false)))

Write-Host ""
Write-Host "迁移完成！" -ForegroundColor Green
Write-Host ""
Write-Host "注意：环境变量需要『新开的进程』才能读到。" -ForegroundColor Yellow
Write-Host "  请完全退出控制面板后，重新双击桌面快捷方式打开。" -ForegroundColor Yellow
Write-Host "  面板体检里『API 密钥』一项应显示来源为「环境变量」。" -ForegroundColor Yellow
Write-Host ""
Read-Host "按回车退出"
