@echo off
setlocal
cd /d "%~dp0"
title FC26 AI Character Studio
if not exist .venv\Scripts\python.exe (
  echo Le Studio n'est pas installe. Lance install_windows.bat.
  pause
  exit /b 1
)
if not exist workspace\config.json (
  call setup_windows.bat
)
call .venv\Scripts\activate.bat
start "" http://127.0.0.1:8765
python -m uvicorn app.main:app --host 127.0.0.1 --port 8765
pause
