#!/usr/bin/env bash
# Install + load the launchd agent so the server runs in the background and
# auto-restarts if it dies. Run this once; takes ~3 seconds. No sudo needed.

set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJ_ROOT="$(dirname "$SCRIPT_DIR")"
PLIST_SRC="$SCRIPT_DIR/com.jadenshapiro318.marketintel.plist"
PLIST_DST="$HOME/Library/LaunchAgents/com.jadenshapiro318.marketintel.plist"

mkdir -p "$HOME/Library/LaunchAgents"
mkdir -p "$PROJ_ROOT/logs"
chmod +x "$SCRIPT_DIR/server-launch.sh"

# Stop any existing instance first
if launchctl print "gui/$(id -u)/com.jadenshapiro318.marketintel" >/dev/null 2>&1; then
    echo "Unloading existing agent..."
    launchctl bootout "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || true
fi

# Stop any manually-started uvicorn so launchd owns port 8000
pkill -f "uvicorn backend.app:app" 2>/dev/null || true
sleep 1

cp "$PLIST_SRC" "$PLIST_DST"
echo "Plist copied to $PLIST_DST"

launchctl bootstrap "gui/$(id -u)" "$PLIST_DST"
echo "Agent loaded."

sleep 3

if curl -s -m 3 -o /dev/null -w "%{http_code}" http://127.0.0.1:8000/api/config | grep -q 200; then
    echo "✓ Server is up at http://127.0.0.1:8000 (auto-restarts if it crashes)"
else
    echo "⚠ Server didn't respond on port 8000. Check logs: tail -f $PROJ_ROOT/logs/launchd.err.log"
fi

echo ""
echo "Status: launchctl list | grep marketintel"
echo "Logs:   tail -f $PROJ_ROOT/logs/launchd.{out,err}.log"
echo "Stop:   $SCRIPT_DIR/uninstall-launchd.sh"
