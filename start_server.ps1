# Polymarket Trades Tracker - PowerShell Launcher
# This script ensures only one server instance runs at a time
# Usage: powershell -ExecutionPolicy Bypass -File start_server.ps1

# Kill any existing Python processes on port 8000
Write-Host "Checking for existing servers..." -ForegroundColor Yellow
$existingConnections = Get-NetTCPConnection -LocalPort 8000 -ErrorAction SilentlyContinue | Where-Object { $_.State -eq "Listen" }

if ($existingConnections) {
    $pids = $existingConnections.OwningProcess | Sort-Object -Unique
    foreach ($pid in $pids) {
        if ($pid -ne 0) {
            try {
                Stop-Process -Id $pid -Force -ErrorAction SilentlyContinue
                Write-Host "Stopped existing process (PID: $pid)" -ForegroundColor Green
            }
            catch { }
        }
    }
}

Start-Sleep -Seconds 2

# Start the server
Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Polymarket Trades Tracker" -ForegroundColor Cyan
Write-Host "  Live Trading Dashboard" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "🚀 Starting server..." -ForegroundColor Yellow
Write-Host "🌐 Access the app at: http://localhost:8000" -ForegroundColor Green
Write-Host "📊 Wallets page: http://localhost:8000/wallets" -ForegroundColor Green
Write-Host ""
Write-Host "📝 Press Ctrl+C to stop the server" -ForegroundColor Gray
Write-Host ""

Set-Location c:\polymarket-trades-v1

# Run server with auto-restart on crash
while ($true) {
    try {
        & ".\venv\Scripts\python.exe" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 2>&1
    }
    catch {
        Write-Host "Server error: $_" -ForegroundColor Red
    }
    
    Write-Host ""
    Write-Host "⚠️  Server stopped. Restarting in 5 seconds..." -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}
