param(
    [string]$Date = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$DailyScript = Join-Path $Root "daily-radar.ps1"
$PublishScript = Join-Path $Root "publish-radar.ps1"
$HistoryPath = Join-Path $Root "watch-history.jsonl"

if ([string]::IsNullOrWhiteSpace($Date)) { $Date=(Get-Date).ToString("yyyy-MM-dd") }
if ($Date -notmatch '^\d{4}-\d{2}-\d{2}$') { throw "Date must use yyyy-MM-dd." }
if (-not (Test-Path $DailyScript)) { throw "daily-radar.ps1 not found." }
if (-not (Test-Path $PublishScript)) { throw "publish-radar.ps1 not found." }

if (-not (Test-Path $HistoryPath)) {
    Write-Host "No meaningful updates for $Date; nothing will be generated or published."
    exit 0
}

$recordsForDate=0
Get-Content $HistoryPath | ForEach-Object {
    if ([string]::IsNullOrWhiteSpace($_)) { return }
    try {
        $item=$_ | ConvertFrom-Json
        $ts=[DateTimeOffset]::Parse([string]$item.detected_at)
        if ($ts.ToLocalTime().ToString("yyyy-MM-dd") -eq $Date) { $recordsForDate++ }
    } catch {}
}

if ($recordsForDate -eq 0) {
    Write-Host "No meaningful updates for $Date; nothing will be generated or published."
    exit 0
}

$RadarPath=Join-Path (Join-Path $Root "radar") "$Date.json"
$generationStarted=[DateTime]::UtcNow

& $DailyScript -Date $Date
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    throw "Daily Radar generation failed with exit code $LASTEXITCODE. Publication was skipped."
}
if (-not (Test-Path $RadarPath)) {
    throw "Expected Radar JSON was not generated: $RadarPath. Publication was skipped."
}

$radarFile=Get-Item -LiteralPath $RadarPath
if ($radarFile.LastWriteTimeUtc -lt $generationStarted.AddSeconds(-2)) {
    throw "Radar JSON is stale and was not regenerated. Publication was skipped."
}

$radar=Get-Content $RadarPath -Raw | ConvertFrom-Json
if (@($radar.highlights).Count -eq 0) {
    Write-Host "Generated Radar has no highlights; nothing will be published."
    exit 0
}

& $PublishScript -Date $Date
if ($LASTEXITCODE -and $LASTEXITCODE -ne 0) {
    throw "Radar publication failed with exit code $LASTEXITCODE."
}
