#!/usr/bin/env bash
# Read-only scheduled reconciliation.
# This job scrapes, reads the CRM, and stores proposals. It never writes.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
mkdir -p "$ROOT/data/logs"
STAMP="$(date -u +"%Y%m%dT%H%M%SZ")"
LOG="$ROOT/data/logs/sync-${STAMP}.log"
PYTHON="$ROOT/.venv/bin/python"

if [[ ! -x "$PYTHON" ]]; then
  echo "Missing ${PYTHON}. Create the project virtualenv before scheduling." >&2
  exit 1
fi

{
  echo "Read-only scheduled sync. CRM writes are not part of this job."
  "$PYTHON" -m bellhaven_sync.cli sync
} >>"$LOG" 2>&1

echo "Log: $LOG"
