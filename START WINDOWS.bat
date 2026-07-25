@echo off
title Telegram Sender
cd /d "%~dp0"

echo ==========================================
echo    Telegram Sender - starting up
echo ==========================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo Python is not installed on this computer.
  echo.
  echo Please install it once:
  echo    1. Go to  https://www.python.org/downloads/
  echo    2. Download Python for Windows
  echo    3. IMPORTANT: tick "Add Python to PATH" on the first screen
  echo    4. Finish the install, then run this file again
  echo.
  start https://www.python.org/downloads/
  pause
  exit /b
)

if not exist "venv\Scripts\python.exe" (
  echo First time setup - this takes about a minute. Please wait...
  python -m venv venv
  if errorlevel 1 (
    echo Could not create the environment. Please send this window to your developer.
    pause
    exit /b
  )
  venv\Scripts\python.exe -m pip install --upgrade pip --quiet
  venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
  if errorlevel 1 (
    echo Could not download the needed files. Check your internet and try again.
    pause
    exit /b
  )
  echo Setup finished.
  echo.
)

rem Quietly grab the latest version if this folder came from git.
rem Never blocks startup: if there is no git or no internet, we just carry on.
if exist ".git" (
  where git >nul 2>nul
  if not errorlevel 1 (
    echo Checking for updates...
    git pull --quiet 2>nul
    if not errorlevel 1 (
      venv\Scripts\python.exe -m pip install --quiet -r requirements.txt 2>nul
    )
  )
)

echo Opening the window...
venv\Scripts\python.exe app.py
if errorlevel 1 (
  echo.
  echo Something went wrong. Please send this window to your developer.
  pause
)
