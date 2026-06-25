@echo off
REM Summary: Runs the start public free batch helper.
REM Details: It is a Windows batch helper for starting or opening the app with simple double-click commands.
@REM Starts the app for free public tunnel access.
setlocal
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0start_public_free.ps1"
endlocal
