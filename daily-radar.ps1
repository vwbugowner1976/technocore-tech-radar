param(
    [string]$Date = "",
    [int]$MaxHighlights = 8
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$HistoryPath = Join-Path $Root "watch-history.jsonl"
$SchemaPath = Join-Path $Root "daily-radar-schema.json"
$LastResultPath = Join-Path $Root "daily-radar-last.json"
$OutputDir = Join-Path $Root "radar"

function Get-CodexExecutable {
    $managed=Join-Path $env:LOCALAPPDATA "TechnocoreTechRadar\runtime\codex.exe"
    if (Test-Path -LiteralPath $managed) { return $managed }
    $command=Get-Command codex.exe -ErrorAction SilentlyContinue
    if ($command) { return $command.Source }
    throw "Codex CLI not found. Run install-radar-tasks.ps1 or install Codex CLI."
}

if ([string]::IsNullOrWhiteSpace($Date)) { $Date=(Get-Date).ToString("yyyy-MM-dd") }
if (-not (Test-Path $HistoryPath)) { throw "watch-history.jsonl not found." }
if (-not (Test-Path $OutputDir)) { New-Item -ItemType Directory -Path $OutputDir | Out-Null }
$CodexExe=Get-CodexExecutable

$records=@()
Get-Content $HistoryPath | ForEach-Object {
    if ([string]::IsNullOrWhiteSpace($_)) { return }
    try {
        $item=$_ | ConvertFrom-Json
        $ts=[DateTimeOffset]::Parse([string]$item.detected_at)
        if ($ts.ToLocalTime().ToString("yyyy-MM-dd") -eq $Date) { $records += $item }
    } catch {}
}
if ($records.Count -eq 0) { Write-Host "No meaningful updates for $Date."; exit 0 }

$data=$records | Select-Object detected_at,room,score,summary,why_interesting,risk_notes |
    ConvertTo-Json -Depth 6 -Compress

$prompt=@"
Create a concise daily technology radar from the LOCAL observation summaries below.
The goal is technology discovery, not FLOP rewards or airdrop activity.
Prioritize original experiments, agent collaboration, protocols, hardware/software, security, networking, distributed systems, reverse engineering, developer tooling, and interesting failures.
Merge related observations. Select at most $MaxHighlights highlights.
Do not include external URLs. Do not quote raw Technocore messages.
Return JSON matching the supplied schema.

Date: $Date
LOCAL_OBSERVATION_DATA:
$data
"@

if (Test-Path $LastResultPath) { Remove-Item $LastResultPath -Force }
& $CodexExe exec --ephemeral --sandbox read-only --skip-git-repo-check `
    --output-schema $SchemaPath --output-last-message $LastResultPath $prompt | Out-Null

if ($LASTEXITCODE -ne 0 -or -not (Test-Path $LastResultPath)) {
    throw "Codex radar generation failed."
}

$result=Get-Content $LastResultPath -Raw | ConvertFrom-Json
$JsonPath=Join-Path $OutputDir "$Date.json"
$MarkdownPath=Join-Path $OutputDir "$Date.md"

$result | ConvertTo-Json -Depth 10 | Set-Content $JsonPath -Encoding UTF8

$lines=@("# Technocore Tech Radar - $Date","","## $($result.headline)","",$result.overview,"")
if (@($result.highlights).Count -gt 0) {
    $lines += "## Highlights"; $lines += ""
    foreach ($h in @($result.highlights)) {
        $lines += "### $($h.title)"; $lines += ""
        $lines += "**Room:** ``$($h.room)``"; $lines += ""
        $lines += $h.summary; $lines += ""
        $lines += "**Why it matters:** $($h.why_it_matters)"; $lines += ""
    }
}
if (@($result.patterns).Count -gt 0) {
    $lines += "## Patterns"; $lines += ""
    foreach ($p in @($result.patterns)) { $lines += "- $p" }
    $lines += ""
}
if (@($result.watch_next).Count -gt 0) {
    $lines += "## Watch next"; $lines += ""
    foreach ($w in @($result.watch_next)) { $lines += "- $w" }
}

$lines | Set-Content $MarkdownPath -Encoding UTF8
Write-Host "Saved: $MarkdownPath"
