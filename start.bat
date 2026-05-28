@echo off
setlocal

set GATEWAY_LOG=gateway.log
set PHOENIX_LOG=phoenix.log

echo Starting Phoenix (log: %PHOENIX_LOG%, UI: http://localhost:6006)...
start /b uv run phoenix serve > "%PHOENIX_LOG%" 2>&1

echo Starting selmakit gateway (log: %GATEWAY_LOG%)...
start /b uv run python gateway.py > "%GATEWAY_LOG%" 2>&1

echo Starting dashboard...
uv run streamlit run dashboard.py

echo.
echo Shutting down background processes...
taskkill /f /im python.exe /t > nul 2>&1
endlocal
