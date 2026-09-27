// NowPlayingBridge
// =================
// Small background process that queries the currently playing media session
// on Windows (SMTC -- the same source behind the Windows volume flyout
// preview) and periodically writes title/artist/album/cover art to files
// that the Python app (musicmode.py) reads.
//
// Why C# and not (only) the PowerShell script (src/NowPlayingBridge.ps1):
// reading a WinRT stream's raw bytes via PowerShell's late-bound COM dispatch
// turned out to be unreliable -- several different workarounds all hit some
// variant of the same type-erasure problem (see NowPlayingBridge.ps1's own
// docstring for details). C# has real, compiler-level WinRT projection and
// `await` support (via the "-windows10.0.19041.0" TargetFramework in
// NowPlayingBridge.csproj), so none of that applies here -- this is the
// standard, well-documented way to consume WinRT APIs from managed code.
// The cost is a one-time .NET SDK build step (see README), which is why the
// PowerShell script remains the zero-install baseline for title/artist/album,
// with this compiled bridge as the optional upgrade that adds cover art.
//
// Usage: NowPlayingBridge.exe <output-dir> [<interval-ms>]
// Writes into <output-dir>:
//   nowplaying.json        {"title": "...", "artist": "...", "album": "...", "hasCover": true/false}
//   nowplaying_cover.img   Raw thumbnail bytes (format depends on the source,
//                          usually PNG/JPEG; PIL on the Python side detects
//                          the format automatically)
//
// Build (once, requires the .NET 8 SDK -- https://dotnet.microsoft.com/download):
//   cd src
//   dotnet publish -c Release -r win-x64 --self-contained false -o out
//
// Runs in an infinite loop until the process is terminated (Python does this
// when Music Mode closes, via subprocess.terminate()).

using System.Text.Json;
using Windows.Media.Control;
using Windows.Storage.Streams;

var outputDir = args.Length > 0 ? args[0] : AppContext.BaseDirectory;
var intervalMs = args.Length > 1 && int.TryParse(args[1], out var parsedInterval) ? parsedInterval : 2000;

Directory.CreateDirectory(outputDir);
var jsonPath = Path.Combine(outputDir, "nowplaying.json");
var coverPath = Path.Combine(outputDir, "nowplaying_cover.img");

string? lastKey = null;

while (true)
{
    try
    {
        var manager = await GlobalSystemMediaTransportControlsSessionManager.RequestAsync();
        var session = manager.GetCurrentSession();

        if (session is null)
        {
            ClearIfNeeded();
        }
        else
        {
            var props = await session.TryGetMediaPropertiesAsync();
            var title = props.Title ?? "";
            var artist = props.Artist ?? "";
            var album = props.AlbumTitle ?? "";
            var key = title + "|" + artist;

            if (key != lastKey)
            {
                lastKey = key;
                var hasCover = await TryWriteCoverAsync(props.Thumbnail);
                WriteJson(title, artist, album, hasCover);
            }
        }
    }
    catch
    {
        // No active session, no media player running, etc. -- just try again next round
        ClearIfNeeded();
    }

    await Task.Delay(intervalMs);
}

void ClearIfNeeded()
{
    if (lastKey is null)
        return;
    lastKey = null;
    WriteJson("", "", "", false);
    if (File.Exists(coverPath))
        File.Delete(coverPath);
}

void WriteJson(string title, string artist, string album, bool hasCover)
{
    var payload = JsonSerializer.Serialize(new { title, artist, album, hasCover },
        new JsonSerializerOptions { WriteIndented = true });
    File.WriteAllText(jsonPath, payload);
}

async Task<bool> TryWriteCoverAsync(IRandomAccessStreamReference? thumbnailRef)
{
    if (thumbnailRef is null)
    {
        if (File.Exists(coverPath))
            File.Delete(coverPath);
        return false;
    }

    try
    {
        using var stream = await thumbnailRef.OpenReadAsync();
        using var reader = new DataReader(stream);
        await reader.LoadAsync((uint)stream.Size);
        var bytes = new byte[stream.Size];
        reader.ReadBytes(bytes);
        await File.WriteAllBytesAsync(coverPath, bytes);
        return true;
    }
    catch
    {
        if (File.Exists(coverPath))
            File.Delete(coverPath);
        return false;
    }
}
