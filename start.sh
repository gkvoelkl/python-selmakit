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

# Wait for the web chat port before starting the dashboard: a gateway that
# dies on startup would otherwise only show up as "Gateway unreachable".
GATEWAY_PORT=$(python3 -c 'import json; print(json.load(open(".selmakit/selmakit.json"))["channels"]["webchat"].get("port", 8000))' 2>/dev/null || echo 8000)
for _ in $(seq 1 60); do
    if ! kill -0 "$GATEWAY_PID" 2>/dev/null; then
        GATEWAY_PID=""
        echo ""
        echo "Gateway exited during startup. Last lines of $GATEWAY_LOG:"
        echo "------------------------------------------------------------"
        tail -n 20 "$GATEWAY_LOG"
        exit 1
    fi
    if lsof -nP -iTCP:"$GATEWAY_PORT" -sTCP:LISTEN >/dev/null 2>&1; then
        echo "Gateway is up on port $GATEWAY_PORT."
        break
    fi
    sleep 1
done

echo "Starting dashboard..."
uv run streamlit run dashboard.py
