[CmdletBinding()]
param([switch]$Integration)

$ErrorActionPreference = 'Stop'
$repositoryRoot = Split-Path -Parent $PSScriptRoot
$python = Join-Path $repositoryRoot '.venv\Scripts\python.exe'
$nodeCommand = Get-Command node -ErrorAction SilentlyContinue
if (-not $nodeCommand -and $env:USERPROFILE) {
    $bundledNode = Join-Path $env:USERPROFILE '.cache\codex-runtimes\codex-primary-runtime\dependencies\node\bin\node.exe'
    if (Test-Path -LiteralPath $bundledNode) {
        $env:Path = (Split-Path -Parent $bundledNode) + ';' + $env:Path
    }
}

if (-not (Test-Path -LiteralPath $python)) {
    throw 'Run scripts\bootstrap.ps1 before running tests.'
}

& $python -m pytest `
    (Join-Path $repositoryRoot 'apps\backend\tests\unit') `
    (Join-Path $repositoryRoot 'apps\backend\tests\contract') -q
if ($LASTEXITCODE -ne 0) { throw 'Backend unit/contract tests failed.' }

Push-Location (Join-Path $repositoryRoot 'apps\frontend')
try {
    & .\node_modules\.bin\vitest.cmd run
    if ($LASTEXITCODE -ne 0) { throw 'Frontend tests failed.' }
    & .\node_modules\.bin\eslint.cmd .
    if ($LASTEXITCODE -ne 0) { throw 'Frontend lint failed.' }
    & .\node_modules\.bin\next.cmd build
    if ($LASTEXITCODE -ne 0) { throw 'Frontend build failed.' }
}
finally {
    Pop-Location
}

if ($Integration) {
    & $python -m pytest `
        (Join-Path $repositoryRoot 'apps\backend\tests\integration') -q -m integration
    if ($LASTEXITCODE -ne 0) { throw 'Docker-backed integration tests failed.' }
}
