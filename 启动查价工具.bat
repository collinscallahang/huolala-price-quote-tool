@echo off
setlocal
cd /d "%~dp0"

if /I "%~1"=="--no-browser" (
  set "PRICE_QUOTE_NO_BROWSER=1"
)

set "PYTHON_EXE=%~dp0runtime\python\python.exe"
if not exist "%PYTHON_EXE%" (
  if exist "%~dp0.venv\Scripts\python.exe" (
    set "PYTHON_EXE=%~dp0.venv\Scripts\python.exe"
  ) else (
    set "PYTHON_EXE=python"
  )
)

set "PYTHONPATH=%~dp0src"
"%PYTHON_EXE%" -m price_quote_tool.server

pause
