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

# Per-song learning (analysis.py) --------------------------------------------------
# The kick's fundamental is measured from the onset spectrum of the detected kicks and the kick detector is
# re-tuned to a band around it, so bass lines and sub swells no longer count as "bass hits". The other bands
# get their hit threshold nudged until the hit rate is plausible. Everything is cached per track.
KICK_LEARN_BAND = (30, 200)      # where the kick fundamental is searched (Hz)
KICK_LEARN_HITS = 8              # kicks needed before the first estimate is used
KICK_LEARN_PHASE = 0.2           # a kick teaches the frequency only within this distance (in beats) of a beat
KICK_LEARN_BLOCKS = 3            # blocks after a kick whose onset spectrum is collected
KICK_LEARN_DECAY_EVERY = 48      # the collected spectrum is halved every this many kicks (follows song changes)
KICK_BAND_Q = 1.2                # tuned detector: Q of the band-pass around the kick (2 stages; higher = narrower)
KICK_BAND_FLOOR = 0.7            # absolute RMS floor is scaled by this once the band is tuned
KICK_PERIOD_GUARD = 0.35         # while the tempo is locked, two bass hits are at least this fraction of a beat apart
CROSS_MASK_S = 0.09              # a bass hit mutes the hits of CROSS_MASK_BANDS for this long (one kick = one hit)
CROSS_MASK_BANDS = ("sub", "mids")
HIT_RATE_WINDOW_S = 8.0          # hit rate of sub / mids / highs is judged over this window
HIT_RATE_MAX = {"sub": 3.0, "mids": 6.0, "highs": 10.0}    # hits per second: above -> threshold goes up
HIT_RATE_MIN = {"sub": 0.15, "mids": 0.25, "highs": 0.4}   # below (while the band is loud) -> threshold goes down
HIT_RATIO_LIMITS = (0.7, 1.8)    # the tuned threshold stays within base * these
LEARN_DEFAULT = True             # the "Learn per song" checkbox at start
SONG_RESET_SILENCE_S = 2.5       # this much silence = the next sound is a new song (learning starts over)
PROFILE_FILE = Path(__file__).resolve().parent / "song_profiles.json"    # learned values per track (git-ignored)
PROFILE_MAX = 400                # oldest tracks are dropped beyond this
PROFILE_AUTOSAVE_S = 30.0

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

# What the mapping grid cells do (engine.py)
FLASH_HOLD_MS = 100              # ON / OFF cells: how long the flash lasts before the device goes back
CYCLE_GAP_MS = 120               # minimum time between two steps of a colour / pattern cycle
SWING_POSITIONS = (10, 110)      # derby swing cell: the two motor positions it toggles between (1..127)
SWING_GAP_MS = 300               # ... and the minimum time between two swings (motor speed)
DERBY_CYCLE_COLOURS = ("red", "green", "blue", "white")        # derby colour cell cycles through these
LASER_CYCLE_COLOURS = ("green", "red")                         # laser colour cell cycles through these

# "Bouncy Bass" extra: on every bass hit the derby motor jumps between these two positions (1..127)
BOUNCE_POS_MIN = 10
BOUNCE_POS_MAX = 110
BOUNCE_TRAVEL_S = 0.45           # time the motor needs to get from MIN to MAX -- ADJUST THIS to your motor.
                                 # If the kicks come faster than this, only every 2nd (3rd, ...) kick bounces.

# =====================================================================================
# UI (music_app.py)    [values from the old, tuned config.py]
# =====================================================================================
UI_FPS = 30                      # the window redraws at most this often (the lights are NOT tied to it)
UI_REF_DT = 0.022                # the "per UI update" decay values below refer to this step; they are scaled by
                                 # the real frame time, so the look is the same at any UI_FPS
VIZ_MIN_SIZE = 540
DISC_SIZE = 200                  # diameter of the rings around the cover
COVER_SIZE = 140
RING_GAP = 48                    # gap between disc and the first spectrum bar (the waveform ring lives in here)
BAND_GAP_DEG = 4.0               # gap between two band arcs
BAND_LABEL_MARGIN = 34           # room outside the bars for the Hz tick labels
PIXEL_DOT_COUNT = 8             # rotating dots shown while there is no cover
PIXEL_DOT_RADIUS = 30
PIXEL_DOT_SIZE = 8
SPIN_STEP_DEG = 6
SPIN_INTERVAL_MS = 80
WAVE_DISPLAY_GAIN = 2.5          # waveform ring: how far a full-scale sample pushes the ring
WAVE_RING_OFFSET = 20            # waveform ring: px between the disc edge and the ring's centre line
WAVE_RING_AMP = 14               # waveform ring: px of deflection at full scale
WAVE_RING_LAYERS = 3             # the ring plus this many - 1 fading echoes (older frames, slightly further out)
WAVE_ECHO_EVERY = 3              # UI frames between two echoes
WAVE_ECHO_STEP = 6               # px each echo sits further out
AXIS_DB_STEPS = (0, -10, -20, -30, -40)                                  # dB rings (relative to the recent peak)
AXIS_HZ_TICKS = (20, 40, 60, 100, 250, 500, 1000, 2000, 4000, 10000, 20000)
AXIS_FADE_STEP = 0.16            # axes fade in / out by this much per UI frame when the mouse enters / leaves
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
