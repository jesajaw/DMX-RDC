"""
Music Mode
==========

A standalone window (MusicModeWindow) that analyzes the system's audio
output (loopback of the current playback device) in real time, visualizes it
as a spectrum ring/bars around the cover + oscilloscope, drives the DMX channels from it through the
LightEngine (lightengine.py: tempo / beat grid / song sections, simple "when -> do" looks) and optionally shows the title/artist/cover art of the track
currently playing.

The main window (DMXUI) is hidden while this is open (root.withdraw()) and
only keeps acting as the backend: the connection and send loop keep running
unchanged, channel values are set through the callbacks passed in by the
main window.

Dependencies:
    pip install numpy pillow
    # Windows:
    pip install PyAudioWPatch pywin32   # pywin32 is optional, only used as a
                                         # fallback for title/artist (see below)
    # Linux:
    pip install pyaudio jeepney         # jeepney is optional, only used for
                                         # title/artist/cover via MPRIS

Platform notes:
- Windows: loopback audio works out of the box (WASAPI loopback of the
  default playback device, via PyAudioWPatch/PortAudio).
- Linux: uses the PulseAudio/PipeWire "Monitor of <sink>" source (via plain
  PyAudio/PortAudio) -- works as long as PulseAudio, or PipeWire with its
  PulseAudio compatibility layer, is running. This is groundwork/best-effort:
  I couldn't test it against a real PulseAudio/PipeWire setup myself, so if
  device detection fails, check `pactl list sources short` for the exact
  monitor source name and adjust _resolve_loopback_device_linux if needed.
- macOS: neither path applies; AudioAnalyzer.start() reports an error via
  on_frame(AudioFrame(error=...)), the rest of the UI stays usable.

Title/artist/album/cover art:
- Windows, tried in priority order:
  1. NowPlayingBridge.exe (see src/Program.cs), if it's been built. Compiled
     C# with real compiler-level WinRT/await support -- provides title,
     artist, album AND cover art (the same source behind the Windows volume
     flyout preview). Needs a one-time .NET SDK build step (see README).
  2. NowPlayingBridge.ps1 (see src/NowPlayingBridge.ps1), a small PowerShell
     script that needs NO install/build step -- ships with every Windows
     install and has built-in support for loading WinRT types. Provides
     title, artist and album, but deliberately no cover art: reading a WinRT
     stream's raw bytes via PowerShell's late-bound COM dispatch turned out
     to be unreliable in practice (see that script's own docstring for the
     full story -- several different workarounds were tried and each hit a
     different symptom of the same underlying type-erasure problem).
  3. A plain window-title heuristic over known player processes
     (config.KNOWN_PLAYER_PROCESSES, e.g. "Artist - Title" for Spotify)
     -- title/artist only, no album, no cover.
  Python itself never speaks COM/WinRT for any of this -- it just launches
  whichever bridge is available as a subprocess and reads its JSON/cover
  output files, or (for option 3) reads window titles via pywin32. If a
  bridge (1 or 2) doesn't produce output within a few seconds, NowPlayingReader
  automatically falls back further down this list.
- Linux: queries MPRIS (the freedesktop.org media-player D-Bus standard) via
  `jeepney`, a pure-Python D-Bus library. Most Linux media players (browsers,
  VLC, Spotify, most desktop players) implement MPRIS, so this tends to have
  broader coverage than the Windows window-title fallback. Cover art comes
  through as a `file://` or `http(s)://` URL (`mpris:artUrl`); this is also
  groundwork/best-effort, since I couldn't test it against a real D-Bus
  session -- if the exact property/message shapes turn out to differ, treat
  _fetch_mpris_metadata as the place to adjust.
- Without a cover, the disc shows a small rotating pixel-art animation
  instead of an empty circle.

Threading model:
- Audio capture does NOT run in its own Python thread: PyAudio/PortAudio
  calls AudioAnalyzer._audio_callback directly from its own native audio
  thread whenever a block is ready.
- Now-playing lookup runs in its own daemon thread (NowPlayingReader._loop),
  which either polls NowPlayingBridge.ps1's output files, scans window
  titles, or queries MPRIS, depending on platform/availability.
- Results are never pushed into Tkinter directly; they go through callbacks,
  and MusicModeWindow marshals them back onto the GUI thread via
  self.after(0, ...).
"""

import io
import json
import logging
import math
import os
import subprocess
import sys
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from tkinter import ttk

import numpy as np

from . import config
from . import lightengine, theme
from .controller import apply_dark_titlebar
from .lightengine import BREAK, BUILD, DROP, SECTION_LABELS, SILENCE, LightEngine

_IS_WINDOWS = sys.platform == "win32"

if _IS_WINDOWS:
    import pyaudiowpatch as pyaudio
else:
    import pyaudio

try:
    from PIL import Image, ImageDraw, ImageTk
    _PIL_AVAILABLE = True
except Exception:
    _PIL_AVAILABLE = False

# Windows fallback: reading the window title of a known player process
try:
    if _IS_WINDOWS:
        import win32gui
        import win32process
        import win32api
        import win32con
        _WIN32_AVAILABLE = True
    else:
        _WIN32_AVAILABLE = False
except Exception:
    _WIN32_AVAILABLE = False

# Linux: MPRIS over D-Bus via jeepney (pure Python, no system dev packages needed)
try:
    if not _IS_WINDOWS:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
        _DBUS_AVAILABLE = True
    else:
        _DBUS_AVAILABLE = False
except Exception:
    _DBUS_AVAILABLE = False


@dataclass
class AudioFrame:
    """One analysis result for a single audio block."""
    bass: float = 0.0
    mid: float = 0.0
    treble: float = 0.0
    beat: float = 0.0
    pitch: float = 0.0
    bars: np.ndarray = None
    waveform: np.ndarray = None
    error: Exception = None
    # Rhythm / structure features, consumed by the LightEngine (lightengine.py)
    dt: float = 0.0                  # seconds of audio this frame covers
    kick_hit: bool = False           # a kick onset was detected in this block (the trigger behind `beat`)
    flux_low: float = 0.0            # onset strength in the kick/bass region (log-compressed spectral flux)
    flux_high: float = 0.0           # the same for the hi-hat region
    bass_db: float = -120.0          # raw band levels in dB (before auto-gain), used for song-structure detection
    mid_db: float = -120.0
    treble_db: float = -120.0
    total_db: float = -120.0


class AudioAnalyzer:
    """Captures system loopback audio and computes several live metrics from it:
    - bass/mid/treble: smoothed energy in three frequency bands (0..1)
    - beat: a short, decaying pulse on sudden energy spikes (simple onset detection;
      BPM tracking is done on top of this by the LightEngine, from the flux values below)
    - pitch: normalized spectral centroid (0 = dull/bassy, 1 = bright/high-frequency) --
      better suited to continuous rotation/speed parameters than a plain band energy
    - bars: spectrum split into log-spaced bands, for the bar display
    - waveform: a short slice of the raw samples, for the oscilloscope display
    - dt / kick_hit / flux_low / flux_high / *_db: raw rhythm and level features for the LightEngine

    On Windows, uses PyAudioWPatch (a dedicated WASAPI-loopback fork of
    PyAudio). On Linux, uses plain PyAudio against the PulseAudio/PipeWire
    "Monitor of <sink>" source, which PortAudio exposes as an ordinary input
    device -- no special loopback flag is needed there. Either way,
    PyAudio/PortAudio calls _audio_callback directly from its own native
    audio thread once a block is ready, so no dedicated threading.Thread is
    needed here.
    """

    def __init__(self, on_frame, blocksize: int = config.BLOCK_SIZE,
                 gain: float = 1.0, smoothing: float = 0.6, n_bars: int = config.N_BARS):
        self.on_frame = on_frame            # callback(frame: AudioFrame)
        self.blocksize = blocksize
        self.gain = gain                    # sensitivity, adjustable live (>1 boosts quiet parts, <1 suppresses them)
        self.smoothing = smoothing          # 0..~0.95, higher = slower fall-off (rises are always instant)
        self.n_bars = n_bars
        self.samplerate = config.SAMPLE_RATE  # placeholder, replaced in start() by the real device
        self._channels = 2                  # placeholder, replaced in start() by the real device

        self._running = False
        self._pa = None
        self._stream = None
        self._levels = {"bass": 0.0, "mid": 0.0, "treble": 0.0}
        self._bar_levels = np.zeros(n_bars)
        self._bar_edges = np.geomspace(config.BAR_FREQ_RANGE[0], config.BAR_FREQ_RANGE[1], n_bars + 1)
        self._energy_history = deque(maxlen=config.ENERGY_HISTORY_LEN)
        self._beat_level = 0.0
        self._beat_cooldown = 0
        self._pitch_level = 0.5
        self._pitch_lo = 0.4
        self._pitch_hi = 0.6
        self._band_ref = {name: -120.0 for name in config.BAND_RANGES}  # loudest recent dB per band (auto-gain)
        self._bars_ref = -120.0
        self._prev_lm = None                # previous log-magnitude spectrum, for the spectral flux
        self._layout_sr = None              # sample rate the FFT index tables below were built for
        self._buf = None
        self._error_logged = False

    def start(self) -> None:
        if self._running:
            return
        try:
            self._pa = pyaudio.PyAudio()
            device = self._resolve_loopback_device(self._pa)
            self.samplerate = int(device["defaultSampleRate"])
            self._channels = device["maxInputChannels"]
            self._stream = self._pa.open(
                format=pyaudio.paFloat32,
                channels=self._channels,
                rate=self.samplerate,
                frames_per_buffer=self.blocksize,
                input=True,
                input_device_index=device["index"],
                stream_callback=self._audio_callback,
            )
            self._stream.start_stream()
        except Exception as e:
            self._cleanup()
            self.on_frame(AudioFrame(error=e))
            return
        self._running = True

    def stop(self) -> None:
        self._running = False
        self._cleanup()

    def _cleanup(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

    @staticmethod
    def _resolve_loopback_device(p) -> dict:
        if _IS_WINDOWS:
            try:
                wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
            except OSError as e:
                raise RuntimeError("WASAPI is not available on this system") from e

            default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
            if default_speakers["isLoopbackDevice"]:
                return default_speakers

            for loopback in p.get_loopback_device_info_generator():
                if default_speakers["name"] in loopback["name"]:
                    return loopback

            raise RuntimeError("No matching WASAPI loopback device found")

        # Linux: PortAudio's PulseAudio/PipeWire host API exposes the "Monitor
        # of <sink>" source as a completely ordinary input device -- no
        # special loopback flag needed, just find it by name.
        for i in range(p.get_device_count()):
            info = p.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0 and "monitor" in info.get("name", "").lower():
                return info

        raise RuntimeError(
            "No PulseAudio/PipeWire monitor source found. Make sure PulseAudio, "
            "or PipeWire with its PulseAudio compatibility layer, is running."
        )

    def _audio_callback(self, in_data, frame_count, time_info, status):
        # Runs on PortAudio's own native thread, not a thread we started ourselves
        samples = np.frombuffer(in_data, dtype=np.float32)
        if self._channels > 1:
            samples = samples.reshape(-1, self._channels).mean(axis=1)
        try:
            self._process(samples)
        except Exception:
            if not self._error_logged:      # log once, never spam from the audio thread
                self._error_logged = True
                logging.exception("Audio analysis failed")
        return (None, pyaudio.paContinue)

    # ---- analysis helpers
    def _ensure_layout(self) -> None:
        """(Re)builds FFT window and bin-index tables when the sample rate is known/changed."""
        sr = self.samplerate
        if self._layout_sr == sr:
            return
        fft = config.FFT_SIZE
        self._window = np.hanning(fft)
        self._mag_scale = 2.0 / self._window.sum()   # a full-scale sine -> magnitude 1.0
        self._power_scale = 1.0 / (2.0 * 1.5)        # magnitude^2 -> mean-square power (Hann noise bandwidth = 1.5 bins)
        freqs = np.fft.rfftfreq(fft, d=1.0 / sr)

        def bins(lo: float, hi: float) -> np.ndarray:
            ids = np.where((freqs >= lo) & (freqs < hi))[0]
            if len(ids) == 0:                        # band narrower than one bin -> use the nearest bin
                ids = np.array([int(np.argmin(np.abs(freqs - (lo * hi) ** 0.5)))])
            return ids

        self._band_idx = {name: bins(lo, hi) for name, (lo, hi) in config.BAND_RANGES.items()}
        self._kick_idx = bins(*config.BEAT_BAND)
        self._flux_low_idx = bins(*config.FLUX_LOW_BAND)
        self._flux_high_idx = bins(*config.FLUX_HIGH_BAND)
        self._total_idx = bins(20, config.BAR_FREQ_RANGE[1])
        self._prev_lm = None
        edges = self._bar_edges
        self._bar_idx = [bins(edges[i], edges[i + 1]) for i in range(self.n_bars)]
        centers = np.sqrt(edges[:-1] * edges[1:])
        self._bar_tilt = config.SPECTRUM_TILT_DB_PER_OCT * np.log2(centers / 1000.0)
        self._bar_pos = np.arange(self.n_bars) / max(1, self.n_bars - 1)
        self._buf = np.zeros(fft, dtype=np.float32)
        self._layout_sr = sr

    @staticmethod
    def _norm(db: float, top_db: float, range_db: float) -> float:
        """Maps [top-range .. top] dB linearly to 0..1."""
        return float(min(1.0, max(0.0, (db - (top_db - range_db)) / range_db)))

    def _fall(self, previous: float, new: float) -> float:
        """Instant attack, smoothed release."""
        return new if new > previous else self.smoothing * previous + (1 - self.smoothing) * new

    def _process(self, samples: np.ndarray) -> None:
        self._ensure_layout()
        n = len(samples)
        fft = config.FFT_SIZE
        if n >= fft:
            self._buf = samples[-fft:].astype(np.float32)
        else:
            self._buf = np.concatenate((self._buf[n:], samples.astype(np.float32)))

        mag = np.abs(np.fft.rfft(self._buf * self._window)) * self._mag_scale
        power = mag * mag * self._power_scale      # mean-square power per bin
        decay = config.AGC_DECAY_DB_PER_S * n / self.samplerate
        gamma = 1.0 / max(self.gain, 0.05)         # sensitivity: >1 lifts quiet parts, <1 suppresses them

        # --- bass/mid/treble: band RMS in dB, each band normalised to its own recent peak
        band_db = {}
        for name, idx in self._band_idx.items():
            db = 10.0 * math.log10(float(power[idx].sum()) + 1e-12)
            band_db[name] = db
            ref = max(db, self._band_ref[name] - decay)
            self._band_ref[name] = ref
            level = self._norm(db, max(ref, config.AGC_MIN_REF_BAND_DB), config.DB_RANGE_BAND) ** gamma
            self._levels[name] = self._fall(self._levels[name], level)

        # --- spectrum bars: one shared reference so the spectrum keeps its shape
        bar_db = np.array([10.0 * math.log10(float(power[idx].sum()) + 1e-12) for idx in self._bar_idx])
        bar_db = bar_db + self._bar_tilt
        self._bars_ref = max(float(bar_db.max()), self._bars_ref - decay)
        top = max(self._bars_ref, config.AGC_MIN_REF_BARS_DB)
        raw = np.clip((bar_db - (top - config.DB_RANGE_BARS)) / config.DB_RANGE_BARS, 0.0, 1.0)
        shown = raw ** gamma
        self._bar_levels = np.where(shown > self._bar_levels, shown,
                                    self.smoothing * self._bar_levels + (1 - self.smoothing) * shown)

        # --- beat: kick-band onset vs. its ~1s average, with a short dead time
        kick = math.sqrt(float(power[self._kick_idx].sum()))
        avg = float(np.mean(self._energy_history)) if self._energy_history else 0.0
        self._energy_history.append(kick)
        if self._beat_cooldown > 0:
            self._beat_cooldown -= 1
        onset = (self._beat_cooldown == 0 and avg > 0
                 and kick > avg * config.BEAT_THRESHOLD_RATIO
                 and kick > config.BEAT_MIN_RMS)
        if onset:
            self._beat_level = 1.0
            self._beat_cooldown = config.BEAT_COOLDOWN_BLOCKS
        else:
            self._beat_level *= config.BEAT_DECAY

        # --- pitch: centroid over the log-spaced bars (0 = lowest bar, 1 = highest),
        # stretched to the range it recently used so it actually moves across 0..1
        weights = raw * raw
        total = float(weights.sum())
        if total > 1e-3:                            # silence -> hold the last value
            centroid = float((weights * self._bar_pos).sum() / total)
            rate = config.PITCH_ADAPT_RATE
            self._pitch_lo = min(centroid, self._pitch_lo + rate)
            self._pitch_hi = max(centroid, self._pitch_hi - rate)
            lo, hi = self._pitch_lo, self._pitch_hi
            if hi - lo < config.PITCH_SPAN_MIN:
                mid = (lo + hi) / 2
                lo, hi = mid - config.PITCH_SPAN_MIN / 2, mid + config.PITCH_SPAN_MIN / 2
            target = min(1.0, max(0.0, (centroid - lo) / (hi - lo)))
            s = max(self.smoothing, config.PITCH_SMOOTHING_MIN)
            self._pitch_level = s * self._pitch_level + (1 - s) * target

        # --- raw rhythm features for the LightEngine: positive spectral flux (log-compressed magnitudes)
        lm = np.log1p(config.FLUX_LOG_COMPRESSION * mag)
        if self._prev_lm is None:
            flux_low = flux_high = 0.0
        else:
            rise = np.maximum(lm - self._prev_lm, 0.0)
            flux_low = float(rise[self._flux_low_idx].sum())
            flux_high = float(rise[self._flux_high_idx].sum())
        self._prev_lm = lm
        total_db = 10.0 * math.log10(float(power[self._total_idx].sum()) + 1e-12)

        step = max(1, n // config.WAVE_POINTS)
        waveform = np.clip(samples[::step], -1.0, 1.0)

        self.on_frame(AudioFrame(
            bass=self._levels["bass"], mid=self._levels["mid"], treble=self._levels["treble"],
            beat=self._beat_level, pitch=self._pitch_level,
            bars=self._bar_levels.copy(), waveform=waveform,
            dt=n / self.samplerate, kick_hit=bool(onset), flux_low=flux_low, flux_high=flux_high,
            bass_db=band_db["bass"], mid_db=band_db["mid"], treble_db=band_db["treble"], total_db=total_db,
        ))


# --------- Windows fallback: window-title heuristic ---------

def _get_process_name(pid: int) -> str:
    try:
        handle = win32api.OpenProcess(
            win32con.PROCESS_QUERY_INFORMATION | win32con.PROCESS_VM_READ, False, pid)
        try:
            path = win32process.GetModuleFileNameEx(handle, 0)
            return path.rsplit("\\", 1)[-1].lower()
        finally:
            win32api.CloseHandle(handle)
    except Exception:
        return ""


def _find_now_playing_title() -> str | None:
    """Scans visible top-level windows for one belonging to a known media
    player process (config.KNOWN_PLAYER_PROCESSES) and returns its window
    title, or None."""
    found = []

    def _callback(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        if not title:
            return
        try:
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
        except Exception:
            return
        if _get_process_name(pid) in config.KNOWN_PLAYER_PROCESSES:
            found.append(title)

    try:
        win32gui.EnumWindows(_callback, None)
    except Exception:
        return None
    return found[0] if found else None


def _parse_title(raw: str) -> tuple[str, str]:
    # Common format used by many players: "Artist - Title"
    if " - " in raw:
        artist, _, title = raw.partition(" - ")
        return artist.strip(), title.strip()
    return "", raw.strip()


# --------- Linux: MPRIS over D-Bus ---------

def _fetch_mpris_metadata():
    """Queries the first available MPRIS player on the session bus for its
    current Metadata (title, artist, cover art URL). Returns None if no MPRIS
    player is currently registered. Best-effort/untested against a real D-Bus
    session -- see module docstring."""
    conn = open_dbus_connection(bus="SESSION")
    try:
        bus_addr = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus",
                                interface="org.freedesktop.DBus")
        names_reply = conn.send_and_get_reply(new_method_call(bus_addr, "ListNames"))
        names = [n for n in names_reply.body[0] if n.startswith("org.mpris.MediaPlayer2.")]
        if not names:
            return None

        player_addr = DBusAddress("/org/mpris/MediaPlayer2", bus_name=names[0],
                                   interface="org.freedesktop.DBus.Properties")
        get_msg = new_method_call(player_addr, "Get", "ss",
                                   ("org.mpris.MediaPlayer2.Player", "Metadata"))
        reply = conn.send_and_get_reply(get_msg)
        metadata = reply.body[0][1]  # ("a{sv}", {...}) -> the actual dict

        title = metadata.get("xesam:title", ("s", ""))[1]
        artist_list = metadata.get("xesam:artist", ("as", []))[1]
        artist = ", ".join(artist_list) if artist_list else ""
        art_url = metadata.get("mpris:artUrl", ("s", ""))[1]
        return title, artist, art_url
    finally:
        conn.close()


def _load_art_bytes(art_url: str):
    if not art_url:
        return None
    try:
        if art_url.startswith("file://"):
            import urllib.parse
            path = urllib.parse.unquote(art_url[len("file://"):])
            return Path(path).read_bytes()
        if art_url.startswith(("http://", "https://")):
            import urllib.request
            with urllib.request.urlopen(art_url, timeout=2) as response:
                return response.read()
    except Exception:
        return None
    return None


class NowPlayingReader:
    """Provides title/artist/album/cover art of the currently playing track.

    Windows, tried in priority order:
      1. NowPlayingBridge.exe (compiled C#, see src/Program.cs) if it has been
         built -- title, artist, album AND cover art (the same source behind
         the Windows volume flyout preview). Real compiler-level WinRT/await
         support, so this is the most reliable option.
      2. NowPlayingBridge.ps1 (plain PowerShell, ships as-is, no install/build
         step) -- title, artist, album, playing state and cover art.
      3. Plain window-title heuristic over known media player processes
         (config.KNOWN_PLAYER_PROCESSES), e.g. "Artist - Title" for
         Spotify -- title/artist only, no album, no cover. Stays inactive
         without pywin32.
    If a bridge (1 or 2) doesn't produce any output within a few seconds,
    this reader gives up on it and falls back further down the list, so a
    broken/missing bridge degrades gracefully instead of silencing everything.
    Its stdout/stderr go into nowplaying_cache/bridge.log for diagnosis.

    Linux: queries MPRIS over D-Bus via `jeepney` -- title, artist and cover
    art (as a file:// or http(s):// URL). Stays inactive without jeepney.
    """

    # How many consecutive polls (roughly this many * poll_interval seconds)
    # a bridge gets to produce its first output file before this reader gives
    # up on it and falls back further down the priority list.
    BRIDGE_MISS_LIMIT = 10

    def __init__(self, on_update, poll_interval: float = 1.0, on_playing=None):
        self.on_update = on_update  # callback(title: str, artist: str, cover_bytes: bytes | None)
        self.on_playing = on_playing  # optional callback(playing: bool), bridge only
        self._last_playing = None
        self.poll_interval = poll_interval
        self._running = False
        self._thread = None
        self._process = None
        self._log_file = None
        self._last_signature = None
        self._bridge_miss_count = 0

        self._bridge_kind = None  # "exe" | "ps1" | None
        if _IS_WINDOWS:
            if config.NOWPLAYING_BRIDGE_EXE.exists():
                self._bridge_kind = "exe"
            elif config.NOWPLAYING_BRIDGE_SCRIPT.exists():
                self._bridge_kind = "ps1"
        self._use_mpris = not _IS_WINDOWS and _DBUS_AVAILABLE

    def start(self) -> None:
        if self._running:
            return
        if not (self._bridge_kind or self._use_mpris or _WIN32_AVAILABLE):
            return
        self._running = True
        if self._bridge_kind:
            self._start_bridge_process()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=2)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def _start_bridge_process(self) -> None:
        cache_dir = config.NOWPLAYING_CACHE_DIR
        cache_dir.mkdir(exist_ok=True)
        try:
            self._log_file = open(cache_dir / "bridge.log", "w", encoding="utf-8")
            if self._bridge_kind == "exe":
                args = [str(config.NOWPLAYING_BRIDGE_EXE),
                        str(cache_dir), str(int(self.poll_interval * 1000))]
            else:  # "ps1"
                args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                        "-File", str(config.NOWPLAYING_BRIDGE_SCRIPT),
                        str(cache_dir), str(int(self.poll_interval * 1000)),
                        str(os.getpid())]   # 3rd arg: bridge exits when this process is gone
                if config.NOWPLAYING_DEBUG:
                    args.append("-RunDebug")
            self._process = subprocess.Popen(
                args,
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=self._log_file, stderr=subprocess.STDOUT,
            )
        except Exception:
            logging.exception("Failed to start NowPlayingBridge (%s)", self._bridge_kind)
            self._process = None
            self._bridge_kind = None

    def _loop(self) -> None:
        while self._running:
            if self._bridge_kind:
                self._poll_bridge_files()
            elif self._use_mpris:
                self._poll_mpris()
            else:
                raw = _find_now_playing_title()
                if raw and raw != self._last_signature:
                    self._last_signature = raw
                    artist, title = _parse_title(raw)
                    self.on_update(title, artist, None)
            time.sleep(self.poll_interval)

    def _poll_bridge_files(self) -> None:
        cache_dir = config.NOWPLAYING_CACHE_DIR
        try:
            data = json.loads((cache_dir / "nowplaying.json").read_text(encoding="utf-8-sig"))
        except Exception:
            self._bridge_miss_count += 1
            if self._bridge_miss_count >= self.BRIDGE_MISS_LIMIT:
                logging.warning(
                    "NowPlayingBridge (%s) produced no output after %d attempts -- "
                    "falling back further. Check %s for errors.",
                    self._bridge_kind, self._bridge_miss_count, cache_dir / "bridge.log",
                )
                self._bridge_kind = None
                if self._process is not None:
                    try:
                        self._process.terminate()
                    except Exception:
                        pass
                    self._process = None
            return
        self._bridge_miss_count = 0

        playing = bool(data.get("playing", True))  # the .exe doesn't report it -> assume playing
        if playing != self._last_playing:
            self._last_playing = playing
            if self.on_playing:
                self.on_playing(playing)

        title = data.get("title", "")
        artist = data.get("artist", "")
        has_cover = bool(data.get("hasCover", False))
        signature = (title, artist, has_cover)
        if signature == self._last_signature:
            return
        self._last_signature = signature

        cover_bytes = None
        if has_cover:
            try:
                cover_bytes = (cache_dir / "nowplaying_cover.img").read_bytes()
            except Exception:
                logging.warning("hasCover is true but nowplaying_cover.img can't be read")
                cover_bytes = None

        self.on_update(title, artist, cover_bytes)

    def _poll_mpris(self) -> None:
        try:
            metadata = _fetch_mpris_metadata()
        except Exception:
            return
        if metadata is None:
            return

        title, artist, art_url = metadata
        signature = (title, artist, art_url)
        if signature == self._last_signature:
            return
        self._last_signature = signature

        cover_bytes = _load_art_bytes(art_url) if art_url else None
        self.on_update(title, artist, cover_bytes)


class MusicModeWindow(tk.Toplevel):
    """Music Mode window. Left: the cover disc with the spectrum around it (ring or bars) and the waveform.
    Right: what the lights do -- a few simple rules ("on every bass hit -> LED blinks + strobe"), two colours
    to switch between, and a handful of sliders.

    Channel 1 (show select) is written once with 0 when the window opens and is never touched again, so the
    fixture stays in per-channel mode whatever is set here."""

    def __init__(self, parent: tk.Tk, channel_names: dict, set_channel_value,
                 restore_sliders, on_closed, colors: dict, is_connected=None):
        """
        channel_names:     {channel_nr: "1: Show Select", ...}
        set_channel_value: callback(channel: int, value: int) -> None
        restore_sliders:   callback(channels: list[int]) -> None
        on_closed:         callback() -> None, called when this window closes
                            (the main window should show itself again then)
        colors:             dict with the keys of config.ACTIVE_SCHEME
        is_connected:       optional callback() -> bool, whether the DMX adapter is connected (status line)
        """
        super().__init__(parent)
        self.title("Music Mode")
        self.colors = colors
        apply_dark_titlebar(self)
        self.configure(bg=colors["BG"])
        theme.apply_theme(self)

        self.channel_names = channel_names
        self.set_channel_value = set_channel_value
        self.restore_sliders = restore_sliders
        self.on_closed = on_closed
        self.is_connected = is_connected

        self.analyzer = AudioAnalyzer(on_frame=self._on_frame)
        self.now_playing = None
        self.look = lightengine.DEFAULT.copy()
        self.engine = LightEngine(self.look)
        self.rule_vars = {}              # (trigger, action) -> BooleanVar
        self._loading = False            # True while a look is written into the widgets
        self._status_cache = {}
        self._active_channels = set()
        self._last_bars = None
        self._peaks = [0.0] * config.N_BARS
        self._pulse = 0.0
        self._wave_w = config.VIZ_MIN_SIZE

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # the window is as big as its content needs, but can grow (and everything scales with it)
        self.update_idletasks()
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(self.winfo_reqwidth(), screen_w - 40)
        height = min(self.winfo_reqheight(), screen_h - 90)
        self.minsize(width, height)
        self.geometry(f"{max(width, 1240)}x{height}")

        self._release_manual_channel()
        self.analyzer.start()

    def _release_manual_channel(self) -> None:
        """Channel 1 = 0 (per-channel control) once, then hands off for good."""
        self.set_channel_value(1, 0)
        self.restore_sliders([1])

    # --------- Layout
    def _build(self) -> None:
        colors = self.colors
        self.columnconfigure(0, weight=3, minsize=480)
        self.columnconfigure(1, weight=2, minsize=500)
        self.rowconfigure(0, weight=1)

        left = ttk.Frame(self)
        left.grid(row=0, column=0, sticky="nsew", padx=(16, 8), pady=(16, 8))
        left.columnconfigure(0, weight=1)
        left.rowconfigure(1, weight=1)
        self._build_visual(left)

        right = ttk.Frame(self)
        right.grid(row=0, column=1, sticky="nsew", padx=(8, 16), pady=(16, 8))
        right.columnconfigure(0, weight=1)
        self._build_live(right, 0)
        self._build_looks(right, 1)
        self._build_rules(right, 2)
        self._build_colours(right, 3)
        self._build_tuning(right, 4)
        self._load_look(self.look.name)

        footer = ttk.Frame(self)
        footer.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(4, 16))
        self.dmx_label = ttk.Label(footer, text="", style="Muted.TLabel")
        self.dmx_label.pack(side="left", padx=(0, 18))
        self.status_label = ttk.Label(footer, text="", foreground="#c0392b")
        self.status_label.pack(side="left")
        ttk.Button(footer, text="Back to Manual Control", command=self._on_close).pack(side="right")
        self.blackout_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(footer, text="\u23fb  Blackout", style="Chip.Toolbutton", variable=self.blackout_var,
                        command=self._on_blackout).pack(side="right", padx=(0, 10))

    def _card(self, parent, title: str, row: int, pady=(0, 10)) -> ttk.Frame:
        outer = ttk.Frame(parent, style="Card.TFrame", padding=(14, 10))
        outer.grid(row=row, column=0, sticky="ew", pady=pady)
        outer.columnconfigure(0, weight=1)
        ttk.Label(outer, text=title.upper(), style="CardTitle.TLabel").grid(row=0, column=0, sticky="w", pady=(0, 8))
        body = ttk.Frame(outer, style="Plain.Card.TFrame")
        body.grid(row=1, column=0, sticky="ew")
        return body

    # --------- Left side: now playing + spectrum around the cover + waveform
    def _build_visual(self, parent: ttk.Frame) -> None:
        colors = self.colors
        head = ttk.Frame(parent)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        head.columnconfigure(0, weight=1)
        titles = ttk.Frame(head)
        titles.grid(row=0, column=0, sticky="w")
        self.track_label = ttk.Label(titles, text="", style="Track.TLabel", wraplength=480)
        self.track_label.pack(anchor="w")
        self.artist_label = ttk.Label(titles, text="", style="Artist.TLabel", wraplength=480)
        self.artist_label.pack(anchor="w")
        modes = ttk.Frame(head)
        modes.grid(row=0, column=1, sticky="e")
        self.viz_mode = tk.StringVar(value="ring")
        for value, text in (("ring", "Ring"), ("bars", "Bars")):
            ttk.Radiobutton(modes, text=text, value=value, variable=self.viz_mode, style="Chip.Toolbutton",
                            command=self._layout_viz).pack(side="left", padx=(6, 0))

        size = config.VIZ_MIN_SIZE
        self.disc_canvas = tk.Canvas(parent, width=size, height=size, bg=colors["BG_LIGHT"],
                                     highlightthickness=1, highlightbackground=colors["LINE"])
        self.disc_canvas.grid(row=1, column=0, sticky="nsew")
        canvas = self.disc_canvas

        # spectrum lines: created first so the disc is drawn on top. 2*N slots (ring is mirrored left/right);
        # bars mode only uses the first N.
        n = config.N_BARS
        self._slots = 2 * n
        self._bar_ids, self._peak_ids = [], []
        for k in range(self._slots):
            idx = k if k < n else self._slots - 1 - k
            colour = theme.lerp_colour(colors["ACCENT"], colors["ACCENT2"], idx / max(1, n - 1))
            self._bar_ids.append(canvas.create_line(0, 0, 0, 0, fill=colour, width=3, capstyle="butt"))
            self._peak_ids.append(canvas.create_line(0, 0, 0, 0, fill=colors["FG"], width=3, capstyle="butt"))
        self._dirs = [(math.sin((k + 0.5) * 2 * math.pi / self._slots),
                       math.cos((k + 0.5) * 2 * math.pi / self._slots)) for k in range(self._slots)]

        # the disc: rings, rotating pixel dots (fallback while there is no cover), centre dot, cover on top
        r_disc = config.DISC_SIZE / 2
        self._disc_radii = [r_disc - 4] + list(range(int(r_disc) - 8, 24, -12))
        self._ring_ovals = []
        for i, radius in enumerate(self._disc_radii):
            self._ring_ovals.append(canvas.create_oval(0, 0, 0, 0, outline=colors["ACCENT_DARK"],
                                                       width=2 if i == 0 else 1))
        self._pixel_ids = []
        for i in range(config.PIXEL_DOT_COUNT):
            colour = colors["ACCENT"] if i % 2 == 0 else colors["ACCENT_DARK"]
            self._pixel_ids.append(canvas.create_rectangle(0, 0, 0, 0, fill=colour, outline=""))
        self._center_dot = canvas.create_oval(0, 0, 0, 0, fill=colors["FG"], outline="")

        # The cover image sits last in the draw order -> automatically covers the pixel dots once a cover
        # is actually set (image=None draws nothing)
        self._cover_image_item = canvas.create_image(size / 2, size / 2, image=None)
        self._cover_photo = None   # keep a reference, or Tkinter garbage-collects the image
        self._cover_frames = []    # pre-rotated PhotoImages, built once per cover (main thread only)
        self._cover_key = None     # identifies the cover the frames were built from
        self._cover_frame_idx = -1
        self._disc_angle = 0.0
        self._playing = True       # disc only spins while music is playing

        self._geo = {}
        canvas.bind("<Configure>", lambda e: self._layout_viz())

        self.wave_canvas = tk.Canvas(parent, height=config.WAVE_CANVAS_HEIGHT, bg=colors["BG_LIGHT"],
                                     highlightthickness=1, highlightbackground=colors["LINE"])
        self.wave_canvas.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self._wave_glow = self.wave_canvas.create_line(0, 0, 0, 0, fill=colors["ACCENT_DARK"], width=5, smooth=True)
        self._wave_line = self.wave_canvas.create_line(0, 0, 0, 0, fill=colors["ACCENT2"], width=1.5, smooth=True)
        self._wave_mid = self.wave_canvas.create_line(0, 0, 0, 0, fill=colors["LINE"])
        self.wave_canvas.bind("<Configure>", self._on_wave_resize)

        have_now_playing_source = (config.NOWPLAYING_BRIDGE_EXE.exists()
                                    or config.NOWPLAYING_BRIDGE_SCRIPT.exists()
                                    or _WIN32_AVAILABLE or _DBUS_AVAILABLE)
        if not have_now_playing_source:
            self.track_label.config(text="(no title source available)")
        else:
            self.now_playing = NowPlayingReader(on_update=self._on_now_playing,
                                             on_playing=self._on_playing)
            self.now_playing.start()

        self._layout_viz()
        self._spin_disc()

    def _on_wave_resize(self, event) -> None:
        self._wave_w = max(10, event.width)
        mid = event.height / 2
        self.wave_canvas.coords(self._wave_mid, 0, mid, self._wave_w, mid)

    def _layout_viz(self) -> None:
        """(Re)computes the geometry of the disc and the spectrum for the current canvas size and mode."""
        canvas = self.disc_canvas
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 60 or h < 60:
            w = h = config.VIZ_MIN_SIZE
        r_disc = config.DISC_SIZE / 2
        ring = self.viz_mode.get() == "ring"
        n = config.N_BARS
        if ring:
            cx, cy = w / 2, h / 2
            r_in = r_disc + config.RING_GAP
            max_len = max(18.0, min(w, h) / 2 - r_in - 12)
            width = max(2.0, 2 * math.pi * (r_in + 8) / self._slots * 0.55)
            geo = dict(ring=True, cx=cx, cy=cy, r_in=r_in, max_len=max_len)
        else:
            bars_h = max(100.0, h * 0.32)
            cx, cy = w / 2, max(r_disc + 10, (h - bars_h) / 2)
            slot = max(2.0, (w - 28) / n)
            width = max(2.0, slot * 0.68)
            geo = dict(ring=False, cx=cx, cy=cy, base_y=h - 10, max_len=max(20.0, bars_h - 24),
                       x0=14, slot=slot)
        self._geo = geo

        for item, radius in zip(self._ring_ovals, self._disc_radii):
            canvas.coords(item, cx - radius, cy - radius, cx + radius, cy + radius)
        canvas.coords(self._center_dot, cx - 5, cy - 5, cx + 5, cy + 5)
        canvas.coords(self._cover_image_item, cx, cy)
        for k in range(self._slots):
            visible = ring or k < n
            state = "normal" if visible else "hidden"
            canvas.itemconfig(self._bar_ids[k], width=width, state=state)
            canvas.itemconfig(self._peak_ids[k], width=width, state=state)

        # bars mode: LED-style segments (thin lines in the canvas colour across the bars)
        canvas.delete("seg")
        if not ring:
            y = geo["base_y"]
            while y > geo["base_y"] - geo["max_len"] - 6:
                canvas.create_line(0, y, w, y, fill=self.colors["BG_LIGHT"], width=2, tags="seg")
                y -= 6
            canvas.tag_lower("seg", self._ring_ovals[0])
        if self._last_bars is not None:
            self._draw_bars()

    def _spin_disc(self) -> None:
        if not self.winfo_exists():
            return
        if self._playing:
            self._disc_angle = (self._disc_angle + config.SPIN_STEP_DEG) % 360
        cx, cy = self._geo.get("cx", config.VIZ_MIN_SIZE / 2), self._geo.get("cy", config.VIZ_MIN_SIZE / 2)
        count = len(self._pixel_ids)
        for i, dot_id in enumerate(self._pixel_ids):
            angle = math.radians(self._disc_angle + i * (360 / count))
            x = cx + config.PIXEL_DOT_RADIUS * math.cos(angle)
            y = cy + config.PIXEL_DOT_RADIUS * math.sin(angle)
            half = config.PIXEL_DOT_SIZE / 2
            self.disc_canvas.coords(dot_id, x - half, y - half, x + half, y + half)

        if self._cover_frames:
            idx = int(self._disc_angle // config.SPIN_STEP_DEG) % len(self._cover_frames)
            if idx != self._cover_frame_idx:
                self._cover_frame_idx = idx
                self.disc_canvas.itemconfig(self._cover_image_item, image=self._cover_frames[idx])

        self.after(config.SPIN_INTERVAL_MS, self._spin_disc)

    # --------- Right side: live status
    def _build_live(self, parent: ttk.Frame, row: int) -> None:
        box = self._card(parent, "Live", row)
        box.columnconfigure(0, weight=1)
        colors = self.colors

        self.bpm_label = ttk.Label(box, text="-- BPM", style="Big.TLabel")
        self.bpm_label.grid(row=0, column=0, sticky="w")
        self.lock_label = ttk.Label(box, text="listening...", style="Hint.TLabel")
        self.lock_label.grid(row=1, column=0, sticky="w")
        self.section_label = ttk.Label(box, text="", style="Section.TLabel")
        self.section_label.grid(row=0, column=1, rowspan=2, sticky="e")

        self.beat_canvas = tk.Canvas(box, width=150, height=22, bg=colors["BG_LIGHT"], highlightthickness=0)
        self.beat_canvas.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self._beat_dots = [self.beat_canvas.create_oval(4 + i * 36, 3, 24 + i * 36, 21,
                                                        outline=colors["ACCENT_DARK"], width=2) for i in range(4)]
        self.bar_label = ttk.Label(box, text="", style="Hint.TLabel")
        self.bar_label.grid(row=2, column=1, sticky="e", pady=(10, 0))

        # what the fixture is being told right now
        self.preview_canvas = tk.Canvas(box, width=360, height=26, bg=colors["BG_LIGHT"], highlightthickness=0)
        self.preview_canvas.grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._preview_dots, self._preview_state = {}, None
        for i, (key, text) in enumerate((("bass", "BASS"), ("led", "LED"), ("laser", "LASER"), ("strobe", "STROBE"))):
            x = 6 + i * 90
            dot = self.preview_canvas.create_oval(x, 5, x + 16, 21, outline=colors["LINE"], width=2)
            self.preview_canvas.create_text(x + 24, 13, text=text, anchor="w", fill=colors["MUTED"],
                                            font=(theme.FONT, 8, "bold"))
            self._preview_dots[key] = dot

        self.build_bar = ttk.Progressbar(box, orient="horizontal", maximum=100, style="Build.Horizontal.TProgressbar")
        self.build_bar.grid(row=4, column=0, columnspan=2, sticky="ew", pady=(10, 0))

    # --------- Right side: quick looks
    def _build_looks(self, parent: ttk.Frame, row: int) -> None:
        box = self._card(parent, "Look", row)
        self.look_var = tk.StringVar(value=self.look.name)
        buttons = ttk.Frame(box, style="Plain.Card.TFrame")
        buttons.grid(row=0, column=0, sticky="ew")
        for i, look in enumerate(lightengine.QUICK_LOOKS):
            buttons.columnconfigure(i % 3, weight=1, uniform="looks")
            ttk.Radiobutton(buttons, text=look.name, value=look.name, variable=self.look_var,
                            style="Look.Toolbutton", command=lambda n=look.name: self._load_look(n)
                            ).grid(row=i // 3, column=i % 3, sticky="ew", padx=2, pady=2)
        self.look_desc = ttk.Label(box, text="", style="Hint.TLabel", wraplength=470, justify="left")
        self.look_desc.grid(row=1, column=0, sticky="w", pady=(6, 0))

    # --------- Right side: the rules
    def _build_rules(self, parent: ttk.Frame, row: int) -> None:
        box = self._card(parent, "When this happens  \u2192  do this", row)
        chips = {"led": "LED", "laser": "Laser", "strobe": "Strobe", "blackout": "Blackout", "colour": "Colour \u21c4"}
        self.every_var = tk.StringVar()
        for r, (trigger, text) in enumerate(lightengine.TRIGGERS):
            cell = ttk.Frame(box, style="Plain.Card.TFrame")
            cell.grid(row=r, column=0, sticky="w", pady=3)
            if trigger == "beat":
                ttk.Label(cell, text="Every", style="Card.TLabel").pack(side="left")
                cb = ttk.Combobox(cell, values=list(lightengine.BEAT_OPTIONS), textvariable=self.every_var,
                                  state="readonly", width=8)
                cb.pack(side="left", padx=(6, 0))
                cb.bind("<<ComboboxSelected>>", lambda e: self._on_every_change())
            else:
                ttk.Label(cell, text=text, style="Card.TLabel", width=14).pack(side="left")
            box.columnconfigure(1, weight=1)
            bar = ttk.Frame(box, style="Plain.Card.TFrame")
            bar.grid(row=r, column=1, sticky="e", pady=3, padx=(10, 0))
            for action, _label in lightengine.ACTIONS:
                var = tk.BooleanVar(value=False)
                self.rule_vars[(trigger, action)] = var
                ttk.Checkbutton(bar, text=chips[action], variable=var, style="Chip.Toolbutton",
                                command=self._on_rule_change).pack(side="left", padx=2)

    # --------- Right side: colours, pattern, base light
    def _build_colours(self, parent: ttk.Frame, row: int) -> None:
        box = self._card(parent, "Colours", row)
        box.columnconfigure(1, weight=1)
        self._colour_vars, self._swatches = {}, {}
        led_labels = [label for label, _ in lightengine.LED_COLOURS.values()]
        laser_labels = [label for label, _ in lightengine.LASER_COLOURS.values()]
        for r, (title, attrs, labels, table) in enumerate((
                ("LED", ("led_a", "led_b"), led_labels, lightengine.LED_COLOURS),
                ("Laser", ("laser_a", "laser_b"), laser_labels, lightengine.LASER_COLOURS))):
            ttk.Label(box, text=title, style="Card.TLabel", width=8).grid(row=r, column=0, sticky="w", pady=3)
            line = ttk.Frame(box, style="Plain.Card.TFrame")
            line.grid(row=r, column=1, sticky="w", pady=3)
            for j, attr in enumerate(attrs):
                if j == 1:
                    ttk.Label(line, text="\u21c4", style="CardMuted.TLabel").pack(side="left", padx=8)
                swatch = tk.Label(line, text="  ", bg=self.colors["LINE"], width=2)
                swatch.pack(side="left", padx=(0, 6))
                var = tk.StringVar()
                cb = ttk.Combobox(line, values=labels, textvariable=var, state="readonly", width=14)
                cb.pack(side="left")
                cb.bind("<<ComboboxSelected>>", lambda e, a=attr, t=table: self._on_colour_change(a, t))
                self._colour_vars[attr] = var
                self._swatches[attr] = swatch

        ttk.Label(box, text="Laser pattern", style="Card.TLabel", width=12).grid(row=2, column=0, sticky="w", pady=3)
        self.pattern_var = tk.StringVar()
        cb = ttk.Combobox(box, values=["Auto (cycle)"] + [f"Pattern {i}" for i in range(1, 19)],
                          textvariable=self.pattern_var, state="readonly", width=14)
        cb.grid(row=2, column=1, sticky="w", pady=3)
        cb.bind("<<ComboboxSelected>>", lambda e: self._on_pattern_change())

        base = ttk.Frame(box, style="Plain.Card.TFrame")
        base.grid(row=3, column=0, columnspan=2, sticky="w", pady=(6, 0))
        self.base_led_var = tk.BooleanVar(value=False)
        self.base_laser_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(base, text="LED always on", variable=self.base_led_var, style="Card.TCheckbutton",
                        command=self._on_base_change).pack(side="left", padx=(0, 18))
        ttk.Checkbutton(base, text="Laser always on", variable=self.base_laser_var, style="Card.TCheckbutton",
                        command=self._on_base_change).pack(side="left")

    # --------- Right side: sliders
    def _build_tuning(self, parent: ttk.Frame, row: int) -> None:
        box = self._card(parent, "Tuning", row, pady=(0, 0))
        box.columnconfigure(0, weight=1, uniform="tune")
        box.columnconfigure(1, weight=1, uniform="tune")
        motor = lambda v: "off" if v < 0.05 else f"\u00d7{v:.1f}"
        specs = (
            ("Strobe rate", "strobe_rate", 0.0, 1.0, lambda v: f"{int(v * 100)} %"),
            ("Blink length", "blink_ms", 40, 400, lambda v: f"{int(v)} ms"),
            ("Derby motor", "k_derby", 0.0, 2.0, motor),
            ("Laser rotation", "k_laser", 0.0, 2.0, motor),
            ("Pattern speed", "k_show", 0.0, 2.0, motor),
            ("Sensitivity", None, 0.2, 5.0, lambda v: f"{v:.1f}\u00d7"),
        )
        self._look_scales = {}
        for i, (text, attr, lo, hi, fmt) in enumerate(specs):
            cell = ttk.Frame(box, style="Plain.Card.TFrame")
            cell.grid(row=i // 2, column=i % 2, sticky="ew", padx=(0 if i % 2 == 0 else 10, 10 if i % 2 == 0 else 0),
                      pady=(0, 6))
            cell.columnconfigure(0, weight=1)
            head = ttk.Frame(cell, style="Plain.Card.TFrame")
            head.grid(row=0, column=0, sticky="ew")
            ttk.Label(head, text=text, style="Card.TLabel").pack(side="left")
            value = ttk.Label(head, text="", style="Value.TLabel")
            value.pack(side="right")
            scale = ttk.Scale(cell, from_=lo, to=hi, orient="horizontal", style="Card.Horizontal.TScale")
            scale.grid(row=1, column=0, sticky="ew", pady=(3, 0))
            scale.configure(command=lambda v, a=attr, f=fmt, lab=value: self._on_slider(a, float(v), f, lab))
            if attr is None:
                scale.set(self.analyzer.gain)
            else:
                self._look_scales[attr] = scale

    # --------- Looks / rules handling
    def _load_look(self, name: str) -> None:
        preset = lightengine.BY_NAME.get(name)
        if preset is None:
            return
        self._loading = True
        try:
            self.look = preset.copy()
            self.engine.set_look(self.look)
            self.look_var.set(preset.name)
            self.look_desc.config(text=preset.description)
            look = self.look
            for (trigger, action), var in self.rule_vars.items():
                var.set(action in look.rules.get(trigger, ()))
            self.every_var.set(min(lightengine.BEAT_OPTIONS,
                                   key=lambda k: abs(lightengine.BEAT_OPTIONS[k] - look.every_beats)))
            for attr, table in (("led_a", lightengine.LED_COLOURS), ("led_b", lightengine.LED_COLOURS),
                                ("laser_a", lightengine.LASER_COLOURS), ("laser_b", lightengine.LASER_COLOURS)):
                key = getattr(look, attr)
                self._colour_vars[attr].set(table[key][0])
                self._swatches[attr].config(bg=table[key][1])
            self.pattern_var.set("Auto (cycle)" if look.pattern == 0 else f"Pattern {look.pattern}")
            self.base_led_var.set(look.base_led)
            self.base_laser_var.set(look.base_laser)
            for attr, scale in self._look_scales.items():
                scale.set(getattr(look, attr))
        finally:
            self._loading = False

    def _mark_custom(self) -> None:
        if self._loading:
            return
        self.look.name = lightengine.CUSTOM
        self.look_var.set(lightengine.CUSTOM)
        self.look_desc.config(text="Your own setup. Pick a look above to start over from a ready-made one.")

    def _on_rule_change(self) -> None:
        if self._loading:
            return
        self.look.rules = {trigger: {action for action, _ in lightengine.ACTIONS
                                     if self.rule_vars[(trigger, action)].get()}
                           for trigger, _ in lightengine.TRIGGERS}
        self._mark_custom()

    def _on_every_change(self) -> None:
        self.look.every_beats = lightengine.BEAT_OPTIONS.get(self.every_var.get(), self.look.every_beats)
        self._mark_custom()

    def _on_colour_change(self, attr: str, table: dict) -> None:
        label = self._colour_vars[attr].get()
        key = next((k for k, (text, _) in table.items() if text == label), getattr(self.look, attr))
        setattr(self.look, attr, key)
        self._swatches[attr].config(bg=table[key][1])
        self._mark_custom()

    def _on_pattern_change(self) -> None:
        text = self.pattern_var.get()
        self.look.pattern = 0 if text.startswith("Auto") else int(text.split()[-1])
        self._mark_custom()

    def _on_base_change(self) -> None:
        if self._loading:
            return
        self.look.base_led = bool(self.base_led_var.get())
        self.look.base_laser = bool(self.base_laser_var.get())
        self._mark_custom()

    def _on_slider(self, attr, value: float, fmt, label) -> None:
        label.config(text=fmt(value))
        if attr is None:                          # sensitivity belongs to the audio analysis, not to the look
            self.analyzer.gain = value
            return
        if self._loading:
            return
        setattr(self.look, attr, int(value) if attr == "blink_ms" else value)
        self._mark_custom()

    def _on_blackout(self) -> None:
        self.engine.force_blackout = bool(self.blackout_var.get())

    # --------- Audio frames (worker thread -> GUI thread)
    def _on_frame(self, frame: AudioFrame) -> None:
        self.after(0, self._apply_frame, frame)

    def _apply_frame(self, frame: AudioFrame) -> None:
        if frame.error is not None:
            logging.error("Music Mode audio error", exc_info=frame.error)
            self.status_label.config(text=f"Audio error: {frame.error}")
            self.analyzer.stop()
            return

        self._pulse = frame.beat * 7.0
        self._update_bars(frame.bars)
        self._update_waveform(frame.waveform)

        values = self.engine.process(frame)         # channels 2..9 -- channel 1 is never in here
        current_channels = set()
        for channel, value in values.items():
            if channel != 1 and channel in self.channel_names:
                current_channels.add(channel)
                self.set_channel_value(channel, int(value))
        self._active_channels = current_channels
        self._update_status(frame)

    def _set_text(self, widget, key: str, text: str) -> None:
        if self._status_cache.get(key) != text:      # only touch Tk when something actually changed
            self._status_cache[key] = text
            widget.config(text=text)

    def _update_status(self, frame: AudioFrame) -> None:
        engine = self.engine
        section = engine.section_state
        if section == SILENCE:
            self._set_text(self.bpm_label, "bpm", "-- BPM")
            self._set_text(self.lock_label, "lock", "no music")
        else:
            self._set_text(self.bpm_label, "bpm", f"{engine.bpm:.1f} BPM" if engine.tempo_known else "-- BPM")
            self._set_text(self.lock_label, "lock", "tempo locked" if engine.locked else
                           ("following kicks" if engine.tempo_known else "listening..."))
        self._set_text(self.section_label, "section", SECTION_LABELS[section])
        if self.is_connected is not None:           # is anything going to reach the fixture at all?
            connected = bool(self.is_connected())
            self._set_text(self.dmx_label, "dmx", "\u25cf DMX connected" if connected else
                           "\u25cb DMX NOT connected \u2013 go back to manual control and click Connect")
            colour = "#4cc38a" if connected else "#e6a23c"
            if self._status_cache.get("dmx_colour") != colour:
                self._status_cache["dmx_colour"] = colour
                self.dmx_label.config(foreground=colour)
        colour = {DROP: "#e74c3c", BUILD: "#e6a23c", BREAK: self.colors["STATUS_TEXT"],
                  SILENCE: self.colors["MUTED"]}.get(section, self.colors["FG"])
        if self._status_cache.get("section_colour") != colour:
            self._status_cache["section_colour"] = colour
            self.section_label.config(foreground=colour)

        beat = engine.beat_in_bar if (section != SILENCE and engine.tempo_known) else -1
        if self._status_cache.get("beat") != beat:
            self._status_cache["beat"] = beat
            for i, dot in enumerate(self._beat_dots):
                self.beat_canvas.itemconfig(dot, fill=(self.colors["ACCENT"] if i == beat else self.colors["BG_LIGHT"]))
            self._set_text(self.bar_label, "bar", f"bar {engine.bar + 1}" if beat >= 0 else "")
        self.build_bar["value"] = engine.build_progress * 100

        # little fixture preview: what the lights are doing right now
        pv = engine.preview
        state = (frame.beat > 0.5, pv["led"], pv["laser"], pv["strobe"], pv["blackout"])
        if state != self._preview_state:
            self._preview_state = state
            off = "#e74c3c" if pv["blackout"] else self.colors["LINE"]
            canvas = self.preview_canvas
            canvas.itemconfig(self._preview_dots["bass"], fill=self.colors["ACCENT2"] if state[0] else "", outline=off if not state[0] else self.colors["ACCENT2"])
            canvas.itemconfig(self._preview_dots["led"], fill=pv["led"] or "", outline=pv["led"] or off)
            canvas.itemconfig(self._preview_dots["laser"], fill=pv["laser"] or "", outline=pv["laser"] or off)
            canvas.itemconfig(self._preview_dots["strobe"], fill="#ffffff" if pv["strobe"] else "",
                              outline="#ffffff" if pv["strobe"] else off)

    # --------- Visualizer drawing
    def _update_bars(self, bars) -> None:
        if bars is None:
            return
        self._last_bars = bars
        for i, level in enumerate(bars):
            # peak cap: jumps up with the bar, then falls slowly
            self._peaks[i] = max(float(level), self._peaks[i] - config.SPECTRUM_PEAK_DECAY)
        self._draw_bars()

    def _draw_bars(self) -> None:
        geo, canvas, bars = self._geo, self.disc_canvas, self._last_bars
        if not geo or bars is None:
            return
        n = config.N_BARS
        max_len = geo["max_len"]
        if geo["ring"]:
            cx, cy = geo["cx"], geo["cy"]
            r0 = geo["r_in"] + self._pulse
            for k in range(self._slots):
                idx = k if k < n else self._slots - 1 - k
                dx, dy = self._dirs[k]
                length = 3 + float(bars[idx]) * max_len
                canvas.coords(self._bar_ids[k], cx + dx * r0, cy + dy * r0,
                              cx + dx * (r0 + length), cy + dy * (r0 + length))
                p = r0 + 5 + self._peaks[idx] * max_len
                canvas.coords(self._peak_ids[k], cx + dx * p, cy + dy * p, cx + dx * (p + 3), cy + dy * (p + 3))
        else:
            base, x0, slot = geo["base_y"], geo["x0"], geo["slot"]
            for k in range(n):
                x = x0 + (k + 0.5) * slot
                canvas.coords(self._bar_ids[k], x, base, x, base - 3 - float(bars[k]) * max_len)
                py = base - 6 - self._peaks[k] * max_len
                canvas.coords(self._peak_ids[k], x, py, x, py - 3)

    def _update_waveform(self, waveform) -> None:
        if waveform is None or len(waveform) < 2:
            return
        wave_w = self._wave_w
        wave_h = self.wave_canvas.winfo_height() or config.WAVE_CANVAS_HEIGHT
        n = len(waveform)
        mid_y = wave_h / 2
        boost = config.WAVE_DISPLAY_GAIN
        points = []
        for i, sample in enumerate(waveform):
            x = i / (n - 1) * wave_w
            s = max(-1.0, min(1.0, float(sample) * boost))
            points.extend((x, mid_y - s * mid_y * 0.9))
        self.wave_canvas.coords(self._wave_line, *points)
        self.wave_canvas.coords(self._wave_glow, *points)

    # --------- Now Playing
    def _on_playing(self, playing: bool) -> None:
        self.after(0, self._set_playing, playing)

    def _set_playing(self, playing: bool) -> None:
        self._playing = playing

    def _on_now_playing(self, title: str, artist: str, cover_bytes) -> None:
        self.after(0, self._apply_now_playing, title, artist, cover_bytes)

    def _apply_now_playing(self, title: str, artist: str, cover_bytes) -> None:
        self.track_label.config(text=title or "")
        self.artist_label.config(text=artist or "")

        if not _PIL_AVAILABLE:
            logging.warning("Pillow is not installed -- no cover art (pip install pillow)")

        if not (_PIL_AVAILABLE and cover_bytes):
            self._clear_cover()
            return

        key = hash(cover_bytes)
        if key == self._cover_key and self._cover_frames:
            return  # same cover already on the disc
        try:
            cover_size = config.COVER_SIZE
            img = Image.open(io.BytesIO(cover_bytes)).convert("RGBA")
            img = img.resize((cover_size, cover_size), Image.LANCZOS)
            mask = Image.new("L", (cover_size, cover_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, cover_size, cover_size), fill=255)
            img.putalpha(mask)

            # Build every rotation ONCE, here on the Tk main thread. Creating/freeing a
            # PhotoImage on every animation tick (and letting the garbage collector
            # free them from whatever thread it happens to run on) can hang Tk.
            # PIL rotates counter-clockwise, so negate to spin clockwise
            step = config.SPIN_STEP_DEG
            frames = [ImageTk.PhotoImage(img.rotate(-a, resample=Image.BICUBIC))
                      for a in range(0, 360, step)]
            self._cover_frames = frames
            self._cover_key = key
            self._cover_frame_idx = -1
        except Exception:
            logging.exception("Failed to process cover art")
            self._clear_cover()

    def _clear_cover(self) -> None:
        self._cover_frames = []
        self._cover_key = None
        self._cover_frame_idx = -1
        self.disc_canvas.itemconfig(self._cover_image_item, image="")

    # --------- Closing
    def _on_close(self) -> None:
        self.analyzer.stop()
        if self.now_playing:
            self.now_playing.stop()
        if self._active_channels:
            for channel in self._active_channels:    # the sliders keep the last driven value -> don't leave strobes running
                self.set_channel_value(channel, 0)
            self.restore_sliders(list(self._active_channels))
        self.on_closed()
        self.destroy()
