"""
Music Mode
==========

A standalone window (MusicModeWindow) that analyzes the system's audio
output (loopback of the current playback device) in real time, visualizes it
as a spectrum + oscilloscope, can map Bass/Mid/Treble/Beat/Pitch onto DMX
channels, and optionally shows the title/artist/cover art of the track
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

from .controller import apply_dark_titlebar

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


class AudioAnalyzer:
    """Captures system loopback audio and computes several live metrics from it:
    - bass/mid/treble: smoothed energy in three frequency bands (0..1)
    - beat: a short, decaying pulse on sudden energy spikes
      (simple onset detection, not real BPM tracking)
    - pitch: normalized spectral centroid (0 = dull/bassy, 1 = bright/high-frequency) --
      better suited to continuous rotation/speed parameters than a plain band energy
    - bars: spectrum split into log-spaced bands, for the bar display
    - waveform: a short slice of the raw samples, for the oscilloscope display

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
        for name, idx in self._band_idx.items():
            db = 10.0 * math.log10(float(power[idx].sum()) + 1e-12)
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

        step = max(1, n // config.WAVE_POINTS)
        waveform = np.clip(samples[::step], -1.0, 1.0)

        self.on_frame(AudioFrame(
            bass=self._levels["bass"], mid=self._levels["mid"], treble=self._levels["treble"],
            beat=self._beat_level, pitch=self._pitch_level,
            bars=self._bar_levels.copy(), waveform=waveform,
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
    """Standalone window: spectrum + oscilloscope side by side, a disc with
    title/artist below, channel mapping, and start/stop settings."""

    def __init__(self, parent: tk.Tk, channel_names: dict, set_channel_value,
                 restore_sliders, on_closed, colors: dict):
        """
        channel_names:     {channel_nr: "1: Show Select", ...}
        set_channel_value: callback(channel: int, value: int) -> None
        restore_sliders:   callback(channels: list[int]) -> None
        on_closed:         callback() -> None, called when this window closes
                            (the main window should show itself again then)
        colors:             dict with BG/BG_LIGHT/FG/ACCENT/ACCENT_DARK/STATUS_TEXT
                            (same shape as config.ACTIVE_SCHEME)
        """
        super().__init__(parent)
        self.title("Music Mode")
        self.colors = colors
        apply_dark_titlebar(self)
        self.configure(bg=colors["BG"])
        self.resizable(False, False)

        self.channel_names = channel_names
        self.set_channel_value = set_channel_value
        self.restore_sliders = restore_sliders
        self.on_closed = on_closed

        self.analyzer = AudioAnalyzer(on_frame=self._on_frame)
        self.now_playing = None
        self.mapping_vars = {}
        self.meters = {}
        self._active_channels = set()
        self._bar_ids = []

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.analyzer.start()

    # --------- Layout
    def _init_styles(self) -> None:
        colors = self.colors
        style = ttk.Style(self)
        style.configure("Meter.Horizontal.TProgressbar",
                        troughcolor=colors["BG_LIGHT"], background=colors["ACCENT"],
                        bordercolor=colors["BG_LIGHT"], lightcolor=colors["ACCENT"],
                        darkcolor=colors["ACCENT"], thickness=10)

    def _build(self) -> None:
        # Two-column dashboard: [ disc | visualizer ] on top, [ mapping | settings ] below.
        self._init_styles()
        self.columnconfigure(0, weight=0)
        self.columnconfigure(1, weight=1)

        now = ttk.LabelFrame(self, text="Now Playing", padding=12)
        now.grid(row=0, column=0, sticky="nsew", padx=(12, 6), pady=(12, 6))
        self._build_disc(now)

        viz = ttk.LabelFrame(self, text="Visualizer", padding=12)
        viz.grid(row=0, column=1, sticky="nsew", padx=(6, 12), pady=(12, 6))
        self._build_plots(viz)

        bottom = ttk.Frame(self)
        bottom.grid(row=1, column=0, columnspan=2, sticky="ew", padx=12, pady=6)
        bottom.columnconfigure(0, weight=3, uniform="bottom")
        bottom.columnconfigure(1, weight=2, uniform="bottom")
        self._build_mapping(bottom)
        self._build_settings(bottom)

        footer = ttk.Frame(self)
        footer.grid(row=2, column=0, columnspan=2, sticky="ew", padx=12, pady=(6, 12))
        self.status_label = ttk.Label(footer, text="", foreground="#c0392b")
        self.status_label.pack(side="left")
        ttk.Button(footer, text="Back to Manual Control", command=self._on_close).pack(side="right")

    def _build_disc(self, parent: ttk.Frame) -> None:
        colors = self.colors
        size = config.DISC_SIZE
        self.disc_canvas = tk.Canvas(parent, width=size, height=size,
                                      bg=colors["BG"], highlightthickness=0)
        self.disc_canvas.pack()

        cx = cy = size / 2
        self.disc_canvas.create_oval(4, 4, size - 4, size - 4,
                                      outline=colors["ACCENT_DARK"], width=2)
        for r in range(int(size / 2) - 8, 24, -12):
            self.disc_canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                          outline=colors["ACCENT_DARK"], width=1)

        # Small rotating pixel-art dots as a fallback "label" while no real
        # cover art is available (see module docstring)
        self._pixel_ids = []
        for i in range(config.PIXEL_DOT_COUNT):
            color = colors["ACCENT"] if i % 2 == 0 else colors["ACCENT_DARK"]
            dot_id = self.disc_canvas.create_rectangle(0, 0, 0, 0, fill=color, outline="")
            self._pixel_ids.append(dot_id)
        self.disc_canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5,
                                      fill=colors["FG"], outline="")

        # The cover image sits last in the draw order -> automatically covers
        # the pixel dots once a cover is actually set (image=None draws nothing)
        self._cover_image_item = self.disc_canvas.create_image(cx, cy, image=None)
        self._cover_photo = None   # keep a reference, or Tkinter garbage-collects the image
        self._cover_frames = []    # pre-rotated PhotoImages, built once per cover (main thread only)
        self._cover_key = None     # identifies the cover the frames were built from
        self._cover_frame_idx = -1
        self._disc_angle = 0.0
        self._playing = True       # disc only spins while music is playing

        self.track_label = ttk.Label(parent, text="", font=("Segoe UI", 11, "bold"),
                                      wraplength=size + 20, justify="center")
        self.track_label.pack(pady=(10, 0))
        self.artist_label = ttk.Label(parent, text="", foreground=colors["STATUS_TEXT"],
                                       wraplength=size + 20, justify="center")
        self.artist_label.pack()

        have_now_playing_source = (config.NOWPLAYING_BRIDGE_EXE.exists()
                                    or config.NOWPLAYING_BRIDGE_SCRIPT.exists()
                                    or _WIN32_AVAILABLE or _DBUS_AVAILABLE)
        if not have_now_playing_source:
            self.track_label.config(text="(no title source available)")
        else:
            self.now_playing = NowPlayingReader(on_update=self._on_now_playing,
                                             on_playing=self._on_playing)
            self.now_playing.start()

        self._spin_disc()

    def _spin_disc(self) -> None:
        if not self.winfo_exists():
            return
        if self._playing:
            self._disc_angle = (self._disc_angle + config.SPIN_STEP_DEG) % 360
        cx = cy = config.DISC_SIZE / 2
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

    def _build_plots(self, parent: ttk.Frame) -> None:
        colors = self.colors
        bar_w, bar_h = config.BAR_CANVAS_WIDTH, config.BAR_CANVAS_HEIGHT
        wave_w, wave_h = config.WAVE_CANVAS_WIDTH, config.WAVE_CANVAS_HEIGHT
        header_font = ("Segoe UI", 9, "bold")

        ttk.Label(parent, text="Spectrum", font=header_font).pack(anchor="w")
        self.bar_canvas = tk.Canvas(parent, width=bar_w, height=bar_h,
                                     bg=colors["BG_LIGHT"], highlightthickness=0)
        self.bar_canvas.pack(pady=(3, 10))
        for frac in (0.25, 0.5, 0.75):   # faint guide lines behind the bars
            self.bar_canvas.create_line(0, bar_h * frac, bar_w, bar_h * frac, fill=colors["BG"])
        self._peaks = [0.0] * config.N_BARS
        self._peak_ids = []
        for _ in range(config.N_BARS):
            self._bar_ids.append(self.bar_canvas.create_rectangle(
                0, bar_h, 0, bar_h, fill=colors["ACCENT"], width=0))
            self._peak_ids.append(self.bar_canvas.create_rectangle(
                0, bar_h, 0, bar_h, fill=colors["STATUS_TEXT"], width=0))

        ttk.Label(parent, text="Waveform", font=header_font).pack(anchor="w")
        self.wave_canvas = tk.Canvas(parent, width=wave_w, height=wave_h,
                                      bg=colors["BG_LIGHT"], highlightthickness=0)
        self.wave_canvas.pack(pady=(3, 0))
        mid_y = wave_h / 2
        self.wave_canvas.create_line(0, mid_y, wave_w, mid_y, fill=colors["BG"])
        self._wave_line = self.wave_canvas.create_line(
            0, mid_y, wave_w, mid_y, fill=colors["ACCENT"], width=1.5, smooth=True)

    def _build_mapping(self, parent: ttk.Frame) -> None:
        mapping = ttk.LabelFrame(parent, text="Channel Mapping", padding=12)
        mapping.grid(row=0, column=0, sticky="nsew", padx=(0, 6))
        mapping.columnconfigure(2, weight=1)

        options = ["None"] + [self.channel_names[c] for c in sorted(self.channel_names)]
        for row, source in enumerate(config.SOURCES):
            ttk.Label(mapping, text=config.SOURCE_LABELS[source], width=8).grid(
                row=row, column=0, sticky="w", pady=4)

            var = tk.StringVar(value="None")
            self.mapping_vars[source] = var
            cb = ttk.Combobox(mapping, values=options, textvariable=var, width=24, state="readonly")
            cb.grid(row=row, column=1, padx=10, pady=4)

            meter = ttk.Progressbar(mapping, orient="horizontal", maximum=100,
                                    style="Meter.Horizontal.TProgressbar")
            meter.grid(row=row, column=2, sticky="ew", pady=4)
            self.meters[source] = meter

    def _build_settings(self, parent: ttk.Frame) -> None:
        settings = ttk.LabelFrame(parent, text="Settings", padding=12)
        settings.grid(row=0, column=1, sticky="nsew", padx=(6, 0))
        settings.columnconfigure(0, weight=1)

        head = ttk.Frame(settings)
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="Sensitivity").pack(side="left")
        self.gain_value = ttk.Label(head, text="", foreground=self.colors["STATUS_TEXT"])
        self.gain_value.pack(side="right")
        self.gain_scale = ttk.Scale(settings, from_=0.2, to=5.0, orient="horizontal",
                                     command=self._on_gain_change)
        self.gain_scale.set(self.analyzer.gain)
        self.gain_scale.grid(row=1, column=0, sticky="ew", pady=(2, 14))

        head = ttk.Frame(settings)
        head.grid(row=2, column=0, sticky="ew")
        ttk.Label(head, text="Smoothing").pack(side="left")
        self.smooth_value = ttk.Label(head, text="", foreground=self.colors["STATUS_TEXT"])
        self.smooth_value.pack(side="right")
        self.smooth_scale = ttk.Scale(settings, from_=0.0, to=0.95, orient="horizontal",
                                       command=self._on_smoothing_change)
        self.smooth_scale.set(self.analyzer.smoothing)
        self.smooth_scale.grid(row=3, column=0, sticky="ew", pady=(2, 0))

    def _channel_for(self, source: str):
        label = self.mapping_vars[source].get()
        if label == "None":
            return None
        for ch, name in self.channel_names.items():
            if name == label:
                return ch
        return None

    # --------- Settings callbacks
    def _on_gain_change(self, value) -> None:
        self.analyzer.gain = float(value)
        if hasattr(self, "gain_value"):
            self.gain_value.config(text=f"{float(value):.1f}x")

    def _on_smoothing_change(self, value) -> None:
        self.analyzer.smoothing = float(value)
        if hasattr(self, "smooth_value"):
            self.smooth_value.config(text=f"{float(value):.2f}")

    # --------- Audio frames (worker thread -> GUI thread)
    def _on_frame(self, frame: AudioFrame) -> None:
        self.after(0, self._apply_frame, frame)

    def _apply_frame(self, frame: AudioFrame) -> None:
        if frame.error is not None:
            logging.error("Music Mode audio error", exc_info=frame.error)
            self.status_label.config(text=f"Audio error: {frame.error}")
            self.analyzer.stop()
            return

        self._update_bars(frame.bars)
        self._update_waveform(frame.waveform)

        levels = {"bass": frame.bass, "mid": frame.mid, "treble": frame.treble,
                  "beat": frame.beat, "pitch": frame.pitch}
        current_channels = set()
        for source, level in levels.items():
            self.meters[source]["value"] = level * 100
            channel = self._channel_for(source)
            if channel is not None:
                current_channels.add(channel)
                self.set_channel_value(channel, int(level * 255))

        # Mapping can change while running -> release sliders that are no
        # longer mapped to anything
        freed = self._active_channels - current_channels
        if freed:
            self.restore_sliders(list(freed))
        self._active_channels = current_channels

    def _update_bars(self, bars) -> None:
        if bars is None:
            return
        bar_w, bar_h = config.BAR_CANVAS_WIDTH, config.BAR_CANVAS_HEIGHT
        slot = bar_w / config.N_BARS
        gap = 3
        for i, level in enumerate(bars):
            x0 = i * slot + gap / 2
            x1 = x0 + slot - gap
            self.bar_canvas.coords(self._bar_ids[i], x0, bar_h - level * bar_h, x1, bar_h)

            # peak cap: jumps up with the bar, then falls slowly
            peak = max(level, self._peaks[i] - config.SPECTRUM_PEAK_DECAY)
            self._peaks[i] = peak
            py = bar_h - peak * bar_h
            self.bar_canvas.coords(self._peak_ids[i], x0, py - 2, x1, py)

    def _update_waveform(self, waveform) -> None:
        if waveform is None or len(waveform) < 2:
            return
        wave_w, wave_h = config.WAVE_CANVAS_WIDTH, config.WAVE_CANVAS_HEIGHT
        n = len(waveform)
        mid_y = wave_h / 2
        boost = config.WAVE_DISPLAY_GAIN
        points = []
        for i, sample in enumerate(waveform):
            x = i / (n - 1) * wave_w
            s = max(-1.0, min(1.0, float(sample) * boost))
            points.extend((x, mid_y - s * mid_y * 0.9))
        self.wave_canvas.coords(self._wave_line, *points)

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
            self.restore_sliders(list(self._active_channels))
        self.on_closed()
        self.destroy()
