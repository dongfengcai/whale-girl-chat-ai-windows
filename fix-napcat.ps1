#Requires -Version 5.1
<#
    fix-napcat.ps1 -- 把 NapCat 接到 AstrBot 上

    什么时候要跑：
        第一次扫码登录 QQ 之后跑一次。
        NapCat 是登录后才生成 onebot11_<QQ号>.json 的，在那之前没文件可改。

    干什么：
        在 NapCat 的配置里加一条「反向 WebSocket 客户端」，
        指向 AstrBot 的 6199 端口，并带上自动生成的 token。

    可以反复运行，不会改坏已有配置。
#>
[CmdletBinding()]
param()

. (Join-Path $PSScriptRoot 'lib\common.ps1')

Write-Banner '鲸鱼娘聊天AI · 配置对接'

# --- AstrBot 侧 -------------------------------------------------------------
Write-Step '读取 AstrBot 配置'
$mainConf = Join-Path $DirData 'cmd_config.json'
if (-not (Test-Path $mainConf)) {
    Stop-WithError "还没生成 AstrBot 配置（$mainConf）。先跑「安装.cmd」。"
}

$conf = Get-Content $mainConf -Raw -Encoding UTF8 | ConvertFrom-Json
$platform = $null
foreach ($p in $conf.platform) {
    if ($p.type -eq 'aiocqhttp') { $platform = $p; break }
}
if (-not $platform) {
    Write-Warn 'AstrBot 配置里没有 OneBot v11 平台，先补上'
    $port = 6199
    $token = ''
} else {
    $port = $platform.ws_reverse_port
    $token = $platform.ws_reverse_token
}
Write-Ok "AstrBot 在 127.0.0.1:$port 等 NapCat 连过来"
if ($token) { Write-Dim "token：$token" } else { Write-Warn 'token 是空的 —— 本机自用问题不大，但不建议' }

# --- NapCat 侧 --------------------------------------------------------------
Write-Step '查找 NapCat 配置'
$napCfgDir = Join-Path $DirNapCat 'config'
if (-not (Test-Path $napCfgDir)) {
    Stop-WithError "找不到 $napCfgDir —— NapCat 还没装好，先跑「安装.cmd」。"
}

$files = @(Get-ChildItem $napCfgDir -Filter 'onebot11*.json' -ErrorAction SilentlyContinue)
if ($files.Count -eq 0) {
    Write-Host ''
    Write-Warn '还没有 onebot11_<QQ号>.json —— 说明你还没登录 QQ。'
    Write-Host ''
    Write-Host '  顺序是这样的：' -ForegroundColor White
    Write-Host '    1. 双击「启动.cmd」' -ForegroundColor White
    Write-Host '    2. 浏览器打开 http://127.0.0.1:6099/webui ，用日志里的密钥登录，扫码' -ForegroundColor White
    Write-Host '    3. 登录成功后再跑一次本脚本' -ForegroundColor White
    Write-Host ''
    Write-Host '  按回车键关闭...' -ForegroundColor DarkGray
    try { $null = Read-Host } catch { }
    exit 0
}

$py = Join-Path $PSScriptRoot 'tools\patch_napcat.py'
foreach ($f in $files) {
    Write-Host ''
    Write-Dim "处理 $($f.Name)"
    & $PyExe $py $f.FullName $port $token 2>&1 | ForEach-Object { Write-Host "        $_" }
    if ($LASTEXITCODE -eq 0) { Write-Ok "$($f.Name) 已配置" }
    else { Write-Bad "$($f.Name) 配置失败" }
}

# --- 提示重启 ---------------------------------------------------------------
Write-Banner '完成'
Write-Host ''
Write-Host '  NapCat 需要重启才会重新读取配置。' -ForegroundColor White
Write-Host '    1. 双击「停止.cmd」' -ForegroundColor White
Write-Host '    2. 双击「启动.cmd」' -ForegroundColor White
Write-Host ''
Write-Host '  重启后跑「状态.cmd」，看到 6199 端口和插件加载记录就说明通了。' -ForegroundColor White
Write-Host ''
Write-Host '  按回车键关闭...' -ForegroundColor DarkGray
try { $null = Read-Host } catch { }
