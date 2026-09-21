@echo off
rem Pure ASCII on purpose -- see the note in the install launcher.
setlocal
cd /d "%~dp0"
title Whale Girl Chat AI - Status
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0status.ps1" %*
endlocal
