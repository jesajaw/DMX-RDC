# Small background script that queries the currently playing media session on
# Windows (SMTC -- the same source behind the Windows volume flyout preview)
# and periodically writes title/artist/album/cover art to files that the
# Python app (musicmode.py) reads.
#
# Pass -IncludeDebugInfo to also write per-stage error details (debug) and the
# last 3 distinct tracks' cover/media errors (debugHistory) into nowplaying.json
# -- off by default, so the JSON that ships normally stays small and doesn't
# carry internal error strings. Useful for diagnosing why title/artist or
# cover art aren't showing up:
#   powershell -File NowPlayingBridge.ps1 <output-dir> <interval-ms> -IncludeDebugInfo

param(
    [string]$OutputDir = $PSScriptRoot,
    [int]$IntervalMs = 2000,
    [switch]$IncludeDebugInfo
)

Add-Type -AssemblyName System.Runtime.WindowsRuntime

# ---------------------------------------------------------------------------
# WinRT types
# ---------------------------------------------------------------------------

[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStreamWithContentType, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IBuffer, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.Buffer, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null

# ---------------------------------------------------------------------------
# WinRT async helpers
# ---------------------------------------------------------------------------

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]

function Await-WinRtTask($WinRtTask, [type]$ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    return $netTask.Result
}

# IInputStream.ReadAsync returns IAsyncOperationWithProgress<IBuffer, UInt32>,
# a different generic shape than plain IAsyncOperation<T> -- needs its own
# AsTask() overload. We deliberately ignore this helper's *return value* when
# reading the thumbnail below: it comes back as an untyped System.__ComObject
# just like everything else routed through reflection here, which is exactly
# what breaks every later typed method/constructor call. Instead we read into
# a buffer we constructed ourselves (see below), which stays properly typed.
$asTaskProgressGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperationWithProgress`2'
})[0]

function Await-WinRtProgress($WinRtTask, [type]$ResultType, [type]$ProgressType) {
    $asTask = $asTaskProgressGeneric.MakeGenericMethod($ResultType, $ProgressType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    return $netTask.Result
}

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

New-Item -ItemType Directory -Force -Path $OutputDir | Out-Null

$jsonPath = Join-Path $OutputDir "nowplaying.json"
$coverPath = Join-Path $OutputDir "nowplaying_cover.img"

$script:lastKey = $null

# Explicitly typed as an array so JSON keeps debugHistory as []
# even when there is only one entry.
[object[]]$script:debugHistory = @()

# ---------------------------------------------------------------------------
# Load existing debug history
# ---------------------------------------------------------------------------

if (Test-Path -LiteralPath $jsonPath) {
    try {
        $existing = Get-Content -Raw -LiteralPath $jsonPath | ConvertFrom-Json

        if ($null -ne $existing.debugHistory) {
            [object[]]$script:debugHistory = @(
                $existing.debugHistory
            ) | Select-Object -First 3
        }
    }
    catch {
        # A malformed/empty cache should not prevent the bridge from starting.
        [object[]]$script:debugHistory = @()
    }
}

# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

function Write-Result($Result) {

    if ($IncludeDebugInfo) {
        # Keep debugHistory as an array.
        [object[]]$Result.debugHistory = @($script:debugHistory)
    }
    else {
        # Debug info is opt-in -- strip it so the JSON that ships by default
        # stays small and doesn't leak internal error strings.
        $Result.Remove('debug') | Out-Null
        $Result.Remove('debugHistory') | Out-Null
    }

    # No -Compress: pretty-printed JSON is much easier to read/diff while debugging.
    $json = $Result | ConvertTo-Json -Depth 10

    # Set-Content can transiently fail with a sharing violation if another
    # process (Python reading it, or a second bridge instance) has the file
    # open at the exact same moment -- a short retry clears this up almost
    # always, since these locks are typically held for microseconds. If it's
    # NOT transient (e.g. a stray leftover process holding the file open
    # indefinitely), silently give up on this write instead of throwing --
    # this function can be called from inside catch blocks with no further
    # error handling above them, so an unhandled exception here would kill
    # the entire polling loop for good, not just skip one write.
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

function Add-DebugHistory($Entry) {

    # Track uniqueness by artist + title.
    $entryKey = "$($Entry.title)|$($Entry.artist)"

    # Remove an older copy of the same song.
    $filtered = @(
        $script:debugHistory | Where-Object {
            "$($_.title)|$($_.artist)" -ne $entryKey
        }
    )

    # Newest first, maximum 3 distinct songs.
    [object[]]$script:debugHistory = @(
        $Entry
        $filtered
    ) | Select-Object -First 3
}

function Clear-IfNeeded {

    if ($null -eq $script:lastKey) {
        return
    }

    $script:lastKey = $null

    $result = @{
        title    = ""
        artist   = ""
        album    = ""
        hasCover = $false
    }

    Write-Result $result

    if (Test-Path $coverPath) {
        Remove-Item $coverPath -Force
    }
}

# ---------------------------------------------------------------------------
# Main polling loop
# ---------------------------------------------------------------------------

while ($true) {

    # Fresh result every polling cycle.
    $result = @{
        title    = ""
        artist   = ""
        album    = ""
        hasCover = $false
    }

    try {

        # -------------------------------------------------------------------
        # Get current media session
        # -------------------------------------------------------------------

        $managerTask =
            [Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager]::RequestAsync()

        $manager = Await-WinRtTask `
            $managerTask `
            ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager])

        $session = $manager.GetCurrentSession()

        if ($null -eq $session) {
            Clear-IfNeeded
        }
        else {

            # ----------------------------------------------------------------
            # Get media properties
            # ----------------------------------------------------------------

            try {

                $propsTask = $session.TryGetMediaPropertiesAsync()

                $props = Await-WinRtTask `
                    $propsTask `
                    ([Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties])

                # Preserve exactly what SMTC gave us.
                $result.title = $props.Title
                $result.artist = $props.Artist
                $result.album = $props.AlbumTitle

                $key = "$($result.title)|$($result.artist)"

                # Only process the cover once per distinct track.
                if ($key -ne $script:lastKey) {

                    $script:lastKey = $key

                    # --------------------------------------------------------
                    # Get thumbnail reference
                    # --------------------------------------------------------

                    $coverDebug = $null
                    $thumb = $null

                    try {
                        $thumb = $props.Thumbnail
                    }
                    catch {
                        $coverDebug = @{
                            status    = "error"
                            stage     = "get_thumbnail"
                            message   = $_.Exception.Message
                            exception = $_.Exception.ToString()
                        }
                    }

                    if ($null -eq $coverDebug -and $null -eq $thumb) {
                        $coverDebug = @{
                            status  = "not_available"
                            stage   = "get_thumbnail"
                            message = "Media session returned no thumbnail."
                        }
                    }

                    # --------------------------------------------------------
                    # Read thumbnail
                    # --------------------------------------------------------

                    if ($null -ne $thumb -and $null -eq $coverDebug) {

                        $stream = $null

                        try {

                            $streamTask = $thumb.OpenReadAsync()

                            $stream = Await-WinRtTask `
                                $streamTask `
                                ([Windows.Storage.Streams.IRandomAccessStreamWithContentType])

                            if ($null -eq $stream) {
                                throw "OpenReadAsync returned no stream."
                            }

                            $streamSize = $stream.Size

                            if ($streamSize -eq 0) {
                                throw "Thumbnail stream is empty."
                            }

                            if ($streamSize -gt [uint32]::MaxValue) {
                                throw "Thumbnail stream is too large to read into a byte array: $streamSize bytes."
                            }

                            $size = [uint32]$streamSize

                            # ------------------------------------------------
                            # Convert the WinRT stream into a regular .NET
                            # Stream via the interop bridge extension method,
                            # then read it with completely ordinary .NET APIs
                            # -- no further WinRT-specific interop (DataReader,
                            # IInputStream reflection, etc.) needed, which is
                            # where the previous attempts kept failing on an
                            # untyped System.__ComObject.
                            # ------------------------------------------------

                            $netStream = [System.IO.WindowsRuntimeStreamExtensions]::AsStreamForRead($stream)

                            try {
                                $memoryStream = New-Object System.IO.MemoryStream
                                try {
                                    $netStream.CopyTo($memoryStream)
                                    $bytes = $memoryStream.ToArray()
                                }
                                finally {
                                    try { $memoryStream.Dispose() } catch { }
                                }
                            }
                            finally {
                                try { $netStream.Dispose() } catch { }
                            }

                            if ($null -eq $bytes -or $bytes.Length -eq 0) {
                                throw "AsStreamForRead() returned an empty buffer."
                            }

                            # ------------------------------------------------
                            # Write thumbnail
                            # ------------------------------------------------

                            [System.IO.File]::WriteAllBytes(
                                $coverPath,
                                $bytes
                            )

                            $result.hasCover = $true

                            $coverDebug = @{
                                status  = "ok"
                                stage   = "read_thumbnail"
                                message = "Thumbnail successfully read and written."
                                bytes   = $bytes.Length
                            }
                        }
                        catch {

                            if (Test-Path -LiteralPath $coverPath) {
                                Remove-Item `
                                    -LiteralPath $coverPath `
                                    -Force `
                                    -ErrorAction SilentlyContinue
                            }

                            $coverDebug = @{
                                status    = "error"
                                stage     = "read_thumbnail"
                                message   = $_.Exception.Message
                                exception = $_.Exception.ToString()
                            }
                        }
                        finally {

                            if ($null -ne $stream) {
                                try {
                                    $stream.Dispose()
                                }
                                catch {
                                    # Ignore cleanup errors.
                                }
                            }
                        }
                    }
                    elseif (Test-Path -LiteralPath $coverPath) {

                        Remove-Item `
                            -LiteralPath $coverPath `
                            -Force `
                            -ErrorAction SilentlyContinue
                    }

                    # --------------------------------------------------------
                    # Persistent debug history
                    # --------------------------------------------------------

                    $historyEntry = @{
                        title    = $result.title
                        artist   = $result.artist
                        album    = $result.album
                        hasCover = $result.hasCover
                        cover    = $coverDebug
                    }

                    Add-DebugHistory $historyEntry

                    # Current diagnostics.
                    $result.debug = @{
                        cover = $coverDebug
                    }

                    Write-Result $result
                }
            }
            catch {

                # Media properties failed, but preserve anything already
                # obtained and keep the previous history.
                $result.debug = @{
                    media_properties = @{
                        status    = "error"
                        stage     = "get_media_properties"
                        message   = $_.Exception.Message
                        exception = $_.Exception.ToString()
                    }
                }

                Write-Result $result
            }
        }
    }
    catch {

        # Session acquisition failed.
        # Existing debug history is deliberately preserved.
        $result.debug = @{
            bridge = @{
                status    = "error"
                stage     = "get_session"
                message   = $_.Exception.Message
                exception = $_.Exception.ToString()
            }
        }

        Write-Result $result
    }

    Start-Sleep -Milliseconds $IntervalMs
}