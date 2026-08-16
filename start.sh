#!/usr/bin/env bash
set -euo pipefail

GATEWAY_LOG="gateway.log"
GATEWAY_PID=""

cleanup() {
    echo ""
    echo "Shutting down..."
    if [ -n "$GATEWAY_PID" ]; then
        kill "$GATEWAY_PID" 2>/dev/null || true
        wait "$GATEWAY_PID" 2>/dev/null || true
    fi
    echo "Done."
}

trap cleanup EXIT INT TERM

echo "Starting selmakit gateway (log: $GATEWAY_LOG)..."
uv run gateway.py > "$GATEWAY_LOG" 2>&1 &
GATEWAY_PID=$!

echo "Starting dashboard..."
uv run streamlit run dashboard.py
