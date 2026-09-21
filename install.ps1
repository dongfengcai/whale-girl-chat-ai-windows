#Requires -Version 5.1
<#
    install.ps1 -- 鲸鱼娘聊天AI · Windows 一键安装

    （文件名故意用英文：批处理文件正文里写中文文件名，
      在 GBK 代码页下会找不到文件。用户看到的是「安装.cmd」，
      它只是转调这个脚本。）

    做四件事：
      1. 下载便携版 Python 3.12（免安装、不写注册表、不需要管理员）
      2. 下载 AstrBot v4.28.1 源码和管理面板，装依赖
      3. 装 6 个插件、写好插件配置、写好 AstrBot 主配置
      4. 下载 NapCat（QQ 协议端）

    可以反复运行。已经装好的部分会自动跳过，只补缺的。

    用法：
        安装.cmd                    交互式
        安装.cmd -Yes               全默认，不提问
        安装.cmd -SkipDeps          跳过 pip 依赖安装
        安装.cmd -Proxy http://127.0.0.1:7897
#>
[CmdletBinding()]
param(
    [switch]$Yes,
    [switch]$SkipDeps,
    [string]$Proxy = ''
)

. (Join-Path $PSScriptRoot 'lib\common.ps1')

Write-Banner '鲸鱼娘聊天AI · Windows 一键安装'

# ============================================================================
#  0. 预检
# ============================================================================
Write-Step '检查环境'
Test-Prerequisites

if ($Proxy) { $script:ProxyUrl = $Proxy }

if (-not (Test-Path $DirPlugins)) { Stop-WithError "找不到 plugins 目录。请在解压后的完整目录里运行。" }
$pluginDirs = @(Get-ChildItem $DirPlugins -Directory)
if ($pluginDirs.Count -eq 0) { Stop-WithError "plugins 目录是空的，发布包不完整。" }
foreach ($p in $pluginDirs) {
    foreach ($f in @('main.py', 'metadata.yaml')) {
        if (-not (Test-Path (Join-Path $p.FullName $f))) {
            Stop-WithError "$($p.Name) 缺少 $f，发布包不完整。"
        }
    }
}
Write-Ok "插件包完整：$($pluginDirs.Count) 个插件"

if (-not (Test-Path $PersonaFile)) { Write-Warn '没找到 persona-whale-girl.md，人设要手动粘贴' }

# 端口冲突检查：SSH 隧道占着 6099/6185 是最常见的情况
$busy = @(Get-BusyPort -Ports 6185, 6199, 6099)
if ($busy.Count -gt 0) {
    Show-BusyPortHint -Busy $busy
    Write-Host ''
    if (-not $Yes) {
        Write-Host '      按回车继续安装（启动前记得先释放端口），Ctrl+C 取消...' -ForegroundColor DarkGray
        try { $null = Read-Host } catch { }
    }
} else {
    Write-Ok '端口 6185 / 6199 / 6099 都是空的'
}

$px = Get-SystemProxy
if ($px) { Write-Ok "检测到系统代理：$px（下载会自动走它）" }
else     { Write-Dim '没有检测到系统代理' }

Write-Host ''
Write-Host '  提示：安装需要下载约 1.5-2 GB 依赖，占用约 3 GB 磁盘，' -ForegroundColor DarkGray
Write-Host '        视网速需要 10-40 分钟。中途可以关掉窗口，下次运行会接着装。' -ForegroundColor DarkGray

# ============================================================================
#  1. 收集配置
# ============================================================================
Write-Step '配置'

$cfg = Read-EnvFile
if ($Proxy) { $cfg['PROXY'] = $Proxy }
if (-not $env.ContainsKey('ASTRBOT_VERSION')) { $cfg['ASTRBOT_VERSION'] = $VerAstrBot }
if (-not $env.ContainsKey('NAPCAT_VERSION'))  { $cfg['NAPCAT_VERSION']  = $VerNapCat }
if (-not $env.ContainsKey('PYTHON_VERSION'))  { $cfg['PYTHON_VERSION']  = $VerPython }

function Ask-Value {
    param([string]$Key, [string]$Prompt, [string]$Hint = '')
    if ($env.ContainsKey($Key) -and $cfg[$Key]) { return $cfg[$Key] }
    if ($Yes) { return '' }
    if ($Hint) { Write-Dim $Hint }
    $v = Read-Host "  $Prompt"
    return $v.Trim()
}

$apiKey = Ask-Value 'DEEPSEEK_API_KEY' 'DeepSeek API Key（可留空，装完在面板里补）' '用于查余额和给表情自动打标签，获取：https://platform.deepseek.com/api_keys'
$adminQq = Ask-Value 'ADMIN_QQ' '你的 QQ 号（管理员，可留空）' '用来接收余额告警、管理表情库'
$groups  = Ask-Value 'COLLECT_GROUPS' '允许收集表情的群号（留空 = 所有群，多个用逗号分隔）' ''

$cfg['DEEPSEEK_API_KEY'] = $apiKey
$cfg['ADMIN_QQ'] = $adminQq
$cfg['COLLECT_GROUPS'] = $groups
Write-EnvFile $cfg
Write-Ok "配置已保存到 config.env（以后重跑安装不会重复问）"
if (-not $apiKey) { Write-Warn '没填 API Key —— 余额熔断和表情自动打标签要用到它，之后可以在面板里补' }

$VerAstrBot = $cfg['ASTRBOT_VERSION']
$VerNapCat  = $cfg['NAPCAT_VERSION']
$VerPython  = $cfg['PYTHON_VERSION']

# ============================================================================
#  2. Python 运行时
# ============================================================================
Write-Step "Python $VerPython（便携版）"

if (Test-Path $PyExe) {
    $v = (& $PyExe --version 2>&1) -join ''
    Write-Ok "已存在：$v，跳过下载"
} else {
    Write-Dim '用 NuGet 的官方 Python 包：免安装、不写注册表、不需要管理员权限'
    $pyPkg = "python.$VerPython.nupkg"
    $ok = Invoke-Download -Title "Python $VerPython (约 14 MB)" -Dest (Join-Path $DirRuntime $pyPkg) -Urls @(
        "https://globalcdn.nuget.org/packages/$pyPkg",
        "https://api.nuget.org/v3-flatcontainer/python/$VerPython/$pyPkg",
        "https://mirrors.huaweicloud.com/nuget/v3-flatcontainer/python/$VerPython/$pyPkg"
    )
    if (-not $ok) {
        Write-Bad 'Python 下载失败。'
        Write-Dim '可以手动下载后放到 runtime 目录再重跑：'
        Write-Dim "  https://api.nuget.org/v3-flatcontainer/python/$VerPython/$pyPkg"
        Stop-WithError '安装中止'
    }
    Expand-ZipTo -Zip (Join-Path $DirRuntime $pyPkg) -Dest $DirPython
    Remove-Item (Join-Path $DirRuntime $pyPkg) -Force -ErrorAction SilentlyContinue
    if (-not (Test-Path $PyExe)) { Stop-WithError "解压后没找到 python.exe，路径：$PyExe" }
    Write-Ok "Python 就绪：$((& $PyExe --version 2>&1) -join '')"
}

# ============================================================================
#  3. AstrBot 源码 + 管理面板
# ============================================================================
Write-Step "AstrBot $VerAstrBot"

if (Test-Path $AppEntry) {
    Write-Ok '源码已存在，跳过下载'
} else {
    $zip = Join-Path $DirRuntime "AstrBot-$VerAstrBot.zip"
    $ok = Invoke-Download -Title "AstrBot $VerAstrBot 源码 (约 5 MB)" -Dest $zip -Urls @(
        "https://codeload.github.com/AstrBotDevs/AstrBot/zip/refs/tags/$VerAstrBot",
        "https://ghfast.top/https://github.com/AstrBotDevs/AstrBot/archive/refs/tags/$VerAstrBot.zip",
        "https://gh-proxy.com/https://github.com/AstrBotDevs/AstrBot/archive/refs/tags/$VerAstrBot.zip",
        "https://ghproxy.net/https://github.com/AstrBotDevs/AstrBot/archive/refs/tags/$VerAstrBot.zip"
    )
    if (-not $ok) { Stop-WithError 'AstrBot 源码下载失败，安装中止' }

    $tmp = Join-Path $DirRuntime 'extract'
    Expand-ZipTo -Zip $zip -Dest $tmp
    $inner = Get-ChildItem $tmp -Directory | Select-Object -First 1
    if (-not $inner) { Stop-WithError '压缩包结构不对，里面没有目录' }
    New-Item -ItemType Directory -Path (Split-Path $DirApp -Parent) -Force | Out-Null
    if (Test-Path $DirApp) { Remove-Item $DirApp -Recurse -Force }
    Move-Item $inner.FullName $DirApp
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    Remove-Item $zip -Force -ErrorAction SilentlyContinue
    Write-Ok "源码就绪：$DirApp"
}

# --- 管理面板（没有它浏览器打开 6185 会 404）--------------------------------
$distDir = Join-Path $DirData 'dist'
if (Test-Path (Join-Path $distDir 'index.html')) {
    Write-Ok '管理面板已存在，跳过下载'
} else {
    $dashZipName = "AstrBot-$VerAstrBot-dashboard.zip"
    $dashZip = Join-Path $DirRuntime $dashZipName
    $base = "https://github.com/AstrBotDevs/AstrBot/releases/download/$VerAstrBot/$dashZipName"
    $ok = Invoke-Download -Title "管理面板 (约 5 MB)" -Dest $dashZip -Urls @(
        $base,
        "https://ghfast.top/$base",
        "https://gh-proxy.com/$base",
        "https://ghproxy.net/$base"
    )
    if ($ok) {
        $tmp = Join-Path $DirRuntime 'dashtmp'
        Expand-ZipTo -Zip $dashZip -Dest $tmp
        # 压缩包里就是 dist\ 目录
        $src = Join-Path $tmp 'dist'
        if (-not (Test-Path $src)) { $src = $tmp }
        New-Item -ItemType Directory -Path $DirData -Force | Out-Null
        if (Test-Path $distDir) { Remove-Item $distDir -Recurse -Force }
        Move-Item $src $distDir
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Remove-Item $dashZip -Force -ErrorAction SilentlyContinue
        Write-Ok '管理面板就绪'
    } else {
        Write-Warn '管理面板没下下来。不影响机器人工作，但浏览器打开面板会 404。'
        Write-Dim "手动补：下载 $base"
        Write-Dim "解压出 dist 文件夹，放到 $DirData\ 下面，然后重新运行本脚本。"
    }
}

# ============================================================================
#  4. 安装依赖
# ============================================================================
Write-Step 'Python 依赖'

if ($SkipDeps) {
    Write-Warn '按要求跳过'
} else {
    $req = Join-Path $DirApp 'requirements.txt'
    if (-not (Test-Path $req)) { Stop-WithError "找不到 $req" }

    # 怎么判断"已经装好了"：让它导入几个装完才有的包
    & $PyExe -c "import aiohttp, sqlmodel, quart, faiss" 2>$null
    if ($LASTEXITCODE -eq 0) {
        Write-Ok '依赖已经装好了，跳过'
    } else {
        Write-Dim '这一步最慢。会下载约 1.5 GB 的包，其中 faiss-cpu、pandas 比较大。'
        Write-Dim '国内默认用清华源；连不上会自动换官方源重试。'
        Write-Host ''

        $mirrors = @(
            'https://pypi.tuna.tsinghua.edu.cn/simple',
            'https://mirrors.aliyun.com/pypi/simple/',
            'https://pypi.org/simple'
        )
        $installed = $false
        foreach ($m in $mirrors) {
            Write-Host "      用源：$m" -ForegroundColor DarkGray
            Push-Location $DirApp
            & $PyExe -m pip install --upgrade pip --disable-pip-version-check -q -i $m 2>&1 |
                ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray }
            & $PyExe -m pip install -r requirements.txt --disable-pip-version-check `
                -i $m --no-warn-script-location 2>&1 |
                ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray }
            $code = $LASTEXITCODE
            Pop-Location

            if ($code -eq 0) { $installed = $true; break }
            Write-Warn "这个源失败了（退出码 $code），换下一个"
        }

        if (-not $installed) {
            Write-Bad '依赖安装失败。'
            Write-Dim '常见原因：网络不通、磁盘空间不足、或某个包在你的系统上没有预编译版本。'
            Write-Dim "可以手动重试："
            Write-Dim "  cd `"$DirApp`""
            Write-Dim "  `"$PyExe`" -m pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple"
            Stop-WithError '安装中止'
        }
        Write-Ok '依赖安装完成'
    }
}

# ============================================================================
#  5. 装插件
# ============================================================================
Write-Step '安装插件'

$dstPlugins = Join-Path $DirData 'plugins'
New-Item -ItemType Directory -Path $dstPlugins -Force | Out-Null

foreach ($p in $pluginDirs) {
    $target = Join-Path $dstPlugins $p.Name
    if (Test-Path $target) { Remove-Item $target -Recurse -Force }
    Copy-Item $p.FullName $target -Recurse -Force
    $pycache = Join-Path $target '__pycache__'
    if (Test-Path $pycache) { Remove-Item $pycache -Recurse -Force }
}
Write-Ok "已装入 $($pluginDirs.Count) 个插件"

# 目录名必须和 metadata.yaml 里的 name 一致，否则 AstrBot 拒绝加载
$bad = 0
foreach ($d in Get-ChildItem $dstPlugins -Directory) {
    $meta = Get-Content (Join-Path $d.FullName 'metadata.yaml') -Encoding UTF8 -Raw
    $m = [regex]::Match($meta, '(?m)^\s*name\s*:\s*"?([^"\r\n]+?)"?\s*$')
    if (-not $m.Success -or $m.Groups[1].Value.Trim() -ne $d.Name) {
        Write-Bad "$($d.Name) 的 metadata.name 是 '$($m.Groups[1].Value.Trim())'，和目录名不一致"
        $bad++
    }
}
if ($bad -gt 0) { Stop-WithError '插件元数据有问题，AstrBot 会拒绝加载' }
Write-Ok '插件目录名与 metadata 一致'

# ============================================================================
#  6. 初始化 AstrBot 主配置
# ============================================================================
Write-Step 'AstrBot 主配置'

New-Item -ItemType Directory -Path $DirData -Force | Out-Null
$mainConf = Join-Path $DirData 'cmd_config.json'
New-Item -ItemType Directory -Path (Join-Path $PSScriptRoot 'run') -Force | Out-Null
$tokenFile = Join-Path $PSScriptRoot 'run\onebot_token.txt'
$mainOvFile = Join-Path $PSScriptRoot 'run\main_override.json'

if (Test-Path $mainConf) {
    Write-Ok 'cmd_config.json 已存在，只补齐我们的设置（不会覆盖你改过的）'
} else {
    Write-Dim '用 AstrBot 官方 CLI 初始化（会同时生成管理面板的初始密码）'
    Push-Location $DirApp
    & $PyExe -m astrbot.cli init 2>&1 | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray }
    Pop-Location
    if (-not (Test-Path $mainConf))  { Stop-WithError "初始化失败，没能生成 $mainConf" }
    Write-Ok '已生成默认配置'
}

# 写入平台 / 模型 / 关闭流式输出等设置
@{ api_key = $apiKey; admin_qq = $adminQq } | ConvertTo-Json -Depth 3 | Set-Content -Path $mainOvFile -Encoding UTF8
& $PyExe (Join-Path $PSScriptRoot 'tools\patch_main_config.py') $mainConf $mainOvFile $tokenFile 2>&1 |
    ForEach-Object { Write-Host "        $_" }
if ($LASTEXITCODE -ne 0) { Write-Warn '主配置写入有问题，可以稍后在面板里手动配（README 里有步骤）' }
else { Write-Ok '主配置就绪（已接上 DeepSeek + OneBot v11 平台）' }

# ============================================================================
#  7. 插件配置
# ============================================================================
Write-Step '插件配置'

$cfgDir = Join-Path $DirData 'config'
New-Item -ItemType Directory -Path $cfgDir -Force | Out-Null

$genScript = Join-Path $PSScriptRoot 'tools\gen_plugin_config.py'
foreach ($p in $pluginDirs) {
    $schema = Join-Path $dstPlugins "$($p.Name)\_conf_schema.json"
    if (-not (Test-Path $schema)) { continue }

    $out = Join-Path $cfgDir "$($p.Name)_config.json"
    $ovFile = Join-Path $PSScriptRoot ("run\override_$($p.Name).json")
    $ov = $null
    switch ($p.Name) {
        'astrbot_plugin_balance_guard' {
            $ov = @{ api_key = $apiKey; admin_qq = $adminQq }
        }
        'astrbot_plugin_meme' {
            $ov = @{ admin_qq = $adminQq }
            if ($groups) {
                $ov['collect_groups'] = @($groups -split ',' | ForEach-Object { $_.Trim() } | Where-Object { $_ })
            }
        }
    }
    if ($ov) { $ov | ConvertTo-Json -Depth 5 | Set-Content -Path $ovFile -Encoding UTF8 }
    else { '{}' | Set-Content -Path $ovFile -Encoding UTF8 }
    & $PyExe $genScript $schema $out $ovFile 2>&1 | ForEach-Object { Write-Host "        $_" -ForegroundColor DarkGray }
    if ($LASTEXITCODE -eq 0) { Write-Ok "$($p.Name)：配置已生成" }
    else { Write-Warn "$($p.Name)：配置生成失败，稍后在面板里填" }
}

# ============================================================================
#  8. NapCat（QQ 协议端）
# ============================================================================
Write-Step "NapCat $VerNapCat"

if (Test-Path (Join-Path $DirNapCat 'napcat.bat')) {
    Write-Ok 'NapCat 已存在，跳过下载'
} else {
    $nz = Join-Path $DirRuntime "NapCat.Shell-$VerNapCat.zip"
    $nb = "https://github.com/NapNeko/NapCatQQ/releases/download/$VerNapCat/NapCat.Shell.zip"
    $ok = Invoke-Download -Title "NapCat Shell (约 28 MB)" -Dest $nz -Urls @(
        $nb,
        "https://ghfast.top/$nb",
        "https://gh-proxy.com/$nb",
        "https://ghproxy.net/$nb"
    )
    if ($ok) {
        Expand-ZipTo -Zip $nz -Dest $DirNapCat
        Remove-Item $nz -Force -ErrorAction SilentlyContinue
        New-Item -ItemType Directory -Path (Join-Path $DirNapCat 'config') -Force | Out-Null
        if (Test-Path (Join-Path $DirNapCat 'napcat.bat')) { Write-Ok 'NapCat 就绪' }
        else { Write-Warn 'NapCat 解压后没看到 napcat.bat，请检查目录内容' }
    } else {
        Write-Warn 'NapCat 没下下来 —— 没有它机器人收不到 QQ 消息。'
        Write-Dim "手动补：下载 $nb"
        Write-Dim "解压到 $DirNapCat 然后重跑本脚本。"
    }
}

# ============================================================================
#  9. 快捷方式
# ============================================================================
Write-Step '创建快捷方式'

try {
    $ws = New-Object -ComObject WScript.Shell
    $desktop = [Environment]::GetFolderPath('Desktop')
    $lnk = $ws.CreateShortcut((Join-Path $desktop '启动鲸鱼娘聊天AI.lnk'))
    $lnk.TargetPath = Join-Path $PSScriptRoot '启动.cmd'
    $lnk.WorkingDirectory = $PSScriptRoot
    $lnk.Description = '启动鲸鱼娘聊天AI（Windows 版）'
    $lnk.Save()
    Write-Ok "桌面快捷方式：启动鲸鱼娘聊天AI"
} catch {
    Write-Warn "建快捷方式失败（不影响使用）：$($_.Exception.Message)"
}

# ============================================================================
#  10. 完成
# ============================================================================
$stamp = Get-Date -Format 'yyyy-MM-dd HH:mm:ss'
Write-Banner '安装完成'

Write-Host @"

  接下来三步：

  1. 双击「启动.cmd」        会同时拉起 AstrBot 和 NapCat

  2. 登录 QQ（只需一次）
     启动后浏览器打开  http://127.0.0.1:6099/webui
     登录密钥在启动窗口里会打印出来（也可以看 logs\napcat.log）
     用手机 QQ 扫码

     ⚠ 强烈建议用小号。非官方协议端有封号风险。

  3. 打开管理面板  http://127.0.0.1:6185
     用户名 astrbot，初始密码在启动日志里（logs\astrbot.log 搜 password）
     登录后第一件事：改密码

  然后私聊机器人发「你好」试试。

  ============================================================
  常用：
    启动.cmd        启动全部
    停止.cmd        停止全部
    状态.cmd        看运行状态和端口
    配置对接.cmd    登录 QQ 之后跑一次，把 NapCat 接到 AstrBot
    设置人设.cmd    把鲸鱼娘人设写进去（启动过一次之后再跑）

  详细说明看 README-Windows.md

  安装时间：$stamp
"@ -ForegroundColor Gray

Write-Host ''
Write-Host '  按回车键关闭...' -ForegroundColor DarkGray
try { $null = Read-Host } catch { }
