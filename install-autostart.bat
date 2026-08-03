@echo off
chcp 65001 >nul
setlocal
REM 在当前用户的启动文件夹注册看板开机自启（隐藏窗口、不弹浏览器）
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "VBS=%STARTUP%\kimi-board.vbs"

REM 优先用免 Python 的 exe；没有则回退到 pythonw + 脚本
if exist "%~dp0kimi-board.exe" (
  set "CMDLINE="""%~dp0kimi-board.exe"" --no-open"
) else (
  set "PYW="
  for %%P in (pythonw.exe) do set "PYW=%%~$PATH:P"
  if not defined PYW (
    echo 未找到 kimi-board.exe，也未找到 pythonw.exe。
    echo 请安装 Python 3.8+ 并加入 PATH，或使用带 exe 的 release 包。
    pause
    exit /b 1
  )
  set "CMDLINE="""%PYW%"" ""%~dp0kimi_board.py"" --no-open"
)

> "%VBS%" echo ' kimi code token 看板 开机自启（由 install-autostart.bat 生成）
>> "%VBS%" echo CreateObject("Wscript.Shell").Run "%CMDLINE%", 0, False

echo 已写入：%VBS%
echo 下次开机起看板服务将静默自启，浏览 http://127.0.0.1:8321 即可。
echo 如需取消，运行 uninstall-autostart.bat 或直接删除该 vbs 文件。
pause
