$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

# electron-builder's native downloader honors the conventional proxy
# environment variables, but not npm's proxy configuration. Reuse the npm
# setting when present so `npm run build:win` behaves consistently on both
# proxied and direct connections.
if (-not $env:HTTPS_PROXY) {
    $ConfiguredProxy = (& npm.cmd config get https-proxy).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Unable to read npm's https-proxy setting." }
    if ($ConfiguredProxy -and $ConfiguredProxy -ne "null") {
        $env:HTTPS_PROXY = $ConfiguredProxy
    }
}
if (-not $env:HTTP_PROXY) {
    $ConfiguredProxy = (& npm.cmd config get proxy).Trim()
    if ($LASTEXITCODE -ne 0) { throw "Unable to read npm's proxy setting." }
    if ($ConfiguredProxy -and $ConfiguredProxy -ne "null") {
        $env:HTTP_PROXY = $ConfiguredProxy
    }
}

$BuildVersion = (& node.exe (Join-Path $PSScriptRoot "build-version.js")).Trim()
if ($LASTEXITCODE -ne 0) { throw "Unable to generate the date build version." }
if ($BuildVersion -notmatch '^\d{2}\.(0?[1-9]|1[0-2])\.(0?[1-9]|[12]\d|3[01])$') {
    throw "Invalid generated build version: $BuildVersion"
}
Write-Host "==> Building Windows installer version $BuildVersion"

& (Join-Path $PSScriptRoot "build-backend-win.ps1")
if ($LASTEXITCODE -ne 0) { throw "Windows backend staging failed with exit code $LASTEXITCODE" }

$Builder = Join-Path $PSScriptRoot "node_modules\.bin\electron-builder.cmd"
if (-not (Test-Path -LiteralPath $Builder)) {
    throw "electron-builder is not installed. Run 'npm install' in electron/ first."
}
& $Builder --win nsis --x64 "-c.extraMetadata.version=$BuildVersion"
if ($LASTEXITCODE -ne 0) { throw "Windows packaging failed with exit code $LASTEXITCODE" }
