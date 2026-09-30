from pathlib import Path

# --- DMX / Controller ---
UNIVERSE_SIZE = 513  # channel 0 unused, DMX should start at 1
SEND_INTERVAL_S = 0.03

# --- Music Mode / audio analysis ---
SAMPLE_RATE = 48000 # placeholder, gets replaced by the real device at start()
BLOCK_SIZE = 1024
FFT_SIZE = 2048 # sliding analysis window (~23 Hz per bin at 48 kHz)
N_BARS = 32
WAVE_POINTS = 160
BAR_FREQ_RANGE = (30, 16000) # log-spaced bounds for the spectrum

# Level normalisation (dB based + automatic gain, so quiet and loud tracks both fill the meters)
DB_RANGE_BAND = 20.0 # dynamic range mapped to 0..1 for bass/mid/treble
DB_RANGE_BARS = 45.0 # same for the spectrum bars
AGC_DECAY_DB_PER_S = 3.0 # how fast the "loudest recent level" reference sinks
AGC_MIN_REF_BAND_DB = -40.0 # reference never drops below this -> near-silence stays at 0
AGC_MIN_REF_BARS_DB = -50.0
SPECTRUM_TILT_DB_PER_OCT = 1.5 # slight high-frequency lift, music rolls off towards the treble

# Beat: onset of the kick-drum band vs. its own ~1s average
BEAT_BAND = (30, 150)
ENERGY_HISTORY_LEN = 43 # ~1s at ~21ms/block
BEAT_THRESHOLD_RATIO = 1.3 # kick energy must be this many times above the rolling average
BEAT_MIN_RMS = 0.003 # absolute minimum (linear RMS, full scale = 1.0) so silence never triggers
BEAT_COOLDOWN_BLOCKS = 7 # ~150 ms dead time after a beat
BEAT_DECAY = 0.75 # decay factor of the beat pulse per block

# Pitch: log-frequency centroid of the spectrum, stretched to the range it has recently used
PITCH_ADAPT_RATE = 0.0004 # how fast the remembered min/max drift towards the current value
PITCH_SPAN_MIN = 0.12 # minimum span so tiny variations aren't blown up to 0..1
PITCH_SMOOTHING_MIN = 0.85 # pitch is always smoothed at least this much (the centroid is jittery)

BAND_RANGES = {
    "bass": (20, 250),
    "mid": (250, 4000),
    "treble": (4000, 16000),
}

SOURCES = ("bass", "mid", "treble", "beat", "pitch")
SOURCE_LABELS = {"bass": "Bass", "mid": "Mid", "treble": "Treble", "beat": "Beat", "pitch": "Pitch"}

# Spectrum and waveform are stacked and share the same width
BAR_CANVAS_WIDTH = 560
BAR_CANVAS_HEIGHT = 140
WAVE_CANVAS_WIDTH = 560
WAVE_CANVAS_HEIGHT = 80
SPECTRUM_PEAK_DECAY = 0.012   # how fast the peak caps above the bars fall (per frame, 0..1 scale)
WAVE_DISPLAY_GAIN = 2.5       # visual boost of the waveform line only (quiet loopback audio is tiny)
DISC_SIZE = 200
COVER_SIZE = 140  # diameter of the cover art on the disc (circular mask)

# Pixel-art "label" on the disc -- fallback shown while no cover art is available
# (no track found, or the platform-specific now-playing source has no cover)
PIXEL_DOT_COUNT = 8
PIXEL_DOT_RADIUS = 30
PIXEL_DOT_SIZE = 8
SPIN_STEP_DEG = 6
SPIN_INTERVAL_MS = 80

# NowPlayingBridge: two possible sources for title/artist/album/cover on Windows, tried in priority order by NowPlayingReader:
#  1. NOWPLAYING_BRIDGE_EXE (src/Program.cs, .NET SDK build): compiled C#, real compiler-level WinRT/await support -- can also read cover art reliably. Optional, one-time build step (see README).
#  2. NOWPLAYING_BRIDGE_SCRIPT (src/NowPlayingBridge.ps1): plain
#     PowerShell, ships as-is, no install/build step -- title, artist,
#     album, play state and cover art.
#  3. If neither is usable, NowPlayingReader falls back further to a
#     plain window-title heuristic on Windows (title/artist only). Not
#     used on Linux (see NowPlayingReader).
NOWPLAYING_BRIDGE_EXE = Path(__file__).resolve().parent / "out" / "NowPlayingBridge.exe"
NOWPLAYING_BRIDGE_SCRIPT = Path(__file__).resolve().parent / "NowPlayingBridge.ps1"
NOWPLAYING_CACHE_DIR = Path(__file__).resolve().parent.parent / "nowplaying_cache"
# True -> the PowerShell bridge runs with -RunDebug and writes nowplaying_cache/cover_debug.log
NOWPLAYING_DEBUG = False

# Known media player processes whose window title is searched for "Artist -
# Title" (Windows fallback, used only if neither bridge above is usable).
# Extend as needed.
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
