@echo off
setlocal EnableExtensions
chcp 65001 >nul
title 批量查价工具 - 一键启动

set "APP_HOME=%LOCALAPPDATA%\HuolalaPriceQuoteTool"
set "APP_DIR=%APP_HOME%\app"
set "ZIP_PATH=%APP_HOME%\huolala-price-quote-tool-portable-latest.zip"
set "DOWNLOAD_URL=https://github.com/collinscallahang/huolala-price-quote-tool/raw/main/releases/huolala-price-quote-tool-portable-latest.zip"

echo.
echo 批量查价工具一键启动
echo.

if not exist "%APP_DIR%\启动查价工具.bat" (
  echo 首次使用：正在下载便携运行包。
  echo 下载地址：%DOWNLOAD_URL%
  echo 保存位置：%APP_HOME%
  echo.
  if not exist "%APP_HOME%" mkdir "%APP_HOME%"

  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop'; [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12; Invoke-WebRequest -Uri '%DOWNLOAD_URL%' -OutFile '%ZIP_PATH%'"
  if errorlevel 1 (
    echo.
    echo 下载失败。请检查网络是否能访问 GitHub，或让发送者直接发你便携版 ZIP。
    pause
    exit /b 1
  )

  echo.
  echo 正在解压运行包...
  powershell -NoProfile -ExecutionPolicy Bypass -Command ^
    "$ErrorActionPreference='Stop'; if (Test-Path '%APP_DIR%') { Remove-Item -LiteralPath '%APP_DIR%' -Recurse -Force }; New-Item -ItemType Directory -Force -Path '%APP_DIR%' | Out-Null; Expand-Archive -Path '%ZIP_PATH%' -DestinationPath '%APP_DIR%' -Force"
  if errorlevel 1 (
    echo.
    echo 解压失败。请确认当前用户目录有写入权限：%APP_HOME%
    pause
    exit /b 1
  )
)

if not exist "%APP_DIR%\启动查价工具.bat" (
  echo.
  echo 启动失败：运行包不完整，缺少“启动查价工具.bat”。
  echo 请删除 %APP_HOME% 后重新运行本脚本。
  pause
  exit /b 1
)

echo 正在启动本地服务...
start "批量查价工具服务" /min "%APP_DIR%\启动查价工具.bat" --no-browser

echo 正在等待网页服务启动...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ports=18765,8765,28765; for($i=0; $i -lt 60; $i++){ foreach($p in $ports){ $url='http://127.0.0.1:'+$p; try { Invoke-WebRequest -Uri ($url + '/api/config') -UseBasicParsing -TimeoutSec 2 | Out-Null; Start-Process $url; exit 0 } catch {} }; Start-Sleep -Seconds 1 }; exit 1"

if errorlevel 1 (
  echo.
  echo 服务没有启动成功。
  echo 请查看刚才打开的“批量查价工具服务”窗口里的中文错误提示。
  pause
  exit /b 1
)

echo.
echo 已打开查价控制页，可以开始使用。
timeout /t 2 >nul
