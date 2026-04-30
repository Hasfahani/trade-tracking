@echo off
REM PolySignal - Server Launcher
REM This script starts the FastAPI server persistently

title PolySignal Server

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PYTHON_EXE="
if exist "venv\Scripts\python.exe" set "PYTHON_EXE=venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"

if not defined PYTHON_EXE (
	echo No virtual environment Python found in .venv or venv.
	echo Create a venv and install dependencies first.
	pause
	exit /b 1
)

"%PYTHON_EXE%" -c "import uvicorn" >nul 2>&1
if errorlevel 1 (
	echo uvicorn is not installed in this environment.
	echo Run: "%PYTHON_EXE%" -m pip install -r requirements.txt
	pause
	exit /b 1
)

echo.
echo ========================================
echo   PolySignal Server
echo   Manual Refresh Watchlist
echo ========================================
echo.
if "%PORT%"=="" set "PORT=8000"
echo Starting server on http://localhost:%PORT%
echo Open browser and visit: http://localhost:%PORT%/wallets
echo.

:LOOP
REM Run server directly via the selected virtual environment Python
"%PYTHON_EXE%" -m uvicorn app.main:app --host 0.0.0.0 --port %PORT% --workers 1

REM If the server crashes, restart it after 5 seconds
echo.
echo Server stopped. Restarting in 5 seconds...
timeout /t 5

goto LOOP
