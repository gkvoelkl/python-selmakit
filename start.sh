#!/usr/bin/env bash
set -euo pipefail

GATEWAY_LOG="gateway.log"
PHOENIX_LOG="phoenix.log"

cleanup() {
    echo ""
    echo "Shutting down..."
    kill "$GATEWAY_PID" 2>/dev/null || true
    kill "$PHOENIX_PID" 2>/dev/null || true
    wait "$GATEWAY_PID" 2>/dev/null || true
    wait "$PHOENIX_PID" 2>/dev/null || true
    echo "Done."
}

trap cleanup EXIT INT TERM

echo "Starting Phoenix (log: $PHOENIX_LOG, UI: http://localhost:6006)..."
uv run phoenix serve > "$PHOENIX_LOG" 2>&1 &
PHOENIX_PID=$!

echo "Starting selmakit gateway (log: $GATEWAY_LOG)..."
uv run gateway.py > "$GATEWAY_LOG" 2>&1 &
GATEWAY_PID=$!

echo "Starting dashboard..."
uv run streamlit run dashboard.py
