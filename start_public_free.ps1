# PolySignal free public launcher using Cloudflare Tunnel quick tunnels.
# Usage: powershell -ExecutionPolicy Bypass -File .\start_public_free.ps1

$ErrorActionPreference = "Stop"

$scriptDir = Split-Path -Parent $MyInvocation.MyCommand.Path
Set-Location $scriptDir

$port = if ($env:PORT) { [int]$env:PORT } else { 8000 }
$cloudflaredDir = Join-Path $scriptDir "tools"
$cloudflaredExe = Join-Path $cloudflaredDir "cloudflared.exe"
$cloudflaredUrl = "https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-windows-amd64.exe"

function New-Secret([int]$bytes = 32) {
    $buffer = New-Object byte[] $bytes
    [System.Security.Cryptography.RandomNumberGenerator]::Fill($buffer)
    return [Convert]::ToBase64String($buffer)
}

function Get-LocalPython {
    $pythonCandidates = @(
        (Join-Path $scriptDir ".venv\Scripts\python.exe"),
        (Join-Path $scriptDir "venv\Scripts\python.exe")
    )

    $pythonExe = $pythonCandidates | Where-Object { Test-Path $_ } | Select-Object -First 1
    if (-not $pythonExe) {
        Write-Host "No virtual environment Python found." -ForegroundColor Red
        Write-Host "Create it with: python -m venv .venv" -ForegroundColor Yellow
        Write-Host "Then install dependencies with: .\.venv\Scripts\python.exe -m pip install -r requirements.txt" -ForegroundColor Yellow
        exit 1
    }

    return $pythonExe
}

function Stop-ExistingListener([int]$targetPort) {
    $existingConnections = Get-NetTCPConnection -LocalPort $targetPort -ErrorAction SilentlyContinue |
        Where-Object { $_.State -eq "Listen" }

    if (-not $existingConnections) {
        return
    }

    $processIds = $existingConnections.OwningProcess | Sort-Object -Unique
    foreach ($processId in $processIds) {
        if ($processId -ne 0) {
            Stop-Process -Id $processId -Force -ErrorAction SilentlyContinue
            Write-Host "Stopped existing process on port $targetPort (PID: $processId)" -ForegroundColor Yellow
        }
    }
}

if (-not $env:DASHBOARD_PASSWORD) {
    $securePassword = Read-Host "Create a password for the public dashboard" -AsSecureString
    $plainPassword = [Runtime.InteropServices.Marshal]::PtrToStringUni(
        [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)
    )

    if (-not $plainPassword -or $plainPassword.Length -lt 8) {
        Write-Host "Password must be at least 8 characters before opening the app publicly." -ForegroundColor Red
        exit 1
    }

    $env:DASHBOARD_PASSWORD = $plainPassword
}

if (-not $env:SESSION_SECRET_KEY -or $env:SESSION_SECRET_KEY.Length -lt 32) {
    $env:SESSION_SECRET_KEY = New-Secret
}

$env:APP_ENV = if ($env:APP_ENV) { $env:APP_ENV } else { "production" }
$env:SESSION_COOKIE_SECURE = "true"
$env:CSRF_COOKIE_SECURE = "true"
$env:PORT = "$port"

$pythonExe = Get-LocalPython
& $pythonExe -c "import uvicorn" 2>$null
if ($LASTEXITCODE -ne 0) {
    Write-Host "uvicorn is not installed. Installing dependencies now..." -ForegroundColor Yellow
    & $pythonExe -m pip install -r requirements.txt
}

if (-not (Test-Path $cloudflaredExe)) {
    New-Item -ItemType Directory -Force -Path $cloudflaredDir | Out-Null
    Write-Host "Downloading cloudflared..." -ForegroundColor Yellow
    Invoke-WebRequest -Uri $cloudflaredUrl -OutFile $cloudflaredExe
}

Stop-ExistingListener $port

Write-Host ""
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "  PolySignal public free tunnel" -ForegroundColor Cyan
Write-Host "========================================" -ForegroundColor Cyan
Write-Host "Local app:  http://localhost:$port" -ForegroundColor Green
Write-Host "Public URL: watch the cloudflared output below for https://*.trycloudflare.com" -ForegroundColor Green
Write-Host ""
Write-Host "Keep this window open. Ctrl+C stops the app and the public URL." -ForegroundColor Gray
Write-Host ""

$server = Start-Process `
    -FilePath $pythonExe `
    -ArgumentList @("-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", "$port") `
    -WorkingDirectory $scriptDir `
    -NoNewWindow `
    -PassThru

try {
    Start-Sleep -Seconds 3
    & $cloudflaredExe tunnel --url "http://127.0.0.1:$port"
}
finally {
    if ($server -and -not $server.HasExited) {
        Stop-Process -Id $server.Id -Force -ErrorAction SilentlyContinue
    }
}
