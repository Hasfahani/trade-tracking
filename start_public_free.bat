@echo off
@REM Starts the app for free public tunnel access.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_public_free.ps1"
endlocal
