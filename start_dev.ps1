$port = if ($env:PORT) { [int]$env:PORT } else { 8000 }
$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$pythonCandidates = @(
    (Join-Path $scriptDir "venv\Scripts\python.exe"),
    (Join-Path $scriptDir ".venv\Scripts\python.exe"),
    (Join-Path $scriptDir ".venv313\Scripts\python.exe")
)

$pythonExe = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1

if (-not $pythonExe) {
    Write-Host "No virtual environment Python found." -ForegroundColor Red
    exit 1
}

& $pythonExe -m uvicorn app.main:app `
    --host 0.0.0.0 `
    --port $port `
    --reload `
    --reload-dir app `
    --reload-dir tests `
    --reload-exclude venv `
    --reload-exclude .venv `
    --reload-exclude .venv313 `
    --reload-exclude data
