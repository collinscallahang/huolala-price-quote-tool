@echo off
setlocal EnableExtensions
chcp 65001 >nul
title 批量查价工具 - 一键启动

set "APP_HOME=%LOCALAPPDATA%\HuolalaPriceQuoteTool"
set "APP_DIR=%APP_HOME%\app"
set "ZIP_PATH=%APP_HOME%\huolala-price-quote-tool-portable-latest.zip"
set "DOWNLOAD_URL=https://github.com/collinscallahang/huolala-price-quote-tool/raw/main/releases/huolala-price-quote-tool-portable-latest.zip"
set "VERSION_URL=https://raw.githubusercontent.com/collinscallahang/huolala-price-quote-tool/main/VERSION"

set "PQ_SELF_PATH=%~f0"
set "PQ_APP_HOME=%APP_HOME%"
set "PQ_APP_DIR=%APP_DIR%"
set "PQ_ZIP_PATH=%ZIP_PATH%"
set "PQ_DOWNLOAD_URL=%DOWNLOAD_URL%"
set "PQ_VERSION_URL=%VERSION_URL%"

echo.
echo 批量查价工具一键启动
echo.

powershell -NoProfile -ExecutionPolicy Bypass -Command "$raw=[System.IO.File]::ReadAllText($env:PQ_SELF_PATH,[System.Text.Encoding]::UTF8); $script=($raw -split '# POWERSHELL_PAYLOAD',2)[1]; Invoke-Expression $script"
if errorlevel 1 (
  echo.
  echo 安装或更新失败。请检查网络是否能访问 GitHub，或让发送者直接发你便携版 ZIP。
  pause
  exit /b 1
)

if not exist "%APP_DIR%\启动查价工具.bat" (
  echo.
  echo 启动失败：运行包不完整，缺少“启动查价工具.bat”。
  echo 请删除 %APP_HOME% 后重新运行本脚本。
  pause
  exit /b 1
)

echo.
echo 正在启动本地服务...
if exist "%APP_DIR%\outputs\server_url.txt" del /q "%APP_DIR%\outputs\server_url.txt" >nul 2>nul
start "批量查价工具服务" /min "%APP_DIR%\启动查价工具.bat" --no-browser

echo 正在等待网页服务启动...
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$appDir=$env:PQ_APP_DIR; $urlFile=Join-Path $appDir 'outputs\server_url.txt'; $versionPath=Join-Path $appDir 'VERSION'; $expected=''; if(Test-Path -LiteralPath $versionPath){ $expected=(Get-Content -LiteralPath $versionPath -Raw -Encoding UTF8).Trim() }; for($i=0; $i -lt 90; $i++){ if(Test-Path -LiteralPath $urlFile){ $url=(Get-Content -LiteralPath $urlFile -Raw -Encoding UTF8).Trim(); if($url){ try { $config=Invoke-RestMethod -Uri ($url + '/api/config') -TimeoutSec 2; if(!$expected -or $config.app_version -eq $expected){ Start-Process $url; exit 0 } } catch {} } }; Start-Sleep -Seconds 1 }; exit 1"

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
exit /b 0

# POWERSHELL_PAYLOAD
$ErrorActionPreference = "Stop"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$appHome = $env:PQ_APP_HOME
$appDir = $env:PQ_APP_DIR
$zipPath = $env:PQ_ZIP_PATH
$downloadUrl = $env:PQ_DOWNLOAD_URL
$versionUrl = $env:PQ_VERSION_URL
$stagingDir = Join-Path $appHome "staging"
$backupDir = Join-Path $appHome "user-data-backup"

function Read-VersionFile($path) {
  if (!(Test-Path -LiteralPath $path)) {
    return ""
  }
  return (Get-Content -LiteralPath $path -Raw -Encoding UTF8).Trim()
}

function Test-UsableApp {
  return (Test-Path -LiteralPath (Join-Path $appDir "启动查价工具.bat"))
}

function Save-UserData {
  if (Test-Path -LiteralPath $backupDir) {
    Remove-Item -LiteralPath $backupDir -Recurse -Force
  }
  New-Item -ItemType Directory -Force -Path $backupDir | Out-Null
  foreach ($relativePath in @("data\browser-profile", "data\browser-storage-state.json", "input", "output", "outputs")) {
    $source = Join-Path $appDir $relativePath
    if (!(Test-Path -LiteralPath $source)) {
      continue
    }
    $target = Join-Path $backupDir $relativePath
    $targetParent = Split-Path -Parent $target
    New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
    Move-Item -LiteralPath $source -Destination $target -Force
  }
}

function Restore-UserData {
  if (!(Test-Path -LiteralPath $backupDir)) {
    return
  }
  foreach ($relativePath in @("data\browser-profile", "data\browser-storage-state.json", "input", "output", "outputs")) {
    $source = Join-Path $backupDir $relativePath
    if (!(Test-Path -LiteralPath $source)) {
      continue
    }
    $target = Join-Path $appDir $relativePath
    $targetParent = Split-Path -Parent $target
    New-Item -ItemType Directory -Force -Path $targetParent | Out-Null
    if (Test-Path -LiteralPath $target) {
      Remove-Item -LiteralPath $target -Recurse -Force
    }
    Move-Item -LiteralPath $source -Destination $target -Force
  }
  Remove-Item -LiteralPath $backupDir -Recurse -Force
}

function Merge-UserConfig($oldConfigJson) {
  if ([string]::IsNullOrWhiteSpace($oldConfigJson)) {
    return
  }
  $newConfigPath = Join-Path $appDir "configs\site.huolala.json"
  if (!(Test-Path -LiteralPath $newConfigPath)) {
    return
  }
  try {
    $oldConfig = $oldConfigJson | ConvertFrom-Json
    $newConfig = Get-Content -LiteralPath $newConfigPath -Raw -Encoding UTF8 | ConvertFrom-Json
    foreach ($key in @("default_input_dir", "output_root", "keep_browser_open_after_run")) {
      if ($oldConfig.PSObject.Properties.Name -contains $key) {
        $value = $oldConfig.$key
        if ($newConfig.PSObject.Properties.Name -contains $key) {
          $newConfig.$key = $value
        } else {
          $newConfig | Add-Member -NotePropertyName $key -NotePropertyValue $value
        }
      }
    }
    $newConfig | ConvertTo-Json -Depth 20 | Set-Content -LiteralPath $newConfigPath -Encoding UTF8
  } catch {
    Write-Host "提示：旧目录设置继承失败，将使用新包默认设置。"
  }
}

function Install-Package($reason) {
  Write-Host $reason
  Write-Host "下载地址：$downloadUrl"
  Write-Host "保存位置：$appHome"
  New-Item -ItemType Directory -Force -Path $appHome | Out-Null

  Invoke-WebRequest -Uri $downloadUrl -OutFile $zipPath -UseBasicParsing -TimeoutSec 180

  if (Test-Path -LiteralPath $stagingDir) {
    Remove-Item -LiteralPath $stagingDir -Recurse -Force
  }
  New-Item -ItemType Directory -Force -Path $stagingDir | Out-Null
  Expand-Archive -LiteralPath $zipPath -DestinationPath $stagingDir -Force

  if (!(Test-Path -LiteralPath (Join-Path $stagingDir "启动查价工具.bat"))) {
    throw "运行包不完整：缺少 启动查价工具.bat"
  }
  if (!(Test-Path -LiteralPath (Join-Path $stagingDir "VERSION"))) {
    throw "运行包不完整：缺少 VERSION"
  }

  $oldConfigPath = Join-Path $appDir "configs\site.huolala.json"
  $oldConfigJson = ""
  if (Test-Path -LiteralPath $oldConfigPath) {
    $oldConfigJson = Get-Content -LiteralPath $oldConfigPath -Raw -Encoding UTF8
  }

  if (Test-Path -LiteralPath $appDir) {
    Save-UserData
    Remove-Item -LiteralPath $appDir -Recurse -Force
  }
  Move-Item -LiteralPath $stagingDir -Destination $appDir -Force
  Restore-UserData
  Merge-UserConfig $oldConfigJson

  $installedVersion = Read-VersionFile (Join-Path $appDir "VERSION")
  Write-Host "已安装版本：$installedVersion"
}

New-Item -ItemType Directory -Force -Path $appHome | Out-Null
$localVersion = Read-VersionFile (Join-Path $appDir "VERSION")
$remoteVersion = ""

try {
  $remoteVersion = (Invoke-WebRequest -Uri $versionUrl -UseBasicParsing -TimeoutSec 20).Content.Trim()
  if ([string]::IsNullOrWhiteSpace($remoteVersion)) {
    throw "远端 VERSION 为空"
  }
  Write-Host "最新版本：$remoteVersion"
  if ($localVersion) {
    Write-Host "本地版本：$localVersion"
  }
} catch {
  if (Test-UsableApp) {
    Write-Host "无法检查最新版本，将继续使用本地已安装版本。"
    exit 0
  }
  try {
    Install-Package "首次使用：无法读取版本文件，正在尝试直接下载便携运行包。"
    exit 0
  } catch {
    Write-Host "首次安装失败：$($_.Exception.Message)"
    exit 1
  }
}

try {
  if (!(Test-UsableApp)) {
    Install-Package "首次使用：正在下载便携运行包。"
  } elseif ($localVersion -ne $remoteVersion) {
    Install-Package "发现新版本，正在更新便携运行包。"
  } else {
    Write-Host "已是最新版本，无需下载。"
  }
  exit 0
} catch {
  if (Test-UsableApp) {
    Write-Host "更新失败，将继续使用本地已安装版本。错误：$($_.Exception.Message)"
    exit 0
  }
  Write-Host "安装失败：$($_.Exception.Message)"
  exit 1
}
