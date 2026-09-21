# ============================================================================
#  lib\common.ps1 -- 公共部分：路径、输出、代理、下载
#
#  被 安装.ps1 / 启动.ps1 / 停止.ps1 / 状态.ps1 / 配置对接.ps1 引用。
#  这个文件必须保存成「UTF-8 带 BOM」，否则 Windows PowerShell 5.1 会按 GBK
#  解码，中文全变乱码。
# ============================================================================

$ErrorActionPreference = 'Stop'

# --- 子进程输出编码 ---------------------------------------------------------
# Python 往管道里写中文时用的是系统本地编码（中文 Windows 上是 cp936），
# 而 PowerShell 默认按 UTF-8 解 —— 不处理这一步，中文路径和中文人设名会变成
# 一串问号。两头都设上：让 Python 说 UTF-8，也让 PowerShell 按 UTF-8 听。
try { $env:PYTHONIOENCODING = 'utf-8' } catch { }
try {
    [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false
    $OutputEncoding = New-Object System.Text.UTF8Encoding $false
} catch { }

# --- 目录结构 ---------------------------------------------------------------
#  $PSScriptRoot 是 lib\，上一级才是包根目录
$script:Root       = Split-Path -Parent $PSScriptRoot
$script:DirRuntime = Join-Path $Root 'runtime'
$script:DirPython  = Join-Path $DirRuntime 'python'
$script:PyExe      = Join-Path $DirPython 'tools\python.exe'
$script:DirApp     = Join-Path $Root 'app\AstrBot'
$script:AppEntry   = Join-Path $DirApp 'main.py'
$script:DirData    = Join-Path $DirApp 'data'
$script:DirPlugins = Join-Path $Root 'plugins'
$script:DirNapCat  = Join-Path $Root 'napcat'
$script:DirLogs    = Join-Path $Root 'logs'
$script:EnvFile    = Join-Path $Root 'config.env'
$script:PersonaFile = Join-Path $Root 'persona-whale-girl.md'

# --- 版本（可以用 config.env 覆盖）------------------------------------------
$script:VerPython  = '3.12.10'
$script:VerAstrBot = 'v4.28.1'
$script:VerNapCat  = 'v4.18.28'

# --- 输出 -------------------------------------------------------------------
function Write-Banner {
    param([string]$Text)
    Write-Host ''
    Write-Host '  ============================================================' -ForegroundColor Cyan
    Write-Host "    $Text" -ForegroundColor Cyan
    Write-Host '  ============================================================' -ForegroundColor Cyan
}

function Write-Step { param([string]$Text) Write-Host ''; Write-Host "  ==> $Text" -ForegroundColor Cyan }
function Write-Ok   { param([string]$Text) Write-Host "      [OK] $Text" -ForegroundColor Green }
function Write-Warn { param([string]$Text) Write-Host "      [!!] $Text" -ForegroundColor Yellow }
function Write-Bad  { param([string]$Text) Write-Host "      [XX] $Text" -ForegroundColor Red }
function Write-Dim  { param([string]$Text) Write-Host "           $Text" -ForegroundColor DarkGray }

function Stop-WithError {
    param([string]$Text)
    Write-Host ''
    Write-Bad $Text
    Write-Host ''
    Write-Host '  按回车键关闭这个窗口...' -ForegroundColor DarkGray
    try { $null = Read-Host } catch { }
    exit 1
}

# --- 代理 -------------------------------------------------------------------
# git / curl / PowerShell 都不会自动使用 Windows 的「系统代理」，
# 而浏览器会 —— 所以会出现「浏览器能上网、脚本下载失败」。
function Get-SystemProxy {
    if ($script:ProxyUrl) { return $script:ProxyUrl }
    try {
        $ie = Get-ItemProperty 'HKCU:\Software\Microsoft\Windows\CurrentVersion\Internet Settings' -ErrorAction Stop
        if ($ie.ProxyEnable -eq 1 -and $ie.ProxyServer) {
            $srv = [string]$ie.ProxyServer
            if ($srv -match '=') {
                $m = [regex]::Match($srv, '(?:https?|socks)=([^;]+)')
                if ($m.Success) { $srv = $m.Groups[1].Value }
            }
            if ($srv -notmatch '://') { $srv = "http://$srv" }
            $script:ProxyUrl = $srv
        }
    } catch { }
    return $script:ProxyUrl
}

# --- 下载 -------------------------------------------------------------------
# 依次尝试多个地址，第一个成功即返回。国内直连 GitHub 经常不通，
# 所以备用地址（加速镜像）是必需的不是可选的。
function Invoke-Download {
    param(
        [Parameter(Mandatory)][string[]]$Urls,
        [Parameter(Mandatory)][string]$Dest,
        [string]$Title = '',
        [long]$MinBytes = 1024
    )

    $dir = Split-Path -Parent $Dest
    if ($dir -and -not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir -Force | Out-Null }
    if (Test-Path $Dest) { Remove-Item $Dest -Force -ErrorAction SilentlyContinue }

    $proxy = Get-SystemProxy
    $tmp = "$Dest.part"
    $n = 0

    foreach ($url in $Urls) {
        $n++
        if ($Title) { Write-Host "      下载 $Title  ($n/$($Urls.Count))" -NoNewline }
        else        { Write-Host "      下载  ($n/$($Urls.Count))" -NoNewline }
        Write-Host "  $url" -ForegroundColor DarkGray

        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }

        $ok = $false
        $curl = Get-Command curl.exe -ErrorAction SilentlyContinue
        if ($curl) {
            # curl 比 Invoke-WebRequest 快得多，而且有进度条
            $curlArgs = @('-L', '--fail', '--silent', '--show-error', '--connect-timeout', '20', '-o', $tmp)
            if ($proxy) { $curlArgs += @('-x', $proxy) }
            $curlArgs += $url
            & $curl.Source @curlArgs 2>&1 | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray }
            if ($LASTEXITCODE -eq 0 -and (Test-Path $tmp)) { $ok = $true }
        }
        if (-not $ok) {
            try {
                $iwrArgs = @{ Uri = $url; OutFile = $tmp; UseBasicParsing = $true; TimeoutSec = 600 }
                if ($proxy) { $iwrArgs['Proxy'] = $proxy }
                $old = $ProgressPreference
                $ProgressPreference = 'SilentlyContinue'
                Invoke-WebRequest @iwrArgs
                $ProgressPreference = $old
                $ok = $true
            } catch {
                Write-Host "        $($_.Exception.Message)" -ForegroundColor DarkGray
            }
        }

        if ($ok -and (Test-Path $tmp)) {
            $len = (Get-Item $tmp).Length
            if ($len -ge $MinBytes) {
                Move-Item $tmp $Dest -Force
                Write-Ok "完成：$([math]::Round($len / 1MB, 1)) MB"
                return $true
            }
            Write-Host "        文件只有 $len 字节，不像完整的包，换下一个源" -ForegroundColor DarkGray
            Remove-Item $tmp -Force -ErrorAction SilentlyContinue
        }
    }

    Write-Bad "所有下载地址都失败了：$Title"
    Write-Dim "多半是网络问题。如果浏览器能打开网页但这里不行，"
    Write-Dim "说明系统代理没被脚本读到 —— 在 config.env 里加一行 PROXY=http://127.0.0.1:7897"
    return $false
}

# --- 解压 -------------------------------------------------------------------
function Expand-ZipTo {
    param(
        [Parameter(Mandatory)][string]$Zip,
        [Parameter(Mandatory)][string]$Dest
    )
    if (Test-Path $Dest) { Remove-Item $Dest -Recurse -Force -ErrorAction SilentlyContinue }
    New-Item -ItemType Directory -Path $Dest -Force | Out-Null
    Add-Type -AssemblyName System.IO.Compression.FileSystem -ErrorAction SilentlyContinue
    [System.IO.Compression.ZipFile]::ExtractToDirectory($Zip, $Dest)
}

# --- config.env -------------------------------------------------------------
# 简单的 KEY=VALUE 文件，不含引号，方便反复重跑安装脚本。
function Read-EnvFile {
    param([string]$Path = $script:EnvFile)
    $h = @{}
    if (-not (Test-Path $Path)) { return $h }
    foreach ($line in Get-Content $Path -Encoding UTF8) {
        $t = $line.Trim()
        if (-not $t -or $t.StartsWith('#')) { continue }
        $i = $t.IndexOf('=')
        if ($i -lt 1) { continue }
        $h[$t.Substring(0, $i).Trim()] = $t.Substring($i + 1).Trim()
    }
    return $h
}

function Write-EnvFile {
    param([hashtable]$Values, [string]$Path = $script:EnvFile)
    $lines = @(
        '# 鲸鱼娘聊天AI（Windows 版）配置',
        '# 改完直接重新运行 安装.cmd 就会生效，不需要重新下载。',
        ''
    )
    foreach ($k in ($Values.Keys | Sort-Object)) {
        $lines += "$k=$($Values[$k])"
    }
    [System.IO.File]::WriteAllLines($Path, $lines, (New-Object System.Text.UTF8Encoding($false)))
}

# --- 环境自检 ---------------------------------------------------------------
function Test-Prerequisites {
    if ([Environment]::Is64BitOperatingSystem -eq $false) {
        Stop-WithError '这个包只支持 64 位 Windows。'
    }
    $v = [Environment]::OSVersion.Version
    if ($v.Major -lt 10) {
        Stop-WithError "需要 Windows 10 或更高版本，当前是 $v"
    }
    Write-Ok "系统：Windows $($v.Major).$($v.Minor) Build $($v.Build)  64 位"
}

# --- 进程与端口 -------------------------------------------------------------
# 只认「可执行文件位于本包目录内」的进程，避免误杀用户自己开的 QQ。
function Get-PackageProcess {
    $prefix = $script:Root
    Get-Process -ErrorAction SilentlyContinue | Where-Object {
        $p = $null
        try { $p = $_.Path } catch { $p = $null }
        if (-not $p) { return $false }
        return $p.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)
    }
}

function Test-Port {
    param([Parameter(Mandatory)][int]$Port, [int]$TimeoutMs = 800)
    $client = New-Object System.Net.Sockets.TcpClient
    try {
        $iar = $client.BeginConnect('127.0.0.1', $Port, $null, $null)
        $done = $iar.AsyncWaitHandle.WaitOne($TimeoutMs, $false)
        if ($done) { $client.EndConnect($iar) }
        return $done
    } catch {
        return $false
    } finally {
        $client.Close()
    }
}

function Get-PortOwnerPid {
    param([Parameter(Mandatory)][int]$Port)
    # 优先用 NetTCPConnection；某些精简系统没有 NetTCPIP 模块，就退回解析 netstat
    try {
        $conn = Get-NetTCPConnection -LocalPort $Port -State Listen -ErrorAction Stop | Select-Object -First 1
        if ($conn) { return $conn.OwningProcess }
    } catch { }
    try {
        $hit = netstat -ano | Select-String "LISTENING" | Where-Object { $_.Line -match "[:.]$Port\s" } | Select-Object -First 1
        if ($hit) {
            $parts = @(($hit.Line -split "\s+") | Where-Object { $_ })
            $last = $parts[-1]
            if ($last -match "^\d+$") { return [int]$last }
        }
    } catch { }
    return $null
}

# 端口被别的程序占了，AstrBot / NapCat 会起不来并报一堆看不懂的错。
# 最常见的元凶：之前用 SSH 隧道连服务器上的面板，隧道的 6099 / 6185 还在本机监听。
function Get-BusyPort {
    param([Parameter(Mandatory)][int[]]$Ports)
    $busy = @()
    foreach ($p in $Ports) {
        if (Test-Port -Port $p) {
            $ownerPid = Get-PortOwnerPid -Port $p
            $ownerName = ''
            if ($ownerPid) {
                $proc = Get-Process -Id $ownerPid -ErrorAction SilentlyContinue
                if ($proc) { $ownerName = $proc.ProcessName }
            }
            $busy += [pscustomobject]@{ Port = $p; Pid = $ownerPid; Name = $ownerName }
        }
    }
    return $busy
}

function Show-BusyPortHint {
    param([Parameter(Mandatory)][array]$Busy)
    Write-Warn '这些端口已经被别的程序占用了：'
    foreach ($b in $Busy) {
        Write-Dim "$($b.Port)  <-  $($b.Name) (PID $($b.Pid))"
    }
    $ssh = @($Busy | Where-Object { $_.Name -eq 'ssh' })
    if ($ssh.Count -gt 0) {
        Write-Host ''
        Write-Host '      占用者是 ssh —— 你之前应该是用 SSH 隧道连服务器上的面板。' -ForegroundColor Yellow
        Write-Host '      先把隧道关掉（关掉那个 tunnel 窗口，或结束上面那个 PID），' -ForegroundColor Yellow
        Write-Host '      否则 Windows 版抢不到端口，会启动失败。' -ForegroundColor Yellow
        Write-Host '      结束隧道： taskkill /PID <上面的PID> /F' -ForegroundColor DarkGray
    }
}
