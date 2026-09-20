"""
Zentrale Konfiguration
=======================

Alle Konstanten des Projekts an einem Ort. controller.py, musicmode.py und
ui.py importieren ausschliesslich von hier (from .config import parameters)
statt eigene Kopien zu pflegen -- eine einzige Quelle der Wahrheit.
"""

from pathlib import Path


class parameters:
    # --- DMX / Controller ---
    UNIVERSE_SIZE = 513  # channel 0 unused, DMX starts at 1
    SEND_INTERVAL_S = 0.03  # ca. 33 Hz

    # --- Music Mode / Audioanalyse ---
    SAMPLE_RATE = 48000                  # Platzhalter, wird beim Start durchs echte Geraet ersetzt
    BLOCK_SIZE = 1024
    N_BARS = 24
    WAVE_POINTS = 160
    BAR_FREQ_RANGE = (20, 16000)         # log-verteilte Grenzen fuers Spektrum
    ENERGY_HISTORY_LEN = 43              # ~1s bei ~21ms/Block, fuer Beat-Erkennung
    BEAT_THRESHOLD_RATIO = 1.3           # Energie muss X-fach ueber dem Mittel liegen
    BEAT_MIN_ENERGY = 0.02               # Mindestenergie, damit Stille keinen Beat ausloest
    BEAT_DECAY = 0.75                    # Abklingfaktor des Beat-Pulses pro Block
    PITCH_REFERENCE_HZ = 4000.0          # Normalisierungsreferenz fuer den Spektralschwerpunkt

    BAND_RANGES = {
        "bass": (20, 250),
        "mid": (250, 4000),
        "treble": (4000, 16000),
    }

    SOURCES = ("bass", "mid", "treble", "beat", "pitch")
    SOURCE_LABELS = {
        "bass": "Bass", "mid": "Mid", "treble": "Treble", "beat": "Beat", "pitch": "Pitch",
    }

    # Spectrum/Waveform bewusst gleich gross, damit sie symmetrisch nebeneinander sitzen
    BAR_CANVAS_WIDTH = 280
    BAR_CANVAS_HEIGHT = 150
    WAVE_CANVAS_WIDTH = 280
    WAVE_CANVAS_HEIGHT = 150
    DISC_SIZE = 150
    COVER_SIZE = 104  # Durchmesser des Covers auf der Scheibe (Kreis-Maske)

    # Pixel-Art-"Label" auf der Scheibe -- Fallback, solange kein Cover vorliegt
    # (kein Titel gefunden, oder NowPlayingBridge.exe noch nicht gebaut)
    PIXEL_DOT_COUNT = 8
    PIXEL_DOT_RADIUS = 22
    PIXEL_DOT_SIZE = 6
    SPIN_STEP_DEG = 6
    SPIN_INTERVAL_MS = 80

    # NowPlayingBridge: kleiner C#/.NET-Hintergrundprozess, der Titel/Interpret/Cover
    # ueber first-party WinRT liefert (siehe src/Program.cs). Muss einmalig gebaut
    # werden (dotnet publish in src/ -> src/out/NowPlayingBridge.exe); ohne die .exe
    # faellt NowPlayingReader automatisch auf reine Fenstertitel-Heuristik zurueck.
    NOWPLAYING_BRIDGE_EXE = Path(__file__).resolve().parent / "out" / "NowPlayingBridge.exe"
    NOWPLAYING_CACHE_DIR = Path(__file__).resolve().parent.parent / "nowplaying_cache"

    # Bekannte Media-Player-Prozesse, deren Fenstertitel nach "Interpret - Titel"
    # durchsucht wird (Fallback ohne Bridge). Bei Bedarf einfach ergaenzen.
    KNOWN_PLAYER_PROCESSES = {
        "spotify.exe", "vlc.exe", "foobar2000.exe", "wmplayer.exe",
        "musicbee.exe", "itunes.exe", "winamp.exe", "aimp.exe",
    }

    # --- Theme ---
    _SCHEMES = {
        "dark_purple": dict(BG="#1e1e24", BG_LIGHT="#2a2a33", FG="#e0dff0", ACCENT="#9b59d9", ACCENT_DARK="#6c3fa0", STATUS_TEXT="#c9a6f5"),
        "dark_blue": dict(BG="#1e1e24", BG_LIGHT="#2a2a33", FG="#e0dff0", ACCENT="#4a90d9", ACCENT_DARK="#2f5f9e", STATUS_TEXT="#a6c9f5"),
        "black_white": dict(BG="#000000", BG_LIGHT="#1a1a1a", FG="#ffffff", ACCENT="#ffffff", ACCENT_DARK="#808080", STATUS_TEXT="#d9d9d9"),
    }
    COLOR_SCHEME = "dark_purple"

    ACTIVE_SCHEME = _SCHEMES[COLOR_SCHEME]
    COLOR_BG = ACTIVE_SCHEME["BG"]
    COLOR_BG_LIGHT = ACTIVE_SCHEME["BG_LIGHT"]
    COLOR_FG = ACTIVE_SCHEME["FG"]
    COLOR = ACTIVE_SCHEME["ACCENT"]
    COLOR_DARK = ACTIVE_SCHEME["ACCENT_DARK"]
    COLOR_STATUS_TEXT = ACTIVE_SCHEME["STATUS_TEXT"]

    # --- UI-Layout ---
    CELL_WIDTH = 260
    CELL_HEIGHT = 90
    STATUS_LABEL_CHARS = 32
    CHANNEL_COUNT = 9

    CHANNEL_NAMES = [
        "1: Show Select",
        "2: Speed",
        "3: Derby Color",
        "4: Derby Strobe",
        "5: Derby Motor",
        "6: Pattern",
        "7: Laser Mode",
        "8: Laser Strobe",
        "9: Laser Rotation",
    ]

    # main.py liegt im Projekt-Root, config.py in <root>/src -- daher zwei Ebenen hoch
    PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"
