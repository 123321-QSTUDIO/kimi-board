#!/bin/sh
# kimi-board 开机自启卸载（macOS）
LABEL="com.kimi-board"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || launchctl unload "$PLIST" 2>/dev/null
rm -f "$PLIST"

echo "kimi-board autostart removed."
