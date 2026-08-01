Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dbPath = Join-Path $repoRoot "data\market_snapshots.db"
$dashboardJsPath = Join-Path $repoRoot "data\policy_rate_dashboard_data.js"
$logDirectory = Join-Path $repoRoot "data\logs"
$logPath = Join-Path $logDirectory ("policy-rates-ingestion-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$pythonPath = (Get-Command python).Source

# The NY Fed publishes each business day for the prior session and occasionally
# revises recent rows, so re-pull a trailing window instead of only the last day.
$startDate = (Get-Date).AddDays(-45).ToString("yyyy-MM-dd")
$endDate = (Get-Date).ToString("yyyy-MM-dd")

Push-Location $repoRoot
try {
    & $pythonPath -c "from whatthefed.policy_rates_ingestion import main; raise SystemExit(main())" --db-path $dbPath --start-date $startDate --end-date $endDate --dashboard-js $dashboardJsPath *>> $logPath
    if ($LASTEXITCODE -ne 0) {
        throw "Policy rate ingestion exited with code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
