# AI GENERATED!
# Small background script that queries the currently playing media session on
# Windows (SMTC -- the same source behind the Windows volume flyout preview)
# and periodically writes title/artist/album/playing-state and the cover art to
# files that the Python app (musicmode.py) reads. Runs directly via Windows'
# built-in PowerShell -- no install, no compiler needed.
#
# Cover art: the stream is opened with OpenReadAsync(), awaited with its real
# WinRT type loaded (IRandomAccessStreamWithContentType), and converted to a
# normal .NET stream via WindowsRuntimeStreamExtensions.AsStreamForRead, then
# copied to a file. The earlier failed attempts used late-bound System.__ComObject
# results without the WinRT stream types loaded, so PowerShell could not pick the
# right overload. Loading the types up front (below) is what makes it dispatch.
# If it still fails on some machine, the reason is written to bridge.log
# (stdout) and the app simply keeps showing the pixel-art fallback.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File NowPlayingBridge.ps1 <output-dir> [<interval-ms>] (musicmode.py launches it exactly like this as a subprocess -- you never need to run this by hand, except to debug it directly.)
#
# Writes into <output-dir>:
#   nowplaying.json        {"title": "...", "artist": "...", "album": "...", "hasCover": true/false, "playing": true/false}
#   nowplaying_cover.img   Raw thumbnail bytes (PNG/JPEG, PIL detects the format)
#
# Runs in an infinite loop until the process is terminated (Python does this when Music Mode closes, via subprocess.terminate()).

param(
    [string]$OutputDir = $PSScriptRoot,
    [int]$IntervalMs = 2000,
    [int]$ParentPid = 0,     # Python's PID: this script exits by itself once that process is gone
    [switch]$RunDebug        # write cover_debug.log into <output-dir> (off by default)
)

Add-Type -AssemblyName System.Runtime.WindowsRuntime

[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionPlaybackStatus, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStreamReference, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStreamWithContentType, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IInputStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null

# Generic "Await" helper for WinRT's IAsyncOperation<T>: converts it into a
# regular .NET Task via AsTask(), then blocks on it.
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

# WinRT streams show up in PowerShell only as opaque System.__ComObject, so
# PowerShell's own method binding / casting ([IInputStream]$x) can't handle them.
# Calling the extension methods through reflection hands the raw object straight
# to the CLR, which knows how to treat it -- same trick as AsTask above.
$asStreamForReadMethod = [System.IO.WindowsRuntimeStreamExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsStreamForRead' -and $_.GetParameters().Count -eq 1
} | Select-Object -First 1
$asStreamMethod = [System.IO.WindowsRuntimeStreamExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsStream' -and $_.GetParameters().Count -eq 1
} | Select-Object -First 1

function Await-WinRtTask($WinRtTask, [type]$ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    return $netTask.Result
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

# Stop stale copies of this bridge (e.g. left over after the app froze/was killed) so that two bridges never fight over the same files.
try {
    Get-CimInstance Win32_Process -Filter "Name = 'powershell.exe'" |
        Where-Object { $_.ProcessId -ne $PID -and $_.CommandLine -like '*NowPlayingBridge.ps1*' } |
        ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }
} catch { }
$jsonPath = Join-Path $OutputDir "nowplaying.json"
$coverPath = Join-Path $OutputDir "nowplaying_cover.img"

$utf8NoBom = New-Object System.Text.UTF8Encoding($false)   # PS 5.1 Set-Content -Encoding UTF8 adds a BOM, which breaks Python's json.loads
$script:lastKey = $null
$script:lastPlaying = $null
$script:hasCover = $false
$script:coverTries = 0
$maxCoverTries = 6   # thumbnails often arrive a moment after the track changes

function Write-Result($Result) {
    $json = $Result | ConvertTo-Json -Depth 5

    # Set-Content can transiently fail with a sharing violation if Python has
    # the file open at the same moment -- a short retry clears that up. If it
    # is not transient, give up on this write instead of throwing, so the
    # polling loop never dies.
    $maxAttempts = 5
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        try {
            [System.IO.File]::WriteAllText($jsonPath, $json, $utf8NoBom)
            return
        }
        catch {
            if ($attempt -eq $maxAttempts) { return }
            Start-Sleep -Milliseconds 50
        }
    }
}

function Remove-Cover {
    if (Test-Path $coverPath) { Remove-Item $coverPath -Force -ErrorAction SilentlyContinue }
}

# Appends a line to cover_debug.log in the output dir. Deliberately NOT Write-Output: inside a function that would become part of the return value.
function Write-DebugLog([string]$Message) {
    if (-not $RunDebug) { return }
    try {
        Add-Content -Path (Join-Path $OutputDir "cover_debug.log") -Value ("{0} {1}" -f (Get-Date -Format "HH:mm:ss"), $Message) -Encoding UTF8 -ErrorAction SilentlyContinue
    } catch { }
}

# Dispose/Close without ever throwing (a throw here used to escape Save-Cover and wipe the whole now-playing state).
function Close-Quietly($Obj) {
    if ($null -eq $Obj) { return }
    try { $Obj.Dispose() } catch {
        try { $Obj.Close() } catch { }
    }
}

# Copies the thumbnail stream into $coverPath. Returns ONLY $true / $false and should never throw.
function Save-Cover($ThumbnailRef) {
    if ($null -eq $ThumbnailRef) { Write-DebugLog "no thumbnail from SMTC"; Remove-Cover; return $false }
    $tmp = "$coverPath.tmp"
    $ok = $false
    $stream = $null
    $netStream = $null
    $file = $null
    try {
        $stream = Await-WinRtTask ($ThumbnailRef.OpenReadAsync()) ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
        if ($null -eq $stream) { throw "OpenReadAsync returned no stream" }

        try {
            $netStream = $asStreamForReadMethod.Invoke($null, [object[]]@($stream))
        }
        catch {
            Write-DebugLog "AsStreamForRead via reflection failed: $($_.Exception.Message) -- trying AsStream"
            $netStream = $asStreamMethod.Invoke($null, [object[]]@($stream))
        }
        if ($null -eq $netStream) { throw "could not convert WinRT stream to a .NET stream" }

        $file = [System.IO.File]::Create($tmp)
        $netStream.CopyTo($file)
        $file.Dispose(); $file = $null

        $len = (Get-Item $tmp).Length
        if ($len -le 0) { throw "thumbnail stream was empty" }
        Move-Item -Path $tmp -Destination $coverPath -Force
        Write-DebugLog "cover saved, $len bytes"
        $ok = $true
    }
    catch {
        Write-DebugLog "cover read failed: $($_.Exception.ToString())"
        Remove-Cover
    }
    finally {
        Close-Quietly $file
        Close-Quietly $netStream
        Close-Quietly $stream
        if (Test-Path $tmp) { Remove-Item $tmp -Force -ErrorAction SilentlyContinue }
    }
    return ($ok -and (Test-Path $coverPath))
}

function Clear-IfNeeded {
    if ($null -eq $script:lastKey) { return }
    $script:lastKey = $null
    $script:lastPlaying = $null
    $script:hasCover = $false
    Remove-Cover
    Write-Result @{ title = ""; artist = ""; album = ""; hasCover = $false; playing = $false }
}

while ($true) {
    if ($ParentPid -gt 0 -and -not (Get-Process -Id $ParentPid -ErrorAction SilentlyContinue)) { exit }
    try {
        $managerTask = [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()
        $manager = Await-WinRtTask $managerTask ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])
        $session = $manager.GetCurrentSession()

        if ($null -eq $session) {
            Clear-IfNeeded
        }
        else {
            $propsTask = $session.TryGetMediaPropertiesAsync()
            $props = Await-WinRtTask $propsTask ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])

            # PlaybackStatus: 4 = Playing (Closed 0, Opened 1, Changing 2, Stopped 3, Playing 4, Paused 5)
            $playing = ([int]$session.GetPlaybackInfo().PlaybackStatus -eq 4)

            $key = "$($props.Title)|$($props.Artist)"
            $changed = $false

            if ($key -ne $script:lastKey) {
                $script:lastKey = $key
                $script:coverTries = 0
                $script:hasCover = $false
                Remove-Cover
                $changed = $true
            }

            # Retry a few polls if the cover is not there yet after a track change
            if (-not $script:hasCover -and $script:coverTries -lt $maxCoverTries) {
                $script:coverTries++
                if (Save-Cover $props.Thumbnail) {
                    $script:hasCover = $true
                    $changed = $true
                }
            }

            if ($playing -ne $script:lastPlaying) {
                $script:lastPlaying = $playing
                $changed = $true
            }

            if ($changed) {
                Write-Result @{
                    title = $props.Title; artist = $props.Artist; album = $props.AlbumTitle
                    hasCover = $script:hasCover; playing = $playing
                }
            }
        }
    }
    catch {
        # No active session, no media player running, WinRT call failed, etc. -- just try again next round
        Write-DebugLog "main loop error: $($_.Exception.Message)"
        Clear-IfNeeded
    }

    Start-Sleep -Milliseconds $IntervalMs
}
