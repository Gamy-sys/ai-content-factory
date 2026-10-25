#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ ! -f data/app.pid ]]; then
  echo "No pid file. Nothing was stopped."
  exit 0
fi
pid="$(cat data/app.pid)"
if kill -0 "${pid}" 2>/dev/null; then
  kill "${pid}"
  echo "Stopped pid ${pid}."
else
  echo "Process ${pid} was not running."
fi
rm -f data/app.pid
