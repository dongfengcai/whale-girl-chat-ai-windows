#Requires -Version 5.1
<#
    status.ps1 -- 看一眼现在什么情况

    进程、端口、日志尾巴、插件是否加载，一次全打出来。
    出问题时先跑这个，把输出发出去基本就能定位。
#>
[CmdletBinding()]
param(
    [int]$Tail = 15
)

. (Join-Path $PSScriptRoot 'lib\common.ps1')

Write-Banner '鲸鱼娘聊天AI · 状态'

# --- 安装情况 ---------------------------------------------------------------
Write-Step '安装情况'
function Show-Item {
    param([string]$Name, [bool]$Ok, [string]$Detail = '')
    if ($Ok) { Write-Host "      [OK] $Name  $Detail" -ForegroundColor Green }
    else     { Write-Host "      [--] $Name  未安装  $Detail" -ForegroundColor DarkGray }
}
Show-Item "Python $VerPython" (Test-Path $PyExe) $(if (Test-Path $PyExe) { (('' + (& $PyExe --version 2>&1)) -join '') } else { '' })
Show-Item "AstrBot $VerAstrBot" (Test-Path $AppEntry)
Show-Item '管理面板 (data\dist)' (Test-Path (Join-Path $DirData 'dist\index.html'))
Show-Item "NapCat $VerNapCat" (Test-Path (Join-Path $DirNapCat 'napcat.bat'))

$installedPlugins = @()
if (Test-Path (Join-Path $DirData 'plugins')) {
    $installedPlugins = @(Get-ChildItem (Join-Path $DirData 'plugins') -Directory)
}
Show-Item "插件 ($($installedPlugins.Count)/6)" ($installedPlugins.Count -gt 0)

# --- 进程 -------------------------------------------------------------------
Write-Step '进程'
$procs = @(Get-PackageProcess)
if ($procs.Count -eq 0) {
    Write-Warn '没有在运行 —— 双击「启动.cmd」'
} else {
    foreach ($p in $procs) {
        $mem = [math]::Round($p.WorkingSet64 / 1MB)
        Write-Host ("      {0,-16} PID {1,-8} 内存 {2} MB  启动于 {3}" -f `
            $p.ProcessName, $p.Id, $mem, $p.StartTime.ToString('HH:mm:ss')) -ForegroundColor Green
    }
}

# --- 端口 -------------------------------------------------------------------
Write-Step '端口'
$ports = @(
    @{ Port = 6185; What = 'AstrBot 管理面板' },
    @{ Port = 6199; What = 'OneBot v11 反向 WS（NapCat 连这里）' },
    @{ Port = 6099; What = 'NapCat WebUI（扫码登录）' }
)
foreach ($x in $ports) {
    if (Test-Port -Port $x.Port) {
        $owner = Get-PortOwnerPid -Port $x.Port
        Write-Host ("      [OK] {0,-6} {1}  (PID {2})" -f $x.Port, $x.What, $owner) -ForegroundColor Green
    } else {
        Write-Host ("      [--] {0,-6} {1}" -f $x.Port, $x.What) -ForegroundColor DarkGray
    }
}

# --- 插件加载 ---------------------------------------------------------------
Write-Step 'AstrBot 日志里的插件加载情况'
$logAstrBot = Join-Path $DirLogs 'astrbot.log'
if (-not (Test-Path $logAstrBot)) {
    Write-Warn "还没有日志（$logAstrBot）"
} else {
    $loaded = @(Select-String -Path $logAstrBot -Pattern 'Loading plugin' -Encoding UTF8 -ErrorAction SilentlyContinue)
    if ($loaded.Count -gt 0) {
        Write-Ok "已加载 $($loaded.Count) 个插件"
        $loaded | Select-Object -Last 8 | ForEach-Object { Write-Dim $_.Line.Trim() }
    } else {
        Write-Warn '日志里没看到插件加载记录'
    }
    $errors = @(Select-String -Path $logAstrBot -Pattern 'Traceback|Failed to import|ERROR' -Encoding UTF8 -ErrorAction SilentlyContinue)
    if ($errors.Count -gt 0) {
        Write-Warn "日志里有 $($errors.Count) 条 ERROR/Traceback，最后 3 条："
        $errors | Select-Object -Last 3 | ForEach-Object { Write-Dim $_.Line.Trim() }
    }
}

# --- 关键信息 ---------------------------------------------------------------
Write-Step '关键信息'
if (Test-Path $logAstrBot) {
    $pwdLine = Select-String -Path $logAstrBot -Pattern 'password|密码' -Encoding UTF8 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($pwdLine) { Write-Dim "面板初始密码：$($pwdLine.Line.Trim())" }
}
$napLog = Join-Path $DirLogs 'napcat.log'
if (Test-Path $napLog) {
    $tok = Select-String -Path $napLog -Pattern 'Token' -Encoding UTF8 -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($tok) { Write-Dim "NapCat 登录密钥：$($tok.Line.Trim())" }
}

# --- 日志尾巴 ---------------------------------------------------------------
Write-Step "日志最后 $Tail 行"
foreach ($f in @($logAstrBot, (Join-Path $DirLogs 'napcat.log'))) {
    Write-Host ''
    Write-Host "      --- $(Split-Path $f -Leaf) ---" -ForegroundColor DarkGray
    if (Test-Path $f) {
        Get-Content $f -Tail $Tail -Encoding UTF8 -ErrorAction SilentlyContinue |
            ForEach-Object { Write-Host "      $_" -ForegroundColor Gray }
    } else {
        Write-Host '      （文件不存在）' -ForegroundColor DarkGray
    }
}

Write-Host ''
Write-Host '  按回车键关闭...' -ForegroundColor DarkGray
try { $null = Read-Host } catch { }
