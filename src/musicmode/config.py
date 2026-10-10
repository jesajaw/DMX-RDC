"""
Config
======

Every tunable parameter of Music Mode in one place.

Names that already existed in the old config.py keep their name. The values of the
sections "Tempo" and "UI" were taken over from your tuned old config.py (src/config.py,
see git history); "Now playing" points at the real project layout.

    audio_source.py   ->  "Audio capture"
    analysis.py       ->  "Analysis", "Hit detection", "Tempo"
    engine.py         ->  "Engine"
    music_app.py      ->  "UI"
    nowplaying.py     ->  "Now playing"

(What the fixture's channels / colours / patterns are lives in src/fixture.py, not here.)
"""

from pathlib import Path

# =====================================================================================
# Audio capture (audio_source.py)
# =====================================================================================
SAMPLE_RATE = 48000              # placeholder until a device reports its real rate
BLOCK_SIZE = 1024                # frames per capture block (~21 ms at 48 kHz)
REF_BLOCK_SIZE = 1024            # block length the per-block smoothing constants refer to

# Several loopback / monitor devices are listened to at once; the one that carries signal is used.
SOURCE_SWITCH_RMS = 0.002        # a device counts as "playing" above this block RMS
SOURCE_SWITCH_RATIO = 2.0        # another device takes over when it is this much louder ...
SOURCE_HOLD_S = 0.6              # ... or when the current one has been silent this long
WATCHDOG_S = 0.3                 # no blocks for this long -> silence is fed in, lights go dark
DEVICE_RESCAN_S = 5.0            # minimum time between two device re-scans
RESCAN_AFTER_SILENCE_S = 2.0     # re-scan only after this much silence (never while music plays)

# =====================================================================================
# Analysis (analysis.py)
# =====================================================================================
FFT_SIZE = 4096                  # ~11.7 Hz per bin at 48 kHz; needed to resolve Sub 20-60 Hz
GAIN_DEFAULT = 1.0               # "Sensitivity" slider: >1 lifts quiet parts, <1 suppresses them
LEVEL_SMOOTHING = 0.6            # fall-off of the band levels (0..~0.95); rises are instant

# Frequency bands: (key, label, low Hz, high Hz). Keys are what rules refer to.
BANDS = (
    ("sub",   "Sub",   20,    60),
    ("bass",  "Bass",  60,    250),
    ("mids",  "Mids",  250,   4000),
    ("highs", "Highs", 4000,  20000),
)
BAND_BARS = {"sub": 4, "bass": 8, "mids": 14, "highs": 10}   # fine-spectrum bars per band (display only)

# Level normalisation: every band is scaled to its own recent peak
AGC_DECAY_DB_PER_S = 2.0         # how fast the remembered peak sinks
AGC_MIN_REF_BAND_DB = -50.0      # the peak never sinks below this (quiet noise stays quiet)
DB_RANGE_BAND = 40.0             # dB below the peak that still show as >0
AGC_MIN_REF_BARS_DB = -50.0
DB_RANGE_BARS = 50.0
SPECTRUM_TILT_DB_PER_OCT = 3.0   # display tilt so treble bars are not always tiny

SILENCE_DB = -70.0               # total level below this counts as silence ...
SILENCE_HOLD_S = 1.5             # ... after this long
WAVE_POINTS = 256                # points of the oscilloscope line

# =====================================================================================
# Hit detection (analysis.py)
# =====================================================================================
# Bass hit: fast time-domain detector (KickOnset), no FFT window lag
KICK_LOWPASS_S = 0.004           # box low-pass length (~110 Hz corner)
KICK_SLICE_S = 0.005             # RMS slice length
KICK_HISTORY_S = 1.0             # running average the slices are compared with
KICK_COOLDOWN_S = 0.12
KICK_RATIO = 1.8                 # slice must exceed the running average by this factor ...
KICK_RISE = 1.15                 # ... and the previous slice by this factor
BEAT_MIN_RMS = 0.004             # absolute floor (low-passed RMS)

# Sub / Mids / Highs hits: spectral flux in the band over its running mean
FLUX_LOG_COMPRESSION = 50.0      # onset strength uses log1p(C * magnitude)
HIT_NORM_S = 1.0                 # running-mean time of the band flux
HIT_RATIO = {"sub": 2.2, "mids": 2.0, "highs": 2.2}
HIT_COOLDOWN_S = {"sub": 0.15, "mids": 0.10, "highs": 0.06}
HIT_MIN_LEVEL = 0.25             # a band must be at least this loud (0..1) for a hit to count

# Kick bookkeeping (decides when the tempo clock coasts through a break)
KICK_READY_HITS = 6              # this many bass hits within 12 s -> "there is a kick"
KICK_GAP_MIN_S = 1.5             # no bass hit for max(this, 3 beats) -> kick is gone

# =====================================================================================
# Tempo (analysis.py, TempoTracker)    [values from the old, tuned config.py]
# =====================================================================================
FLUX_LOW_BAND = (30, 200)        # onset strength for the tempo: kick region ...
FLUX_HIGH_BAND = (5000, 16000)   # ... plus a little hi-hat
ONSET_HAT_WEIGHT = 0.35
ONSET_NORM_S = 4.0
TEMPO_WINDOW_S = 8.0
TEMPO_UPDATE_S = 0.5
TEMPO_BPM_MIN = 80.0
TEMPO_BPM_MAX = 190.0
TEMPO_PRIOR_BPM = 135.0          # soft prior that settles half / double tempo
TEMPO_PRIOR_WIDTH_OCT = 0.45
TEMPO_LOCK_ON = 0.30             # autocorrelation score needed to lock
TEMPO_LOCK_OFF = 0.12            # below this for LOCK_HOLD_S the lock is dropped
TEMPO_BPM_SMOOTH = 0.25
LOCK_HOLD_S = 10.0
PHASE_GAIN = 0.5                 # fraction of the phase error corrected per update
FOLD_BINS = 24
FOLD_MIN_PEAKINESS = 1.6         # onsets must pile up on one spot of the beat
ODF_LATENCY_FRAMES = 2.0           # onset-function latency in blocks
SYNC_OFFSET_MS_DEFAULT = 40     # lights lag behind the audio (processing + DMX + fixture); positive = fire earlier
FALLBACK_BPM = 120.0             # used before any tempo is known

# =====================================================================================
# Engine (engine.py)
# =====================================================================================
LEVEL_HYSTERESIS = 0.08          # "rises above / falls below" re-arm distance (0..1)

# =====================================================================================
# UI (music_app.py)    [values from the old, tuned config.py]
# =====================================================================================
UI_FPS = 30                      # the window redraws at most this often (the lights are NOT tied to it)
UI_REF_DT = 0.022                # the "per UI update" decay values below refer to this step; they are scaled by
                                 # the real frame time, so the look is the same at any UI_FPS
VIZ_MIN_SIZE = 460
DISC_SIZE = 200                  # diameter of the rings around the cover
COVER_SIZE = 140
RING_GAP = 14                    # gap between disc and the first spectrum bar
BAND_GAP_DEG = 4.0               # gap between two band arcs
BAND_LABEL_MARGIN = 36           # room outside the bars for the band labels
PIXEL_DOT_COUNT = 8             # rotating dots shown while there is no cover
PIXEL_DOT_RADIUS = 30
PIXEL_DOT_SIZE = 8
SPIN_STEP_DEG = 6
SPIN_INTERVAL_MS = 80
WAVE_CANVAS_HEIGHT = 84
WAVE_DISPLAY_GAIN = 2.5
SPECTRUM_PEAK_DECAY = 0.012      # peak caps fall by this per UI_REF_DT
HIT_GLOW_DECAY = 0.80            # band arc flash fades by this factor per UI_REF_DT

# =====================================================================================
# Now playing (nowplaying.py)
# =====================================================================================
# Windows: NowPlayingBridge.ps1 (PowerShell), Linux: MPRIS over D-Bus. Anywhere else (or if the source does
# not deliver) Music Mode just runs without title / artist / cover.
NOWPLAYING_BRIDGE_SCRIPT = Path(__file__).resolve().parent / "NowPlayingBridge.ps1"
NOWPLAYING_CACHE_DIR = Path(__file__).resolve().parent / "nowplaying_cache"    # bridge output + log (git-ignored)
NOWPLAYING_DEBUG = False
