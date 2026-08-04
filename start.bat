@echo off
cd /d "%~dp0"
title kimi-board (Ctrl+C or close window to stop)
if exist "%~dp0kimi-board.exe" (
  "%~dp0kimi-board.exe"
) else (
  python kimi_board.py
)
pause
