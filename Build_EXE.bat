@echo off
setlocal
cd /d "%~dp0"
title Building PDF Recolor EXE

where python >nul 2>&1
if errorlevel 1 goto nopython

echo [1/3] Creating build environment...
python -m venv build_env
if errorlevel 1 goto fail

echo [2/3] Installing packages...
"build_env\Scripts\python.exe" -m pip install --upgrade pip
"build_env\Scripts\python.exe" -m pip install pymupdf pillow numpy tkinterdnd2 pyinstaller
if errorlevel 1 goto fail

echo [3/3] Building executable (this takes a few minutes)...
"build_env\Scripts\python.exe" -m PyInstaller --noconfirm --clean --onefile --windowed ^
    --name "PDF Recolor" --collect-all tkinterdnd2 PDF_Recolor_App.py
if errorlevel 1 goto fail

echo.
echo Done!  Your app is here:  dist\PDF Recolor.exe
echo You can copy that single file anywhere - no Python needed on the target PC.
explorer dist
pause
exit /b 0

:nopython
echo Python 3.9 or newer is required to BUILD the exe.
echo Download it from https://www.python.org/downloads/  (tick "Add to PATH")
pause
exit /b 1

:fail
echo Build failed. Read the messages above for the reason.
pause
exit /b 1
