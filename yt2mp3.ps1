[CmdletBinding()]
param(
    [Parameter(Position=0)]
    [string]$Url,

    [string]$MusicRoot = 'C:\temp\SpotifyDownloadOnTheSpot\Sorted',
    [string]$TempRoot  = 'C:\temp\SpotifyDownloadOnTheSpot\Tracks',
    [string]$YtDlp,
    [string]$Ffmpeg,
    [string]$ContactEmail = '',
    [string]$Artist,
    [string]$Album,
    [string]$Title,
    [string]$Track,
    [string]$Year,
    [string]$CoverUrl,
    [string]$PathOut,
    [switch]$KeepTemp,
    [switch]$NoLookup,
    [switch]$NonInteractive
)

$ErrorActionPreference = 'Stop'

# Settings from config.json beside this script -- the same file the Python side
# reads (see settings.py). Used only for options not given on the command line,
# so the defaults above still apply when a key is missing.
$cfgFile = Join-Path $PSScriptRoot 'config.json'
$cfg = $null
if (Test-Path -LiteralPath $cfgFile) {
    try { $cfg = Get-Content -LiteralPath $cfgFile -Raw -Encoding UTF8 | ConvertFrom-Json }
    catch { Write-Warning "config.json could not be read: $($_.Exception.Message)" }
}
function Get-Setting([string]$Name) {
    if ($cfg -and $cfg.PSObject.Properties[$Name] -and $cfg.$Name) {
        return [Environment]::ExpandEnvironmentVariables([string]$cfg.$Name)
    }
    return $null
}
if (-not $PSBoundParameters.ContainsKey('MusicRoot') -and (Get-Setting 'staging_root')) {
    $MusicRoot = Get-Setting 'staging_root' }
if (-not $PSBoundParameters.ContainsKey('TempRoot') -and (Get-Setting 'temp_root')) {
    $TempRoot = Get-Setting 'temp_root' }
if (-not $PSBoundParameters.ContainsKey('ContactEmail') -and (Get-Setting 'contact_email')) {
    $ContactEmail = Get-Setting 'contact_email' }
if (-not $YtDlp  -and (Get-Setting 'ytdlp_path'))  { $YtDlp  = Get-Setting 'ytdlp_path' }
if (-not $Ffmpeg -and (Get-Setting 'ffmpeg_path')) { $Ffmpeg = Get-Setting 'ffmpeg_path' }

# MusicBrainz asks for contact details in the User-Agent; set contact_email.
$UserAgent = if ($ContactEmail) { "yt2mp3-script/1.0 ( $ContactEmail )" } else { "yt2mp3-script/1.0" }

function Resolve-Tool {
    param([string]$Explicit, [string]$Name)
    if ($Explicit -and (Test-Path -LiteralPath $Explicit)) { return $Explicit }
    # Next to the script, then its parent folder (C:\common, where yt-dlp.exe and
    # ffmpeg.exe live), and only then PATH. PATH alone would pick up a pip-installed
    # yt-dlp that is months older, and YouTube breaks old versions.
    # tools\ first: that's where the Settings screen's "Download latest" installs.
    foreach ($dir in @((Join-Path $PSScriptRoot 'tools'), $PSScriptRoot,
                       (Split-Path -Parent $PSScriptRoot))) {
        $candidate = Join-Path $dir $Name
        if (Test-Path -LiteralPath $candidate) { return $candidate }
    }
    $cmd = Get-Command $Name -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    throw "$Name not found (looked next to the script, in its parent folder, and on PATH)"
}

$YtDlp  = Resolve-Tool -Explicit $YtDlp  -Name 'yt-dlp.exe'
$Ffmpeg = Resolve-Tool -Explicit $Ffmpeg -Name 'ffmpeg.exe'
Write-Verbose "yt-dlp: $YtDlp"
Write-Verbose "ffmpeg: $Ffmpeg"
Write-Verbose "MusicRoot: $MusicRoot"
Write-Verbose "TempRoot: $TempRoot"
Write-Verbose "User-Agent: $UserAgent"

if ([string]::IsNullOrWhiteSpace($Url)) {
    $Url = (Read-Host -Prompt 'Paste YouTube URL').Trim()
    if ([string]::IsNullOrWhiteSpace($Url)) { throw 'No URL provided.' }
}

function Sanitize-PathComponent {
    param([string]$s)
    if ([string]::IsNullOrWhiteSpace($s)) { return '_' }
    $s = $s -replace '[<>:"/\\|?*]', '_'
    $s = $s -replace '[\x00-\x1F]', ''
    $s = $s.Trim().TrimEnd('.')
    if ([string]::IsNullOrWhiteSpace($s)) { return '_' }
    return $s
}

function Get-MbRecording {
    param([string]$ArtistQ, [string]$TitleQ)

    $escA = $ArtistQ -replace '"', '\"'
    $escT = $TitleQ  -replace '"', '\"'
    $query = "artist:`"$escA`" AND recording:`"$escT`""
    $uri = 'https://musicbrainz.org/ws/2/recording/?query=' +
           [Uri]::EscapeDataString($query) + '&fmt=json&limit=10'

    $resp = Invoke-RestMethod -Uri $uri -UserAgent $UserAgent -ErrorAction Stop
    Start-Sleep -Milliseconds 1100  # MB asks for <= 1 req/sec

    if (-not $resp.recordings -or $resp.recordings.Count -eq 0) { return $null }

    # MB returns sorted by score already; take the top hit.
    $rec = $resp.recordings | Select-Object -First 1

    $artistName = ''
    if ($rec.'artist-credit') {
        foreach ($ac in $rec.'artist-credit') {
            $artistName += $ac.name
            if ($ac.joinphrase) { $artistName += $ac.joinphrase }
        }
    }
    $artistName = $artistName.Trim()
    if (-not $artistName) { $artistName = $ArtistQ }

    # Choose best release: prefer Album, then Single, then anything; prefer earliest date.
    $releases = @($rec.releases)
    if ($releases.Count -eq 0) {
        return [pscustomobject]@{
            Artist = $artistName; Title = $rec.title; Album = $null
            Track = $null; Year = $null; ReleaseId = $null
        }
    }

    $rank = @{ 'Album' = 0; 'EP' = 1; 'Single' = 2; 'Compilation' = 3 }
    $scored = foreach ($r in $releases) {
        $type = $r.'release-group'.'primary-type'
        $score = if ($rank.ContainsKey($type)) { $rank[$type] } else { 9 }
        $date  = if ($r.date) { $r.date } else { '9999' }
        [pscustomobject]@{ Release = $r; Score = $score; Date = $date }
    }
    $best = $scored | Sort-Object Score, Date | Select-Object -First 1
    $rel  = $best.Release

    $trackNum = $null
    if ($rel.media -and $rel.media.Count -gt 0) {
        $med = $rel.media[0]
        if ($med.track -and $med.track.Count -gt 0) {
            $trackNum = $med.track[0].number
        }
    }

    $year = $null
    if ($rel.date) { $year = ($rel.date -split '-')[0] }

    return [pscustomobject]@{
        Artist    = $artistName
        Title     = $rec.title
        Album     = $rel.title
        Track     = $trackNum
        Year      = $year
        ReleaseId = $rel.id
    }
}

# --- Main ---

if (-not (Test-Path $TempRoot)) { New-Item -ItemType Directory -Path $TempRoot -Force | Out-Null }
$tempDir = Join-Path $TempRoot ([Guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $tempDir | Out-Null

try {
    Write-Host "==============================" -ForegroundColor Cyan
    Write-Host "  yt2mp3" -ForegroundColor Cyan
    Write-Host "==============================" -ForegroundColor Cyan
    Write-Host "URL: $Url"
    Write-Host "Downloading..."

    $outTpl = Join-Path $tempDir '%(title)s.%(ext)s'

    # Title-cleanup regexes are applied in PowerShell after parsing (PS 5.1 strips
    # empty-string args, which breaks yt-dlp's --replace-in-metadata FIELDS REGEX REPLACE).
    $ytArgs = @(
        '--js-runtimes', 'node',
        # Tell yt-dlp which ffmpeg to use (and its ffprobe beside it) rather than
        # relying on it finding one on PATH or next to itself.
        '--ffmpeg-location', (Split-Path -Parent $Ffmpeg),
        '-f', 'bestaudio',
        '-x',
        '--audio-format', 'mp3',
        '--audio-quality', '0',
        '--postprocessor-args', '-ar 44100',
        '-o', $outTpl,
        '--print', 'after_move:filepath',
        '--no-warnings',
        '--quiet',
        $Url
    )

    $stdout = & $YtDlp @ytArgs
    if ($LASTEXITCODE -ne 0) { throw "yt-dlp failed (exit $LASTEXITCODE)" }

    $downloadedPath = $null
    foreach ($line in @($stdout)) {
        $candidate = "$line".Trim('"').Trim()
        if ($candidate -and (Test-Path -LiteralPath $candidate)) { $downloadedPath = $candidate }
    }
    if (-not $downloadedPath) {
        $first = Get-ChildItem -Path $tempDir -Filter *.mp3 -File | Select-Object -First 1
        if ($first) { $downloadedPath = $first.FullName }
    }
    if (-not $downloadedPath -or -not (Test-Path -LiteralPath $downloadedPath)) {
        throw "yt-dlp did not produce an mp3"
    }
    Write-Host "Downloaded: $downloadedPath"

    # Parse 'Artist - Title' from filename; prompt if it can't be split.
    $fileBase = [System.IO.Path]::GetFileNameWithoutExtension($downloadedPath)
    $parts = $fileBase -split '\s+-\s+', 2
    if ($Artist -and $Title) {
        # Caller supplied both (unattended runs): trust them, never prompt.
        $rawArtist = $Artist
        $rawTitle  = $Title
    } elseif ($parts.Count -eq 2) {
        $rawArtist = $parts[0].Trim()
        $rawTitle  = $parts[1].Trim()
    } elseif ($NonInteractive) {
        throw "Cannot parse '$fileBase' as 'Artist - Title'; pass -Artist and -Title."
    } else {
        Write-Host ""
        Write-Warning "Filename '$fileBase' doesn't match 'Artist - Title'."
        Write-Host "Please enter artist and title manually:"
        do {
            $rawArtist = (Read-Host -Prompt '  Artist').Trim()
        } while ([string]::IsNullOrWhiteSpace($rawArtist))
        do {
            $rawTitle = (Read-Host -Prompt ('  Title [default: {0}]' -f $fileBase)).Trim()
            if ([string]::IsNullOrWhiteSpace($rawTitle)) { $rawTitle = $fileBase }
        } while ([string]::IsNullOrWhiteSpace($rawTitle))
    }

    # Strip bracketed groups like "(Official Video)", "[HD]" from YouTube-derived
    # titles only. A caller-supplied -Title is authoritative: "(Acoustic)" and
    # "(Reimagined)" identify the recording and must survive.
    if (-not $Title) {
        $rawTitle = ($rawTitle -replace '\s*[\[\(].*?[\]\)]\s*', ' ').Trim()
        $rawTitle = $rawTitle -replace '\s+$', ''
    }

    # Strip "feat. X" only for the lookup query (keep it in the final title)
    $lookupTitle = $rawTitle -replace '\s*[\(\[]?\s*(feat|ft|featuring)\.?\s+[^\)\]]+[\)\]]?\s*', ' '
    $lookupTitle = ($lookupTitle -replace '\s+', ' ').Trim()

    Write-Host "Parsed - Artist: '$rawArtist' | Title: '$rawTitle'"

    # MusicBrainz lookup (unless overridden)
    $mb = $null
    if (-not $NoLookup) {
        try {
            $mb = Get-MbRecording -ArtistQ $rawArtist -TitleQ $lookupTitle
        } catch {
            Write-Warning "MusicBrainz lookup failed: $($_.Exception.Message)"
        }
    }

    if ($mb) {
        Write-Host ("MB hit  - Artist: '{0}' | Album: '{1}' | Track: {2} | Year: {3}" -f `
            $mb.Artist, $mb.Album, $mb.Track, $mb.Year)
    } else {
        Write-Host "No MusicBrainz match; using parsed filename"
    }

    # Resolve final values (CLI overrides win over MB which wins over filename)
    $finalArtist = if ($Artist)              { $Artist }
                   elseif ($mb -and $mb.Artist) { $mb.Artist }
                   else                      { $rawArtist }
    $finalTitle  = if ($Title)               { $Title }
                   elseif ($mb -and $mb.Title)  { $mb.Title }
                   else                      { $rawTitle }
    $finalAlbum  = if ($Album)               { $Album }
                   elseif ($mb -and $mb.Album)  { $mb.Album }
                   else                      { 'Singles' }
    $finalYear   = if ($Year)                { $Year }
                   elseif ($mb -and $mb.Year)   { $mb.Year }
                   else                      { $null }

    $trackInt = 0
    if ($Track) {
        $m = [regex]::Match([string]$Track, '\d+')
        if ($m.Success) { $trackInt = [int]$m.Value }
    } elseif ($mb -and $mb.Track) {
        $m = [regex]::Match([string]$mb.Track, '\d+')
        if ($m.Success) { $trackInt = [int]$m.Value }
    }
    $trackStr = '{0:D2}' -f $trackInt

    # Cover art (best-effort)
    $coverPath = $null
    if ($CoverUrl) {
        $coverPath = Join-Path $tempDir 'cover.jpg'
        try {
            Invoke-WebRequest -Uri $CoverUrl -OutFile $coverPath `
                -UserAgent $UserAgent -UseBasicParsing -ErrorAction Stop
            Write-Host "Cover art: fetched (caller-supplied)"
        } catch {
            Write-Host "Cover art: caller URL failed"
            $coverPath = $null
        }
    }
    if (-not $coverPath -and $mb -and $mb.ReleaseId) {
        $coverPath = Join-Path $tempDir 'cover.jpg'
        try {
            Invoke-WebRequest -Uri "https://coverartarchive.org/release/$($mb.ReleaseId)/front-500" `
                -OutFile $coverPath -UserAgent $UserAgent -UseBasicParsing -ErrorAction Stop
            Write-Host "Cover art: fetched"
        } catch {
            Write-Host "Cover art: none available"
            $coverPath = $null
        }
    }

    # Final destination
    $artistDir = Sanitize-PathComponent $finalArtist
    $albumDir  = Sanitize-PathComponent $finalAlbum
    $finalDir  = Join-Path (Join-Path $MusicRoot $artistDir) $albumDir
    New-Item -ItemType Directory -Path $finalDir -Force | Out-Null

    $finalFileName = "$(Sanitize-PathComponent $finalArtist) - $trackStr - $(Sanitize-PathComponent $finalTitle).mp3"
    $finalPath = Join-Path $finalDir $finalFileName

    if (Test-Path -LiteralPath $finalPath) {
        $stamp = Get-Date -Format 'yyyyMMdd-HHmmss'
        $finalPath = Join-Path $finalDir ("$(Sanitize-PathComponent $finalArtist) - $trackStr - $(Sanitize-PathComponent $finalTitle) ($stamp).mp3")
    }

    # ffmpeg: stream-copy audio, optionally attach cover, write ID3v2.3
    $ffArgs = @('-hide_banner', '-loglevel', 'error', '-y', '-i', $downloadedPath)
    if ($coverPath) { $ffArgs += @('-i', $coverPath) }
    $ffArgs += @('-map', '0:a')
    if ($coverPath) {
        $ffArgs += @('-map', '1:v', '-c:v', 'copy', '-disposition:v:0', 'attached_pic')
        $ffArgs += @('-metadata:s:v:0', 'title=Album cover')
        $ffArgs += @('-metadata:s:v:0', 'comment=Cover (front)')
    }
    $ffArgs += @('-c:a', 'copy', '-id3v2_version', '3', '-write_id3v1', '1')
    $ffArgs += @('-metadata', "artist=$finalArtist")
    $ffArgs += @('-metadata', "title=$finalTitle")
    $ffArgs += @('-metadata', "album=$finalAlbum")
    $ffArgs += @('-metadata', "album_artist=$finalArtist")
    $ffArgs += @('-metadata', "track=$trackStr")
    if ($finalYear) { $ffArgs += @('-metadata', "date=$finalYear") }
    $ffArgs += $finalPath

    & $Ffmpeg @ffArgs
    if ($LASTEXITCODE -ne 0) { throw "ffmpeg failed (exit $LASTEXITCODE)" }

    Write-Host ""
    Write-Host "==============================" -ForegroundColor Green
    Write-Host "  DONE" -ForegroundColor Green
    Write-Host "==============================" -ForegroundColor Green
    Write-Host $finalPath

    # Hand the final path back in UTF-8. Parsing it out of stdout breaks on
    # non-ASCII names, because the console encoding mangles them.
    if ($PathOut) {
        [System.IO.File]::WriteAllText($PathOut, $finalPath, (New-Object System.Text.UTF8Encoding($false)))
    }
}
finally {
    if (-not $KeepTemp) {
        Remove-Item -Path $tempDir -Recurse -Force -ErrorAction SilentlyContinue
    } else {
        Write-Host "Temp kept: $tempDir"
    }
}
