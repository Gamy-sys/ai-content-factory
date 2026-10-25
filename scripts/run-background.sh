#!/usr/bin/env bash
# User-level background start. The process keeps running after the terminal closes.
# It listens on 127.0.0.1 only.
set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p data/logs
PORT="${APP_PORT:-8000}"
if [[ -f data/app.pid ]] && kill -0 "$(cat data/app.pid)" 2>/dev/null; then
  echo "Already running as pid $(cat data/app.pid) on http://127.0.0.1:${PORT}"
  exit 0
fi
nohup .venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port "${PORT}" >> data/logs/server.log 2>&1 &
echo $! > data/app.pid
echo "Started pid $(cat data/app.pid) on http://127.0.0.1:${PORT}"
echo "Log: data/logs/server.log"
