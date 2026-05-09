param(
  [string]$OutputZip = "",
  [switch]$SkipVerification
)

$ErrorActionPreference = "Stop"

$Root = Resolve-Path (Join-Path $PSScriptRoot "..")
$RootPath = $Root.Path
$VersionPath = Join-Path $RootPath "VERSION"
$ReleaseDir = Join-Path $RootPath "releases"
$StageDir = Join-Path $RootPath ".tmp\portable_zip_stage"

if (!(Test-Path -LiteralPath $VersionPath)) {
  throw "Missing VERSION file at repository root."
}

$Version = (Get-Content -LiteralPath $VersionPath -Raw -Encoding UTF8).Trim()
if ([string]::IsNullOrWhiteSpace($Version)) {
  throw "VERSION must not be empty."
}

if ([string]::IsNullOrWhiteSpace($OutputZip)) {
  $OutputZip = Join-Path $ReleaseDir "huolala-price-quote-tool-portable-latest.zip"
}

function Copy-RequiredFile($RelativePath) {
  $source = Join-Path $RootPath $RelativePath
  if (!(Test-Path -LiteralPath $source)) {
    throw "Missing required release file: $RelativePath"
  }
  $target = Join-Path $StageDir $RelativePath
  New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
  Copy-Item -LiteralPath $source -Destination $target -Force
}

function Test-ExcludedPath($RelativePath) {
  $parts = $RelativePath -split "[\\/]+"
  if ($parts -contains "__pycache__") {
    return $true
  }
  foreach ($part in $parts) {
    if ($part.EndsWith(".egg-info")) {
      return $true
    }
  }
  return $false
}

function Copy-CleanTree($RelativePath) {
  $sourceRoot = Join-Path $RootPath $RelativePath
  if (!(Test-Path -LiteralPath $sourceRoot)) {
    throw "Missing required release directory: $RelativePath"
  }
  $targetRoot = Join-Path $StageDir $RelativePath
  New-Item -ItemType Directory -Force -Path $targetRoot | Out-Null

  Get-ChildItem -LiteralPath $sourceRoot -Recurse -Force | ForEach-Object {
    $relative = $_.FullName.Substring($sourceRoot.Length).TrimStart("\", "/")
    if (![string]::IsNullOrWhiteSpace($relative) -and !(Test-ExcludedPath $relative)) {
      $target = Join-Path $targetRoot $relative
      if ($_.PSIsContainer) {
        New-Item -ItemType Directory -Force -Path $target | Out-Null
      } else {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $target) | Out-Null
        Copy-Item -LiteralPath $_.FullName -Destination $target -Force
      }
    }
  }
}

if (Test-Path -LiteralPath $StageDir) {
  Remove-Item -LiteralPath $StageDir -Recurse -Force
}
New-Item -ItemType Directory -Force -Path $StageDir, $ReleaseDir | Out-Null

foreach ($file in @(
  "VERSION",
  "README.md",
  "pyproject.toml",
  "requirements.txt",
  "一键启动查价工具.bat",
  "启动查价工具.bat",
  "打开查价工具网页.hta"
)) {
  Copy-RequiredFile $file
}

Copy-CleanTree "configs"
Copy-CleanTree "src"
Copy-CleanTree "scripts"
Copy-CleanTree "runtime\python"

New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "input") | Out-Null
New-Item -ItemType Directory -Force -Path (Join-Path $StageDir "output") | Out-Null
if (Test-Path -LiteralPath (Join-Path $RootPath "input\.gitkeep")) {
  Copy-RequiredFile "input\.gitkeep"
}
if (Test-Path -LiteralPath (Join-Path $RootPath "output\.gitkeep")) {
  Copy-RequiredFile "output\.gitkeep"
}

if (!(Test-Path -LiteralPath (Join-Path $StageDir "runtime\python\python.exe"))) {
  throw "Missing runtime\python\python.exe. Run scripts\prepare_portable_runtime.ps1 first."
}

if (Test-Path -LiteralPath $OutputZip) {
  Remove-Item -LiteralPath $OutputZip -Force
}

Compress-Archive -Path (Join-Path $StageDir "*") -DestinationPath $OutputZip -Force

if (!$SkipVerification) {
  Add-Type -AssemblyName System.IO.Compression.FileSystem
  $zip = [System.IO.Compression.ZipFile]::OpenRead($OutputZip)
  try {
    $names = @($zip.Entries | ForEach-Object { $_.FullName.Replace("\", "/") })
    foreach ($required in @(
      "VERSION",
      "configs/site.huolala.json",
      "src/price_quote_tool/server.py",
      "runtime/python/python.exe",
      "一键启动查价工具.bat",
      "启动查价工具.bat",
      "打开查价工具网页.hta"
    )) {
      if ($names -notcontains $required) {
        throw "ZIP verification failed; missing $required"
      }
    }
    foreach ($name in $names) {
      if (
        $name.StartsWith("data/") -or
        $name.StartsWith("runtime/downloads/") -or
        $name.StartsWith("outputs/") -or
        $name.Contains("/__pycache__/") -or
        $name.Contains(".egg-info/")
      ) {
        throw "ZIP verification failed; excluded path found: $name"
      }
    }
  } finally {
    $zip.Dispose()
  }
}

$sizeMb = [Math]::Round((Get-Item -LiteralPath $OutputZip).Length / 1MB, 2)
Write-Host "Built portable ZIP v$Version`: $OutputZip ($sizeMb MB)"
