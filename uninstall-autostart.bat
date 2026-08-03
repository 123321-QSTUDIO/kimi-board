@echo off
chcp 65001 >nul
set "VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\kimi-board.vbs"
if exist "%VBS%" (
  del "%VBS%"
  echo 已取消开机自启（当前运行的服务不受影响，重启后生效）。
) else (
  echo 未找到自启项，无需操作。
)
pause
