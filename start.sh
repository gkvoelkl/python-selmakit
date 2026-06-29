#!/usr/bin/env bash
set -euo pipefail

GATEWAY_LOG="gateway.log"
PHOENIX_CONTAINER="selmakit-phoenix"

cleanup() {
    echo ""
    echo "Shutting down..."
    kill "$GATEWAY_PID" 2>/dev/null || true
    wait "$GATEWAY_PID" 2>/dev/null || true
    docker stop "$PHOENIX_CONTAINER" >/dev/null 2>&1 || true
    echo "Done."
}

trap cleanup EXIT INT TERM

# Phoenix runs as a standalone container, not as a Python dependency: the
# arize-phoenix package pins pydantic-ai-slim<2 and crashes under pydantic-ai
# 2.x. The container exposes the UI (6006) and the OTLP/gRPC endpoint (4317)
# that selmakit/tracing.py exports spans to.
if command -v docker >/dev/null 2>&1; then
    echo "Starting Phoenix container (UI: http://localhost:6006, OTLP: localhost:4317)..."
    docker rm -f "$PHOENIX_CONTAINER" >/dev/null 2>&1 || true
    docker run -d --rm --name "$PHOENIX_CONTAINER" \
        -p 6006:6006 -p 4317:4317 \
        arizephoenix/phoenix:latest >/dev/null
else
    echo "WARNING: docker not found — skipping Phoenix. Gateway runs without tracing."
fi

echo "Starting selmakit gateway (log: $GATEWAY_LOG)..."
uv run gateway.py > "$GATEWAY_LOG" 2>&1 &
GATEWAY_PID=$!

echo "Starting dashboard..."
uv run streamlit run dashboard.py
