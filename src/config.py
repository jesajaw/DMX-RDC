"""
Central Configuration
=======================

All project constants live in one place. controller.py, musicmode.py and
ui.py only ever import from here (from .config import parameters) instead of
keeping their own copies -- a single source of truth.
"""

from pathlib import Path


class parameters:
    # --- DMX / Controller ---
    UNIVERSE_SIZE = 513  # channel 0 unused, DMX starts at 1
    SEND_INTERVAL_S = 0.03  # ~33 Hz

    # --- Music Mode / audio analysis ---
    SAMPLE_RATE = 48000                  # placeholder, replaced by the real device at start()
    BLOCK_SIZE = 1024
    N_BARS = 24
    WAVE_POINTS = 160
    BAR_FREQ_RANGE = (20, 16000)         # log-spaced bounds for the spectrum
    ENERGY_HISTORY_LEN = 43              # ~1s at ~21ms/block, used for beat detection
    BEAT_THRESHOLD_RATIO = 1.3           # energy must be this many times above the rolling average
    BEAT_MIN_ENERGY = 0.02               # minimum energy so silence never triggers a beat
    BEAT_DECAY = 0.75                    # decay factor of the beat pulse per block
    PITCH_REFERENCE_HZ = 4000.0          # normalization reference for the spectral centroid

    BAND_RANGES = {
        "bass": (20, 250),
        "mid": (250, 4000),
        "treble": (4000, 16000),
    }

    SOURCES = ("bass", "mid", "treble", "beat", "pitch")
    SOURCE_LABELS = {
        "bass": "Bass", "mid": "Mid", "treble": "Treble", "beat": "Beat", "pitch": "Pitch",
    }

    # Spectrum/waveform are deliberately the same size so they sit symmetrically side by side
    BAR_CANVAS_WIDTH = 280
    BAR_CANVAS_HEIGHT = 150
    WAVE_CANVAS_WIDTH = 280
    WAVE_CANVAS_HEIGHT = 150
    DISC_SIZE = 150
    COVER_SIZE = 104  # diameter of the cover art on the disc (circular mask)

    # Pixel-art "label" on the disc -- fallback shown while no cover art is available
    # (no track found, or the platform-specific now-playing source has no cover)
    PIXEL_DOT_COUNT = 8
    PIXEL_DOT_RADIUS = 22
    PIXEL_DOT_SIZE = 6
    SPIN_STEP_DEG = 6
    SPIN_INTERVAL_MS = 80

    # NowPlayingBridge: small C#/.NET background process that provides title/
    # artist/cover on Windows via first-party WinRT (see src/Program.cs). Must
    # be built once (dotnet publish in src/ -> src/out/NowPlayingBridge.exe);
    # without the .exe, NowPlayingReader falls back to a plain window-title
    # heuristic on Windows. Not used on Linux (see NowPlayingReader).
    NOWPLAYING_BRIDGE_EXE = Path(__file__).resolve().parent / "out" / "NowPlayingBridge.exe"
    NOWPLAYING_CACHE_DIR = Path(__file__).resolve().parent.parent / "nowplaying_cache"

    # Known media player processes whose window title is searched for "Artist -
    # Title" (Windows fallback, used only if NowPlayingBridge.exe hasn't been
    # built). Extend as needed.
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

    # --- UI layout ---
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

    # main.py lives at the project root, config.py in <root>/src -- hence two levels up
    PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"
