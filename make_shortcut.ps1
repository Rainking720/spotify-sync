param([string]$Python, [switch]$DryRun)   # -DryRun: show what would be created
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
$Gui  = Join-Path $Dir 'gui.py'
$Icon = Join-Path (Split-Path -Parent $Py) 'DLLs\py.ico'
$Name = 'Spotify Sync.lnk'
$Arg  = '"' + $Gui + '"'
if (-not (Test-Path $Gui)) { throw "gui.py not found: $Gui" }

if ($DryRun) {
    Write-Host "shortcut: $Name (dry run, nothing created)"
    Write-Host "  execute     : $Py"
    Write-Host "  arguments   : $Arg"
    Write-Host "  working dir : $Dir"
    Write-Host "  icon        : $Icon"
    return
}
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
