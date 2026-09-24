$ErrorActionPreference = 'Stop'
$TaskName = 'Spotify Liked Sync'
$Py   = 'C:\Program Files\Python313\pythonw.exe'
$Dir  = 'C:\common\spotify-sync'
$Arg  = '-u "C:\common\spotify-sync\run_sync.py"'

if (-not (Test-Path $Py))  { throw "pythonw not found: $Py" }
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

Register-ScheduledTask -TaskName $TaskName -Action $action -Trigger $trigger `
    -Principal $principal -Settings $settings -Force `
    -Description 'Downloads newly liked Spotify songs that are not already in D:\Mp3.' | Out-Null

$t = Get-ScheduledTask -TaskName $TaskName
$i = Get-ScheduledTaskInfo -TaskName $TaskName
Write-Host "registered : $($t.TaskName)"
Write-Host "state      : $($t.State)"
Write-Host "next run   : $($i.NextRunTime)"
