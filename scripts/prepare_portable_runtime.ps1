param(
  [string]$PythonVersion = "3.13.7"
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$RuntimeDir = Join-Path $Root "runtime\python"
$DownloadDir = Join-Path $Root "runtime\downloads"
$TempDir = Join-Path $Root ".tmp"
$ZipPath = Join-Path $DownloadDir "python-$PythonVersion-embed-amd64.zip"
$GetPipPath = Join-Path $DownloadDir "get-pip.py"
$PythonUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"

New-Item -ItemType Directory -Force -Path $DownloadDir, $TempDir | Out-Null

if (!(Test-Path $ZipPath)) {
  Invoke-WebRequest -Uri $PythonUrl -OutFile $ZipPath
}

if (Test-Path $RuntimeDir) {
  Remove-Item -LiteralPath $RuntimeDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $RuntimeDir | Out-Null
Expand-Archive -LiteralPath $ZipPath -DestinationPath $RuntimeDir -Force

$PthPath = Join-Path $RuntimeDir "python313._pth"
if (Test-Path $PthPath) {
  $lines = Get-Content -LiteralPath $PthPath
  $updated = @()
  $hasSitePackages = $false
  $hasSourcePath = $false
  foreach ($line in $lines) {
    if ($line.Trim() -eq "Lib\site-packages") {
      $hasSitePackages = $true
    }
    if ($line.Trim() -eq "..\..\src") {
      $hasSourcePath = $true
    }
    if ($line.Trim() -eq "#import site") {
      $updated += "import site"
    } else {
      $updated += $line
    }
  }
  if (!$hasSitePackages) {
    $updated = @("Lib\site-packages") + $updated
  }
  if (!$hasSourcePath) {
    $updated = @("..\..\src") + $updated
  }
  Set-Content -LiteralPath $PthPath -Value $updated -Encoding ASCII
}

if (!(Test-Path $GetPipPath)) {
  Invoke-WebRequest -Uri $GetPipUrl -OutFile $GetPipPath
}

$env:TEMP = $TempDir
$env:TMP = $TempDir
& (Join-Path $RuntimeDir "python.exe") $GetPipPath --no-warn-script-location
& (Join-Path $RuntimeDir "python.exe") -m pip install --no-warn-script-location -r (Join-Path $Root "requirements.txt")

Write-Host "Portable runtime ready: $RuntimeDir"
