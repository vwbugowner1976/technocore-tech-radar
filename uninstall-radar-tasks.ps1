$ErrorActionPreference = "Stop"
$TaskPath = "\"
$TaskNames = @(
    "Technocore Tech Scout",
    "Technocore Tech Watch",
    "Technocore Daily Radar"
)

foreach ($name in $TaskNames) {
    $task=Get-ScheduledTask -TaskName $name -TaskPath $TaskPath -ErrorAction SilentlyContinue
    if ($null -eq $task) {
        Write-Host "Not installed: $name"
        continue
    }
    Unregister-ScheduledTask -TaskName $name -TaskPath $TaskPath -Confirm:$false
    Write-Host "Removed: $name"
}
