@echo off
title Telegram Sender - Update
cd /d "%~dp0"

echo ==========================================
echo    Getting the latest version
echo ==========================================
echo.

if not exist ".git" (
  echo This folder was not set up for updates.
  echo Please ask your developer to send you a fresh copy.
  echo.
  pause
  exit /b
)

where git >nul 2>nul
if errorlevel 1 (
  echo Git is not installed on this computer.
  echo.
  echo Install it once from  https://git-scm.com/download/win
  echo Just click Next on every screen. Then run this file again.
  echo.
  start https://git-scm.com/download/win
  pause
  exit /b
)

echo Your message, your lists and your login are NOT touched by this.
echo.

git pull
if errorlevel 1 (
  echo.
  echo Could not get the update. Check your internet connection.
  echo If it keeps failing, send this window to your developer.
  echo.
  pause
  exit /b
)

echo.
echo Checking for new requirements...
if exist "venv\Scripts\python.exe" (
  venv\Scripts\python.exe -m pip install --quiet -r requirements.txt
)

echo.
echo ==========================================
echo    Up to date. You can close this window
echo    and start the sender as usual.
echo ==========================================
echo.
pause
