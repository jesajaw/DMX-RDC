# Small background script that queries the currently playing media session on
# Windows (SMTC -- the same source behind the Windows volume flyout preview)
# and periodically writes title/artist/cover art to files that the Python app
# (musicmode.py) reads.
#
# Runs directly via Windows' built-in PowerShell -- NO extra install, NO
# compiler, NO .NET SDK needed. PowerShell has built-in support for loading
# WinRT types (the `[Type,Assembly,ContentType=WindowsRuntime]` syntax below),
# which is what lets it call Windows.Media.Control directly.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File NowPlayingBridge.ps1 <output-dir> [<interval-ms>]
# (musicmode.py launches it exactly like this as a subprocess -- you never
# need to run this by hand.)
#
# Writes into <output-dir>:
#   nowplaying.json        {"title": "...", "artist": "...", "hasCover": true/false}
#   nowplaying_cover.img   Raw thumbnail bytes (format depends on the source,
#                          usually PNG/JPEG; PIL on the Python side detects
#                          the format automatically)
#
# This script is community-pattern based: the "Await" WinRT-interop helper
# below is a well-known public pattern for calling WinRT's async APIs from
# PowerShell (PowerShell has no native `await`), but I could not run this
# script myself against a live Windows media session. If something in the
# WinRT call chain doesn't quite match on your system, this is the file to
# adjust -- run it directly in a terminal (see Usage above) to see errors
# instead of them disappearing into the background process.
#
# Runs in an infinite loop until the process is terminated (Python does this
# when Music Mode closes, via subprocess.terminate()).

param(
    [string]$OutputDir = $PSScriptRoot,
    [int]$IntervalMs = 2000
)

Add-Type -AssemblyName System.Runtime.WindowsRuntime

# Load the WinRT types we need
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.DataReader, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStreamWithContentType, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null

# Generic "Await" helper for WinRT's IAsyncOperation<T>: converts it into a
# regular .NET Task via the AsTask() extension method, then blocks on it.
# This is the standard community pattern for calling WinRT async APIs from
# PowerShell, which has no native `await`.
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
    })[0]

function Await-WinRtTask($WinRtTask, [type]$ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    return $netTask.Result
}

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null
$jsonPath = Join-Path $OutputDir "nowplaying.json"
$coverPath = Join-Path $OutputDir "nowplaying_cover.img"

$script:lastKey = $null

function Clear-IfNeeded {
    if ($null -eq $script:lastKey) { return }
    $script:lastKey = $null
    (@{ title = ""; artist = ""; hasCover = $false } | ConvertTo-Json -Compress) |
        Set-Content -Path $jsonPath -Encoding UTF8
    if (Test-Path $coverPath) { Remove-Item $coverPath -Force }
}

while ($true) {
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
            $title = $props.Title
            $artist = $props.Artist
            $key = "$title|$artist"

            if ($key -ne $script:lastKey) {
                $script:lastKey = $key
                $hasCover = $false
                $thumb = $props.Thumbnail

                if ($null -ne $thumb) {
                    try {
                        $streamTask = $thumb.OpenReadAsync()
                        $stream = Await-WinRtTask $streamTask ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])
                        $reader = [Windows.Storage.Streams.DataReader]::new($stream)
                        $loadTask = $reader.LoadAsync([uint32]$stream.Size)
                        Await-WinRtTask $loadTask ([uint32]) | Out-Null
                        $bytes = New-Object byte[] ($stream.Size)
                        $reader.ReadBytes($bytes)
                        [System.IO.File]::WriteAllBytes($coverPath, $bytes)
                        $hasCover = $true
                    }
                    catch {
                        if (Test-Path $coverPath) { Remove-Item $coverPath -Force }
                    }
                }
                elseif (Test-Path $coverPath) {
                    Remove-Item $coverPath -Force
                }

                (@{ title = $title; artist = $artist; hasCover = $hasCover } | ConvertTo-Json -Compress) |
                    Set-Content -Path $jsonPath -Encoding UTF8
            }
        }
    }
    catch {
        # No active session, no media player running, WinRT call failed, etc. -- just try again next round
        Clear-IfNeeded
    }

    Start-Sleep -Milliseconds $IntervalMs
}
