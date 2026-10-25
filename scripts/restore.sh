#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [[ $# -ne 1 ]]; then
  echo "Usage: scripts/restore.sh data/backups/studio-YYYYMMDDTHHMMSSZ.db" >&2
  exit 2
fi
exec .venv/bin/python -m app.backup restore "$1"
