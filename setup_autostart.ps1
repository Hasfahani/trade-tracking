# Polymarket Trades Tracker - Auto-Start Setup
# This script sets up the server to start automatically with Windows

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Setting up Auto-Start" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

# Check if running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")

if (-not $isAdmin) {
    Write-Host "❌ This script must be run as Administrator!" -ForegroundColor Red
    Write-Host ""
    Write-Host "Please follow these steps:" -ForegroundColor Yellow
    Write-Host "1. Right-click on PowerShell"
    Write-Host "2. Select 'Run as Administrator'"
    Write-Host "3. Run this command:"
    Write-Host "   powershell -ExecutionPolicy Bypass -File c:\polymarket-trades-v1\setup_autostart.ps1"
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "✅ Running as Administrator" -ForegroundColor Green
Write-Host ""

# Kill any existing scheduled tasks
Write-Host "Removing existing scheduled task (if any)..." -ForegroundColor Yellow
Unregister-ScheduledTask -TaskName "PolymarketTracker" -Confirm:$false -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# Create the task action
Write-Host "Creating scheduled task..." -ForegroundColor Yellow

$taskAction = New-ScheduledTaskAction `
    -Execute "C:\polymarket-trades-v1\venv\Scripts\python.exe" `
    -Argument "-m uvicorn app.main:app --host 0.0.0.0 --port 8000" `
    -WorkingDirectory "C:\polymarket-trades-v1"

# Create trigger for system startup
$taskTrigger = New-ScheduledTaskTrigger -AtStartup

# Create task settings for reliability
$taskSettings = New-ScheduledTaskSettingsSet `
    -AllowStartIfOnBatteries `
    -DontStopIfGoingOnBatteries `
    -StartWhenAvailable `
    -RunOnlyIfNetworkAvailable:$false `
    -MultipleInstances IgnoreNew

# Register the task
Register-ScheduledTask `
    -TaskName "PolymarketTracker" `
    -Action $taskAction `
    -Trigger $taskTrigger `
    -Settings $taskSettings `
    -RunLevel Highest `
    -Description "Polymarket Trades Tracker - Auto-starts the live trading dashboard server" `
    -Force | Out-Null

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  ✅ AUTO-START SETUP COMPLETE!" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "What happens next:" -ForegroundColor Cyan
Write-Host "  1️⃣  Every time you restart your computer, the server auto-starts"
Write-Host "  2️⃣  Open browser: http://localhost:8000/wallets"
Write-Host "  3️⃣  Live tracking is automatically active (every 2 minutes)"
Write-Host "  4️⃣  Close browser, close everything, server keeps running!"
Write-Host ""
Write-Host "To start the server RIGHT NOW:" -ForegroundColor Yellow
Write-Host "  Start-ScheduledTask -TaskName 'PolymarketTracker'"
Write-Host ""
Write-Host "To see if it's running:" -ForegroundColor Yellow
Write-Host "  Get-ScheduledTask -TaskName 'PolymarketTracker' | Select-Object State"
Write-Host ""
Write-Host "To disable auto-start (remove from startup):" -ForegroundColor Yellow
Write-Host "  Unregister-ScheduledTask -TaskName 'PolymarketTracker' -Confirm:`$false"
Write-Host ""
Read-Host "Press Enter to close this window"
