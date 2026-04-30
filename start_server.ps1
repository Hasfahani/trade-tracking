# PolySignal - PowerShell Launcher
# This script ensures only one server instance runs at a time
# Usage: powershell -ExecutionPolicy Bypass -File start_server.ps1

# Resolve port (defaults to 8000)
$port = if ($env:PORT) { [int]$env:PORT } else { 8000 }

# Kill any existing Python processes on target port
Write-Host "Checking for existing servers..." -ForegroundColor Yellow
$existingConnections = Get-NetTCPConnection -LocalPort $port -ErrorAction SilentlyContinue | Where-Object { $_.State -eq "Listen" }

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
Write-Host "  PolySignal" -ForegroundColor Cyan
Write-Host "  Manual Refresh Watchlist" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""
Write-Host "Starting server..." -ForegroundColor Yellow
Write-Host "Access the app at: http://localhost:$port" -ForegroundColor Green
Write-Host "Wallets page: http://localhost:$port/wallets" -ForegroundColor Green
Write-Host ""
Write-Host "Press Ctrl+C to stop the server" -ForegroundColor Gray
Write-Host ""

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$pythonCandidates = @(
    (Join-Path $scriptDir "venv\Scripts\python.exe"),
    (Join-Path $scriptDir ".venv\Scripts\python.exe")
)

$pythonExe = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $pythonExe) {
    Write-Host "No virtual environment Python found." -ForegroundColor Red
    Write-Host "Expected one of:" -ForegroundColor Red
    Write-Host "  $($pythonCandidates[0])" -ForegroundColor Red
    Write-Host "  $($pythonCandidates[1])" -ForegroundColor Red
    Write-Host "Create a venv and install dependencies first." -ForegroundColor Yellow
    exit 1
}

& $pythonExe -c "import uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "uvicorn is not installed in this environment." -ForegroundColor Red
    Write-Host "Run: & '$pythonExe' -m pip install -r requirements.txt" -ForegroundColor Yellow
    exit 1
}

# Run server with auto-restart on crash
while ($true) {
    try {
        & $pythonExe -m uvicorn app.main:app --host 0.0.0.0 --port $port 2>&1
    }
    catch {
        Write-Host "Server error: $_" -ForegroundColor Red
    }
    
    Write-Host ""
    Write-Host "Server stopped. Restarting in 5 seconds..." -ForegroundColor Yellow
    Start-Sleep -Seconds 5
}
