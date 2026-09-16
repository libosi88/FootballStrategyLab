@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1" -ExistingPythonOnly
if errorlevel 1 echo Startup failed. Keep workspace files and review the error above.
pause
