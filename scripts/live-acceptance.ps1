[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$EnvFile
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"
$ProgressPreference = "SilentlyContinue"

$repo = Split-Path -Parent $PSScriptRoot
Set-Location -LiteralPath $repo
$previousSecrets = @{}
$startedAt = [DateTimeOffset]::UtcNow
$failed = $false
$results = [System.Collections.Generic.List[object]]::new()
$states = [System.Collections.Generic.List[object]]::new()
$versions = [ordered]@{}
$instruments = @()
$counts = [ordered]@{}

function Add-Result([string]$Name, [string]$Status, [string]$Reason) {
    $results.Add([ordered]@{ name = $Name; status = $Status; reason = $Reason })
}

function Assert-Gate([bool]$Condition, [string]$Reason) {
    if (-not $Condition) { throw $Reason }
}

function Invoke-Compose {
    & docker compose --env-file $EnvFile @args
}

function Wait-Api([string]$Path, [int]$Seconds, [scriptblock]$Accept) {
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($Seconds)
    do {
        try {
            $response = Invoke-RestMethod -Uri "http://127.0.0.1:8000$Path" -TimeoutSec 5
            if (& $Accept $response) { return $response }
        } catch {
            # Expected while a container starts or readiness is false.
        }
        Start-Sleep -Seconds 2
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    return $null
}

function Require-PrivateFeedDirectory {
    Invoke-Compose exec -T worker python -c "import os,stat,sys;p=os.stat('/run/groww-feed');sys.exit(stat.S_IMODE(p.st_mode)!=0o700 or p.st_uid!=10001 or p.st_gid!=10001)"
    Assert-Gate ($LASTEXITCODE -eq 0) "WORKER_FEED_TMPFS_NOT_PRIVATE"
}

function Require-NoRuntimeFiles([string]$Service) {
    if ($Service -in @("api", "worker")) {
        Invoke-Compose exec -T $Service python -c "import os,sys;bad={'.env','tests','fixtures','artifacts'};sys.exit(any(set(p.replace(chr(92),'/').split('/'))&bad or os.path.basename(p).startswith('.env') for p,_,fs in os.walk('/app') for f in fs for p in [os.path.join(p,f)]))"
    } else {
        Invoke-Compose exec -T $Service sh -lc "if find /app -type f \( -name '.env' -o -name '.env.*' -o -path '*/tests/*' -o -path '*/fixtures/*' -o -path '*/artifacts/*' \) -print -quit | grep -q .; then exit 1; fi"
    }
    Assert-Gate ($LASTEXITCODE -eq 0) "RUNTIME_IMAGE_CONTAINS_FORBIDDEN_FILE"
}

try {
    Assert-Gate (Test-Path -LiteralPath $EnvFile -PathType Leaf) "EXTERNAL_ENV_FILE_REQUIRED"
    $EnvFile = (Resolve-Path -LiteralPath $EnvFile).Path
    Assert-Gate (-not $EnvFile.StartsWith($repo, [StringComparison]::OrdinalIgnoreCase)) "ENV_FILE_MUST_BE_OUTSIDE_REPOSITORY"
    Assert-Gate ($EnvFile -notmatch '(?i)[\\/]OneDrive[\\/]') "ENV_FILE_MUST_BE_OUTSIDE_ONEDRIVE"
    $unsafeAcl = (Get-Acl -LiteralPath $EnvFile).Access | Where-Object {
        $_.AccessControlType -eq "Allow" -and $_.IdentityReference -match '(?i)(Everyone|Authenticated Users|BUILTIN\\Users)$'
    }
    Assert-Gate (-not $unsafeAcl) "ENV_FILE_ACL_IS_TOO_BROAD"
    $envLines = Get-Content -LiteralPath $EnvFile
    foreach ($name in @("GROWW_TOTP_TOKEN", "GROWW_TOTP_SECRET", "POSTGRES_PASSWORD", "REDIS_PASSWORD")) {
        $line = $envLines | Where-Object { $_ -match "^\s*$name\s*=" } | Select-Object -Last 1
        $value = if ($null -eq $line) { "" } else { ($line -replace "^\s*$name\s*=\s*", "").Trim() }
        $configured = $value.Length -gt 0 -and $value -notin @("''", '""')
        Assert-Gate $configured "REQUIRED_LOCAL_CONFIGURATION_MISSING"
        $previousSecrets[$name] = [Environment]::GetEnvironmentVariable($name, "Process")
        [Environment]::SetEnvironmentVariable($name, $value, "Process")
    }
    Remove-Variable envLines, line, value, configured -ErrorAction SilentlyContinue
    Add-Result "local_configuration" "PASS" "PRIVATE_EXTERNAL_ENV_AND_REQUIRED_NAMES_PRESENT"

    Assert-Gate ($null -ne (Get-Command docker -ErrorAction SilentlyContinue)) "DOCKER_REQUIRED"
    & docker info *> $null
    Assert-Gate ($LASTEXITCODE -eq 0) "DOCKER_DAEMON_UNAVAILABLE"
    Invoke-Compose version *> $null
    Assert-Gate ($LASTEXITCODE -eq 0) "DOCKER_COMPOSE_REQUIRED"
    Invoke-Compose config --quiet
    Assert-Gate ($LASTEXITCODE -eq 0) "COMPOSE_CONFIG_INVALID"
    $services = @(Invoke-Compose config --services)
    $expectedServices = @("api", "frontend", "postgres", "redis", "worker")
    $actualServiceList = ($services | Sort-Object) -join "`n"
    $expectedServiceList = ($expectedServices | Sort-Object) -join "`n"
    Assert-Gate ($actualServiceList -ceq $expectedServiceList) "COMPOSE_SERVICE_BOUNDARY_FAILED"
    Add-Result "compose_topology" "PASS" "EXACTLY_FIVE_SERVICES"

    $forbidden = & rg -l "place_order|modify_order|cancel_order|create_smart_order|__getattr__|_nats_client" apps/backend/src 2>$null
    Assert-Gate (-not $forbidden) "EXECUTION_OR_PRIVATE_SDK_SURFACE_FOUND"
    Add-Result "static_read_only_boundary" "PASS" "NO_EXECUTION_OR_PRIVATE_SDK_SURFACE"

    Invoke-Compose up -d --build
    Assert-Gate ($LASTEXITCODE -eq 0) "FIVE_SERVICE_START_FAILED"
    $live = Wait-Api "/health/live" 90 { param($body) $body.status -eq "alive" }
    Assert-Gate ($null -ne $live) "API_LIVENESS_TIMEOUT"
    Add-Result "five_service_start" "PASS" "API_AND_FIVE_SERVICES_STARTED"

    Invoke-Compose exec -T api python -c "import os,sys;sys.exit(any(k.startswith('GROWW_') for k in os.environ))"
    Assert-Gate ($LASTEXITCODE -eq 0) "API_HAS_GROWW_ENVIRONMENT_NAME"
    foreach ($service in @("api", "worker", "frontend")) { Require-NoRuntimeFiles $service }
    Require-PrivateFeedDirectory
    $tmpfs = (& docker inspect --format "{{json .HostConfig.Tmpfs}}" ((Invoke-Compose ps -q worker).Trim())).Trim()
    Assert-Gate ($tmpfs -match '"/run/groww-feed"' -and $tmpfs -match 'mode=0700' -and $tmpfs -match 'uid=10001' -and $tmpfs -match 'gid=10001') "WORKER_FEED_TMPFS_CONFIGURATION_FAILED"
    Invoke-Compose exec -T worker sh -lc "if find /app /usr/local/lib/python3.12/site-packages -type f \( -name a.creds -o -name b.creds \) -print -quit | grep -q .; then exit 1; fi"
    Assert-Gate ($LASTEXITCODE -eq 0) "FEED_CREDENTIAL_FOUND_OUTSIDE_TMPFS"
    Add-Result "runtime_boundaries" "PASS" "API_ENV_IMAGES_AND_WORKER_TMPFS_VERIFIED"

    Assert-Gate (Test-Path -LiteralPath ".venv\Scripts\python.exe" -PathType Leaf) "BOOTSTRAPPED_VENV_REQUIRED"
    $previousLive = [Environment]::GetEnvironmentVariable("LIVE_ACCEPTANCE")
    $previousBase = [Environment]::GetEnvironmentVariable("LIVE_API_BASE_URL")
    $env:LIVE_ACCEPTANCE = "1"
    $env:LIVE_API_BASE_URL = "http://127.0.0.1:8000"
    try {
        & .venv\Scripts\python.exe -m pytest apps/backend/tests/live/test_live_foundation.py -q -rs
        Assert-Gate ($LASTEXITCODE -eq 0) "CREDENTIALED_LIVE_PYTEST_FAILED"
    } finally {
        if ($null -eq $previousLive) { Remove-Item Env:LIVE_ACCEPTANCE -ErrorAction SilentlyContinue } else { $env:LIVE_ACCEPTANCE = $previousLive }
        if ($null -eq $previousBase) { Remove-Item Env:LIVE_API_BASE_URL -ErrorAction SilentlyContinue } else { $env:LIVE_API_BASE_URL = $previousBase }
    }
    Add-Result "credentialed_live_pytest" "PASS" "DIRECT_AND_API_READS_RECONCILED"

    $versions.growwSdk = (& .venv\Scripts\python.exe -c "from importlib.metadata import version;print(version('growwapi'))").Trim()
    $versions.app = (& .venv\Scripts\python.exe -c "from trading_system import __version__;print(__version__)").Trim()
    $market = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/market/nifty" -TimeoutSec 5
    $options = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/market/nifty/options" -TimeoutSec 5
    $account = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/account/summary" -TimeoutSec 5
    $orders = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/account/orders" -TimeoutSec 5
    $instruments = @(
        [ordered]@{ symbol = $market.spot.instrument.trading_symbol; token = $market.spot.instrument.exchange_token },
        [ordered]@{ symbol = $market.future.instrument.trading_symbol; token = $market.future.instrument.exchange_token }
    )
    $counts.options = @($options.contracts).Count
    $counts.positions = @($account.positions).Count
    $counts.orders = @($orders.orders).Count
    $counts.fills = @($orders.orders | ForEach-Object { @($_.fills).Count } | Measure-Object -Sum).Sum
    Remove-Variable market, options, account, orders -ErrorAction SilentlyContinue

    $initial = Invoke-RestMethod -Uri "http://127.0.0.1:8000/api/v1/system/status" -TimeoutSec 5
    $states.Add([ordered]@{ timestamp = [DateTimeOffset]::UtcNow.ToString("o"); state = [string]$initial.state })
    $marketClosed = $initial.state -eq "MARKET_CLOSED"
    Assert-Gate ($marketClosed -or $initial.state -in @("LIVE", "DEGRADED")) "INITIAL_LIVE_STATE_NOT_ACCEPTABLE"

    Invoke-Compose stop worker
    Assert-Gate ($LASTEXITCODE -eq 0) "WORKER_STOP_FAILED"
    if ($marketClosed) {
        Add-Result "feed_stale_transition" "PENDING" "MARKET_CLOSED"
    } else {
        $stale = Wait-Api "/api/v1/system/status" 25 { param($body) $body.state -eq "STALE" }
        Assert-Gate ($null -ne $stale) "STOPPED_FEED_DID_NOT_BECOME_STALE"
        $states.Add([ordered]@{ timestamp = [DateTimeOffset]::UtcNow.ToString("o"); state = "STALE" })
        Add-Result "feed_stale_transition" "PASS" "STOPPED_OPEN_SESSION_FEED_BECAME_STALE"
    }
    $workerId = (Invoke-Compose ps -aq worker).Trim()
    $workerState = (& docker inspect --format "{{.State.Status}}" $workerId).Trim()
    Assert-Gate ($workerState -eq "exited") "WORKER_DID_NOT_SHUT_DOWN"
    Add-Result "feed_credentials_after_shutdown" "PASS" "TMPFS_UNMOUNTED_AND_RUNTIME_IMAGE_HAS_NO_FEED_CREDENTIALS"

    $restartAt = [DateTimeOffset]::UtcNow
    Invoke-Compose start worker
    Assert-Gate ($LASTEXITCODE -eq 0) "WORKER_RESTART_FAILED"
    $reconciled = Wait-Api "/api/v1/account/summary" 90 {
        param($body)
        try { [DateTimeOffset]::Parse([string]$body.source_at) -ge $restartAt } catch { $false }
    }
    Assert-Gate ($null -ne $reconciled) "ACCOUNT_RECONCILIATION_AFTER_RESTART_FAILED"
    if ($marketClosed) {
        Add-Result "readiness_after_restart" "PENDING" "MARKET_CLOSED_FEED_UPDATE_UNAVAILABLE"
    } else {
        $ready = Wait-Api "/health/ready" 90 { param($body) $body.ready -eq $true }
        Assert-Gate ($null -ne $ready) "READINESS_AFTER_RESTART_FAILED"
        $states.Add([ordered]@{ timestamp = [DateTimeOffset]::UtcNow.ToString("o"); state = "LIVE_READY" })
        Add-Result "readiness_after_restart" "PASS" "RECONCILIATION_PRECEDED_READINESS"
    }
    Require-PrivateFeedDirectory
    Add-Result "worker_restart" "PASS" "ACCOUNT_RECONCILED_AFTER_RESTART"
} catch {
    $failed = $true
    $safeReason = [string]$_.Exception.Message
    if ($safeReason -notmatch '^[A-Z0-9_]+$') { $safeReason = "ACCEPTANCE_GATE_FAILED" }
    Add-Result "acceptance" "FAIL" $safeReason
    Write-Warning "Live acceptance failed; inspect the allowlisted evidence reason and container health."
} finally {
    foreach ($name in $previousSecrets.Keys) {
        [Environment]::SetEnvironmentVariable($name, $previousSecrets[$name], "Process")
    }
    $artifactDirectory = Join-Path $repo "artifacts\live-acceptance"
    New-Item -ItemType Directory -Path $artifactDirectory -Force | Out-Null
    $evidence = [ordered]@{
        version = 1
        startedAt = $startedAt.ToString("o")
        completedAt = [DateTimeOffset]::UtcNow.ToString("o")
        versions = $versions
        instruments = $instruments
        counts = $counts
        states = $states
        results = $results
    }
    $evidence | ConvertTo-Json -Depth 8 | Set-Content -LiteralPath (Join-Path $artifactDirectory "latest.json") -Encoding utf8
}

if ($failed) { exit 1 }
