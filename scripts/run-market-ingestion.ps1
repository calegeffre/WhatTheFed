Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$watchlistPath = Join-Path $repoRoot "config\market_watchlist.json"
$dbPath = Join-Path $repoRoot "data\market_snapshots.db"
$logDirectory = Join-Path $repoRoot "data\logs"
$logPath = Join-Path $logDirectory ("market-ingestion-{0}.log" -f (Get-Date -Format "yyyy-MM-dd"))

New-Item -ItemType Directory -Force -Path $logDirectory | Out-Null
$pythonPath = (Get-Command python).Source

Push-Location $repoRoot
try {
    & $pythonPath -c "from whatthefed.market_ingestion import main; raise SystemExit(main())" --watchlist $watchlistPath --db-path $dbPath *>> $logPath
    if ($LASTEXITCODE -ne 0) {
        throw "Market ingestion exited with code $LASTEXITCODE."
    }
}
finally {
    Pop-Location
}
