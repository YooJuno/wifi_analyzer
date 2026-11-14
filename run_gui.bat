@echo off
setlocal
if exist "%~dp0.venv\Scripts\python.exe" (
    "%~dp0.venv\Scripts\python.exe" "%~dp0wifi_gui.py"
) else (
    py -3 "%~dp0wifi_gui.py"
)
if errorlevel 1 pause
