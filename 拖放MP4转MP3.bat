@echo off
setlocal
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
cd /d "%~dp0"

if "%~1"=="" (
    echo.
    echo   Drag one or more MP4 files or folders onto this BAT file.
    echo   The MP3 will be saved next to the source video.
    echo.
    echo   For the GUI, double-click mp4_to_mp3.py.
    echo.
    pause
    exit /b 0
)

where python >nul 2>nul
if errorlevel 1 (
    echo.
    echo   [ERROR] Python not found.
    echo   Please install Python 3 and enable "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

python "%~dp0mp4_to_mp3.py" %*
echo.
echo   Done. Press any key to close.
pause >nul