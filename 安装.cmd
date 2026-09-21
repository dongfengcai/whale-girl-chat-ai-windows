@echo off
rem Keep this file pure ASCII: cmd.exe reads batch files in the OEM codepage
rem (936 on a Chinese Windows), so Chinese literals here would be garbled and
rem would not match the real file names. The Chinese output lives in the
rem PowerShell script, which is saved as UTF-8 with BOM.
setlocal
cd /d "%~dp0"
title Whale Girl Chat AI - Install

rem Files inside a downloaded zip carry a "mark of the web"; PowerShell can
rem refuse to run them even with -ExecutionPolicy Bypass. Clearing it for our
rem own folder is harmless and saves users a confusing failure.
powershell -NoProfile -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath '%~dp0' -Recurse -File -Include *.ps1,*.cmd,*.py -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1

powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1" %*
endlocal