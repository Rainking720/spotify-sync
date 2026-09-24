param([string]$Python, [string]$TaskName = 'Spotify Liked Sync', [switch]$DryRun)   # -DryRun: show what would be registered
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
$Arg  = '-u "' + (Join-Path $Dir 'run_sync.py') + '"'
if (-not (Test-Path (Join-Path $Dir 'run_sync.py'))) { throw 'run_sync.py not found' }

$action  = New-ScheduledTaskAction -Execute $Py -Argument $Arg -WorkingDirectory $Dir
$trigger = New-ScheduledTaskTrigger -Daily -At 3am

# Interactive: runs as you, when you're logged on, so no stored password is needed.
$principal = New-ScheduledTaskPrincipal -UserId "$env:USERDOMAIN\$env:USERNAME" `
             -LogonType Interactive -RunLevel Limited

# StartWhenAvailable catches up a run missed while the machine was off.
$settings = New-ScheduledTaskSettingsSet -StartWhenAvailable `
            -DontStopIfGoingOnBatteries -AllowStartIfOnBatteries `
            -ExecutionTimeLimit (New-TimeSpan -Hours 3) `
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
    -Description 'Downloads newly liked Spotify songs that are not already in your music library.' | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
$i = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "registered : $($t.TaskName)"
Write-Host "state      : $($t.State)"
Write-Host "next run   : $($i.NextRunTime)"
