param(
    [string]$TaskName = "WhatTheFed Market Ingestion",
    [string]$RunAt = "08:00",
    [string]$TaskPath = "\WhatTheFed\"
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$repoRoot = (Resolve-Path (Join-Path $PSScriptRoot "..")).Path
$runnerScript = Join-Path $repoRoot "scripts\run-market-ingestion.ps1"
if (-not (Test-Path $runnerScript)) {
    throw "Runner script not found: $runnerScript"
}

if (-not $TaskPath.EndsWith("\")) {
    $TaskPath = "$TaskPath\"
}

$scheduler = New-Object -ComObject "Schedule.Service"
$scheduler.Connect()
$currentFolder = $scheduler.GetFolder("\")
$segments = $TaskPath.Trim("\").Split("\", [System.StringSplitOptions]::RemoveEmptyEntries)
foreach ($segment in $segments) {
    try {
        $currentFolder = $currentFolder.GetFolder($segment)
    }
    catch {
        $currentFolder = $currentFolder.CreateFolder($segment)
    }
}

$startAt = [DateTime]::ParseExact($RunAt, "HH:mm", [System.Globalization.CultureInfo]::InvariantCulture)
$taskAction = New-ScheduledTaskAction -Execute "powershell.exe" -Argument "-NoProfile -ExecutionPolicy Bypass -File `"$runnerScript`""
$taskTrigger = New-ScheduledTaskTrigger -Daily -At $startAt
$taskSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$taskPrincipal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" -LogonType Interactive -RunLevel Limited

$existingRootTask = Get-ScheduledTask -TaskName $TaskName -TaskPath "\" -ErrorAction SilentlyContinue
if ($null -ne $existingRootTask -and $TaskPath -ne "\") {
    Unregister-ScheduledTask -TaskName $TaskName -TaskPath "\" -Confirm:$false
}

Register-ScheduledTask `
    -TaskName $TaskName `
    -TaskPath $TaskPath `
    -Description "Daily ingestion of Kalshi and Polymarket watchlist snapshots for WhatTheFed." `
    -Action $taskAction `
    -Trigger $taskTrigger `
    -Settings $taskSettings `
    -Principal $taskPrincipal `
    -Force | Out-Null

Write-Host "Registered scheduled task '$TaskName' at $TaskPath for $RunAt local time."
