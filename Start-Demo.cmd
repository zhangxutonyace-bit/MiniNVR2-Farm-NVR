@echo off
cd /d "%~dp0"
set PYTHONUTF8=1
if not exist ".venv\Scripts\python.exe" (
  powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0setup.ps1"
  if errorlevel 1 goto failed
)
".venv\Scripts\python.exe" scripts\prepare_demo.py
if errorlevel 1 goto failed
".venv\Scripts\python.exe" main.py --data-dir data-demo --port 8801
if errorlevel 1 goto failed
exit /b 0
:failed
echo MiniNVR2 demo could not start. Check the message above.
pause
exit /b 1
