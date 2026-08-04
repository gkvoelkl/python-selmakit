@echo off
setlocal

set GATEWAY_LOG=gateway.log
set PHOENIX_CONTAINER=selmakit-phoenix

REM Phoenix runs as a standalone container, not as a Python dependency: the
REM arize-phoenix package is the full Phoenix server and would drag ~43 extra
REM packages into the agent venv.
REM The container exposes the UI and OTLP/HTTP collector on port 6006 (/v1/traces)
REM that selmakit/tracing.py exports spans to.
where docker >nul 2>&1
if %errorlevel%==0 (
    echo Starting Phoenix container ^(UI + OTLP/HTTP: http://localhost:6006^)...
    docker rm -f %PHOENIX_CONTAINER% >nul 2>&1
    docker run -d --rm --name %PHOENIX_CONTAINER% -p 6006:6006 arizephoenix/phoenix:latest >nul
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
