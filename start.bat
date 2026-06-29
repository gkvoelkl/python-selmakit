@echo off
setlocal

set GATEWAY_LOG=gateway.log
set PHOENIX_CONTAINER=selmakit-phoenix

REM Phoenix runs as a standalone container, not as a Python dependency: the
REM arize-phoenix package pins pydantic-ai-slim<2 and crashes under pydantic-ai
REM 2.x. The container exposes the UI (6006) and the OTLP/gRPC endpoint (4317)
REM that selmakit/tracing.py exports spans to.
where docker >nul 2>&1
if %errorlevel%==0 (
    echo Starting Phoenix container ^(UI: http://localhost:6006, OTLP: localhost:4317^)...
    docker rm -f %PHOENIX_CONTAINER% >nul 2>&1
    docker run -d --rm --name %PHOENIX_CONTAINER% -p 6006:6006 -p 4317:4317 arizephoenix/phoenix:latest >nul
) else (
    echo WARNING: docker not found - skipping Phoenix. Gateway runs without tracing.
)

echo Starting selmakit gateway (log: %GATEWAY_LOG%)...
start /b uv run python gateway.py > "%GATEWAY_LOG%" 2>&1

echo Starting dashboard...
uv run streamlit run dashboard.py

echo.
echo Shutting down background processes...
taskkill /f /im python.exe /t > nul 2>&1
docker stop %PHOENIX_CONTAINER% >nul 2>&1
endlocal
