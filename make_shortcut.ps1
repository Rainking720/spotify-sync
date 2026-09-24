$ErrorActionPreference = 'Stop'

$Py   = 'C:\Program Files\Python313\pythonw.exe'   # pythonw: no console window
$Dir  = 'C:\common\spotify-sync'
$Gui  = Join-Path $Dir 'gui.py'
$Icon = 'C:\Program Files\Python313\DLLs\py.ico'
$Name = 'Spotify Sync.lnk'

if (-not (Test-Path $Py))  { throw "pythonw not found: $Py" }
if (-not (Test-Path $Gui)) { throw "gui.py not found: $Gui" }

$desktop = [Environment]::GetFolderPath('Desktop')
$link    = Join-Path $desktop $Name

$sh = New-Object -ComObject WScript.Shell
$sc = $sh.CreateShortcut($link)
$sc.TargetPath       = $Py
$sc.Arguments        = '"{0}"' -f $Gui
$sc.WorkingDirectory = $Dir
$sc.Description      = 'Review and manage Spotify downloads'
if (Test-Path $Icon) { $sc.IconLocation = $Icon }
$sc.Save()

# Read it back rather than trusting Save()
$v = $sh.CreateShortcut($link)
Write-Host "created : $link"
Write-Host "target  : $($v.TargetPath)"
Write-Host "args    : $($v.Arguments)"
Write-Host "workdir : $($v.WorkingDirectory)"
Write-Host "icon    : $($v.IconLocation)"
Write-Host "on disk : $(Test-Path $link)"
