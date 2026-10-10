"""
Analysis
========

Raw mono audio blocks in, one Features object per block out. No audio library, no DMX, no GUI.

Per frequency band (config.BANDS: sub / bass / mids / highs):
    level   0..1, relative to the band's own recent peak (so it does not care how loud the track is)
    hit     True for the block in which an onset happened
              bass            fast time-domain detector (KickOnset), no FFT window lag
              sub/mids/highs  spectral flux in the band over its running mean
plus the tempo: BPM, a phase-locked beat clock and a beat tick (TempoTracker).

Per song the analysis learns two things and caches them (song_profiles.json, keyed by "artist - title"):
    * the kick's fundamental frequency -> the bass detector is tuned to a band around it, so bass lines and
      sub swells stop counting as bass hits; one kick is also only ever one hit (cross-band mask + beat guard)
    * a plausible hit threshold for sub / mids / highs (nudged until the hit rate fits)

Song sections (build-up / drop) are deliberately gone. The only "structure" kept is whether
there is music at all (silence gate) and whether the bass kick is currently present (so the
tempo clock coasts through a kick-less break instead of being pulled off by hats).
"""

import json
import logging
import math
from collections import deque
from dataclasses import dataclass, field

import numpy as np

from . import config

BAND_NAMES = tuple(band[0] for band in config.BANDS)
BAND_LABELS = {band[0]: band[1] for band in config.BANDS}
BAND_RANGES = {band[0]: (band[2], band[3]) for band in config.BANDS}
FLUX_BANDS = tuple(name for name in BAND_NAMES if name != "bass")     # bass uses the kick detector


def _clamp(x: float, lo: float, hi: float) -> float:
    return lo if x < lo else hi if x > hi else x


@dataclass
class Features:
    """Everything the engine and the UI need to know about one audio block."""
    dt: float = 0.0                  # seconds of audio this block covers
    active: bool = False             # music present (False = silence)
    level: dict = field(default_factory=lambda: {n: 0.0 for n in BAND_NAMES})
    hit: dict = field(default_factory=lambda: {n: False for n in BAND_NAMES})
    bars: dict = None                # band -> np.ndarray of fine-spectrum bar levels (display)
    waveform: np.ndarray = None      # short slice of the raw samples (display)
    bpm: float = 0.0
    tempo_known: bool = False
    locked: bool = False
    beat_tick: bool = False          # True for exactly one block per beat
    beat_index: int = 0              # beats since the tempo locked on (or since the song started)
    total_db: float = -120.0
    error: Exception = None
    kick_hz: float = 0.0             # learned kick fundamental (0 = not known yet)
    kick_interval: float = 0.0       # recent time between two kicks in s (0 = unknown)
    kick_learned: bool = False
    song_key: str = ""

    @property
    def beat_in_bar(self) -> int:
        return self.beat_index % 4

    @property
    def bar(self) -> int:
        return self.beat_index // 4


class KickOnset:
    """Fast kick / bass onset detector working on the raw samples (no FFT, so no window lag).

    Generic mode: low-pass (box filter, ~110 Hz) -> RMS of short slices.
    Tuned mode (set_freq): the samples are decimated (cheap) and sent through a 4th-order band-pass centred on the
    learned kick frequency, so a bass line below / above it no longer triggers.
    A slice counts as a hit when it is clearly above the ~1 s running average and clearly above the slice before."""

    DECIMATE = 8                                      # tuned mode works at samplerate / 8 (kick band is < 300 Hz)

    def __init__(self, samplerate: int):
        self.sr = samplerate
        self.hz = 0.0
        self.taps = max(8, round(config.KICK_LOWPASS_S * samplerate))
        self._tail = np.zeros(self.taps - 1, np.float32)
        self._rest = np.zeros(0, np.float32)           # samples left over by the decimation
        self._coef = None
        self._z = [0.0, 0.0, 0.0, 0.0]
        self._avg = 0.0
        self._prev = 0.0
        self._cool = 0

    def set_freq(self, hz: float) -> None:
        """Tune the detector to a kick fundamental (hz <= 0: back to the generic low-pass)."""
        if hz <= 0:
            if self.hz:
                self.hz, self._coef, self._avg = 0.0, None, 0.0
            return
        if self.hz and abs(hz - self.hz) / self.hz < 0.08:
            return                                     # not worth re-tuning (and re-learning the average)
        self.hz = hz
        w0 = 2.0 * math.pi * hz / (self.sr / self.DECIMATE)
        alpha = math.sin(w0) / (2.0 * config.KICK_BAND_Q)
        a0 = 1.0 + alpha                               # RBJ band-pass (constant 0 dB peak gain)
        self._coef = (alpha / a0, -alpha / a0, -2.0 * math.cos(w0) / a0, (1.0 - alpha) / a0)
        self._z = [0.0, 0.0, 0.0, 0.0]
        self._avg = 0.0

    def _band(self, samples: np.ndarray) -> np.ndarray:
        d = self.DECIMATE
        x = np.concatenate((self._rest, samples.astype(np.float32)))
        m = len(x) // d
        self._rest = x[m * d:]
        x = x[:m * d].reshape(m, d).mean(axis=1).tolist()
        b0, b2, a1, a2 = self._coef
        z1a, z2a, z1b, z2b = self._z
        out = []
        for v in x:                                    # two cascaded biquads (transposed direct form II)
            y = b0 * v + z1a
            z1a = -a1 * y + z2a
            z2a = b2 * v - a2 * y
            y2 = b0 * y + z1b
            z1b = -a1 * y2 + z2b
            z2b = b2 * y - a2 * y2
            out.append(y2)
        self._z = [z1a, z2a, z1b, z2b]
        return np.asarray(out)

    def process(self, samples: np.ndarray, min_gap_s: float = 0.0) -> bool:
        n = len(samples)
        floor = config.BEAT_MIN_RMS
        if self._coef is None:
            z = np.concatenate((self._tail, samples.astype(np.float32)))
            self._tail = z[-(self.taps - 1):]
            c = np.cumsum(np.concatenate(([0.0], z)), dtype=np.float64)
            y = (c[self.taps:] - c[:-self.taps]) / self.taps       # moving average = low-pass, same length as samples
            rate = self.sr
        else:
            y = self._band(samples)
            rate = self.sr / self.DECIMATE
            floor *= config.KICK_BAND_FLOOR
        sl = max(4, round(config.KICK_SLICE_S * rate))
        slice_s = sl / rate
        alpha = slice_s / config.KICK_HISTORY_S
        cool_slices = max(1, round(max(config.KICK_COOLDOWN_S, min_gap_s) / slice_s))
        hit = False
        for k in range(0, len(y) - sl + 1, sl):
            e = float(np.sqrt(np.mean(y[k:k + sl] ** 2)))
            if self._cool > 0:
                self._cool -= 1
            if (self._cool == 0 and self._avg > 0 and e > self._avg * config.KICK_RATIO
                    and e > floor and e > self._prev * config.KICK_RISE):
                hit = True
                self._cool = cool_slices
            self._avg = e if self._avg == 0 else self._avg + alpha * (e - self._avg)
            self._prev = e
        return hit


def _load_profiles() -> dict:
    try:
        with open(config.PROFILE_FILE, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class TempoTracker:
    """BPM and a phase-locked beat position, from the audio's onset strength.

    1. Every block: onset strength x = kick flux / its running mean (+ a little hi-hat flux).
    2. Every TEMPO_UPDATE_S: autocorrelation of the last TEMPO_WINDOW_S of x. A comb over the first four
       multiples of each candidate lag scores how well a steady pulse of that period explains the onsets;
       a soft prior around TEMPO_PRIOR_BPM settles half / double tempo.
    3. Once locked, beat_pos (a continuous beat counter) advances with the BPM. The same window is folded
       modulo one beat; where the onsets pile up tells how far the clock is off, and a fraction of that is
       corrected each update. A kick-less break leaves the fold flat, so the clock simply free-runs.
    """

    def __init__(self):
        self.frame_rate = 46.875
        self._rate_set = False
        self.prior_bpm = config.TEMPO_PRIOR_BPM      # centre of the half / double tempo prior (a cached song sets its own)
        self.resync = False                          # set when the BPM / lock changed: the bar counter starts over
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
        self.resync = False

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
        prior = np.exp(-0.5 * (np.log2(bpm_c / self.prior_bpm) / config.TEMPO_PRIOR_WIDTH_OCT) ** 2)
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
                    self.resync = True               # a real tempo change: count the bars from here again
                    return
        self._align(config.PHASE_GAIN)

    def snap_to_beat(self, extra_lag_frames: int = 0) -> None:
        """Hard-sync: a kick was just detected -> put the nearest beat boundary right there.
        Used when the kick comes back after a break."""
        late = (config.ODF_LATENCY_FRAMES + extra_lag_frames) * self.bpm / 60.0 / self.frame_rate
        self.beat_pos = math.floor(self.beat_pos - late + 0.5) + late

    def pop_resync(self) -> bool:
        flag, self.resync = self.resync, False
        return flag

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


class Analyzer:
    """Turns raw audio blocks into Features. process() must be called from one thread at a time."""

    def __init__(self):
        self.gain = config.GAIN_DEFAULT              # sensitivity, adjustable live
        self.smoothing = config.LEVEL_SMOOTHING
        self.tempo = TempoTracker()
        self.samplerate = config.SAMPLE_RATE

        self._layout_sr = None
        self._levels = {n: 0.0 for n in BAND_NAMES}
        self._band_ref = {n: -120.0 for n in BAND_NAMES}   # loudest recent dB per band (auto-gain)
        self._bars_ref = -120.0
        self._bar_levels = None
        self._prev_lm = None                         # previous log-magnitude spectrum, for the spectral flux
        self._buf = None
        self._ema = {n: 0.0 for n in FLUX_BANDS}     # running mean of each band's flux
        self._cool = {n: 0.0 for n in FLUX_BANDS}    # per-band hit cooldown (seconds)
        self._silent_t = 0.0
        self._t = 0.0
        self._kick_times = deque()
        self._since_kick = 99.0
        self._kick_ready = False
        self._fallback_bpm = config.FALLBACK_BPM
        self._pos = 0.0                              # beat clock: position, announced beat, first beat
        self._idx = 0
        self._origin = 0
        self._was_locked = False
        self._bar_reset = False                      # start counting bars from the next beat

        # per-song learning
        self.learning = config.LEARN_DEFAULT         # "Learn per song" checkbox
        self._profiles = _load_profiles()
        self._song_key = ""
        self._requests = []                          # (kind, arg) from the GUI thread, handled on the audio thread
        self._profile_dirty = False
        self._last_save = 0.0
        self._silence_reset = False
        self._reset_learning()

    # ---- state
    @property
    def bpm(self) -> float:
        return self.tempo.bpm if self.tempo.bpm > 0 else self._fallback_bpm

    # ---- requests from the GUI thread (handled at the start of the next block, so no locks are needed)
    def request_song(self, key) -> None:
        """A different track started (key = "artist - title" or None)."""
        self._requests.append(("song", key))

    def request_tempo_recheck(self) -> None:
        """Forget the tempo and bar count and listen again (the BPM of the last song stays as a hint)."""
        self._requests.append(("tempo", None))

    def flush(self) -> None:
        """Write the learned values to disk (also done on song change and every PROFILE_AUTOSAVE_S)."""
        self._save_profile()
        try:
            with open(config.PROFILE_FILE, "w", encoding="utf-8") as fh:
                json.dump(self._profiles, fh, indent=1)
            self._profile_dirty = False
        except OSError:
            logging.debug("Could not write %s", config.PROFILE_FILE, exc_info=True)

    # ---- per-song learning
    def _reset_learning(self) -> None:
        self.kick_hz = 0.0
        self._kick_acc = None
        self._kick_hits = 0
        self._learn_hits = 0                         # kicks that sat on the beat grid (only those teach the frequency)
        self._learn_ttl = 0
        self._kick_gap = 0.0
        self._last_kick_t = -1e9
        self._ratio = dict(config.HIT_RATIO)
        self._rate_hits = {n: deque() for n in FLUX_BANDS}
        self._rate_t = 0.0
        self._mask_t = 0.0
        self._song_t0 = self._t
        self._kick_times = deque()
        self._since_kick = 99.0
        self._kick_ready = False
        self.tempo.prior_bpm = config.TEMPO_PRIOR_BPM
        if hasattr(self, "_kick"):
            self._kick.set_freq(0.0)

    def _begin_song(self, key) -> None:
        self._save_profile()
        self._reset_learning()
        self.tempo.reset()
        self._was_locked = False
        self._bar_reset = True
        self._song_key = key or ""
        prof = self._profiles.get(self._song_key) if (self.learning and self._song_key) else None
        if prof:
            hz = float(prof.get("kick_hz", 0.0))
            if 25.0 < hz < 300.0:
                self.kick_hz = hz
                self._learn_hits = config.KICK_LEARN_HITS          # counts as learned
                self._kick.set_freq(hz)
            for name, value in prof.get("ratios", {}).items():
                if name in self._ratio:
                    self._ratio[name] = float(value)
            bpm = float(prof.get("bpm", 0.0))
            if config.TEMPO_BPM_MIN <= bpm <= config.TEMPO_BPM_MAX:
                self.tempo.prior_bpm = bpm

    def _save_profile(self) -> None:
        key = self._song_key
        if not key or not self.learning:
            return
        prof = {}
        if self.kick_hz:
            prof["kick_hz"] = round(self.kick_hz, 1)
        ratios = {n: round(r, 2) for n, r in self._ratio.items() if abs(r - config.HIT_RATIO[n]) > 0.01}
        if ratios:
            prof["ratios"] = ratios
        if self.tempo.locked and self.tempo.bpm > 0:
            prof["bpm"] = round(self.tempo.bpm, 1)
        if not prof or self._profiles.get(key) == prof:
            return
        self._profiles.pop(key, None)                                # re-insert = newest
        self._profiles[key] = prof
        while len(self._profiles) > config.PROFILE_MAX:
            self._profiles.pop(next(iter(self._profiles)))
        self._profile_dirty = True

    def _learn_accumulate(self, rise: np.ndarray) -> None:
        """Collect the onset spectrum around the kick fundamental in the blocks right after a kick."""
        if self._kick_acc is None:
            self._kick_acc = np.zeros(len(self._learn_idx))
        self._kick_acc += rise[self._learn_idx]
        self._learn_ttl -= 1
        if self._learn_ttl > 0 or self._learn_hits < config.KICK_LEARN_HITS:
            return
        acc = self._kick_acc
        if acc.max() < 1.5 * acc.mean() or acc.sum() <= 1e-6:        # no clear peak: keep what we have
            return
        peak = int(np.argmax(acc))
        lo, hi = max(0, peak - 2), min(len(acc), peak + 3)
        weights = acc[lo:hi]
        hz = float((self._learn_freqs[lo:hi] * weights).sum() / weights.sum())
        self.kick_hz = hz if self.kick_hz == 0.0 else self.kick_hz + 0.3 * (hz - self.kick_hz)
        self._kick.set_freq(self.kick_hz)
        self._profile_dirty = True
        if self._learn_hits % config.KICK_LEARN_DECAY_EVERY == 0:
            acc *= 0.5

    def _tune_ratios(self, active: bool) -> None:
        """Nudge the hit threshold of sub / mids / highs until the hit rate is plausible."""
        window = config.HIT_RATE_WINDOW_S
        lo_lim, hi_lim = config.HIT_RATIO_LIMITS
        for name in FLUX_BANDS:
            hits = self._rate_hits[name]
            while hits and self._t - hits[0] > window:
                hits.popleft()
            if not (self.learning and active) or self._t - self._song_t0 < window:
                continue
            rate = len(hits) / window
            ratio = self._ratio[name]
            if rate > config.HIT_RATE_MAX[name]:
                ratio *= 1.06
            elif rate < config.HIT_RATE_MIN[name] and self._levels[name] > 0.35:
                ratio *= 0.97
            base = config.HIT_RATIO[name]
            self._ratio[name] = _clamp(ratio, base * lo_lim, base * hi_lim)

    # ---- layout (rebuilt when the sample rate changes)
    def _ensure_layout(self) -> None:
        sr = self.samplerate
        if self._layout_sr == sr:
            return
        fft = config.FFT_SIZE
        self._window = np.hanning(fft)
        self._mag_scale = 2.0 / self._window.sum()   # a full-scale sine -> magnitude 1.0
        self._power_scale = 1.0 / (2.0 * 1.5)        # magnitude^2 -> mean-square power (Hann noise bandwidth = 1.5 bins)
        freqs = np.fft.rfftfreq(fft, d=1.0 / sr)

        def bins(lo: float, hi: float) -> np.ndarray:
            ids = np.where((freqs >= lo) & (freqs < min(hi, sr / 2)))[0]
            if len(ids) == 0:                        # band narrower than one bin -> use the nearest bin
                ids = np.array([int(np.argmin(np.abs(freqs - (lo * hi) ** 0.5)))])
            return ids

        self._band_idx = {name: bins(*BAND_RANGES[name]) for name in BAND_NAMES}
        self._kick = KickOnset(sr)
        if self.kick_hz:
            self._kick.set_freq(self.kick_hz)
        self._learn_idx = bins(*config.KICK_LEARN_BAND)
        self._learn_freqs = freqs[self._learn_idx]
        self._kick_acc = None
        self._flux_low_idx = bins(*config.FLUX_LOW_BAND)
        self._flux_high_idx = bins(*config.FLUX_HIGH_BAND)
        self._total_idx = bins(20, 20000)
        self._prev_lm = None

        # fine-spectrum bars: log-spaced inside each band, laid out band after band (low to high)
        self._bar_idx, self._bar_slices, tilt = [], {}, []
        for name in BAND_NAMES:
            lo, hi = BAND_RANGES[name]
            count = config.BAND_BARS[name]
            edges = np.geomspace(lo, hi, count + 1)
            start = len(self._bar_idx)
            for i in range(count):
                self._bar_idx.append(bins(edges[i], edges[i + 1]))
                tilt.append(config.SPECTRUM_TILT_DB_PER_OCT * math.log2(math.sqrt(edges[i] * edges[i + 1]) / 1000.0))
            self._bar_slices[name] = slice(start, len(self._bar_idx))
        self._bar_tilt = np.array(tilt)
        self._bar_levels = np.zeros(len(self._bar_idx))
        self._buf = np.zeros(fft, dtype=np.float32)
        self._layout_sr = sr

    # ---- main entry
    def process(self, samples: np.ndarray, samplerate: int) -> Features:
        self.samplerate = int(samplerate)
        self._ensure_layout()
        while self._requests:
            kind, arg = self._requests.pop(0)
            if kind == "song":
                self._begin_song(arg)
            elif kind == "tempo":
                self.tempo.reset()
                self._was_locked = False
                self._bar_reset = True
        n = len(samples)
        dt = n / self.samplerate
        self._t += dt
        scale = n / config.REF_BLOCK_SIZE
        smooth = self.smoothing ** scale
        samples = samples.astype(np.float32, copy=False)

        fft = config.FFT_SIZE
        if n >= fft:
            self._buf = samples[-fft:].copy()
        else:
            self._buf = np.concatenate((self._buf[n:], samples))
        mag = np.abs(np.fft.rfft(self._buf * self._window)) * self._mag_scale
        power = mag * mag * self._power_scale        # mean-square power per bin
        decay = config.AGC_DECAY_DB_PER_S * dt
        gamma = 1.0 / max(self.gain, 0.05)           # sensitivity: >1 lifts quiet parts, <1 suppresses them

        # --- band levels: band RMS in dB, each band normalised to its own recent peak
        for name, idx in self._band_idx.items():
            db = 10.0 * math.log10(float(power[idx].sum()) + 1e-12)
            ref = max(db, self._band_ref[name] - decay)
            self._band_ref[name] = ref
            top = max(ref, config.AGC_MIN_REF_BAND_DB)
            level = _clamp((db - (top - config.DB_RANGE_BAND)) / config.DB_RANGE_BAND, 0.0, 1.0) ** gamma
            prev = self._levels[name]
            self._levels[name] = level if level > prev else smooth * prev + (1 - smooth) * level

        # --- fine spectrum bars (display): one shared reference so the spectrum keeps its shape
        bar_db = np.array([10.0 * math.log10(float(power[idx].sum()) + 1e-12) for idx in self._bar_idx]) + self._bar_tilt
        self._bars_ref = max(float(bar_db.max()), self._bars_ref - decay)
        top = max(self._bars_ref, config.AGC_MIN_REF_BARS_DB)
        shown = np.clip((bar_db - (top - config.DB_RANGE_BARS)) / config.DB_RANGE_BARS, 0.0, 1.0) ** gamma
        self._bar_levels = np.where(shown > self._bar_levels, shown, smooth * self._bar_levels + (1 - smooth) * shown)

        # --- silence gate
        total_db = 10.0 * math.log10(float(power[self._total_idx].sum()) + 1e-12)
        self._silent_t = self._silent_t + dt if total_db < config.SILENCE_DB else 0.0
        active = self._silent_t < config.SILENCE_HOLD_S
        if self._silent_t > config.SONG_RESET_SILENCE_S:           # a pause: whatever comes next is a new song
            if not self._silence_reset:
                self._silence_reset = True
                self._begin_song(self._song_key)                   # starts over (a cached song gets its values back)
        else:
            self._silence_reset = False
        self._mask_t = max(0.0, self._mask_t - dt)

        # --- onset strength (log-compressed positive spectral flux) per band
        lm = np.log1p(config.FLUX_LOG_COMPRESSION * mag)
        if self._prev_lm is None:
            rise = np.zeros_like(lm)
        else:
            rise = np.maximum(lm - self._prev_lm, 0.0)
        self._prev_lm = lm
        flux_low = float(rise[self._flux_low_idx].sum())
        flux_high = float(rise[self._flux_high_idx].sum())

        # --- hits
        hit = {name: False for name in BAND_NAMES}
        # a locked tempo tells how far two kicks are at least apart (a bass note between two kicks is no kick)
        min_gap = config.KICK_PERIOD_GUARD * 60.0 / self.bpm if (self.tempo.locked and self.learning) else 0.0
        hit["bass"] = bool(self._kick.process(samples, min_gap)) and active
        if hit["bass"]:
            self._mask_t = config.CROSS_MASK_S
            self._kick_hits += 1
            gap = self._t - self._last_kick_t
            if 0.12 < gap < 1.6:
                self._kick_gap = gap if self._kick_gap == 0.0 else 0.75 * self._kick_gap + 0.25 * gap
            self._last_kick_t = self._t
            # only a bass hit that sits ON the beat can be the kick: an off-beat bass note (the bass line) must not
            # teach the detector its own frequency. Needs a locked tempo, so learning starts a few seconds in.
            if self.tempo.locked:
                phase = self.tempo.beat_pos % 1.0
                if min(phase, 1.0 - phase) < config.KICK_LEARN_PHASE:
                    self._learn_hits += 1
                    self._learn_ttl = config.KICK_LEARN_BLOCKS
        if self.learning and active and self._learn_ttl > 0:
            self._learn_accumulate(rise)
        for name in FLUX_BANDS:
            flux = float(rise[self._band_idx[name]].sum())
            ema = self._ema[name]
            ema = max(flux, 1e-3) if ema == 0.0 else ema
            x = flux / (ema + 1e-3)
            self._ema[name] = ema + min(1.0, dt / config.HIT_NORM_S) * (flux - ema)
            self._cool[name] = max(0.0, self._cool[name] - dt)
            masked = self._mask_t > 0.0 and name in config.CROSS_MASK_BANDS
            if (active and not masked and self._cool[name] == 0.0 and x >= self._ratio[name]
                    and self._levels[name] >= config.HIT_MIN_LEVEL):
                hit[name] = True
                self._cool[name] = config.HIT_COOLDOWN_S[name]
                self._rate_hits[name].append(self._t)
        self._rate_t += dt
        if self._rate_t >= 1.0:
            self._rate_t = 0.0
            self._tune_ratios(active)
        if self._profile_dirty and self._t - self._last_save > config.PROFILE_AUTOSAVE_S:
            self._last_save = self._t
            self.flush()

        # --- kick bookkeeping: is there a bass kick right now? (decides whether the tempo clock may adapt)
        period = 60.0 / self.bpm
        gap = max(config.KICK_GAP_MIN_S, 3.0 * period)
        after_gap = False
        if hit["bass"]:
            after_gap = self._since_kick > gap
            self._since_kick = 0.0
            self._kick_times.append(self._t)
        else:
            self._since_kick += dt
        while self._kick_times and self._t - self._kick_times[0] > 12.0:
            self._kick_times.popleft()
        if len(self._kick_times) >= config.KICK_READY_HITS:
            self._kick_ready = True
        elif self._since_kick > 20.0:
            self._kick_ready = False
        kick_present = self._since_kick < gap

        # --- tempo + beat clock
        tempo = self.tempo
        freeze = self._kick_ready and not kick_present
        tempo.update(flux_low, flux_high, dt, active, config.LOCK_HOLD_S, freeze)
        if tempo.bpm > 0:
            self._fallback_bpm = tempo.bpm
        if after_gap and tempo.locked:
            tempo.snap_to_beat()
        tick = self._advance_clock(dt, hit["bass"], active)

        step = max(1, n // config.WAVE_POINTS)
        return Features(
            dt=dt, active=active, level=dict(self._levels), hit=hit,
            bars={name: self._bar_levels[sl].copy() for name, sl in self._bar_slices.items()},
            waveform=np.clip(samples[::step], -1.0, 1.0),
            bpm=self.bpm, tempo_known=tempo.bpm > 0, locked=tempo.locked,
            beat_tick=tick, beat_index=self._idx - self._origin, total_db=total_db,
            kick_hz=self.kick_hz, kick_interval=self._kick_gap if self._t - self._last_kick_t < 3.0 else 0.0,
            kick_learned=self.kick_hz > 0 and self._learn_hits >= config.KICK_LEARN_HITS, song_key=self._song_key)

    # ---- beat clock
    def _advance_clock(self, dt: float, bass_hit: bool, active: bool) -> bool:
        tempo = self.tempo
        tick = False
        if tempo.locked:
            pos = tempo.beat_pos + config.SYNC_OFFSET_MS_DEFAULT / 1000.0 * self.bpm / 60.0
            idx = math.floor(pos)
            resync = tempo.pop_resync() or self._bar_reset
            if not self._was_locked or resync:   # (re)acquired / tempo changed: start counting beats from here
                self._idx = self._origin = idx
                self._bar_reset = False
            elif idx > self._idx:
                self._idx = idx
                tick = True
            self._pos = pos
        elif active:
            if self._bar_reset:                  # tempo re-check asked for: bar 1 starts with the next beat
                self._origin = self._idx
                self._bar_reset = False
            # no stable tempo (yet): every bass hit is a beat, in between the clock coasts at the last known BPM
            self._pos = min(self._pos + dt * self.bpm / 60.0, self._idx + 0.999)
            if bass_hit:
                self._idx += 1
                self._pos = float(self._idx)
                tick = True
        self._was_locked = tempo.locked
        return tick
