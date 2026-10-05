"""
Light Engine
============

Turns the raw audio features from AudioAnalyzer (musicmode.py) into DMX values
for the Razor Derby -- the "musical brain" of Music Mode.

The old version had a behaviour per channel, programs, palettes, pattern pools ...
This one is deliberately small. A *Look* is just a handful of simple rules:

    WHEN (trigger)              DO (actions, any combination)
    --------------------------  ----------------------------------------------
    every bass hit              LED blink | Laser blink | Strobe | Blackout | Colour change
    every N beats               (same)
    during a build-up           (same, held for the whole build-up; strobe ramps up)
    on the drop                 (same, held for a few beats)

plus: two LED colours (A -> B) and two laser colours, a strobe rate, the blink
length, an optional base light, and the motor / rotation speeds (BPM x K).

Channel 1 (show select) is NEVER driven here: it stays at 0 so the fixture
is under per-channel control. The window writes that 0 once and then leaves it alone.

Pieces
------
TempoTracker    BPM + phase-locked beat position from the onset strength   (unchanged)
SectionTracker  silence / groove / break / build / drop from level trends  (unchanged)
Look            the rules + parameters described above
LightEngine     runs the two trackers and renders the current Look to DMX

LightEngine.process(frame) is fed one AudioFrame per audio block and returns
{channel: value} for channels 2..9. No GUI and no audio dependencies in here.
"""

import math
from collections import deque
from dataclasses import dataclass, field, replace

import numpy as np

from . import config

# --------- Song sections
SILENCE, GROOVE, BREAK, BUILD, DROP = "silence", "groove", "break", "build", "drop"
SECTION_LABELS = {SILENCE: "Silence", GROOVE: "Groove", BREAK: "Break", BUILD: "Build-up", DROP: "DROP"}

# --------- Colour tables (a value inside the manual's range of each colour)
DERBY = dict(red=13, green=28, blue=43, white=58, red_green=73, red_blue=88, red_white=103,
             green_blue=118, green_white=133, blue_white=148, rgb=163, rgw=178, gbw=193, rgbw=208,
             auto4=223, auto7=240)
LASER = dict(red=30, green=70, red_green=110)

# key -> (label, preview colour) -- the colours offered in the UI
LED_COLOURS = {
    "red": ("Red", "#ff3b4e"), "green": ("Green", "#2dff7a"), "blue": ("Blue", "#3d7bff"),
    "white": ("White", "#f4f6ff"), "red_green": ("Yellow (R+G)", "#ffd23d"),
    "red_blue": ("Magenta (R+B)", "#d83dff"), "green_blue": ("Cyan (G+B)", "#33e6ff"),
    "red_white": ("Pink (R+W)", "#ff9fb2"), "blue_white": ("Ice (B+W)", "#a9c8ff"),
    "green_white": ("Mint (G+W)", "#a6ffd0"),
}
LASER_COLOURS = {
    "red": ("Red", "#ff3b4e"), "green": ("Green", "#2dff7a"), "red_green": ("Red + Green", "#ffd23d"),
}

# --------- Rules: triggers and actions (key, label)
TRIGGERS = [
    ("bass", "Every bass hit"),
    ("beat", "Every N beats"),
    ("build", "During build-up"),
    ("drop", "On the drop"),
]
ACTIONS = [
    ("led", "LED"),
    ("laser", "Laser"),
    ("strobe", "Strobe"),
    ("blackout", "Blackout"),
    ("colour", "Colour change"),
]
BEAT_OPTIONS = {"1 beat": 1, "2 beats": 2, "1 bar": 4, "2 bars": 8, "4 bars": 16}

# Strobe channels: ch4 (derby strobe) and ch8 (laser strobe); the rate setting 0..1 is mapped into their ranges
STROBE_CH4 = (6, 255)
STROBE_CH8 = (10, 254)


def _empty_rules() -> dict:
    return {key: set() for key, _ in TRIGGERS}


@dataclass
class Look:
    """Everything the engine needs to know. The UI edits these fields directly."""
    name: str
    description: str = ""
    rules: dict = field(default_factory=_empty_rules)
    every_beats: int = 4             # for the "every N beats" trigger
    led_a: str = "blue"              # colour change toggles A <-> B
    led_b: str = "white"
    laser_a: str = "green"
    laser_b: str = "red"
    base_led: bool = False           # LED stays on (current colour) even when nothing triggers it
    base_laser: bool = False
    strobe_rate: float = 0.55        # 0..1 -> fixture strobe speed
    blink_ms: int = 120              # length of one blink
    k_show: float = 1.0              # channel 2 (show/pattern speed) = K x BPM
    k_derby: float = 0.8             # derby motor, 0 = stopped
    k_laser: float = 0.6             # laser rotation, 0 = stopped
    laser_dir: str = "alt"           # cw | ccw | alt
    pattern: int = 0                 # 0 = cycle through the patterns, 1..18 = fixed pattern

    def copy(self, **changes) -> "Look":
        return replace(self, rules={k: set(v) for k, v in self.rules.items()}, **changes)


def _rules(**kw) -> dict:
    rules = _empty_rules()
    for key, actions in kw.items():
        rules[key] = set(actions.split()) if actions else set()
    return rules


# One-click starting points, shown as chips in Music Mode. Everything can be changed afterwards.
QUICK_LOOKS = [
    Look("Kick Flash", "LED flashes on every bass hit, colour flips every bar, strobe on the drop.",
         rules=_rules(bass="led", beat="colour", drop="strobe led laser"),
         led_a="blue", led_b="white", laser_a="green", laser_b="red", base_laser=True,
         every_beats=4, k_derby=0.8, k_laser=0.6),
    Look("Colour Pulse", "Every bass hit flashes the LED and steps to the next colour.",
         rules=_rules(bass="led colour", drop="strobe"),
         led_a="red", led_b="blue", laser_a="red", laser_b="green", base_laser=True,
         every_beats=4, k_derby=1.0, k_laser=0.8),
    Look("Strobe Kick", "LED and strobe hit together on the bass, full flash on the drop.",
         rules=_rules(bass="led strobe", build="strobe", drop="led laser strobe"),
         led_a="white", led_b="red_white", laser_a="red_green", laser_b="red", blink_ms=90,
         strobe_rate=0.8, k_derby=1.2, k_laser=1.0),
    Look("Dark Build", "Calm base light with slow colour changes; blackout in the build-up, light bomb on the drop.",
         rules=_rules(beat="colour", build="blackout", drop="led laser strobe"),
         led_a="blue", led_b="red_blue", laser_a="green", laser_b="green", base_led=True,
         every_beats=16, k_derby=0.5, k_laser=0.4),
    Look("Ambient", "No reaction to single hits: steady light, colour changes every 2 bars.",
         rules=_rules(beat="colour"),
         led_a="blue", led_b="green_blue", laser_a="green", laser_b="green", base_led=True, base_laser=True,
         every_beats=8, k_derby=0.4, k_laser=0.3),
]
BY_NAME = {look.name: look for look in QUICK_LOOKS}
DEFAULT = QUICK_LOOKS[0]
CUSTOM = "Custom"

def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


class TempoTracker:
    """BPM and a phase-locked beat position, from the audio's onset strength.

    1. Every frame: onset strength x = kick flux / its running mean (+ a little hi-hat flux).
    2. Every TEMPO_UPDATE_S: autocorrelation of the last TEMPO_WINDOW_S of x. A
       comb over the first four multiples of each candidate lag scores how well a
       steady pulse of that period explains the onsets; a soft prior around
       TEMPO_PRIOR_BPM settles half / double tempo.
    3. Once locked, beat_pos (a continuous beat counter) advances with the BPM.
       The same window is folded modulo one beat; where the onsets pile up tells how
       far the clock is off, and a fraction of that is corrected each update.
       A kick-less break leaves the fold flat, so the clock simply free-runs.
    """

    def __init__(self):
        self.frame_rate = 46.875
        self._rate_set = False
        self.reset()

    def reset(self) -> None:
        self._odf = deque(maxlen=int(self.frame_rate * config.TEMPO_WINDOW_S))
        self._ema_low = 0.0
        self._ema_high = 0.0
        self._silent_t = 0.0
        self._since = 0.0
        self._good = 0
        self._jump = 0
        self._low_t = 0.0
        self.bpm = 0.0           # 0 = unknown
        self.score = 0.0         # autocorrelation score of the current estimate (0..~1)
        self.peakiness = 0.0
        self.locked = False
        self.beat_pos = 0.0      # continuous, in beats; the integer part counts beats

    def _set_rate(self, dt: float) -> None:
        rate = 1.0 / dt
        if not self._rate_set or abs(rate - self.frame_rate) > 0.03 * self.frame_rate:
            self.frame_rate = rate
            self._rate_set = True
            self._odf = deque(self._odf, maxlen=int(rate * config.TEMPO_WINDOW_S))

    def update(self, flux_low: float, flux_high: float, dt: float, active: bool, hold_s: float,
               freeze: bool = False) -> None:
        """freeze=True: keep the clock running but neither re-estimate tempo nor correct phase
        (no kick to go by -- risers and hats would only pull the grid off)."""
        self._set_rate(dt)
        if not active:
            self._silent_t += dt
            if self._silent_t > 2.0 and (self._odf or self.locked):
                self.reset()                     # new song after a pause -> start from scratch
            return
        self._silent_t = 0.0

        if self._ema_low == 0.0:
            self._ema_low, self._ema_high = max(flux_low, 1e-3), max(flux_high, 1e-3)
        a = min(1.0, dt / config.ONSET_NORM_S)
        self._ema_low += a * (flux_low - self._ema_low)
        self._ema_high += a * (flux_high - self._ema_high)
        x = flux_low / (self._ema_low + 1e-3) + config.ONSET_HAT_WEIGHT * flux_high / (self._ema_high + 1e-3)
        self._odf.append(min(x, 8.0))

        if self.locked and self.bpm > 0:
            self.beat_pos += dt * self.bpm / 60.0

        self._since += dt
        if freeze:
            self._since = 0.0
        elif self._since >= config.TEMPO_UPDATE_S and len(self._odf) >= 0.6 * self._odf.maxlen:
            self._since = 0.0
            self._estimate(hold_s)

    # ---- tempo
    def _estimate(self, hold_s: float) -> None:
        x = np.asarray(self._odf, dtype=np.float64)
        n = len(x)
        x = x - x.mean()
        if not np.any(x):
            return
        spec = np.fft.rfft(x, 2 * n)
        ac = np.fft.irfft(spec * np.conj(spec))[:n]
        ac = ac / (n - np.arange(n))             # unbiased: long lags have fewer overlapping samples
        if ac[0] <= 1e-9:
            return
        ac = ac / ac[0]

        fr = self.frame_rate
        lags = np.arange(fr * 60.0 / config.TEMPO_BPM_MAX, fr * 60.0 / config.TEMPO_BPM_MIN, 0.1)
        idx = np.arange(n)
        comb = np.zeros_like(lags)
        wsum = 0.0
        for mult, w in ((1, 1.0), (2, 0.5), (3, 0.33), (4, 0.25)):
            pos = lags * mult
            if pos[-1] >= n - 1:
                break
            comb += w * np.interp(pos, idx, ac)
            wsum += w
        comb /= max(wsum, 1e-9)
        bpm_c = 60.0 * fr / lags
        prior = np.exp(-0.5 * (np.log2(bpm_c / config.TEMPO_PRIOR_BPM) / config.TEMPO_PRIOR_WIDTH_OCT) ** 2)
        best = int(np.argmax(comb * prior))
        bpm_new, self.score = float(bpm_c[best]), float(comb[best])

        if not self.locked:
            if self.score >= config.TEMPO_LOCK_ON:
                self._good += 1
                if self._good >= 2:
                    self.bpm = bpm_new
                    if self._fold()[2] >= config.FOLD_MIN_PEAKINESS:    # onsets must pile up on one spot of the beat
                        self.locked, self._jump, self._low_t = True, 0, 0.0
                        self._align(1.0, force=True)
                    else:
                        self.bpm, self._good = 0.0, 0
            else:
                self._good = 0
            return

        if self.score < config.TEMPO_LOCK_OFF:
            self._low_t += config.TEMPO_UPDATE_S
            if self._low_t > hold_s:
                self.locked, self._good = False, 0
            return                               # no evidence -> keep the clock as it is
        self._low_t = 0.0

        if self.score >= config.TEMPO_LOCK_ON:
            rel = abs(bpm_new - self.bpm) / self.bpm
            if rel < 0.025:
                self.bpm += config.TEMPO_BPM_SMOOTH * (bpm_new - self.bpm)
                self._jump = 0
            elif self._is_octave(bpm_new):
                pass                             # half / double tempo flip-flop: stay with what we have
            else:
                self._jump += 1                  # a real tempo change must persist for a few updates
                if self._jump >= 3:
                    self.bpm, self._jump = bpm_new, 0
                    self._align(1.0, force=True)
                    return
        self._align(config.PHASE_GAIN)

    def snap_to_beat(self, extra_lag_frames: int = 0) -> None:
        """Hard-sync: a kick was just detected -> put the nearest beat boundary right there.
        Used when the kick comes back after a break (a drop starts on the beat)."""
        late = (config.ODF_LATENCY_FRAMES + extra_lag_frames) * self.bpm / 60.0 / self.frame_rate  # beats since the transient
        self.beat_pos = math.floor(self.beat_pos - late + 0.5) + late

    def _is_octave(self, bpm_new: float) -> bool:
        ratio = bpm_new / self.bpm
        return any(abs(ratio / r - 1.0) < 0.05 for r in (0.5, 2.0, 1.0 / 3.0, 3.0))

    # ---- phase
    def _fold(self) -> tuple:
        """The recent onset strength folded modulo one beat -> (histogram, peak bin, peakiness)."""
        x = np.asarray(self._odf, dtype=np.float64)
        n = len(x)
        beats_per_frame = self.bpm / 60.0 / self.frame_rate
        ages = np.arange(n - 1, -1, -1) + config.ODF_LATENCY_FRAMES
        pos = self.beat_pos - ages * beats_per_frame
        frac = pos - np.floor(pos)
        bins = config.FOLD_BINS
        hist = np.bincount((frac * bins).astype(int) % bins, weights=x, minlength=bins)
        hist = hist + 0.5 * (np.roll(hist, 1) + np.roll(hist, -1))
        peak = int(np.argmax(hist))
        mean = float(hist.mean())
        return hist, peak, (float(hist[peak] / mean) if mean > 0 else 0.0)

    def _align(self, gain: float, force: bool = False) -> None:
        bins = config.FOLD_BINS
        hist, peak, self.peakiness = self._fold()
        if self.peakiness < config.FOLD_MIN_PEAKINESS and not force:
            return
        left, mid, right = hist[(peak - 1) % bins], hist[peak], hist[(peak + 1) % bins]
        denom = left - 2.0 * mid + right
        delta = _clamp(0.5 * (left - right) / denom, -0.5, 0.5) if denom != 0 else 0.0
        centre = (peak + 0.5 + delta) / bins     # where in the beat the onsets pile up (our phase there)
        err = ((centre + 0.5) % 1.0) - 0.5       # -> that is how far ahead our clock runs
        self.beat_pos -= gain * err


class SectionTracker:
    """Classifies the song as silence / groove / break / build-up / drop.

    Uses only relative measures, so it does not care how loud the track is:
    * kick present: a kick onset within the last max(KICK_GAP_MIN_S, 3 beats)
    * break:  the kick was there (>= KICK_READY_HITS in 12 s) and is gone
    * build:  overall level and treble rise together for a while (risers, snare rolls)
    * drop:   the bass slams back in -- fast bass level far above its slow average --
              after a break or a build-up (or a very large jump out of a groove)
    A drop lasts at least DROP_HOLD_S / 4 beats, then it is groove again.
    """

    def __init__(self):
        self.reset()

    def reset(self) -> None:
        self.state = SILENCE
        self.state_t = 0.0
        self.t = 0.0
        self.drop_event = False          # True for exactly one update when a drop starts
        self.kick_after_gap = False      # True for exactly one update when a kick returns after a gap
        self.hit = False                 # a kick onset that is also loud enough in the bass to be a real kick
        self.hit_lag = 0                 # frames between that onset and the moment the level confirmed it
        self.build_progress = 0.0
        self.kick_ready = False
        self.since_kick = 99.0
        self.kick_present = False
        self._silent_t = 0.0
        self._kick_times = deque()
        self._levels = {}
        self._ref_peak = None            # slowly sinking peak of the bass level
        self._pending = 0                # frames left to confirm an onset whose bass level was still rising
        self._rise_t = 0.0
        self._gap_seen = False
        self._last_drop_t = -99.0
        self._start_t = 0.0
        self._build_start_db = 0.0

    def _ema(self, key: str, value: float, tau: float, dt: float) -> float:
        prev = self._levels.get(key)
        new = value if prev is None else prev + (1.0 - math.exp(-dt / tau)) * (value - prev)
        self._levels[key] = new
        return new

    def _enter(self, state: str) -> None:
        self.state = state
        self.state_t = 0.0
        self._gap_seen = False
        self._rise_t = 0.0               # every state starts counting a new rise from scratch
        if state == DROP:
            self.drop_event = True
            self._last_drop_t = self.t
        if state == BUILD:
            self._build_start_db = self._levels.get("tot_fast", 0.0)

    def update(self, f, dt: float, beat_period: float) -> None:
        self.drop_event = False
        self.kick_after_gap = False
        self.t += dt
        self.state_t += dt

        if f.total_db < config.SILENCE_DB:
            self._silent_t += dt
        else:
            self._silent_t = 0.0
        if self._silent_t >= config.SILENCE_HOLD_S:
            if self.state != SILENCE:
                keep_t = self.t
                self.reset()
                self.t = keep_t
            return
        if self.state == SILENCE:
            if f.total_db < config.SILENCE_DB:
                return
            self._enter(GROOVE)
            self._start_t = self.t

        # --- kick bookkeeping. A kick onset only counts if the bass is near its recent peak:
        # snare rolls and risers also trigger the onset detector, but never at kick level
        self._ref_peak = f.bass_db if self._ref_peak is None else max(
            f.bass_db, self._ref_peak - config.KICK_REF_DECAY_DB_S * dt)
        level_ok = f.bass_db >= self._ref_peak - config.KICK_LEVEL_DB
        near_peak = f.bass_db >= self._ref_peak - 2.0 * config.KICK_LEVEL_DB   # a slam is loud, not just "up from silence"
        self.hit, self.hit_lag = False, 0
        if f.kick_hit and level_ok:
            self.hit, self._pending = True, 0
        elif f.kick_hit:
            self._pending = 3            # the first frame of a kick often only holds its attack: give it a moment
        elif self._pending > 0:
            self._pending -= 1
            if level_ok:
                self.hit, self.hit_lag, self._pending = True, 3 - self._pending, 0
        gap = max(config.KICK_GAP_MIN_S, 3.0 * beat_period)
        if self.hit:
            if self.since_kick > gap:
                self.kick_after_gap = True
            self.since_kick = 0.0
            self._kick_times.append(self.t)
        else:
            self.since_kick += dt
        while self._kick_times and self.t - self._kick_times[0] > 12.0:
            self._kick_times.popleft()
        if len(self._kick_times) >= config.KICK_READY_HITS:
            self.kick_ready = True
        elif self.since_kick > 20.0:
            self.kick_ready = False
        self.kick_present = self.since_kick < gap
        recent_hits = sum(1 for t in self._kick_times if self.t - t < 1.5)

        # --- level trends (dB domain)
        # (the fast average spans about two beats so a kick's own envelope does not look like a jump at slow tempi)
        bass_jump = (self._ema("bass_fast", f.bass_db, max(0.35, 2.0 * beat_period), dt)
                     - self._ema("bass_slow", f.bass_db, 6.0, dt))
        tot_fast = self._ema("tot_fast", f.total_db, 1.0, dt)
        tot_trend = tot_fast - self._ema("tot_slow", f.total_db, 8.0, dt)
        tre_trend = self._ema("tre_fast", f.treble_db, 1.0, dt) - self._ema("tre_slow", f.treble_db, 8.0, dt)
        rising = tot_trend >= config.BUILD_TREND_DB and tre_trend >= config.BUILD_TREBLE_TREND_DB
        self._rise_t = self._rise_t + dt if rising else max(0.0, self._rise_t - 2.0 * dt)

        cooldown_ok = (self.t - self._last_drop_t > config.DROP_COOLDOWN_S
                       and self.t - self._start_t > config.STARTUP_GUARD_S)
        state = self.state
        if state == DROP:
            if self.state_t >= max(config.DROP_HOLD_S, 4.0 * beat_period):
                self._enter(GROOVE if (self.kick_present or not self.kick_ready) else BREAK)

        elif state == BREAK:
            kick_back = self.kick_present and (bass_jump >= config.DROP_BASS_JUMP_DB or recent_hits >= 3)
            if kick_back:
                self._enter(DROP if (self.state_t >= config.BREAK_MIN_S and cooldown_ok) else GROOVE)
            elif rising and self._rise_t >= config.BUILD_MIN_RISE_S:
                self._enter(BUILD)

        elif state == BUILD:
            if not self.kick_present and self.kick_ready:
                self._gap_seen = True
            returned = self._gap_seen and self.kick_present and (bass_jump >= config.DROP_BASS_JUMP_DB
                                                                 or recent_hits >= 3)
            slammed = self.state_t >= 2.0 and near_peak and bass_jump >= config.DROP_BASS_JUMP_DB + 2.0
            if (returned or slammed) and cooldown_ok:
                self._enter(DROP)
            elif (self._rise_t <= 0.1 and tot_trend < 0.5 and self.state_t > 3.0) \
                    or self.state_t > config.BUILD_MAX_S:
                self._enter(GROOVE if (self.kick_present or not self.kick_ready) else BREAK)
            else:
                ramp_s = config.BUILD_RAMP_BEATS * beat_period
                level = (tot_fast - self._build_start_db) / 10.0
                self.build_progress = _clamp(max(self.state_t / ramp_s, level), 0.0, 1.0)

        else:  # GROOVE
            if self.kick_ready and not self.kick_present:
                self._enter(BREAK)
            elif cooldown_ok and self.kick_present and near_peak and bass_jump >= config.DROP_FROM_GROOVE_JUMP_DB:
                self._enter(DROP)
            elif cooldown_ok and rising and self._rise_t >= config.BUILD_MIN_RISE_S:
                self._enter(BUILD)
        if self.state != BUILD:
            self.build_progress = 0.0




class LightEngine:
    """Runs the trackers and renders the current Look to DMX values.

    State for the UI: bpm, tempo_known, locked, beat_in_bar, bar, section_state, build_progress,
    and `preview` (what the LED / laser / strobe are doing right now, for the little fixture preview).
    """

    def __init__(self, look: Look):
        self.tempo = TempoTracker()
        self.section = SectionTracker()
        self.look = look
        self._fallback_bpm = config.FALLBACK_BPM
        self._pos = 0.0                  # beat position of the clock the lights follow
        self._idx = 0                    # integer beat already announced
        self._origin = 0                 # beat index of the last phrase start (a drop, else lock-on)
        self._was_locked = False
        self._gap_idx = None             # (beat index, time) of the kick that came back after a gap
        self._t = 0.0                    # engine time in seconds (sum of the audio block lengths)
        self._until = {key: 0.0 for key, _ in ACTIONS}   # action -> time until which a blink / hold is active
        self._colour = 0                 # 0 = colour A, 1 = colour B
        self.force_blackout = False      # the Blackout button of the window: everything dark, whatever the rules say
        self.preview = dict(led=None, laser=None, strobe=False, blackout=False)

    def set_look(self, look: Look) -> None:
        self.look = look

    # ---- state for the UI
    @property
    def bpm(self) -> float:
        return self.tempo.bpm if self.tempo.bpm > 0 else self._fallback_bpm

    @property
    def tempo_known(self) -> bool:
        return self.tempo.bpm > 0

    @property
    def locked(self) -> bool:
        return self.tempo.locked

    @property
    def section_state(self) -> str:
        return self.section.state

    @property
    def build_progress(self) -> float:
        return self.section.build_progress

    @property
    def beat_index(self) -> int:
        return self._idx - self._origin

    @property
    def beat_in_bar(self) -> int:
        return self.beat_index % 4

    @property
    def bar(self) -> int:
        return self.beat_index // 4

    # ---- main entry
    def process(self, f) -> dict:
        """f: one analysis frame with dt, total_db, bass_db, treble_db, bass, kick_hit, flux_low, flux_high.
        Returns {channel: value} for channels 2..9 (channel 1 is never part of it)."""
        dt = f.dt if f.dt > 0 else 0.02
        self._t += dt
        sec = self.section
        sec.update(f, dt, 60.0 / self.bpm)
        state = sec.state
        active = state != SILENCE
        hold = config.LOCK_HOLD_BREAK_S if state in (BREAK, BUILD) else config.LOCK_HOLD_S
        freeze = state in (BREAK, BUILD) and not sec.kick_present
        self.tempo.update(f.flux_low, f.flux_high, dt, active, hold, freeze)
        if self.tempo.bpm > 0:
            self._fallback_bpm = self.tempo.bpm
        if sec.kick_after_gap and self.tempo.locked:
            self.tempo.snap_to_beat(sec.hit_lag)

        tick = self._advance_clock(dt, sec.hit, active)
        if sec.kick_after_gap:
            self._gap_idx = (self._idx, sec.t)
        if sec.drop_event:
            self._on_drop()

        if not active:
            self.preview = dict(led=None, laser=None, strobe=False, blackout=False)
            return {ch: 0 for ch in range(2, 10)}

        look = self.look
        blink = max(0.04, look.blink_ms / 1000.0)
        period = 60.0 / self.bpm
        if f.kick_hit:
            self._fire("bass", blink)
        if tick and self.beat_index % max(1, look.every_beats) == 0:
            self._fire("beat", blink)
        if sec.drop_event:
            self._fire("drop", _clamp(4 * period, 1.5, 4.0))
        if state == BUILD and tick and "colour" in look.rules.get("build", ()):
            self._colour ^= 1                    # build-up: the colour steps on every beat
        return self._render(state)

    def _fire(self, trigger: str, length: float) -> None:
        for action in self.look.rules.get(trigger, ()):
            if action == "colour":
                self._colour ^= 1
            else:
                self._until[action] = max(self._until[action], self._t + length)

    # ---- clock
    def _advance_clock(self, dt: float, kick_hit: bool, active: bool) -> bool:
        tempo = self.tempo
        tick = False
        if tempo.locked:
            pos = tempo.beat_pos + config.SYNC_OFFSET_MS_DEFAULT / 1000.0 * self.bpm / 60.0
            idx = math.floor(pos)
            if not self._was_locked:             # (re)acquired: start counting beats from here
                self._idx = self._origin = idx
            elif idx > self._idx:
                self._idx = idx
                tick = True
            self._pos = pos
        elif active:
            # no stable tempo (yet): every kick is a beat, in between the clock coasts at the last known BPM
            self._pos = min(self._pos + dt * self.bpm / 60.0, self._idx + 0.999)
            if kick_hit:
                self._idx += 1
                self._pos = float(self._idx)
                tick = True
        self._was_locked = tempo.locked
        return tick

    def _on_drop(self) -> None:
        """A drop starts a new phrase: the beat the kick came back on becomes 'beat 1'."""
        gap = self._gap_idx
        if gap is not None and self.section.t - gap[1] < 3.0:
            self._origin = gap[0]
        else:
            self._origin = round(self._pos)
        self._gap_idx = None

    # ---- rendering
    def _render(self, state: str) -> dict:
        look, sec, t = self.look, self.section, self._t
        rules = look.rules
        bpm = self.bpm

        def speed(k: float) -> float:
            scale = config.BREAK_SPEED_SCALE if state == BREAK else 1.0
            return _clamp(k * bpm / config.SPEED_FULLSCALE_BPM, 0.0, 1.0) * scale

        live = {action for action, until in self._until.items() if t < until}
        if state == BUILD:                       # the build-up holds its actions for as long as it lasts
            live |= {a for a in rules.get("build", ()) if a != "colour"}
        blackout = "blackout" in live or self.force_blackout
        led_on = (look.base_led or "led" in live) and not blackout
        laser_on = (look.base_laser or "laser" in live) and not blackout
        strobe_on = "strobe" in live and not blackout

        rate = look.strobe_rate
        if state == BUILD and "strobe" in rules.get("build", ()):
            rate *= 0.3 + 0.7 * sec.build_progress      # strobe ramps up towards the drop

        led_key = look.led_b if self._colour else look.led_a
        laser_key = look.laser_b if self._colour else look.laser_a

        out = {2: int(250 * speed(look.k_show))}
        out[3] = DERBY[led_key] if led_on else 0
        out[4] = int(STROBE_CH4[0] + rate * (STROBE_CH4[1] - STROBE_CH4[0])) if strobe_on else 0

        s = speed(look.k_derby)                  # derby motor: 128..255 = rotation, 0 = stopped
        out[5] = 128 + int(127 * s) if look.k_derby > 0 else 0

        if laser_on:
            pool = look.pattern or (1 + (self.beat_index // 16) % 18)
            out[6] = 10 + 10 * (pool - 1) + int(round(9 * speed(look.k_show)))
            out[7] = LASER.get(laser_key, LASER["green"])
        else:
            out[6] = out[7] = 0
        out[8] = int(STROBE_CH8[0] + rate * (STROBE_CH8[1] - STROBE_CH8[0])) if strobe_on else 0

        s = speed(look.k_laser)                  # laser rotation: 5..127 cw, 134..255 ccw, 0 = stopped
        if look.k_laser <= 0 or not laser_on:
            out[9] = 0
        else:
            cw = look.laser_dir == "cw" or (look.laser_dir == "alt" and (self.beat_index // 32) % 2 == 0)
            out[9] = 5 + int(122 * s) if cw else 134 + int(121 * s)

        self.preview = dict(
            led=LED_COLOURS[led_key][1] if led_on else None,
            laser=LASER_COLOURS.get(laser_key, LASER_COLOURS["green"])[1] if laser_on else None,
            strobe=strobe_on, blackout=blackout)
        return out
