@echo off
setlocal
cd /d "%~dp0"
py -3 -c "import sys" >nul 2>nul
if not errorlevel 1 (
    py -3 run.py
) else (
    python run.py
)
pause
