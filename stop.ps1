#Requires -Version 5.1
<#
    stop.ps1 -- 停止 AstrBot 和 NapCat

    只结束「可执行文件位于本包目录内」的进程，
    不会碰你自己开的 QQ，也不会碰别的 Python 程序。
#>
[CmdletBinding()]
param(
    [switch]$KeepQq
)

. (Join-Path $PSScriptRoot 'lib\common.ps1')

Write-Banner '鲸鱼娘聊天AI · 停止'

$procs = @(Get-PackageProcess)
if ($procs.Count -eq 0) {
    Write-Ok '没有在运行的进程'
} else {
    Write-Step "找到 $($procs.Count) 个进程"
    foreach ($p in $procs) {
        Write-Dim "$($p.ProcessName)  PID $($p.Id)  内存 $([math]::Round($p.WorkingSet64 / 1MB)) MB"
    }

    foreach ($p in $procs) {
        # NapCat 是靠注入 QQ.exe 工作的，-KeepQq 可以留着 QQ 不关
        if ($KeepQq -and $p.ProcessName -like 'QQ*') {
            Write-Dim "跳过 QQ（PID $($p.Id)）"
            continue
        }
        try {
            Stop-Process -Id $p.Id -Force -ErrorAction Stop
            Write-Ok "已结束 $($p.ProcessName) (PID $($p.Id))"
        } catch {
            Write-Warn "结束 $($p.ProcessName) (PID $($p.Id)) 失败：$($_.Exception.Message)"
        }
    }
}

# 端口确认
Start-Sleep -Milliseconds 800
Write-Step '端口检查'
foreach ($port in 6185, 6199, 6099) {
    if (Test-Port -Port $port) {
        $owner = Get-PortOwnerPid -Port $port
        Write-Warn "$port 还在监听（PID $owner）—— 可能是上一个进程还没退干净，或别的程序占用"
    } else {
        Write-Ok "$port 已释放"
    }
}

Write-Host ''
Write-Host '  按回车键关闭...' -ForegroundColor DarkGray
try { $null = Read-Host } catch { }
