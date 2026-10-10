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

Song sections (build-up / drop) are deliberately gone. The only "structure" kept is whether
there is music at all (silence gate) and whether the bass kick is currently present (so the
tempo clock coasts through a kick-less break instead of being pulled off by hats).
"""

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

    @property
    def beat_in_bar(self) -> int:
        return self.beat_index % 4

    @property
    def bar(self) -> int:
        return self.beat_index // 4


class KickOnset:
    """Fast kick / bass onset detector working on the raw samples (no FFT, so no window lag).

    Low-pass (box filter, ~110 Hz) -> RMS of short slices -> a slice counts as a hit when it is clearly above
    the ~1 s running average and clearly above the slice before it."""

    def __init__(self, samplerate: int):
        self.taps = max(8, round(config.KICK_LOWPASS_S * samplerate))
        self.slice = max(16, round(config.KICK_SLICE_S * samplerate))
        self.alpha = self.slice / (config.KICK_HISTORY_S * samplerate)
        self.cooldown_slices = max(1, round(config.KICK_COOLDOWN_S * samplerate / self.slice))
        self._tail = np.zeros(self.taps - 1, np.float32)
        self._avg = 0.0
        self._prev = 0.0
        self._cool = 0

    def process(self, samples: np.ndarray) -> bool:
        z = np.concatenate((self._tail, samples.astype(np.float32)))
        self._tail = z[-(self.taps - 1):]
        c = np.cumsum(np.concatenate(([0.0], z)), dtype=np.float64)
        y = (c[self.taps:] - c[:-self.taps]) / self.taps       # moving average = low-pass, same length as samples
        hit = False
        for k in range(0, len(y) - self.slice + 1, self.slice):
            e = float(np.sqrt(np.mean(y[k:k + self.slice] ** 2)))
            if self._cool > 0:
                self._cool -= 1
            if (self._cool == 0 and self._avg > 0 and e > self._avg * config.KICK_RATIO
                    and e > config.BEAT_MIN_RMS and e > self._prev * config.KICK_RISE):
                hit = True
                self._cool = self.cooldown_slices
            self._avg = e if self._avg == 0 else self._avg + self.alpha * (e - self._avg)
            self._prev = e
        return hit


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
        Used when the kick comes back after a break."""
        late = (config.ODF_LATENCY_FRAMES + extra_lag_frames) * self.bpm / 60.0 / self.frame_rate
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

    # ---- state
    @property
    def bpm(self) -> float:
        return self.tempo.bpm if self.tempo.bpm > 0 else self._fallback_bpm

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
        hit["bass"] = bool(self._kick.process(samples)) and active
        for name in FLUX_BANDS:
            flux = float(rise[self._band_idx[name]].sum())
            ema = self._ema[name]
            ema = max(flux, 1e-3) if ema == 0.0 else ema
            x = flux / (ema + 1e-3)
            self._ema[name] = ema + min(1.0, dt / config.HIT_NORM_S) * (flux - ema)
            self._cool[name] = max(0.0, self._cool[name] - dt)
            if (active and self._cool[name] == 0.0 and x >= config.HIT_RATIO[name]
                    and self._levels[name] >= config.HIT_MIN_LEVEL):
                hit[name] = True
                self._cool[name] = config.HIT_COOLDOWN_S[name]

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
            beat_tick=tick, beat_index=self._idx - self._origin, total_db=total_db)

    # ---- beat clock
    def _advance_clock(self, dt: float, bass_hit: bool, active: bool) -> bool:
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
            # no stable tempo (yet): every bass hit is a beat, in between the clock coasts at the last known BPM
            self._pos = min(self._pos + dt * self.bpm / 60.0, self._idx + 0.999)
            if bass_hit:
                self._idx += 1
                self._pos = float(self._idx)
                tick = True
        self._was_locked = tempo.locked
        return tick
