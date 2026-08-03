[CmdletBinding()]
param(
    [string]$PythonExecutable = 'C:\Users\Home\.cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe'
)

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$venvPath = Join-Path $repositoryRoot '.venv'
$venvPython = Join-Path $venvPath 'Scripts\python.exe'
$lockFile = Join-Path $repositoryRoot 'apps\backend\requirements.lock'

if (-not (Test-Path -LiteralPath $PythonExecutable)) {
    throw "Python 3.12 executable was not found: $PythonExecutable"
}

& $PythonExecutable -c "import sys; assert sys.version_info[:2] == (3, 12), sys.version"
if ($LASTEXITCODE -ne 0) {
    throw 'The selected Python executable is not Python 3.12.'
}

& $PythonExecutable -m venv $venvPath
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to create the workspace virtual environment.'
}

& $venvPython -m pip install --require-hashes --requirement $lockFile
if ($LASTEXITCODE -ne 0) {
    throw 'Failed to install the hash-locked backend dependencies.'
}
