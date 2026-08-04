@echo off
setlocal enabledelayedexpansion
cd /d "%~dp0"

REM Already running? Just open the page.
curl -s -o nul --max-time 2 http://127.0.0.1:8321/
if %errorlevel%==0 (
  echo kimi-board is already running, opening the page...
  start http://127.0.0.1:8321
  ping -n 4 127.0.0.1 >nul
  exit /b 0
)

REM Launch hidden and detached, so closing this window does NOT stop the service.
if exist "%~dp0kimi-board.exe" (
  set "RUNARGS=""%~dp0kimi-board.exe"""
) else (
  set "PYW="
  for %%P in (pythonw.exe) do set "PYW=%%~$PATH:P"
  if not defined PYW (
    echo Neither kimi-board.exe nor pythonw.exe was found.
    echo Install Python 3.8+ and add it to PATH, or use the release zip with the exe.
    pause
    exit /b 1
  )
  set "RUNARGS=""!PYW!"" ""%~dp0kimi_board.py"""
)
set "VBS=%TEMP%\kimi-board-run.vbs"
> "%VBS%" echo CreateObject("Wscript.Shell").Run "%RUNARGS%", 0, False
wscript "%VBS%"

echo kimi-board started in background: http://127.0.0.1:8321
echo It keeps running after this window closes.
echo To stop: taskkill /im kimi-board.exe /f  ^(or end pythonw.exe^)
ping -n 6 127.0.0.1 >nul
