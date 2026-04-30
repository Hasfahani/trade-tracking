$port = if ($env:PORT) { [int]$env:PORT } else { 8000 }
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
$startScript = Join-Path $scriptDir "start_dev.ps1"

if (-not (Test-Path $startScript)) {
    Write-Host "Could not find start_dev.ps1 in $scriptDir" -ForegroundColor Red
    exit 1
}

Start-Process powershell -ArgumentList @(
    "-NoExit",
    "-ExecutionPolicy", "Bypass",
    "-File", "`"$startScript`""
)

Start-Sleep -Seconds 2
Start-Process "http://localhost:$port/wallets"

Write-Host "Server launcher started. Opening browser at http://localhost:$port/wallets" -ForegroundColor Green
