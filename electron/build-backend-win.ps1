$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# Windows counterpart to build-backend.sh. It stages the same source
# whitelist, but uses Python's official embeddable distribution instead of
# copying a virtual environment tied to the build machine.
$Here = [System.IO.Path]::GetFullPath($PSScriptRoot)
$ProjectRoot = [System.IO.Path]::GetFullPath((Split-Path -Parent $Here))
$Resources = [System.IO.Path]::GetFullPath((Join-Path $Here "resources"))
$AppResources = Join-Path $Resources "app"
$PythonRuntime = Join-Path $Resources "venv"

# Guard the only recursive deletion in this script. The resolved target must
# be the electron/resources directory and must remain below electron/.
$ExpectedResources = [System.IO.Path]::GetFullPath((Join-Path $Here "resources"))
$HerePrefix = $Here.TrimEnd([System.IO.Path]::DirectorySeparatorChar) + [System.IO.Path]::DirectorySeparatorChar
if ($Resources -ne $ExpectedResources -or -not $Resources.StartsWith($HerePrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "Refusing to clean unexpected resources path: $Resources"
}

Write-Host "==> Cleaning $Resources"
if (Test-Path -LiteralPath $Resources) {
    Remove-Item -LiteralPath $Resources -Recurse -Force
}
New-Item -ItemType Directory -Path $AppResources -Force | Out-Null

Write-Host "==> Copying Python project source"
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

# Pin the runtime so Windows builds are reproducible. Update this deliberately
# when moving to another supported Python patch release.
$PythonVersion = "3.11.9"
$PythonArchiveUrl = "https://www.python.org/ftp/python/$PythonVersion/python-$PythonVersion-embed-amd64.zip"
$GetPipUrl = "https://bootstrap.pypa.io/get-pip.py"
$PythonArchive = Join-Path $Resources "python-embed.zip"
$GetPipScript = Join-Path $Resources "get-pip.py"

Write-Host "==> Downloading embedded Python $PythonVersion"
Invoke-WebRequest -Uri $PythonArchiveUrl -OutFile $PythonArchive
New-Item -ItemType Directory -Path $PythonRuntime -Force | Out-Null
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
# Unlike a conventional Python install, the embeddable runtime does not put
# the launched script's directory on sys.path. The staged project is a sibling
# of the runtime directory, so make that explicit for imports such as
# `core.settings` and `dashboard`.
$PathLines += "..\app"
[System.IO.File]::WriteAllLines(
    $PythonPathFile[0].FullName,
    [string[]]$PathLines,
    [System.Text.Encoding]::ASCII
)

Write-Host "==> Installing Python dependencies into the embedded runtime"
Invoke-WebRequest -Uri $GetPipUrl -OutFile $GetPipScript
$PythonExe = Join-Path $PythonRuntime "python.exe"
& $PythonExe $GetPipScript --no-warn-script-location --disable-pip-version-check
if ($LASTEXITCODE -ne 0) { throw "get-pip.py failed with exit code $LASTEXITCODE" }
& $PythonExe -m pip install --no-warn-script-location --disable-pip-version-check -r (Join-Path $ProjectRoot "requirements.txt")
if ($LASTEXITCODE -ne 0) { throw "pip install failed with exit code $LASTEXITCODE" }

Remove-Item -LiteralPath $PythonArchive, $GetPipScript -Force
Write-Host "==> Done. Staged app + embedded Python under $Resources"
