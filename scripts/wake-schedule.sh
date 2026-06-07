#!/usr/bin/env bash
# Schedule the Mac to wake from sleep at 9:25 AM ET on weekdays so the
# 9:30 ET market_open job has a live server to run in.
#
# Requires sudo (pmset is a system command). Asks once for your password.

set -euo pipefail

echo "This will schedule your Mac to wake Mon-Fri at 9:25 AM local time."
echo "Current local timezone: $(date '+%Z %z')"
echo ""
echo "If your Mac is set to Eastern time, this wakes at 9:25 AM ET (market opens 9:30)."
echo "If you're in a different timezone, edit the time in this script first."
echo ""
read -p "Proceed? [y/N] " ANS
if [[ ! "$ANS" =~ ^[Yy]$ ]]; then
    echo "Aborted."
    exit 0
fi

sudo pmset repeat wake MTWRF 09:25:00
echo ""
echo "✓ Wake schedule set. Current schedule:"
pmset -g sched
echo ""
echo "To cancel: sudo pmset repeat cancel"
