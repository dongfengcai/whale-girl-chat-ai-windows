#Requires -Version 5.1
<#
    set-persona.ps1 -- 把鲸鱼娘人设写进 AstrBot

    为什么不能安装时一次做完：
        人设存在 SQLite 数据库里，而 data_v4.db 是 AstrBot 第一次启动时
        才建出来的。所以顺序必须是：先启动一次 → 再跑本脚本。

    做的事：
        1. 往 personas 表插一条人设（已存在就更新）
        2. 把它设成默认人设

    可以反复运行。
#>
[CmdletBinding()]
param(
    [string]$Name = '鲸鱼娘'
)

. (Join-Path $PSScriptRoot 'lib\common.ps1')

Write-Banner '鲸鱼娘聊天AI · 设置人设'

if (-not (Test-Path $PersonaFile)) { Stop-WithError "找不到人设文件：$PersonaFile" }

$db = Join-Path $DirData 'data_v4.db'
if (-not (Test-Path $db)) {
    Write-Host ''
    Write-Warn "还没有数据库：$db"
    Write-Host ''
    Write-Host '  说明 AstrBot 还没成功启动过。顺序：' -ForegroundColor White
    Write-Host '    1. 双击「启动.cmd」' -ForegroundColor White
    Write-Host '    2. 等它打印出管理面板地址（约 30-60 秒）' -ForegroundColor White
    Write-Host '    3. 再跑一次本脚本' -ForegroundColor White
    Write-Host ''
    Write-Host '  按回车键关闭...' -ForegroundColor DarkGray
    try { $null = Read-Host } catch { }
    exit 0
}

Write-Step '写入人设'
$prompt = Get-Content $PersonaFile -Raw -Encoding UTF8
Write-Dim "人设文件 $(Split-Path $PersonaFile -Leaf)：$($prompt.Length) 个字符"

$py = Join-Path $PSScriptRoot 'tools\seed_persona.py'
& $PyExe $py $db $Name $PersonaFile (Join-Path $DirData 'cmd_config.json') 2>&1 |
    ForEach-Object { Write-Host "        $_" }
$code = $LASTEXITCODE

if ($code -eq 0) {
    Write-Ok "人设「$Name」已写入并设为默认"
    Write-Host ''
    Write-Host '  ⚠ AstrBot 需要重启才会读到新人设。' -ForegroundColor Yellow
    Write-Host '    双击「停止.cmd」再「启动.cmd」，或者直接在面板里点重载。' -ForegroundColor White
} else {
    Write-Bad "写入失败（退出码 $code）"
    Write-Dim '可以退而求其次：打开面板 → 人格设置 → 新建 → 把 persona-whale-girl.md 的内容粘进去'
}

Write-Host ''
Write-Host '  按回车键关闭...' -ForegroundColor DarkGray
try { $null = Read-Host } catch { }
