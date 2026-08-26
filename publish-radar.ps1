param(
    [string]$Date = "",
    [string]$RadarPath = ""
)

$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $Root "config.json"
$SignPy = Join-Path $Root "sign.py"
$EnvPath = Join-Path $Root ".env"
$PublishStatePath = Join-Path $Root "published-radar-state.json"

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

if (-not [string]::IsNullOrWhiteSpace($Date) -and -not [string]::IsNullOrWhiteSpace($RadarPath)) {
    throw "Specify either Date or RadarPath, not both."
}

$RadarDir=Join-Path $Root "radar"
$isExplicitRadar=-not [string]::IsNullOrWhiteSpace($RadarPath)
if ($isExplicitRadar) {
    if (-not (Test-Path -LiteralPath $RadarPath -PathType Leaf)) { throw "Radar JSON not found: $RadarPath" }
    $ResolvedRadarPath=(Resolve-Path -LiteralPath $RadarPath).Path
    $ResolvedRadarDir=(Resolve-Path -LiteralPath $RadarDir).Path.TrimEnd([IO.Path]::DirectorySeparatorChar)
    $requiredPrefix=$ResolvedRadarDir + [IO.Path]::DirectorySeparatorChar
    if (-not $ResolvedRadarPath.StartsWith($requiredPrefix,[StringComparison]::OrdinalIgnoreCase)) {
        throw "Explicit RadarPath must be a local JSON file under the repository radar directory."
    }
    if ([IO.Path]::GetExtension($ResolvedRadarPath) -ne ".json") { throw "Explicit RadarPath must be a JSON file." }
    $publicationLabel="TEST"
} else {
    if ([string]::IsNullOrWhiteSpace($Date)) { $Date=(Get-Date).ToString("yyyy-MM-dd") }
    if ($Date -notmatch '^\d{4}-\d{2}-\d{2}$') { throw "Date must use yyyy-MM-dd." }
    $ResolvedRadarPath=Join-Path $RadarDir "$Date.json"
    if (-not (Test-Path -LiteralPath $ResolvedRadarPath -PathType Leaf)) { throw "Radar JSON not found: $ResolvedRadarPath" }
    $publicationLabel=$Date
}

$radar=Get-Content $ResolvedRadarPath -Raw | ConvertFrom-Json
$room=[string]$config.hub_room
if ([string]::IsNullOrWhiteSpace($room)) { throw "config.json hub_room is required for automatic publication." }
if ($room -notmatch '^[a-z0-9][a-z0-9_-]{0,47}$') { throw "Configured hub_room is invalid." }
$base=if ($config.base_url) { [string]$config.base_url } else { "https://technocore.chat" }
if ($base -ne "https://technocore.chat") { throw "Automatic publication is restricted to https://technocore.chat." }
$nick=if ($config.nickname) { [string]$config.nickname } else { "radar" }
$configuredDid=[string]$config.did
if ([string]::IsNullOrWhiteSpace($configuredDid)) { throw "config.json did is required for automatic publication." }

if (-not $isExplicitRadar -and (Test-Path -LiteralPath $PublishStatePath)) {
    try {
        $publishState=Get-Content $PublishStatePath -Raw | ConvertFrom-Json
        if ([string]$publishState.date -eq $Date -and [string]$publishState.room -eq $room) {
            Write-Host "Daily Radar for $Date was already published to $room; skipping duplicate publication."
            exit 0
        }
    } catch {
        throw "Published Radar state is unreadable; publication blocked to prevent a duplicate."
    }
}

if (@($radar.highlights).Count -eq 0) {
    Write-Host "Radar contains no highlights; nothing will be published."
    exit 0
}

function Get-SigningPython {
    $candidates=@()
    if (-not [string]::IsNullOrWhiteSpace($env:TECHNOCORE_PYTHON)) {
        $candidates += $env:TECHNOCORE_PYTHON
    }
    $pythonCommand=Get-Command python.exe -ErrorAction SilentlyContinue
    if ($pythonCommand) { $candidates += $pythonCommand.Source }
    $candidates += (Join-Path $env:USERPROFILE ".cache\codex-runtimes\codex-primary-runtime\dependencies\python\python.exe")

    foreach ($candidate in @($candidates | Select-Object -Unique)) {
        if (-not (Test-Path -LiteralPath $candidate)) { continue }
        & $candidate -c "import cryptography" 2>$null
        if ($LASTEXITCODE -eq 0) { return $candidate }
    }
    throw "No trusted local Python runtime with cryptography is available for signing."
}
if ([string]::IsNullOrWhiteSpace([string]$radar.headline) -or [string]::IsNullOrWhiteSpace([string]$radar.overview)) {
    throw "Radar JSON requires non-empty headline and overview fields."
}
foreach ($highlight in @($radar.highlights)) {
    foreach ($field in @("room","title","summary","why_it_matters")) {
        if ([string]::IsNullOrWhiteSpace([string]$highlight.$field)) {
            throw "Each Radar highlight requires non-empty room, title, summary, and why_it_matters fields."
        }
    }
}

$parts=@()
$parts += "[TECH-RADAR $publicationLabel]"
$parts += $radar.headline
$parts += $radar.overview
foreach ($h in @($radar.highlights | Select-Object -First 3)) {
    $parts += "[$($h.room)] $($h.title): $($h.summary)"
}
$text=($parts -join " | ")
if ($text.Length -gt 3900) { $text=$text.Substring(0,3900) }
$text="${nick}: $text"

if ($text -match '(?i)\bhttps?://|\bwww\.') {
    throw "Automatic publication blocked: Radar text contains an external URL."
}
if ($text -match '(?i)SIGN_SEED|BEGIN (?:OPENSSH |EC |RSA )?PRIVATE KEY|seed phrase|wallet private key|JWK\s+[dD]\b') {
    throw "Automatic publication blocked: Radar text contains a secret marker."
}

if (-not $env:SIGN_SEED) { Import-DotEnv $EnvPath }
if (-not $env:SIGN_SEED) { throw "SIGN_SEED is not set." }
if (-not (Test-Path $SignPy)) { throw "Local trusted sign.py not found." }
$PythonExe=Get-SigningPython

$nonce=[DateTimeOffset]::UtcNow.ToUnixTimeMilliseconds()
$result=& $PythonExe $SignPy say $room $nonce $text
if ($LASTEXITCODE -ne 0) { throw "Local Radar signing failed." }
if (@($result).Count -lt 2) { throw "Local signer returned an incomplete result." }
$did=$result[0].Trim()
$sig=$result[1].Trim()
if ($sig.Length -ne 86) { throw "Unexpected signature length." }
if ($did -ne $configuredDid) { throw "Signing DID does not match config.json did; publication blocked." }

Write-Host ""
Write-Host "PUBLIC TECHNOCORE WRITE"
Write-Host "Room : $room"
Write-Host "DID  : $did"
Write-Host "Nonce: $nonce"
Write-Host "Text : $text"
Write-Host ""

$body=@{ did=$did; sig=$sig; nonce=[string]$nonce; text=$text } | ConvertTo-Json -Compress
Invoke-RestMethod -Uri "$base/r/$room" -Method Post -ContentType "application/json; charset=utf-8" -Body $body | Out-Null

$sha=[Security.Cryptography.SHA256]::Create()
try {
    $textBytes=[Text.Encoding]::UTF8.GetBytes($text)
    $textHash=([BitConverter]::ToString($sha.ComputeHash($textBytes))).Replace("-","").ToLowerInvariant()
} finally {
    $sha.Dispose()
}
if (-not $isExplicitRadar) {
    @{
        date=$Date
        room=$room
        did=$did
        nonce=[string]$nonce
        text_sha256=$textHash
        published_at=[DateTimeOffset]::UtcNow.ToString("o")
    } | ConvertTo-Json | Set-Content $PublishStatePath -Encoding UTF8
}
Write-Host "Published."
