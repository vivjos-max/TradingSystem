[CmdletBinding()]
param(
    # Override with -PythonExecutable when the bundled runtime is elsewhere.
    [string]$PythonExecutable,
    [string]$NodeExecutable
)

$ErrorActionPreference = 'Stop'
$runtimePath = '.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
if (-not $PythonExecutable) {
    if (-not $env:USERPROFILE) {
        throw 'USERPROFILE is unavailable; provide -PythonExecutable explicitly.'
    }
    $PythonExecutable = Join-Path $env:USERPROFILE $runtimePath
}

$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $repositoryRoot '.venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'
$lockFile = Join-Path $repositoryRoot 'apps\backend\requirements.lock'

if (-not $NodeExecutable) {
    $nodeCommand = Get-Command node -ErrorAction SilentlyContinue
    if ($nodeCommand) {
        $NodeExecutable = $nodeCommand.Source
    }
    elseif ($env:USERPROFILE) {
        $NodeExecutable = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
    }
}

if (-not (Test-Path -LiteralPath $PythonExecutable)) {
    throw "Python 3.12 executable was not found: $PythonExecutable"
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
    throw 'Node.js was not found; Node 20.9 or newer is required.'
}
$nodeVersion = [Version]($nodeVersionText.TrimStart('v'))
if ($nodeVersion -lt [Version]'20.9.0') {
    throw "Node 20.9 or newer is required; selected version: $nodeVersion"
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
    & pnpm install --frozen-lockfile --config.confirmModulesPurge=false
    if ($LASTEXITCODE -ne 0) {
        throw 'Failed to install the frozen frontend lockfile.'
    }
}
finally {
    Pop-Location
}
