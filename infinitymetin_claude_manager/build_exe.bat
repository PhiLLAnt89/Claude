@echo off
REM Builds "InfinityMetin Claude Manager.exe" (one file, no console window) into the dist folder.
REM Needs Python 3.9 or newer. Everything this script prints is also saved to build_log.txt.
setlocal EnableDelayedExpansion
cd /d "%~dp0"
set "LOG=%~dp0build_log.txt"
echo InfinityMetin Claude Manager build - %DATE% %TIME% > "%LOG%"

REM ---- find a Python 3 interpreter: py launcher, python, python3 (Store Python has no "py") ----
set "PY="
for %%C in ("py -3" "python" "python3") do (
    if not defined PY (
        %%~C -c "import sys; raise SystemExit(0 if sys.version_info >= (3, 9) else 1)" >nul 2>nul
        if !errorlevel! equ 0 set "PY=%%~C"
    )
)
if not defined PY (
    echo Python 3.9 or newer was not found.
    echo Install it from https://www.python.org/downloads/windows/ and tick "Add python.exe to PATH",
    echo then run this file again.
    echo Python not found >> "%LOG%"
    pause
    exit /b 1
)
echo Using: %PY%
%PY% --version
%PY% --version >> "%LOG%" 2>&1

REM ---- build environment ----
if not exist "build_venv\Scripts\python.exe" (
    echo Creating the build environment...
    %PY% -m venv build_venv >> "%LOG%" 2>&1 || goto :fail
)
set "VPY=build_venv\Scripts\python.exe"
echo Installing pygame and PyInstaller (first time takes a minute)...
"%VPY%" -m pip install --upgrade pip >> "%LOG%" 2>&1
"%VPY%" -m pip install pygame pyinstaller >> "%LOG%" 2>&1 || goto :fail

REM ---- quick check that the game imports before spending time on PyInstaller ----
set SDL_VIDEODRIVER=dummy
set SDL_AUDIODRIVER=dummy
"%VPY%" -c "import infinitymetin_claude_manager" >> "%LOG%" 2>&1 || goto :fail
set SDL_VIDEODRIVER=
set SDL_AUDIODRIVER=

echo Building the exe...
"%VPY%" -m PyInstaller --noconfirm --clean --onefile --windowed --name "InfinityMetin Claude Manager" infinitymetin_claude_manager.py >> "%LOG%" 2>&1 || goto :fail

echo.
echo Done: %~dp0dist\InfinityMetin Claude Manager.exe
echo Tip: make a shortcut and add   --project "D:\Path\To\Your\Project"   to its target.
echo BUILD OK >> "%LOG%"
pause
exit /b 0

:fail
echo.
echo Build failed. The last lines of build_log.txt:
echo ------------------------------------------------------------
%PY% -c "import io; lines = io.open(r'%LOG%', encoding='utf-8', errors='replace').read().splitlines(); print('\n'.join(lines[-25:]))" 2>nul
echo ------------------------------------------------------------
echo Send build_log.txt (next to this file) to get it fixed.
pause
exit /b 1
