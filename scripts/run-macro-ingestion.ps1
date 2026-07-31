Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$dbPath = Join-Path $repoRoot "data\market_snapshots.db"
$logDirectory = Join-Path $repoRoot "data\logs"
$logPath = Join-Path $logDirectory ("macro-ingestion-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$pythonPath = (Get-Command python).Source

# BLS revises prior months, so always re-pull a rolling multi-year window rather
# than only the newest print.
$endYear = (Get-Date).Year
$startYear = $endYear - 3

Push-Location $repoRoot
try {
    & $pythonPath -c "from whatthefed.cpi_ingestion import main; raise SystemExit(main())" `
        --db-path $dbPath `
        --start-year $startYear `
        --end-year $endYear `
        --dashboard-js (Join-Path $repoRoot "data\cpi_dashboard_data.js") `
        --kg-js (Join-Path $repoRoot "data\kg_dashboard_data.js") *>> $logPath
    if ($LASTEXITCODE -ne 0) {
        throw "CPI ingestion exited with code $LASTEXITCODE."
    }

    & $pythonPath -c "from whatthefed.labor_ingestion import main; raise SystemExit(main())" `
        --db-path $dbPath `
        --start-year $startYear `
        --end-year $endYear `
        --dashboard-js (Join-Path $repoRoot "data\labor_dashboard_data.js") `
        --kg-js (Join-Path $repoRoot "data\labor_kg_dashboard_data.js") *>> $logPath
    if ($LASTEXITCODE -ne 0) {
        throw "Labor ingestion exited with code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
