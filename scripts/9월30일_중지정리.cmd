@echo off
chcp 65001 >nul
cd /d "%~dp0.."
python "scripts\cleanup_september30_stops.py" --no-pause
echo.
pause
