param(
  [string]$ZipPath = "",
  [switch]$KeepTemp
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$RootPath = $Root.Path
if ([string]::IsNullOrWhiteSpace($ZipPath)) {
  $ZipPath = Join-Path $RootPath "releases\huolala-price-quote-tool-portable-latest.zip"
}
if (!(Test-Path -LiteralPath $ZipPath)) {
  throw "Portable ZIP not found: $ZipPath"
}

function Get-FreePort {
  $listener = [System.Net.Sockets.TcpListener]::new([System.Net.IPAddress]::Parse("127.0.0.1"), 0)
  try {
    $listener.Start()
    return $listener.LocalEndpoint.Port
  } finally {
    $listener.Stop()
  }
}

$stamp = Get-Date -Format "yyyyMMdd_HHmmss_fff"
$SmokeRoot = Join-Path $RootPath ".tmp\portable_smoke_$stamp"
$ExtractDir = Join-Path $SmokeRoot "app"
$StdoutPath = Join-Path $SmokeRoot "server.out.log"
$StderrPath = Join-Path $SmokeRoot "server.err.log"
$process = $null
$oldNoBrowser = $env:PRICE_QUOTE_NO_BROWSER
$oldPort = $env:PRICE_QUOTE_PORT
$oldPythonPath = $env:PYTHONPATH

try {
  New-Item -ItemType Directory -Force -Path $ExtractDir | Out-Null
  Expand-Archive -LiteralPath $ZipPath -DestinationPath $ExtractDir -Force

  $python = Join-Path $ExtractDir "runtime\python\python.exe"
  if (!(Test-Path -LiteralPath $python)) {
    throw "Smoke failed: ZIP does not contain runtime\python\python.exe"
  }
  if (!(Test-Path -LiteralPath (Join-Path $ExtractDir "VERSION"))) {
    throw "Smoke failed: ZIP does not contain VERSION"
  }
  if (!(Test-Path -LiteralPath (Join-Path $ExtractDir "input"))) {
    throw "Smoke failed: ZIP does not contain input directory"
  }
  if (!(Test-Path -LiteralPath (Join-Path $ExtractDir "output"))) {
    throw "Smoke failed: ZIP does not contain output directory"
  }

  $port = Get-FreePort
  $baseUrl = "http://127.0.0.1:$port"
  $env:PRICE_QUOTE_NO_BROWSER = "1"
  $env:PRICE_QUOTE_PORT = [string]$port
  $env:PYTHONPATH = Join-Path $ExtractDir "src"

  $psi = New-Object System.Diagnostics.ProcessStartInfo
  $psi.FileName = $python
  $psi.Arguments = "-m price_quote_tool.server"
  $psi.WorkingDirectory = $ExtractDir
  $psi.UseShellExecute = $false
  $psi.CreateNoWindow = $true
  $process = [System.Diagnostics.Process]::Start($psi)

  $config = $null
  for ($i = 0; $i -lt 60; $i++) {
    if ($process.HasExited) {
      throw "Smoke failed: server exited early with code $($process.ExitCode)"
    }
    try {
      $config = Invoke-RestMethod -Uri "$baseUrl/api/config" -TimeoutSec 2
      break
    } catch {
      Start-Sleep -Seconds 1
    }
  }
  if ($null -eq $config) {
    throw "Smoke failed: /api/config did not respond"
  }
  if ([string]::IsNullOrWhiteSpace($config.app_version)) {
    throw "Smoke failed: /api/config missing app_version"
  }

  $homeResponse = Invoke-WebRequest -Uri $baseUrl -UseBasicParsing -TimeoutSec 5
  if ($homeResponse.Content -notmatch "/static/app.js") {
    throw "Smoke failed: home page did not render expected app shell"
  }

  $appJs = Invoke-WebRequest -Uri "$baseUrl/static/app.js" -UseBasicParsing -TimeoutSec 5
  if ($appJs.Content -notmatch "app_version") {
    throw "Smoke failed: static app.js did not include version display logic"
  }

  $inputFiles = Invoke-RestMethod -Uri "$baseUrl/api/input-files" -TimeoutSec 5
  if ($null -eq $inputFiles.files) {
    throw "Smoke failed: /api/input-files missing files list"
  }

  $openApi = Invoke-RestMethod -Uri "$baseUrl/openapi.json" -TimeoutSec 5
  $paths = @($openApi.paths.PSObject.Properties.Name)
  if ($paths -notcontains "/api/runs/{run_id}/download/{filename}") {
    throw "Smoke failed: result download endpoint missing from OpenAPI"
  }

  Write-Host "Portable smoke passed: $ZipPath"
  Write-Host "Version: $($config.app_version)"
  Write-Host "URL: $baseUrl"
} finally {
  $env:PRICE_QUOTE_NO_BROWSER = $oldNoBrowser
  $env:PRICE_QUOTE_PORT = $oldPort
  $env:PYTHONPATH = $oldPythonPath
  if ($process) {
    $runningProcess = Get-Process -Id $process.Id -ErrorAction SilentlyContinue
    if ($runningProcess -and !$runningProcess.HasExited) {
      Stop-Process -Id $process.Id -Force -ErrorAction SilentlyContinue
    }
  }
  if (!$KeepTemp -and (Test-Path -LiteralPath $SmokeRoot)) {
    Remove-Item -LiteralPath $SmokeRoot -Recurse -Force -ErrorAction SilentlyContinue
  } elseif ($KeepTemp) {
    Write-Host "Smoke temp kept at: $SmokeRoot"
  }
}
