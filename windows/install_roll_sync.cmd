@echo off
rem Install TzujenRollSync (read-only, no trading) into every MT5 on this PC (ASCII only on purpose)
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install_roll_sync.ps1"
if errorlevel 1 pause
