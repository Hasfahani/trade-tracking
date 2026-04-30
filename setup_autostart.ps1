# PolySignal - Auto-Start Setup
# This script sets up the server to start automatically with Windows

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  Setting up Auto-Start" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host ""

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$pythonCandidates = @(
    (Join-Path $scriptDir "venv\Scripts\python.exe"),
    (Join-Path $scriptDir ".venv\Scripts\python.exe")
)
$pythonExe = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $pythonExe) {
    Write-Host "No virtual environment Python found in project folder." -ForegroundColor Red
    Write-Host "Expected one of:" -ForegroundColor Red
    Write-Host "  $($pythonCandidates[0])" -ForegroundColor Red
    Write-Host "  $($pythonCandidates[1])" -ForegroundColor Red
    Read-Host "Press Enter to exit"
    exit 1
}

# Check if running as Administrator
$isAdmin = ([Security.Principal.WindowsPrincipal] [Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole] "Administrator")

if (-not $isAdmin) {
    Write-Host "This script must be run as Administrator." -ForegroundColor Red
    Write-Host ""
    Write-Host "Please follow these steps:" -ForegroundColor Yellow
    Write-Host "1. Right-click on PowerShell"
    Write-Host "2. Select 'Run as Administrator'"
    Write-Host "3. Run this command:"
    Write-Host "   powershell -ExecutionPolicy Bypass -File '$scriptDir\setup_autostart.ps1'"
    Write-Host ""
    Read-Host "Press Enter to exit"
    exit 1
}

Write-Host "Running as Administrator" -ForegroundColor Green
Write-Host ""

# Kill any existing scheduled tasks
Write-Host "Removing existing scheduled task (if any)..." -ForegroundColor Yellow
Unregister-ScheduledTask -TaskName "PolymarketTracker" -Confirm:$false -ErrorAction SilentlyContinue
Start-Sleep -Seconds 1

# Create the task action
Write-Host "Creating scheduled task..." -ForegroundColor Yellow

$launcherScript = Join-Path $scriptDir "start_server.ps1"

$taskAction = New-ScheduledTaskAction `
    -Execute "powershell.exe" `
    -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$launcherScript`"" `
    -WorkingDirectory $scriptDir

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
    -Description "PolySignal - Auto-starts the manual refresh watchlist server" `
    -Force | Out-Null

Write-Host ""
Write-Host "========================================" -ForegroundColor Green
Write-Host "  AUTO-START SETUP COMPLETE" -ForegroundColor Green
Write-Host "========================================" -ForegroundColor Green
Write-Host ""
Write-Host "What happens next:" -ForegroundColor Cyan
Write-Host "  1️⃣  Every time you restart your computer, the server auto-starts"
Write-Host "  2️⃣  Open browser: http://localhost:8000/wallets"
Write-Host "  3️⃣  Use manual refresh buttons in the app when you want new data"
Write-Host "  4️⃣  If the server crashes, it auto-restarts"
Write-Host "  5️⃣  Close browser, close everything, server keeps running!"
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
