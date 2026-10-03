@echo off
rem ===========================================================
rem  ImageTool launcher  (ASCII ONLY - do not put Chinese here)
rem  This file MUST stay pure ASCII: cmd.exe parses .bat files
rem  using the local ANSI code page (GBK on Chinese Windows),
rem  so non-ASCII bytes corrupt the quoting and break every line.
rem ===========================================================
setlocal
cd /d "%~dp0"

rem Use UTF-8 console so the Chinese messages printed by Python show correctly.
rem This only affects console output, the .bat itself stays ASCII.
chcp 65001 >nul 2>nul

echo ============================================
echo   ImageTool  -  Batch Image Processor
echo ============================================
echo.

set "PY_CMD=python"
"%PY_CMD%" -V >nul 2>nul
if errorlevel 1 (
    set "PY_CMD=py"
    "%PY_CMD%" -V >nul 2>nul
    if errorlevel 1 (
        echo [ERROR] Python not found.
        echo         Please install Python 3.8+ and check "Add Python to PATH".
        pause
        exit /b 1
    )
)

"%PY_CMD%" -c "import PIL" >nul 2>nul
if errorlevel 1 (
    echo [ERROR] Pillow is missing in this Python environment.
    echo         Run manually:  python -m pip install -r requirements.txt
    pause
    exit /b 1
)

echo Starting ImageTool ...
"%PY_CMD%" main.py

if errorlevel 1 (
    echo.
    echo [ERROR] ImageTool exited with code %errorlevel%
    echo         For diagnostics run:  python selfcheck.py
    pause
    exit /b 1
)

endlocal
