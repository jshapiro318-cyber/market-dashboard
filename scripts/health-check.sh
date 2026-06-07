#!/usr/bin/env bash
# External health check — runs every 5 min during market hours via launchd.
# If the server is down OR the scheduler is wedged, restart it.

LOG=~/market-dashboard/logs/health-check.log
mkdir -p ~/market-dashboard/logs

log_line() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" >> "$LOG"
}

# Hit the health endpoint
RESPONSE=$(curl -s -m 5 http://127.0.0.1:8000/api/scheduler/health 2>/dev/null)

if [ -z "$RESPONSE" ]; then
    log_line "Server unreachable on port 8000 — restarting launchd agent"
    launchctl bootout gui/$(id -u) ~/Library/LaunchAgents/com.jadenshapiro318.marketintel.plist 2>/dev/null
    sleep 2
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.jadenshapiro318.marketintel.plist
    exit 0
fi

# Parse health (Python for safety)
HEALTH=$(echo "$RESPONSE" | /usr/bin/python3 -c "
import json, sys
d = json.load(sys.stdin)
ok = d.get('scheduler_running') and d.get('watchdog_thread_alive') and not d.get('overdue_jobs')
print('OK' if ok else f\"BAD scheduler_running={d.get('scheduler_running')} watchdog={d.get('watchdog_thread_alive')} overdue={d.get('overdue_jobs')}\")
" 2>/dev/null)

if [ "$HEALTH" = "OK" ]; then
    # Healthy — log only occasionally to avoid log spam
    if [ $(($(date +%M) % 30)) -eq 0 ]; then
        log_line "OK"
    fi
else
    log_line "$HEALTH — force-restarting server"
    pkill -f "uvicorn backend.app:app" 2>/dev/null
    # launchd will respawn within 30s
fi
