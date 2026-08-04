@echo off
set "VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\kimi-board.vbs"
if exist "%VBS%" (
  del "%VBS%"
  echo Autostart removed. The currently running service is unaffected until reboot.
) else (
  echo No autostart entry found, nothing to do.
)
pause
