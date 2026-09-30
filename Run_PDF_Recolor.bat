@echo off
setlocal
cd /d "%~dp0"
title PDF Recolor

where python >nul 2>&1
if errorlevel 1 goto nopython

if not exist "venv\Scripts\python.exe" (
    echo First-time setup: installing required packages, please wait...
    python -m venv venv
    if errorlevel 1 goto fail
    "venv\Scripts\python.exe" -m pip install --upgrade pip
    "venv\Scripts\python.exe" -m pip install pymupdf pillow numpy tkinterdnd2
    if errorlevel 1 goto fail
)

start "" "venv\Scripts\pythonw.exe" "PDF_Recolor_App.py"
exit /b 0

:nopython
echo Python 3.9 or newer is required but was not found.
echo Download it from https://www.python.org/downloads/
echo IMPORTANT: tick "Add python.exe to PATH" during installation.
pause
exit /b 1

:fail
echo Setup failed. Check your internet connection and try again.
pause
exit /b 1
