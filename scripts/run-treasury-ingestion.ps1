Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dbPath = Join-Path $repoRoot "data\market_snapshots.db"
$dashboardJsPath = Join-Path $repoRoot "data\treasury_dashboard_data.js"
$logDirectory = Join-Path $repoRoot "data\logs"
$logPath = Join-Path $logDirectory ("treasury-ingestion-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$pythonPath = (Get-Command python).Source

# The Treasury feed serves a full calendar year per request, so re-pulling the
# current year keeps the daily curve history complete even after missed runs.
$currentYear = (Get-Date).Year

Push-Location $repoRoot
try {
    & $pythonPath -c "from whatthefed.treasury_ingestion import main; raise SystemExit(main())" --db-path $dbPath --year $currentYear --dashboard-js $dashboardJsPath *>> $logPath
    if ($LASTEXITCODE -ne 0) {
        throw "Treasury ingestion exited with code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
