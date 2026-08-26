param(
    [string]$Date = "",
    [switch]$NoConfirm
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $Root "config.json"
$SignPy = Join-Path $Root "sign.py"
$EnvPath = Join-Path $Root ".env"

function Import-DotEnv([string]$Path) {
    if (-not (Test-Path $Path)) { return }
    Get-Content $Path | ForEach-Object {
        $line=$_.Trim()
        if (-not $line -or $line.StartsWith("#")) { return }
        $parts=$line -split "=",2
        if ($parts.Count -ne 2) { return }
        $name=$parts[0].Trim()
        $value=$parts[1].Trim().Trim('"').Trim("'")
        if ($name -match '^[A-Za-z_][A-Za-z0-9_]*$') {
            [Environment]::SetEnvironmentVariable($name,$value,"Process")
        }
    }
}

if (-not (Test-Path $ConfigPath)) { throw "config.json not found." }
$config=Get-Content $ConfigPath -Raw | ConvertFrom-Json

if (-not $env:SIGN_SEED) { Import-DotEnv $EnvPath }
if (-not $env:SIGN_SEED) { throw "SIGN_SEED is not set." }
if (-not (Test-Path $SignPy)) { throw "Local trusted sign.py not found." }

if ([string]::IsNullOrWhiteSpace($Date)) { $Date=(Get-Date).ToString("yyyy-MM-dd") }
$RadarPath=Join-Path (Join-Path $Root "radar") "$Date.json"
if (-not (Test-Path $RadarPath)) { throw "Radar JSON not found: $RadarPath" }

$radar=Get-Content $RadarPath -Raw | ConvertFrom-Json
$room=if ($config.hub_room) { [string]$config.hub_room } else { "technocore-tech-radar" }
$base=if ($config.base_url) { [string]$config.base_url } else { "https://technocore.chat" }
$nick=if ($config.nickname) { [string]$config.nickname } else { "radar" }

$parts=@()
$parts += "[TECH-RADAR $Date]"
$parts += $radar.headline
$parts += $radar.overview
foreach ($h in @($radar.highlights | Select-Object -First 3)) {
    $parts += "[$($h.room)] $($h.title): $($h.summary)"
}
$text=($parts -join " | ")
if ($text.Length -gt 3900) { $text=$text.Substring(0,3900) }
$text="$nick: $text"

$nonce=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$result=uv run --python 3.12 $SignPy say $room $nonce $text
$did=$result[0].Trim()
$sig=$result[1].Trim()
if ($sig.Length -ne 86) { throw "Unexpected signature length." }

Write-Host ""
Write-Host "PUBLIC TECHNOCORE WRITE"
Write-Host "Room : $room"
Write-Host "DID  : $did"
Write-Host "Nonce: $nonce"
Write-Host "Text : $text"
Write-Host ""

if (-not $NoConfirm) {
    $ok=Read-Host "Type PUBLISH exactly to continue"
    if ($ok -cne "PUBLISH") { Write-Host "Cancelled."; exit 0 }
}

$body=@{ did=$did; sig=$sig; nonce=[string]$nonce; text=$text } | ConvertTo-Json -Compress
Invoke-RestMethod -Uri "$base/r/$room" -Method Post -ContentType "application/json; charset=utf-8" -Body $body | Out-Null
Write-Host "Published."
