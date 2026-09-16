@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONUTF8=1"
if not exist ".venv\Scripts\python.exe" goto setup
".venv\Scripts\python.exe" -B -m lab.launcher --open
if %errorlevel% equ 3 goto stale
if not errorlevel 1 (
 exit /b 0
)
".venv\Scripts\python.exe" -c "import sys,numpy,numba,llvmlite; assert (3,11)<=sys.version_info[:2]<=(3,13) and sys.maxsize>2**32; assert numpy.__version__=='2.3.5' and numba.__version__=='0.65.1' and llvmlite.__version__=='0.47.0'"
if errorlevel 1 goto setup
".venv\Scripts\python.exe" -B app.py serve
goto end
:stale
echo 本工作区已有另一个版本的工作台在运行。请先关闭那个工作台窗口（或在其页面点“关闭界面服务”），再重新启动。
pause
exit /b 3
:setup
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup_windows.ps1"
:end
if errorlevel 1 echo Startup failed. Keep workspace files and review the error above.
pause
