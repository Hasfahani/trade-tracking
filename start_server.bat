@echo off
REM Polymarket Trades Tracker - Server Launcher
REM This script starts the FastAPI server persistently

title Polymarket Trades Tracker Server

cd /d "c:\polymarket-trades-v1"

echo.
echo ========================================
echo   Polymarket Trades Tracker Server
echo   Version 1.0
echo ========================================
echo.
echo Starting server on http://localhost:8000
echo Open browser and visit: http://localhost:8000/wallets
echo.

:LOOP
REM Activate virtual environment and run server
call venv\Scripts\activate.bat
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 1

REM If the server crashes, restart it after 5 seconds
echo.
echo Server stopped. Restarting in 5 seconds...
timeout /t 5

goto LOOP
