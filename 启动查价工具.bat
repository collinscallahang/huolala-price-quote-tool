@echo off
setlocal
cd /d "%~dp0"
title 批量查价工具 - 本地服务

echo.
echo 正在启动批量查价工具...
echo 当前目录：%CD%
echo.

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

echo 使用 Python：%PYTHON_EXE%
echo.

if not exist "%~dp0src\price_quote_tool\server.py" (
  echo 启动失败：没有找到 src\price_quote_tool\server.py
  echo 请确认你下载的是完整工具包，并且已经解压后再双击启动。
  echo.
  pause
  exit /b 1
)

if not exist "%~dp0runtime\python\python.exe" (
  echo 提示：当前文件夹不是便携成品包，未找到 runtime\python\python.exe。
  echo 如果这是从 GitHub 直接下载的源码 ZIP，另一台电脑通常不能直接运行。
  echo 请下载 README 里提供的“便携版 ZIP”，或先安装 Python 依赖。
  echo.
)

set "PYTHONPATH=%~dp0src"
"%PYTHON_EXE%" -m price_quote_tool.server

if errorlevel 1 (
  echo.
  echo 启动失败。
  echo 常见原因：
  echo 1. 下载的是 GitHub 源码 ZIP，不是便携版 ZIP。
  echo 2. 当前电脑没有 Python 或缺少依赖。
  echo 3. 安全软件拦截了本地服务。
  echo.
)

pause
