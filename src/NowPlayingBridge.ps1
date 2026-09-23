# Small background script that queries the currently playing media session on
# Windows (SMTC -- the same source behind the Windows volume flyout preview)
# and periodically writes title/artist/album/cover art to files that the
# Python app (musicmode.py) reads.
#
# The JSON cache also keeps the last 3 distinct songs in debugHistory so that
# cover/media errors remain available for debugging instead of being lost on
# the next polling cycle.

param(
    [string]$OutputDir = $PSScriptRoot,
    [int]$IntervalMs = 2000
)

Add-Type -AssemblyName System.Runtime.WindowsRuntime

# ---------------------------------------------------------------------------
# WinRT types
# ---------------------------------------------------------------------------

[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionManager, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Control.GlobalSystemMediaTransportControlsSessionMediaProperties, Windows.Media.Control, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IBuffer, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IInputStream, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.Streams.IRandomAccessStreamWithContentType, Windows.Storage.Streams, ContentType = WindowsRuntime] | Out-Null

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

$asTaskProgressGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
    $_.Name -eq 'AsTask' -and
    $_.GetParameters().Count -eq 1 -and
    $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperationWithProgress`2'
})[0]

function Await-WinRtProgress($WinRtTask, [type]$ResultType, [type]$ProgressType) {
    $asTask = $asTaskProgressGeneric.MakeGenericMethod(
        $ResultType,
        $ProgressType
    )

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

    # Keep debugHistory as an array.
    [object[]]$Result.debugHistory = @($script:debugHistory)

    # Normal direct write, same approach as the original working script.
    $Result |
        ConvertTo-Json -Depth 10 -Compress |
        Set-Content -Path $jsonPath -Encoding UTF8
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
        title        = ""
        artist       = ""
        album        = ""
        hasCover     = $false
        debugHistory = [object[]]$script:debugHistory
    }

    $result |
        ConvertTo-Json -Depth 10 -Compress |
        Set-Content -Path $jsonPath -Encoding UTF8

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
                            # Prepare WinRT buffer
                            # ------------------------------------------------

                            $bytes = New-Object byte[] ([int]$size)

                            $buffer =
                                [System.Runtime.InteropServices.WindowsRuntime.WindowsRuntimeBufferExtensions]::AsBuffer(
                                    $bytes
                                )

                            # ------------------------------------------------
                            # Invoke IInputStream.ReadAsync through reflection
                            # ------------------------------------------------

                            $readMethod =
                                [Windows.Storage.Streams.IInputStream].GetMethod(
                                    "ReadAsync",
                                    [type[]]@(
                                        [Windows.Storage.Streams.IBuffer],
                                        [uint32],
                                        [Windows.Storage.Streams.InputStreamOptions]
                                    )
                                )

                            if ($null -eq $readMethod) {
                                throw "Could not locate IInputStream.ReadAsync."
                            }

                            $readOperation = $readMethod.Invoke(
                                $stream,
                                @(
                                    $buffer,
                                    $size,
                                    [Windows.Storage.Streams.InputStreamOptions]::None
                                )
                            )

                            $readBuffer = Await-WinRtProgress `
                                $readOperation `
                                ([Windows.Storage.Streams.IBuffer]) `
                                ([System.UInt32])

                            if ($null -eq $readBuffer) {
                                throw "ReadAsync returned no buffer."
                            }

                            $bytes = [System.Runtime.InteropServices.WindowsRuntime.WindowsRuntimeBufferExtensions]::ToArray($readBuffer)

                            if ($null -eq $bytes -or $bytes.Length -eq 0) {
                                throw "ReadAsync returned an empty buffer."
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