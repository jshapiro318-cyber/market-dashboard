#!/usr/bin/env bash
set -e
cd "$(dirname "$0")"

if [ ! -d ".venv" ]; then
  echo "Creating virtualenv..."
  python3 -m venv .venv
fi

source .venv/bin/activate
pip install --upgrade pip >/dev/null
pip install -r backend/requirements.txt

echo ""
echo "Starting dashboard at http://127.0.0.1:8000"
echo "Press Ctrl+C to stop."
echo ""

# Run without --reload so file edits don't restart the scheduler.
# Auto-restart on crash for 24/7 resilience.
while true; do
  caffeinate -i uvicorn backend.app:app --host 127.0.0.1 --port 8000 || true
  echo "[$(date)] Server exited — restarting in 5 seconds..."
  sleep 5
done
