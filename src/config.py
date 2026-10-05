from pathlib import Path

# --- DMX / Controller ---
UNIVERSE_SIZE = 513  # channel 0 unused, DMX should start at 1
SEND_INTERVAL_S = 0.03

# --- Music Mode / audio analysis ---
SAMPLE_RATE = 48000 # placeholder, gets replaced by the real device at start()
BLOCK_SIZE = 1024
FFT_SIZE = 2048 # sliding analysis window (~23 Hz per bin at 48 kHz)
N_BARS = 40
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

# --- Light engine (src/lightengine.py): rhythm + song-structure interpretation ---
# Extra analysis features the AudioAnalyzer hands over for it
FLUX_LOW_BAND = (30, 200)         # onset detection for tempo tracking: kick / bass region
FLUX_HIGH_BAND = (5000, 16000)    # hi-hat region (weak extra evidence for the tempo)
FLUX_LOG_COMPRESSION = 100.0      # log(1 + C * magnitude) before taking the spectral flux
SILENCE_DB = -65.0                # total level below this counts as "no music"
SILENCE_HOLD_S = 1.5              # ... for at least this long

# Tempo tracking: onset strength -> autocorrelation -> BPM, then a beat clock that is phase-corrected against the onsets
ONSET_NORM_S = 4.0                # onset strength is divided by its own running mean (time constant)
ONSET_HAT_WEIGHT = 0.35           # weight of the hi-hat onsets relative to the kick onsets
TEMPO_BPM_MIN = 80.0
TEMPO_BPM_MAX = 190.0
TEMPO_PRIOR_BPM = 135.0           # soft preference that resolves half/double-tempo ambiguity (techno/house range)
TEMPO_PRIOR_WIDTH_OCT = 0.45      # width of that preference in octaves
TEMPO_WINDOW_S = 8.0              # analysis window
TEMPO_UPDATE_S = 0.5              # how often the tempo / phase is re-estimated
TEMPO_LOCK_ON = 0.30              # autocorrelation score needed to lock on
TEMPO_LOCK_OFF = 0.12             # below this the lock starts to time out
TEMPO_BPM_SMOOTH = 0.25           # how fast the BPM follows small drifts
LOCK_HOLD_S = 10.0                # lock survives this long without a clear beat
LOCK_HOLD_BREAK_S = 45.0          # ... and this long during a break / build-up (the grid keeps running)
FOLD_BINS = 24                    # phase histogram resolution (per beat)
FOLD_MIN_PEAKINESS = 1.6          # phase histogram must be this peaky before the clock is corrected by it
PHASE_GAIN = 0.5                  # fraction of the measured phase error corrected per update
ODF_LATENCY_FRAMES = 1.0          # onset peaks show up about one block after the real transient
FALLBACK_BPM = 120.0              # used for speed coupling until / unless a tempo is known
SYNC_OFFSET_MS_DEFAULT = 40       # lights lag behind the audio (processing + DMX + fixture); positive = fire earlier

# Song structure (break / build-up / drop), all relative so quiet and loud tracks behave the same
KICK_GAP_MIN_S = 1.6              # no kick for longer than max(this, 3 beats) = kick is gone
KICK_READY_HITS = 6               # kicks needed (within 12 s) before "kick gone" can mean "break"
KICK_LEVEL_DB = 9.0                # a kick onset must reach within this many dB of the recent bass peak
KICK_REF_DECAY_DB_S = 0.4         # how fast that bass peak reference sinks
BREAK_MIN_S = 3.0                 # a kick dropout shorter than this is not a break
DROP_BASS_JUMP_DB = 4.0           # fast bass level vs. its slow average -> kick/bass slams back in
DROP_FROM_GROOVE_JUMP_DB = 8.0    # same, but out of a normal groove (needs a much bigger jump)
DROP_COOLDOWN_S = 10.0          # after a drop: no new drop / build-up for this long
STARTUP_GUARD_S = 8.0              # no drop / build-up detection this long after the music starts
DROP_HOLD_S = 2.0                 # minimum length of the DROP state
BUILD_TREND_DB = 2.0              # overall level, fast vs. slow average
BUILD_TREBLE_TREND_DB = 1.5       # treble level, fast vs. slow average (risers, snare rolls)
BUILD_MIN_RISE_S = 1.5            # the rise must persist this long
BUILD_RAMP_BEATS = 32             # build-up progress reaches 100 % after this many beats (8 bars)
BUILD_MAX_S = 45.0

# Output side
K_RANGE = (0.0, 2.0)              # allowed range of the speed factors K (0 = stopped)
BREAK_SPEED_SCALE = 0.4           # motors / laser rotation slow down to this fraction during a break
SPEED_FULLSCALE_BPM = 256.0       # speed = K * BPM / this  (K = 1 @ 128 BPM = 50 % of the fixture's range)

BAND_RANGES = {
    "bass": (20, 250),
    "mid": (250, 4000),
    "treble": (4000, 16000),
}

# Visualizer (spectrum ring/bars around the cover + waveform): the canvases scale with the window,
# these are only the sizes they are requested with
VIZ_MIN_SIZE = 460
WAVE_CANVAS_HEIGHT = 84
RING_GAP = 14                 # distance between the disc edge and the inner end of the spectrum ring
SPECTRUM_PEAK_DECAY = 0.012   # how fast the peak caps above the bars fall (per frame, 0..1 scale)
WAVE_DISPLAY_GAIN = 2.5       # visual boost of the waveform line only (quiet loopback audio is tiny)
DISC_SIZE = 200  # the disc (cover + rings) keeps this size, the spectrum ring grows around it
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
_BASE_SCHEMES = {
    "dark_purple": dict(BG="#1e1e24", BG_LIGHT="#2a2a33", FG="#e0dff0", ACCENT="#9b59d9", ACCENT_DARK="#6c3fa0", STATUS_TEXT="#c9a6f5"),
    "dark_blue": dict(BG="#1e1e24", BG_LIGHT="#2a2a33", FG="#e0dff0", ACCENT="#4a90d9", ACCENT_DARK="#2f5f9e", STATUS_TEXT="#a6c9f5"),
    "black_white": dict(BG="#000000", BG_LIGHT="#1a1a1a", FG="#ffffff", ACCENT="#ffffff", ACCENT_DARK="#808080", STATUS_TEXT="#d9d9d9"),
}


def _mix(a: str, b: str, t: float) -> str:
    # blends two #rrggbb colours (t = 0 -> a, t = 1 -> b)
    a, b = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join("%02x" % round(int(a[i:i + 2], 16) + (int(b[i:i + 2], 16) - int(a[i:i + 2], 16)) * t)
                         for i in (0, 2, 4))


# the new layout also uses: ACCENT2 (second spectrum colour, titles, hover), MUTED (secondary text), LINE (borders)
_SCHEMES = {
    name: dict(s, ACCENT2=s["STATUS_TEXT"], MUTED=_mix(s["BG_LIGHT"], s["FG"], 0.55), LINE=s["ACCENT_DARK"])
    for name, s in _BASE_SCHEMES.items()
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
