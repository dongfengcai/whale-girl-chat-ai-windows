#Requires -Version 5.1
<#
    start.ps1 -- 启动 AstrBot + NapCat

    两个进程都用隐藏窗口在后台跑，日志写到 logs\ 目录。
    关闭这个窗口不会影响它们；要停请用 停止.cmd。
#>
[CmdletBinding()]
param(
    [switch]$Foreground
)

. (Join-Path $PSScriptRoot 'lib\common.ps1')

Write-Banner '鲸鱼娘聊天AI · 启动'

# --- 前置检查 ---------------------------------------------------------------
$ready = $true
if (-not (Test-Path $PyExe))    { Write-Bad "没有 Python：$PyExe";    $ready = $false }
if (-not (Test-Path $AppEntry)) { Write-Bad "没有 AstrBot：$AppEntry"; $ready = $false }
if (-not $ready) {
    Stop-WithError '还没装好。请先运行「安装.cmd」。'
}

# 端口被外部程序占着（最常见是 SSH 隧道）就先说清楚，不然只会看到一堆报错
$busy = @(Get-BusyPort -Ports 6185, 6199, 6099)
if ($busy.Count -gt 0) {
    Show-BusyPortHint -Busy $busy
    Write-Host ''
}

New-Item -ItemType Directory -Path $DirLogs -Force | Out-Null
$logAstrBot = Join-Path $DirLogs 'astrbot.log'
$errAstrBot = Join-Path $DirLogs 'astrbot.err.log'
$logNapCat  = Join-Path $DirLogs 'napcat.log'

$running = Get-PackageProcess
$astrbotUp = $false
$napcatUp  = $false
foreach ($p in $running) {
    if ($p.ProcessName -like 'python*') { $astrbotUp = $true }
    if ($p.ProcessName -like 'QQ*' -or $p.ProcessName -like '*NapCat*') { $napcatUp = $true }
}

# ============================================================================
#  AstrBot
# ============================================================================
Write-Step 'AstrBot'
if ($astrbotUp) {
    Write-Ok '已经在运行了，跳过'
} else {
    if (Test-Path $logAstrBot) {
        # 每次启动前把旧日志挪走，方便看「这一次」的初始密码
        $bak = Join-Path $DirLogs ('astrbot-' + (Get-Date -Format 'MMdd-HHmmss') + '.log')
        Move-Item $logAstrBot $bak -Force -ErrorAction SilentlyContinue
        Write-Dim "上一次的日志存为 $(Split-Path $bak -Leaf)"
    }
    if ($Foreground) {
        Write-Dim '前台运行（Ctrl+C 停止）'
        Push-Location $DirApp
        & $PyExe 'main.py'
        Pop-Location
        exit 0
    }
    $p = Start-Process -FilePath $PyExe -ArgumentList @('main.py') -WorkingDirectory $DirApp `
        -RedirectStandardOutput $logAstrBot -RedirectStandardError $errAstrBot `
        -WindowStyle Hidden -PassThru
    Write-Ok "已启动（PID $($p.Id)）"
}

# ============================================================================
#  NapCat
# ============================================================================
Write-Step 'NapCat'
$napcatBat = Join-Path $DirNapCat 'napcat.bat'
if (-not (Test-Path $napcatBat)) {
    Write-Warn "没找到 $napcatBat —— NapCat 没装上，QQ 消息收不到"
    Write-Dim '重新运行「安装.cmd」补装，或手动下载 NapCat.Shell.zip 解压到 napcat 目录'
} elseif ($napcatUp) {
    Write-Ok '已经在运行了，跳过'
} else {
    # NapCat 需要自己的控制台窗口（要显示二维码/登录提示），最小化启动
    Start-Process -FilePath 'cmd.exe' -ArgumentList '/c', "`"$napcatBat`" > `"$logNapCat`" 2>&1" `
        -WorkingDirectory $DirNapCat -WindowStyle Minimized | Out-Null
    Write-Ok '已启动（窗口已最小化，日志也写到 logs\napcat.log）'
}

# ============================================================================
#  等待就绪
# ============================================================================
Write-Step '等待服务就绪'
Write-Dim 'AstrBot 首次启动要初始化数据库，最多等 90 秒'

$panelOk = $false
for ($i = 0; $i -lt 45; $i++) {
    if (Test-Port -Port 6185) { $panelOk = $true; break }
    Start-Sleep -Seconds 2
    Write-Host '.' -NoNewline -ForegroundColor DarkGray
}
Write-Host ''

if ($panelOk) { Write-Ok 'AstrBot 管理面板已就绪' }
else { Write-Warn '90 秒内面板还没起来。可能只是慢，看一下 logs\astrbot.log 的最后几行' }

$onebotOk = Test-Port -Port 6199
if ($onebotOk) { Write-Ok 'OneBot v11 端口 6199 已监听（等 NapCat 连过来）' }
else { Write-Warn '6199 还没监听。如果是首次安装，正常 —— 面板里平台配置建好后重启一次才会监听' }

# ============================================================================
#  提示
# ============================================================================
Write-Banner '启动完成'

# 从日志里把初始密码和 NapCat 登录密钥捞出来，省得用户自己翻
$pwdLine = $null
if (Test-Path $logAstrBot) {
    $pwdLine = Select-String -Path $logAstrBot -Pattern 'password|密码' -Encoding UTF8 -ErrorAction SilentlyContinue |
        Select-Object -First 1
}
$tokenLine = $null
if (Test-Path $logNapCat) {
    $tokenLine = Select-String -Path $logNapCat -Pattern 'Token|token' -Encoding UTF8 -ErrorAction SilentlyContinue |
        Select-Object -First 1
}

Write-Host ''
Write-Host '  管理面板    http://127.0.0.1:6185' -ForegroundColor White
Write-Host '  用户名      astrbot' -ForegroundColor White
if ($pwdLine) { Write-Host "  初始密码    $($pwdLine.Line.Trim())" -ForegroundColor Yellow }
else { Write-Host '  初始密码    见 logs\astrbot.log（搜 password）' -ForegroundColor DarkGray }

Write-Host ''
Write-Host '  QQ 登录     http://127.0.0.1:6099/webui' -ForegroundColor White
if ($tokenLine) { Write-Host "  登录密钥    $($tokenLine.Line.Trim())" -ForegroundColor Yellow }
else { Write-Host '  登录密钥    见 logs\napcat.log（搜 token）' -ForegroundColor DarkGray }
Write-Dim '扫码登录后，跑一次「配置对接.cmd」把 NapCat 接到 AstrBot'

Write-Host ''
Write-Host '  按回车键关闭这个窗口（机器人继续在后台跑）...' -ForegroundColor DarkGray
try { $null = Read-Host } catch { }
