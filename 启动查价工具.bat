@echo off
setlocal
cd /d "%~dp0"

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
