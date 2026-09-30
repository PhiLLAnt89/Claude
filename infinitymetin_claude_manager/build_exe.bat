@echo off
REM Builds "InfinityMetin Claude Manager.exe" (one file, no console) into the dist folder.
REM Needs Python 3.10+ from python.org on this PC. Run it by double-clicking.
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if errorlevel 1 (
    echo Python was not found. Install it from https://www.python.org/downloads/ and tick "Add python.exe to PATH".
    pause
    exit /b 1
)

if not exist build_venv (
    echo Creating a build environment...
    py -3 -m venv build_venv || goto :fail
)
call build_venv\Scripts\activate.bat
python -m pip install --upgrade pip >nul
python -m pip install pygame pyinstaller || goto :fail

echo Building the exe...
python -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "InfinityMetin Claude Manager" ^
    infinitymetin_claude_manager.py || goto :fail

echo.
echo Done: "%~dp0dist\InfinityMetin Claude Manager.exe"
echo Tip: make a shortcut and add   --project "D:\Path\To\Your\Project"   to its target.
pause
exit /b 0

:fail
echo.
echo Build failed. See the messages above.
pause
exit /b 1
