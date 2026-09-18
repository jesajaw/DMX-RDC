from src import ui
import tkinter as tk
from pathlib import Path

class parameters:
    UNIVERSE_SIZE = 513  # channel 0 unused, DMX starts at 1
    SEND_INTERVAL_S = 0.03  # ca. 33 Hz
    SAMPLE_RATE = 48000
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

    CHANNEL_CATEGORIES = {
        1: "mode",     # Show Select
        2: "speed",    # Speed
        3: "color",    # Derby Color
        4: "strobe",   # Derby Strobe
        5: "speed",    # Derby Motor
        6: "pattern",  # Pattern
        7: "mode",     # Laser Mode
        8: "strobe",   # Laser Strobe
        9: "speed",    # Laser Rotation
    }

    SOURCES = ("bass", "mid", "treble", "beat", "pitch")
    SOURCE_INFO = {
        "bass":   {"label": "Bass",   "hint": "empfohlen: Farbe/Pattern",   "recommended": {"color", "pattern"}},
        "mid":    {"label": "Mid",    "hint": "empfohlen: Farbe/Pattern",   "recommended": {"color", "pattern"}},
        "treble": {"label": "Treble", "hint": "empfohlen: Farbe/Pattern",   "recommended": {"color", "pattern"}},
        "beat":   {"label": "Beat",   "hint": "empfohlen: Strobe",          "recommended": {"strobe"}},
        "pitch":  {"label": "Pitch",  "hint": "empfohlen: Speed/Rotation",  "recommended": {"speed"}},
    }

    BAR_CANVAS_WIDTH = 260
    BAR_CANVAS_HEIGHT = 120
    WAVE_CANVAS_WIDTH = 200
    WAVE_CANVAS_HEIGHT = 120
    DISC_SIZE = 150

    KNOWN_PLAYER_PROCESSES = {
        "spotify.exe", "vlc.exe", "foobar2000.exe", "wmplayer.exe",
        "musicbee.exe", "itunes.exe", "winamp.exe", "aimp.exe",
    }
    _SCHEMES = {
        "dark_purple": dict(BG="#1e1e24", BG_LIGHT="#2a2a33", FG="#e0dff0", ACCENT="#9b59d9", ACCENT_DARK="#6c3fa0", STATUS_TEXT="#c9a6f5",),
        "dark_blue": dict(BG="#1e1e24", BG_LIGHT="#2a2a33", FG="#e0dff0", ACCENT="#4a90d9", ACCENT_DARK="#2f5f9e", STATUS_TEXT="#a6c9f5",),
        "black_white": dict(BG="#000000", BG_LIGHT="#1a1a1a", FG="#ffffff", ACCENT="#ffffff", ACCENT_DARK="#808080", STATUS_TEXT="#d9d9d9",),
    }
    COLOR_SCHEME = "dark_purple"

    _active = _SCHEMES[COLOR_SCHEME]
    COLOR_BG = _active["BG"]
    COLOR_BG_LIGHT = _active["BG_LIGHT"]
    COLOR_FG = _active["FG"]
    COLOR = _active["ACCENT"]
    COLOR_DARK = _active["ACCENT_DARK"]
    COLOR_STATUS_TEXT = _active["STATUS_TEXT"]

    CELL_WIDTH = 260
    CELL_HEIGHT = 90
    STATUS_LABEL_CHARS = 32

    CHANNEL_COUNT = 9

    PRESETS_DIR = Path(__file__).parent / "presets" # used for json channel settings

def main() -> None:
    ui.enable_dpi_awareness()
    root = tk.Tk()
    root.protocol("WM_DELETE_WINDOW", ui.DMXUI(root).on_close)
    root.mainloop()


if __name__ == "__main__":
    main()