// NowPlayingBridge
// =================
// Kleiner Hintergrund-Prozess, der die aktuell unter Windows spielende Media-
// Session (SMTC -- dieselbe Quelle, die auch die Windows-Lautstaerke-Vorschau
// zeigt) abfragt und Titel/Interpret/Cover periodisch in Dateien schreibt, die
// die Python-App (musicmode.py) ausliest. Kapselt die WinRT-Zugriffe komplett
// in .NET (first-party WinRT-Unterstuetzung ueber die "-windows"-TargetFramework),
// damit Python keine eigene WinRT-Bindung (winsdk/winrt, archiviert) braucht.
//
// Aufruf: NowPlayingBridge.exe <ausgabe-ordner> [<intervall-ms>]
// Schreibt in <ausgabe-ordner>:
//   nowplaying.json        {"title": "...", "artist": "...", "hasCover": true/false}
//   nowplaying_cover.img   Rohe Thumbnail-Bytes (Format je nach Quelle, meist PNG/JPEG,
//                          PIL auf Python-Seite erkennt das automatisch)
//
// Build (einmalig, braucht das .NET 8 SDK -- https://dotnet.microsoft.com/download):
//   cd src
//   dotnet publish -c Release -r win-x64 --self-contained false -o out
//
// Laeuft in einer Endlosschleife, bis der Prozess beendet wird (Python beendet
// ihn beim Schliessen von Music Mode ueber subprocess.terminate()).

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
            var key = title + "|" + artist;

            if (key != lastKey)
            {
                lastKey = key;
                var hasCover = await TryWriteCoverAsync(props.Thumbnail);
                WriteJson(title, artist, hasCover);
            }
        }
    }
    catch
    {
        // Keine laufende Session, kein Media-Player aktiv, o.ae. -- einfach naechste Runde versuchen
        ClearIfNeeded();
    }

    await Task.Delay(intervalMs);
}

void ClearIfNeeded()
{
    if (lastKey is null)
        return;
    lastKey = null;
    WriteJson("", "", false);
    if (File.Exists(coverPath))
        File.Delete(coverPath);
}

void WriteJson(string title, string artist, bool hasCover)
{
    var payload = JsonSerializer.Serialize(new { title, artist, hasCover });
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
