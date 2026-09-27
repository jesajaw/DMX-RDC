# Small background script that queries the currently playing media session on
# Windows (SMTC -- the same source behind the Windows volume flyout preview)
# and periodically writes title/artist/album to a file that the Python app
# (musicmode.py) reads. Runs directly via Windows' built-in PowerShell -- no
# install, no compiler needed.
#
# Deliberately does NOT attempt to read cover art. Reading a WinRT stream's
# raw bytes via PowerShell's late-bound COM dispatch (System.__ComObject)
# turned out to be unreliable in practice: simple property/method access
# (Title, Artist, Thumbnail, OpenReadAsync(), Size) works fine, because those
# members are declared directly on the interfaces involved -- but
# IInputStream.ReadAsync is *inherited*, not directly declared, on the stream
# interfaces here, and late-bound COM objects only reliably dispatch
# directly-declared members by name. Several different workarounds (DataReader,
# AsStreamForRead, a self-constructed IBuffer) were tried and each hit a
# different symptom of the same underlying type-erasure problem. Cover art is
# instead handled by the optional compiled NowPlayingBridge.exe (src/Program.cs,
# needs a one-time .NET SDK build -- see README), which has real compiler-level
# WinRT/await support and doesn't hit this limitation. NowPlayingReader in
# musicmode.py prefers that .exe when it's been built, and only falls back to
# this script otherwise.
#
# Usage:
#   powershell -NoProfile -ExecutionPolicy Bypass -File NowPlayingBridge.ps1 <output-dir> [<interval-ms>]
# (musicmode.py launches it exactly like this as a subprocess -- you never
# need to run this by hand, except to debug it directly.)
#
# Writes into <output-dir>:
#   nowplaying.json   {"title": "...", "artist": "...", "album": "..."}
#
# Runs in an infinite loop until the process is terminated (Python does this
# when Music Mode closes, via subprocess.terminate()).

param(
    [string]$OutputDir = $PSScriptRoot,
    [int]$IntervalMs = 2000
)

Add-Type -AssemblyName System.Runtime.WindowsRuntime

[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null

# Generic "Await" helper for WinRT's IAsyncOperation<T>: converts it into a
# regular .NET Task via the AsTask() extension method, then blocks on it.
# This is the standard community pattern for calling WinRT async APIs from
# PowerShell, which has no native `await`. Works fine here because we only
# ever call simple, directly-declared property/method members (Title, Artist,
# AlbumTitle, GetCurrentSession) on the results -- see the module docstring
# above for why this same general approach breaks down for stream reading.
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

$script:lastKey = $null

function Write-Result($Result) {
    $json = $Result | ConvertTo-Json -Depth 5

    # Set-Content can transiently fail with a sharing violation if another
    # process (Python reading it) has the file open at the exact same moment
    # -- a short retry clears this up almost always. If it's not transient,
    # silently give up on this write instead of throwing: this function can
    # be called from inside catch blocks with no further error handling
    # above them, so an unhandled exception here would kill the entire
    # polling loop for good, not just skip one write.
    $maxAttempts = 5
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        try {
            Set-Content -Path $jsonPath -Value $json -Encoding UTF8 -ErrorAction Stop
            return
        }
        catch {
            if ($attempt -eq $maxAttempts) {
                return
            }
            Start-Sleep -Milliseconds 50
        }
    }
}

function Clear-IfNeeded {
    if ($null -eq $script:lastKey) { return }
    $script:lastKey = $null
    Write-Result @{ title = ""; artist = ""; album = "" }
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

            $key = "$($props.Title)|$($props.Artist)"
            if ($key -ne $script:lastKey) {
                $script:lastKey = $key
                Write-Result @{ title = $props.Title; artist = $props.Artist; album = $props.AlbumTitle }
            }
        }
    }
    catch {
        # No active session, no media player running, WinRT call failed, etc. -- just try again next round
        Clear-IfNeeded
    }

    Start-Sleep -Milliseconds $IntervalMs
}
