@echo off
chcp 65001 >nul
if exist "%~dp0meal_stop_cleanup.exe" (
    "%~dp0meal_stop_cleanup.exe" --no-pause
) else if exist "%~dp0..\dist\meal_stop_cleanup.exe" (
    "%~dp0..\dist\meal_stop_cleanup.exe" --no-pause
) else (
    cd /d "%~dp0.."
    python "scripts\cleanup_september30_stops.py" --no-pause
)
echo.
pause
