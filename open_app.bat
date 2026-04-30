@echo off
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if "%PORT%"=="" set "PORT=8000"

start "Polymarket App Server" powershell -NoExit -ExecutionPolicy Bypass -File "%SCRIPT_DIR%start_dev.ps1"
timeout /t 2 >nul
start "" "http://localhost:%PORT%/wallets"

echo Server launcher started. Opening browser at http://localhost:%PORT%/wallets
