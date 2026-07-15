#!/usr/bin/env bash
set -euo pipefail

GATEWAY_LOG="gateway.log"
PHOENIX_CONTAINER="selmakit-phoenix"
GATEWAY_PID=""

cleanup() {
    echo ""
    echo "Shutting down..."
    if [ -n "$GATEWAY_PID" ]; then
        kill "$GATEWAY_PID" 2>/dev/null || true
        wait "$GATEWAY_PID" 2>/dev/null || true
    fi
    docker stop "$PHOENIX_CONTAINER" >/dev/null 2>&1 || true
    echo "Done."
}

trap cleanup EXIT INT TERM

# Phoenix runs as a standalone container, not as a Python dependency: the
# arize-phoenix package pins pydantic-ai-slim<2 and crashes under pydantic-ai
# 2.x. The container exposes the UI (6006) and the OTLP/gRPC endpoint (4317)
# that selmakit/tracing.py exports spans to.
# A failure here (docker CLI present but daemon not running, image pull error,
# port already bound, …) must not abort the script — Phoenix is optional and the
# gateway runs fine without tracing.
if command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
    echo "Starting Phoenix container (UI: http://localhost:6006, OTLP: localhost:4317)..."
    docker rm -f "$PHOENIX_CONTAINER" >/dev/null 2>&1 || true
    if ! docker run -d --rm --name "$PHOENIX_CONTAINER" \
        -p 6006:6006 -p 4317:4317 \
        arizephoenix/phoenix:latest >/dev/null; then
        echo "WARNING: failed to start Phoenix container — continuing without tracing."
    fi
elif command -v docker >/dev/null 2>&1; then
    echo "WARNING: docker daemon not reachable — skipping Phoenix. Gateway runs without tracing."
else
    echo "WARNING: docker not found — skipping Phoenix. Gateway runs without tracing."
fi

echo "Starting selmakit gateway (log: $GATEWAY_LOG)..."
uv run gateway.py > "$GATEWAY_LOG" 2>&1 &
GATEWAY_PID=$!

echo "Starting dashboard..."
uv run streamlit run dashboard.py
