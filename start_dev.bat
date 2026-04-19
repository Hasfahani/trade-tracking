@echo off
set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

set "PYTHON_EXE="
if exist "venv\Scripts\python.exe" set "PYTHON_EXE=venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist ".venv\Scripts\python.exe" set "PYTHON_EXE=.venv\Scripts\python.exe"
if not defined PYTHON_EXE if exist ".venv313\Scripts\python.exe" set "PYTHON_EXE=.venv313\Scripts\python.exe"

if not defined PYTHON_EXE (
    echo No virtual environment Python found.
    exit /b 1
)

if "%PORT%"=="" set "PORT=8000"

"%PYTHON_EXE%" -m uvicorn app.main:app ^
  --host 0.0.0.0 ^
  --port %PORT% ^
  --reload ^
  --reload-dir app ^
  --reload-dir tests ^
  --reload-exclude venv ^
  --reload-exclude .venv ^
  --reload-exclude .venv313 ^
  --reload-exclude data
