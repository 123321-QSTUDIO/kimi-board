@echo off
chcp 65001 >nul
cd /d "%~dp0"
title kimi code token 看板 (Ctrl+C 或关窗停止)
if exist "%~dp0kimi-board.exe" (
  "%~dp0kimi-board.exe"
) else (
  python kimi_board.py
)
pause
