[CmdletBinding()]
param(
    # Override with -PythonExecutable when the bundled runtime is elsewhere.
    [string]$PythonExecutable,
    [string]$NodeExecutable,
    [string]$PnpmExecutable,
    [switch]$ValidateOnly
)

$ErrorActionPreference = 'Stop'
$pythonRuntimePath = '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
$nodeRuntimePath = '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
$pnpmRuntimePath = '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\node_modules\pnpm\bin\pnpm.cjs'
if (-not $PythonExecutable) {
    if (-not $env:USERPROFILE) {
        throw 'USERPROFILE is unavailable; provide -PythonExecutable explicitly.'
    }
    $PythonExecutable = Join-Path $env:USERPROFILE $pythonRuntimePath
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $repositoryRoot '.venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'
$lockFile = Join-Path $repositoryRoot 'apps\backend\requirements.lock'

if (-not $NodeExecutable) {
    if (-not $env:USERPROFILE) {
        throw 'USERPROFILE is unavailable; provide -NodeExecutable explicitly.'
    }
    $NodeExecutable = Join-Path $env:USERPROFILE $nodeRuntimePath
}
if (-not $PnpmExecutable) {
    if (-not $env:USERPROFILE) {
        throw 'USERPROFILE is unavailable; provide -PnpmExecutable explicitly.'
    }
    $PnpmExecutable = Join-Path $env:USERPROFILE $pnpmRuntimePath
}

if (-not (Test-Path -LiteralPath $PythonExecutable)) {
    throw "Python 3.12 executable was not found: $PythonExecutable"
}
if (-not (Test-Path -LiteralPath $NodeExecutable)) {
    throw "Bundled Node.js 24.14.0 executable was not found: $NodeExecutable"
}
if (-not (Test-Path -LiteralPath $PnpmExecutable)) {
    throw "Bundled pnpm 11.9.0 entrypoint was not found: $PnpmExecutable"
}

& $PythonExecutable -c "import sys; assert sys.version_info >= (3, 11), sys.version"
if ($LASTEXITCODE -ne 0) {
    throw 'Python 3.11 or newer is required.'
}

$pythonVersion = & $PythonExecutable -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')"
if ($LASTEXITCODE -ne 0 -or $pythonVersion -ne '3.12') {
    throw "The local stack is locked to Python 3.12; selected version: $pythonVersion"
}

$nodeVersionText = & $NodeExecutable --version
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to run the selected Node.js executable.'
}
$nodeVersion = $nodeVersionText.Trim().TrimStart('v')
if ($nodeVersion -ne '24.14.0') {
    throw "The local stack is locked to Node.js 24.14.0; selected version: $nodeVersion"
}

$pnpmVersion = (& $NodeExecutable $PnpmExecutable --version).Trim()
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to run the selected pnpm entrypoint with the selected Node.js executable.'
}
if ($pnpmVersion -ne '11.9.0') {
    throw "The local stack is locked to pnpm 11.9.0; selected version: $pnpmVersion"
}

$selectedNodeDirectory = Split-Path -Parent $NodeExecutable
$env:Path = "$selectedNodeDirectory$([IO.Path]::PathSeparator)$env:Path"

if ($ValidateOnly) {
    Write-Output 'Validated Python 3.12, Node.js 24.14.0 and pnpm 11.9.0.'
    return
}

& $PythonExecutable -m venv $venvPath
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to create the workspace virtual environment.'
}

& $venvPython -m pip install --require-hashes --requirement $lockFile
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to install the hash-locked backend dependencies.'
}

Push-Location (Join-Path $repositoryRoot 'apps\frontend')
try {
    & $NodeExecutable $PnpmExecutable install --frozen-lockfile --config.confirmModulesPurge=false
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to install the frozen frontend lockfile.'
    }
}
finally {
    Pop-Location
}
