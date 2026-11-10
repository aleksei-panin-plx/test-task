@echo off
rem Double-click to install CardForge on Windows (NOT VERIFIED on Windows yet).
cd /d "%~dp0.."
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0install.ps1"
echo.
pause
