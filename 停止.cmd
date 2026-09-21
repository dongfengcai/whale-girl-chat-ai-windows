@echo off
rem Pure ASCII on purpose -- see the note in the install launcher.
setlocal
cd /d "%~dp0"
title Whale Girl Chat AI - Stop
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0stop.ps1" %*
endlocal
