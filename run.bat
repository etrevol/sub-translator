@echo off
rem Double-click this file to translate everything in input\ with the defaults.
rem Any arguments are passed straight through, e.g.  run.bat --engine google
rem
rem Prefers the project's own .venv so nothing has to be installed globally.
rem If there is no .venv yet, it falls back to whatever python is on PATH and
rem lets subtrans.py print the setup instructions.

setlocal
cd /d "%~dp0"

if exist ".venv\Scripts\python.exe" (
    set "PY=.venv\Scripts\python.exe"
) else (
    set "PY=python"
)

"%PY%" subtrans.py %*
set "CODE=%ERRORLEVEL%"

echo.
if not "%CODE%"=="0" echo Finished with exit code %CODE%.
pause
exit /b %CODE%
