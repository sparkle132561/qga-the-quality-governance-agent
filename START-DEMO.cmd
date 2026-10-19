@echo off
cd /d "%~dp0"
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\start-demo.ps1" -PostgresPort 56543 %*
if errorlevel 1 pause
