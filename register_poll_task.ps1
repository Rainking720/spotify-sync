param([string]$Python, [string]$TaskName = 'Spotify Plays to iTunes', [switch]$DryRun)   # -DryRun: show what would be registered
$ErrorActionPreference = 'Stop'
# This script's own folder, so the repo can live anywhere.
$Dir = $PSScriptRoot
# pythonw.exe (no console window): -Python if given, else the one on PATH.
if (-not $Python) {
    $found = Get-Command pythonw.exe -ErrorAction SilentlyContinue
    if ($found) { $Python = $found.Source }
}
if (-not $Python -or -not (Test-Path -LiteralPath $Python)) {
    throw 'pythonw.exe not found - pass -Python "<path to pythonw.exe>"'
}
$Py = $Python
$Arg  = '"' + (Join-Path $Dir 'poll_to_itunes.py') + '"'
if (-not (Test-Path (Join-Path $Dir 'poll_to_itunes.py'))) { throw 'poll_to_itunes.py not found' }

# :03 on odd hours -- 15 minutes after SpotifyPlayTracker's :48 on even hours,
# so each run picks up the plays that poll just logged.
$start = (Get-Date).Date.AddHours((Get-Date).Hour).AddMinutes(3)
while ($start -le (Get-Date) -or ($start.Hour % 2) -eq 0) { $start = $start.AddHours(1) }

$action  = New-ScheduledTaskAction -Execute $Py -Argument $Arg -WorkingDirectory $Dir
$trigger = New-ScheduledTaskTrigger -Once -At $start `
           -RepetitionInterval (New-TimeSpan -Hours 2) `
           -RepetitionDuration (New-TimeSpan -Days 3650)   # same as the poller task
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
             -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Minutes 30) `
            -MultipleInstances IgnoreNew -Hidden

if ($DryRun) {
    Write-Host "task: $TaskName (dry run, nothing registered)"
    Write-Host "  execute     : $Py"
    Write-Host "  arguments   : $Arg"
    Write-Host "  working dir : $Dir"
    return
}
Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force `
    -Description 'Applies new Spotify plays (from SpotifyPlayTracker) to iTunes play counts and the spotify-sync database.' | Out-Null

$i = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "registered : $TaskName"
Write-Host "next run   : $($i.NextRunTime)"
