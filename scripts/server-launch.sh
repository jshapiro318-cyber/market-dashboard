#!/usr/bin/env bash
# Boot script invoked by launchd. Activates venv and runs uvicorn.
# Self-healing: if uvicorn dies, launchd restarts us (KeepAlive=true).

set -euo pipefail
cd "$(dirname "$0")/.."

# Use the project venv if present, otherwise build one
if [ ! -d ".venv" ]; then
  python3 -m venv .venv
  source .venv/bin/activate
  pip install --upgrade pip --quiet
  pip install -r backend/requirements.txt --quiet
else
  source .venv/bin/activate
fi

# Make uv available for the Markov subprocess
export PATH="$HOME/.local/bin:$PATH"

exec uvicorn backend.app:app --host 127.0.0.1 --port 8000 --log-level warning
