@echo off
setlocal
cd /d "%~dp0"
title FC26 AI Character Studio - Auto-test
if not exist .venv\Scripts\python.exe (
  echo Lance d'abord install_windows.bat.
  pause
  exit /b 1
)
call .venv\Scripts\activate.bat
python tools\self_test.py
pause
