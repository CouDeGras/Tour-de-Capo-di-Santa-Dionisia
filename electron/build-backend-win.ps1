param(
    [switch]$RefreshRuntime
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Windows counterpart to build-backend.sh. Application source is restaged on
# every build, but the embedded Python runtime and every download are retained.
# A runtime is rebuilt only when its Python version, requirements.txt content,
# or this script's cache schema changes (or -RefreshRuntime is requested).
$Here = [System.IO.Path]::GetFullPath($PSScriptRoot)
$ProjectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $Here))
$Resources = [System.IO.Path]::GetFullPath((Join-Path $Here "resources"))
$AppResources = [System.IO.Path]::GetFullPath((Join-Path $Resources "app"))
$PythonRuntime = [System.IO.Path]::GetFullPath((Join-Path $Resources "venv"))
$CacheRoot = [System.IO.Path]::GetFullPath((Join-Path $Here ".build-cache\windows"))
$DownloadCache = Join-Path $CacheRoot "downloads"
$PipCache = Join-Path $CacheRoot "pip"

function Assert-PathBelow([string]$Path, [string]$Parent, [string]$Label) {
    $ResolvedPath = [System.IO.Path]::GetFullPath($Path)
    $ResolvedParent = [System.IO.Path]::GetFullPath($Parent).TrimEnd([System.IO.Path]::DirectorySeparatorChar)
    $Prefix = $ResolvedParent + [System.IO.Path]::DirectorySeparatorChar
    if (-not $ResolvedPath.StartsWith($Prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
        throw "$Label escaped its expected parent: $ResolvedPath"
    }
}

Assert-PathBelow $AppResources $Resources "App staging directory"
Assert-PathBelow $PythonRuntime $Resources "Python runtime directory"
Assert-PathBelow $CacheRoot $Here "Build cache directory"

New-Item -ItemType Directory -Path $Resources, $DownloadCache, $PipCache -Force | Out-Null

# Only source is cleaned every time. Runtime files remain in place so normal
# CSS/JS/Python/template edits never invoke pip or touch the network.
Write-Host "==> Restaging application source"
if (Test-Path -LiteralPath $AppResources) {
    Remove-Item -LiteralPath $AppResources -Recurse -Force
}
New-Item -ItemType Directory -Path $AppResources -Force | Out-Null

$SourceItems = @(
    "core",
    "dashboard",
    "static",
    "templates",
    "weather_mqtt.py",
    "manage.py",
    "requirements.txt"
)
foreach ($Item in $SourceItems) {
    $Source = Join-Path $ProjectRoot $Item
    if (-not (Test-Path -LiteralPath $Source)) {
        throw "Required source item is missing: $Source"
    }
    Copy-Item -LiteralPath $Source -Destination (Join-Path $AppResources $Item) -Recurse -Force
}
Get-ChildItem -LiteralPath $AppResources -Directory -Recurse -Filter "__pycache__" |
    Remove-Item -Recurse -Force

# Pin the runtime. CacheSchema should be incremented only when the way the
# runtime is assembled changes and an automatic one-time rebuild is required.
$PythonVersion = "3.11.9"
$CacheSchema = "2"
$RequirementsFile = Join-Path $ProjectRoot "requirements.txt"
$RequirementsHash = (Get-FileHash -LiteralPath $RequirementsFile -Algorithm SHA256).Hash.ToLowerInvariant()
$RuntimeKey = "python-$PythonVersion-amd64-requirements-$RequirementsHash-schema-$CacheSchema"
$RuntimeMarker = Join-Path $PythonRuntime ".capo-runtime-key"
$PythonExe = Join-Path $PythonRuntime "python.exe"

function Test-RuntimeImports {
    if (-not (Test-Path -LiteralPath $PythonExe)) { return $false }
    & $PythonExe -c "import django, requests, paho.mqtt.client, cryptography, tzdata" 2>$null
    return $LASTEXITCODE -eq 0
}

$ReuseRuntime = $false
if (-not $RefreshRuntime -and (Test-Path -LiteralPath $RuntimeMarker)) {
    $CachedKey = (Get-Content -LiteralPath $RuntimeMarker -Raw).Trim()
    if ($CachedKey -eq $RuntimeKey -and (Test-RuntimeImports)) {
        $ReuseRuntime = $true
    }
}

# Adopt a valid runtime created by the earlier uncached script once, avoiding
# an unnecessary download immediately after upgrading to this cached version.
if (-not $RefreshRuntime -and -not $ReuseRuntime -and
    -not (Test-Path -LiteralPath $RuntimeMarker) -and
    (Test-Path -LiteralPath $PythonExe)) {
    $DetectedVersion = (& $PythonExe -c "import sys; print('.'.join(map(str, sys.version_info[:3])))").Trim()
    if ($LASTEXITCODE -eq 0 -and $DetectedVersion -eq $PythonVersion -and (Test-RuntimeImports)) {
        [System.IO.File]::WriteAllText($RuntimeMarker, $RuntimeKey, [System.Text.Encoding]::ASCII)
        $ReuseRuntime = $true
        Write-Host "==> Adopted existing embedded Python runtime into the cache"
    }
}

if ($ReuseRuntime) {
    Write-Host "==> Reusing cached embedded Python $PythonVersion (no pip, no downloads)"
    Write-Host "==> Done. Staged source + cached runtime under $Resources"
    return
}

Write-Host "==> Runtime cache miss; assembling embedded Python $PythonVersion once"
if (Test-Path -LiteralPath $PythonRuntime) {
    Remove-Item -LiteralPath $PythonRuntime -Recurse -Force
}
New-Item -ItemType Directory -Path $PythonRuntime -Force | Out-Null

$PythonArchiveUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"
$PythonArchive = Join-Path $DownloadCache "python-$PythonVersion-embed-amd64.zip"
$GetPipScript = Join-Path $DownloadCache "get-pip.py"

if (-not (Test-Path -LiteralPath $PythonArchive)) {
    Write-Host "==> Downloading embedded Python archive into persistent cache"
    Invoke-WebRequest -Uri $PythonArchiveUrl -OutFile $PythonArchive
} else {
    Write-Host "==> Reusing cached embedded Python archive"
}
Expand-Archive -LiteralPath $PythonArchive -DestinationPath $PythonRuntime -Force

# The embeddable runtime disables site-packages by default. Enabling `site`
# lets pip-installed Django and the other requirements load normally.
$PythonPathFile = @(Get-ChildItem -LiteralPath $PythonRuntime -Filter "python*._pth")
if ($PythonPathFile.Count -ne 1) {
    throw "Expected one embedded-Python ._pth file; found $($PythonPathFile.Count)."
}
$PathLines = [System.IO.File]::ReadAllLines($PythonPathFile[0].FullName)
$PathLines = $PathLines | ForEach-Object {
    if ($_ -eq "#import site") { "import site" } else { $_ }
}
if ($PathLines -notcontains "..\app") {
    $PathLines += "..\app"
}
[System.IO.File]::WriteAllLines(
    $PythonPathFile[0].FullName,
    [string[]]$PathLines,
    [System.Text.Encoding]::ASCII
)

if (-not (Test-Path -LiteralPath $GetPipScript)) {
    Write-Host "==> Downloading pip bootstrap into persistent cache"
    Invoke-WebRequest -Uri $GetPipUrl -OutFile $GetPipScript
} else {
    Write-Host "==> Reusing cached pip bootstrap"
}

# pip's own HTTP/wheel cache is explicitly persistent. Even when a changed
# requirements file invalidates the runtime, already-downloaded wheels are
# reused rather than fetched again.
$env:PIP_CACHE_DIR = $PipCache
& $PythonExe $GetPipScript --no-warn-script-location --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "get-pip.py failed with exit code $LASTEXITCODE" }
& $PythonExe -m pip install --cache-dir $PipCache --no-warn-script-location --disable-pip-version-check -r $RequirementsFile
if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE" }
if (-not (Test-RuntimeImports)) { throw "Cached Python runtime failed its import validation." }

[System.IO.File]::WriteAllText($RuntimeMarker, $RuntimeKey, [System.Text.Encoding]::ASCII)
Write-Host "==> Cached runtime ready; future source-only builds will skip pip and downloads"
Write-Host "==> Done. Staged source + cached runtime under $Resources"
