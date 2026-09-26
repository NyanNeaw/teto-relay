@echo off
rem Starts the control panel from a source checkout (see README).
cd /d "%~dp0"
".venv\Scripts\python.exe" -m teto_relay --web
if errorlevel 1 pause
