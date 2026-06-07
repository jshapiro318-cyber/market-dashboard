#!/usr/bin/env bash
# Uninstall the launchd agent. Server stops; pmset wake schedule is unaffected.

set -euo pipefail
PLIST_DST="$HOME/Library/LaunchAgents/com.jadenshapiro318.marketintel.plist"

if launchctl print "gui/$(id -u)/com.jadenshapiro318.marketintel" >/dev/null 2>&1; then
    launchctl bootout "gui/$(id -u)" "$PLIST_DST" 2>/dev/null || true
    echo "✓ Agent unloaded."
else
    echo "Agent was not running."
fi

if [ -f "$PLIST_DST" ]; then
    rm "$PLIST_DST"
    echo "✓ Plist removed: $PLIST_DST"
fi

echo ""
echo "To also stop the daily wake-up at 9:25 AM ET, run:"
echo "  sudo pmset repeat cancel"
