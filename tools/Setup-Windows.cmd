@echo off
setlocal
set "DECKPILOT_SETUP=%~dp0setup-windows.ps1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -Command "$p=$env:DECKPILOT_SETUP; Start-Process powershell.exe -Verb RunAs -Wait -ArgumentList ('-NoProfile -NoExit -ExecutionPolicy Bypass -File ""' + $p + '""')"
if errorlevel 1 pause
