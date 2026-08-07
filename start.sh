#!/bin/sh
# kimi-board 启动脚本（macOS）
cd "$(dirname "$0")"

# Already running? Just open the page.
if curl -s -o /dev/null --max-time 2 http://127.0.0.1:8321/; then
  echo "kimi-board is already running, opening the page..."
  open http://127.0.0.1:8321
  exit 0
fi

# Launch detached with nohup, so closing this window does NOT stop the service.
if [ -x "./kimi-board" ]; then
  nohup ./kimi-board >/dev/null 2>&1 &
elif command -v python3 >/dev/null 2>&1; then
  nohup python3 ./kimi_board.py >/dev/null 2>&1 &
else
  echo "Neither ./kimi-board nor python3 was found."
  echo "Install Python 3.8+, or use the release zip with the binary."
  exit 1
fi

echo "kimi-board started in background: http://127.0.0.1:8321"
echo "It keeps running after this window closes."
echo "To stop: pkill -f kimi-board"
sleep 3
