$ErrorActionPreference = "Stop"
$Root = Split-Path -Parent $MyInvocation.MyCommand.Path
$ConfigPath = Join-Path $Root "config.json"
$PowerShellExe = Join-Path $env:SystemRoot "System32\WindowsPowerShell\v1.0\powershell.exe"
$TaskPath = "\"
$RuntimeDir = Join-Path $env:LOCALAPPDATA "TechnocoreTechRadar\runtime"
$ManagedCodex = Join-Path $RuntimeDir "codex.exe"

$TaskNames = [ordered]@{
    Scout = "Technocore Tech Scout"
    Watch = "Technocore Tech Watch"
    Daily = "Technocore Daily Radar"
}

foreach ($path in @(
    $ConfigPath,
    (Join-Path $Root "tech-scout.ps1"),
    (Join-Path $Root "tech-watch.ps1"),
    (Join-Path $Root "run-daily-radar.ps1")
)) {
    if (-not (Test-Path -LiteralPath $path)) { throw "Required file not found: $path" }
}

$codexCommand=Get-Command codex.exe -ErrorAction SilentlyContinue
if (-not $codexCommand) { throw "Codex CLI not found." }
if (-not (Test-Path -LiteralPath $RuntimeDir)) {
    New-Item -ItemType Directory -Path $RuntimeDir | Out-Null
}
$copyCodex=-not (Test-Path -LiteralPath $ManagedCodex)
if (-not $copyCodex) {
    $copyCodex=(Get-Item -LiteralPath $ManagedCodex).Length -ne (Get-Item -LiteralPath $codexCommand.Source).Length
}
if ($copyCodex) {
    Copy-Item -LiteralPath $codexCommand.Source -Destination $ManagedCodex -Force
}
& $ManagedCodex --version | Out-Null
if ($LASTEXITCODE -ne 0) { throw "Managed Codex CLI validation failed." }

$config=Get-Content $ConfigPath -Raw | ConvertFrom-Json
if ([string]::IsNullOrWhiteSpace([string]$config.hub_room)) {
    throw "config.json hub_room is required."
}
if ([string]$config.hub_room -ne "technocore-tech-radar") {
    throw "Automatic publishing is restricted to hub_room technocore-tech-radar."
}

$currentUser=[Security.Principal.WindowsIdentity]::GetCurrent().Name
$principal=New-ScheduledTaskPrincipal -UserId $currentUser -LogonType Interactive -RunLevel Limited
$logonTrigger=New-ScheduledTaskTrigger -AtLogOn -User $currentUser
$dailyTrigger=New-ScheduledTaskTrigger -Daily -At ([datetime]::Today.AddHours(21))

$continuousSettings=New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) `
    -ExecutionTimeLimit ([TimeSpan]::Zero) `
    -StartWhenAvailable

$dailySettings=New-ScheduledTaskSettingsSet `
    -MultipleInstances IgnoreNew `
    -ExecutionTimeLimit (New-TimeSpan -Hours 2) `
    -StartWhenAvailable

function New-RadarAction([string]$ScriptName) {
    $scriptPath=Join-Path $Root $ScriptName
    $arguments="-NoProfile -NonInteractive -ExecutionPolicy Bypass -File `"$scriptPath`""
    return New-ScheduledTaskAction -Execute $PowerShellExe -Argument $arguments -WorkingDirectory $Root
}

$scoutTask=New-ScheduledTask `
    -Action (New-RadarAction "tech-scout.ps1") `
    -Trigger $logonTrigger `
    -Settings $continuousSettings `
    -Principal $principal `
    -Description "Continuously discovers and classifies new public Technocore rooms."

$watchTask=New-ScheduledTask `
    -Action (New-RadarAction "tech-watch.ps1") `
    -Trigger $logonTrigger `
    -Settings $continuousSettings `
    -Principal $principal `
    -Description "Continuously watches selected Technocore rooms for meaningful technical updates."

$dailyTask=New-ScheduledTask `
    -Action (New-RadarAction "run-daily-radar.ps1") `
    -Trigger $dailyTrigger `
    -Settings $dailySettings `
    -Principal $principal `
    -Description "Generates and automatically publishes the local Daily Radar at 21:00 local time."

Register-ScheduledTask -TaskName $TaskNames.Scout -TaskPath $TaskPath -InputObject $scoutTask -Force | Out-Null
Register-ScheduledTask -TaskName $TaskNames.Watch -TaskPath $TaskPath -InputObject $watchTask -Force | Out-Null
Register-ScheduledTask -TaskName $TaskNames.Daily -TaskPath $TaskPath -InputObject $dailyTask -Force | Out-Null

$envPath=Join-Path $Root ".env"
$seedAvailable=(-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable("SIGN_SEED","Process"))) -or
    (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable("SIGN_SEED","User"))) -or
    (-not [string]::IsNullOrWhiteSpace([Environment]::GetEnvironmentVariable("SIGN_SEED","Machine")))
if (-not $seedAvailable -and (Test-Path -LiteralPath $envPath)) {
    $seedAvailable=@(Get-Content $envPath | Where-Object {
        $_ -match '^\s*SIGN_SEED\s*=\s*.+$' -or $_ -match '^\s*\$env:SIGN_SEED\s*=\s*.+$'
    }).Count -gt 0
}

Write-Host "Installed or updated scheduled tasks:"
Write-Host "- $($TaskNames.Scout): at logon, restart on failure, one instance"
Write-Host "- $($TaskNames.Watch): at logon, restart on failure, one instance"
Write-Host "- $($TaskNames.Daily): daily at 21:00 local time ($([TimeZoneInfo]::Local.Id))"
if (-not $seedAvailable) {
    Write-Warning "SIGN_SEED is not available to the scheduled task. Daily publication will fail safely until the existing local signing setup provides it."
}
