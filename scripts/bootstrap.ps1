$ErrorActionPreference = 'Stop'

$taskPython = (Get-Command python -ErrorAction Stop).Source
& $taskPython -c "import sys; assert (3, 11) <= sys.version_info[:2] < (3, 15), sys.version"
& $taskPython -m venv .venv
& .venv\Scripts\python.exe -m pip install --disable-pip-version-check -r apps\backend\requirements.lock
& .venv\Scripts\python.exe -m pip install --disable-pip-version-check --no-deps -e apps\backend
