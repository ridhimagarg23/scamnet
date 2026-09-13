@echo off
REM run_all.bat - start the whole TraceAI / SCAMNET project on Windows.
REM
REM   run_all.bat                backend + dashboard
REM   run_all.bat --streamlit    + Streamlit UI
REM   run_all.bat --reload       backend restarts on file changes
REM
REM Prefers the project virtualenv; falls back to whatever python is on PATH.
setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" scripts\run_all.py %*
) else (
    python scripts\run_all.py %*
)

endlocal
