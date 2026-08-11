#!/bin/sh
# kimi-board 开机自启安装（macOS，launchd LaunchAgent，无需 root）
cd "$(dirname "$0")"
DIR="$(pwd)"
LABEL="com.kimi-board"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

# Prefer the bundled binary; fall back to python3 + source.
if [ -x "$DIR/kimi-board" ]; then
  ARGS="    <string>$DIR/kimi-board</string>"
elif command -v python3 >/dev/null 2>&1 && [ -f "$DIR/kimi_board.py" ]; then
  PY="$(command -v python3)"
  ARGS="    <string>$PY</string>
    <string>$DIR/kimi_board.py</string>"
else
  echo "Neither ./kimi-board nor python3 + kimi_board.py was found."
  exit 1
fi

mkdir -p "$HOME/Library/LaunchAgents"
cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LABEL</string>
  <key>ProgramArguments</key>
  <array>
$ARGS
  </array>
  <key>WorkingDirectory</key>
  <string>$DIR</string>
  <key>RunAtLoad</key>
  <true/>
  <key>StandardOutPath</key>
  <string>/tmp/kimi-board.log</string>
  <key>StandardErrorPath</key>
  <string>/tmp/kimi-board.log</string>
</dict>
</plist>
EOF

# Reload (ignore "not loaded" errors), then start now.
launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null
launchctl bootstrap "gui/$(id -u)" "$PLIST" 2>/dev/null || launchctl load -w "$PLIST"

echo "kimi-board autostart installed: $PLIST"
echo "It now starts at every login. Dashboard: http://127.0.0.1:8321"
echo "Log: /tmp/kimi-board.log  To remove: ./uninstall-autostart.sh"
