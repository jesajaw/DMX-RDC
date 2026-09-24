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

Title/artist/cover art:
- Windows (primary): starts NowPlayingBridge.ps1, a small PowerShell script
  (see src/NowPlayingBridge.ps1) that talks to the Windows Media Control APIs
  (SMTC, WinRT) -- the same source behind the Windows volume flyout preview.
  Reason for going through PowerShell instead of a Python WinRT binding:
  winsdk/winrt are archived and no longer ship wheels for recent Python
  versions. PowerShell, unlike a Python binding, needs NO install or build
  step at all -- it ships with every Windows install and has built-in support
  for loading WinRT types. Python itself never speaks COM/WinRT here -- it
  just launches the script as a subprocess and reads its JSON/cover output
  files.
  If NowPlayingBridge.ps1 is missing, or PowerShell itself isn't available,
  falls back to a plain window-title heuristic over known player processes
  (parameters.KNOWN_PLAYER_PROCESSES, e.g. "Artist - Title" for Spotify) --
  title/artist only, no cover.
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

from .config import parameters
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

    def __init__(self, on_frame, blocksize: int = parameters.BLOCK_SIZE,
                 gain: float = 1.5, smoothing: float = 0.7, n_bars: int = parameters.N_BARS):
        self.on_frame = on_frame            # callback(frame: AudioFrame)
        self.blocksize = blocksize
        self.gain = gain                    # sensitivity, adjustable live
        self.smoothing = smoothing          # 0..~0.95, higher = slower/smoother
        self.n_bars = n_bars
        self.samplerate = parameters.SAMPLE_RATE  # placeholder, replaced in start() by the real device
        self._channels = 2                  # placeholder, replaced in start() by the real device

        self._running = False
        self._pa = None
        self._stream = None
        self._levels = {"bass": 0.0, "mid": 0.0, "treble": 0.0}
        self._bar_levels = np.zeros(n_bars)
        self._bar_edges = np.geomspace(parameters.BAR_FREQ_RANGE[0], parameters.BAR_FREQ_RANGE[1], n_bars + 1)
        self._window = np.hanning(blocksize)
        self._energy_history = deque(maxlen=parameters.ENERGY_HISTORY_LEN)
        self._beat_level = 0.0
        self._pitch_level = 0.0

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
        self._process(samples)
        return (None, pyaudio.paContinue)

    def _process(self, samples: np.ndarray) -> None:
        window = self._window if len(samples) == len(self._window) else np.hanning(len(samples))
        windowed = samples * window
        spectrum = np.abs(np.fft.rfft(windowed)) / len(windowed)
        freqs = np.fft.rfftfreq(len(windowed), d=1.0 / self.samplerate)

        for name, (lo, hi) in parameters.BAND_RANGES.items():
            mask = (freqs >= lo) & (freqs < hi)
            energy = float(np.sqrt(np.mean(spectrum[mask] ** 2))) if mask.any() else 0.0
            level = min(1.0, energy * self.gain)
            self._levels[name] = self.smoothing * self._levels[name] + (1 - self.smoothing) * level

        for i in range(self.n_bars):
            lo, hi = self._bar_edges[i], self._bar_edges[i + 1]
            mask = (freqs >= lo) & (freqs < hi)
            energy = float(np.sqrt(np.mean(spectrum[mask] ** 2))) if mask.any() else 0.0
            level = min(1.0, energy * self.gain)
            self._bar_levels[i] = self.smoothing * self._bar_levels[i] + (1 - self.smoothing) * level

        # Beat/onset: total energy vs. its rolling average over the last ~1s
        total_energy = float(np.sqrt(np.mean(spectrum ** 2)))
        avg_energy = float(np.mean(self._energy_history)) if self._energy_history else 0.0
        self._energy_history.append(total_energy)
        is_onset = (avg_energy > 0 and total_energy > avg_energy * parameters.BEAT_THRESHOLD_RATIO
                    and total_energy > parameters.BEAT_MIN_ENERGY)
        self._beat_level = max(1.0 if is_onset else 0.0, self._beat_level * parameters.BEAT_DECAY)

        # Pitch: normalized spectral centroid (0 = bassy, 1 = bright)
        magnitude_sum = float(np.sum(spectrum))
        centroid = float(np.sum(freqs * spectrum) / magnitude_sum) if magnitude_sum > 0 else 0.0
        pitch_norm = min(1.0, centroid / parameters.PITCH_REFERENCE_HZ)
        self._pitch_level = self.smoothing * self._pitch_level + (1 - self.smoothing) * pitch_norm

        step = max(1, len(samples) // parameters.WAVE_POINTS)
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
    player process (parameters.KNOWN_PLAYER_PROCESSES) and returns its window
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
        if _get_process_name(pid) in parameters.KNOWN_PLAYER_PROCESSES:
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
    """Provides title/artist/cover art of the currently playing track.

    Windows (primary): starts NowPlayingBridge.ps1 (a PowerShell script, no
    install/build step needed) as a background process and polls its
    JSON/cover output files -- provides title, artist AND cover art (the same
    source behind the Windows volume flyout preview). Its own stdout/stderr
    go into nowplaying_cache/bridge.log for diagnosis. If it doesn't produce
    any output within a few seconds (e.g. the WinRT interop pattern it uses
    doesn't fully match on this system -- see its own docstring), this reader
    automatically gives up on it and falls back to the window-title heuristic
    below, so a broken bridge script degrades gracefully instead of silencing
    everything.
    Windows (fallback, used automatically if the above doesn't pan out, or if
    NowPlayingBridge.ps1 is missing / PowerShell is unavailable): plain
    window-title heuristic over known media player processes
    (parameters.KNOWN_PLAYER_PROCESSES), e.g. "Artist - Title" for Spotify --
    title/artist only, no cover. Stays inactive without pywin32.

    Linux: queries MPRIS over D-Bus via `jeepney` -- title, artist and cover
    art (as a file:// or http(s):// URL). Stays inactive without jeepney.
    """

    # How many consecutive polls (roughly this many * poll_interval seconds)
    # the bridge script gets to produce its first output file before this
    # reader gives up on it and falls back to the window-title heuristic.
    BRIDGE_MISS_LIMIT = 10

    def __init__(self, on_update, poll_interval: float = 1.0):
        self.on_update = on_update  # callback(title: str, artist: str, cover_bytes: bytes | None)
        self.poll_interval = poll_interval
        self._running = False
        self._thread = None
        self._process = None
        self._log_file = None
        self._last_signature = None
        self._bridge_miss_count = 0
        self._use_bridge = _IS_WINDOWS and parameters.NOWPLAYING_BRIDGE_SCRIPT.exists()
        self._use_mpris = not _IS_WINDOWS and _DBUS_AVAILABLE

    def start(self) -> None:
        if self._running:
            return
        if not (self._use_bridge or self._use_mpris or _WIN32_AVAILABLE):
            return
        self._running = True
        if self._use_bridge:
            self._start_bridge_process()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._process is not None:
            try:
                self._process.terminate()
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
        cache_dir = parameters.NOWPLAYING_CACHE_DIR
        cache_dir.mkdir(exist_ok=True)
        try:
            self._log_file = open(cache_dir / "bridge.log", "w", encoding="utf-8")
            args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(parameters.NOWPLAYING_BRIDGE_SCRIPT),
                    str(cache_dir), str(int(self.poll_interval * 1000))]
            if parameters.NOWPLAYING_DEBUG:
                args.append("-IncludeDebugInfo")
            self._process = subprocess.Popen(
                args,
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=self._log_file, stderr=subprocess.STDOUT,
            )
        except Exception:
            logging.exception("Failed to start NowPlayingBridge.ps1")
            self._process = None
            self._use_bridge = False

    def _loop(self) -> None:
        while self._running:
            if self._use_bridge:
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
        cache_dir = parameters.NOWPLAYING_CACHE_DIR
        try:
            data = json.loads((cache_dir / "nowplaying.json").read_text(encoding="utf-8"))
        except Exception:
            self._bridge_miss_count += 1
            if self._bridge_miss_count >= self.BRIDGE_MISS_LIMIT:
                logging.warning(
                    "NowPlayingBridge.ps1 produced no output after %d attempts -- "
                    "falling back to the window-title heuristic. Check %s for errors.",
                    self._bridge_miss_count, cache_dir / "bridge.log",
                )
                self._use_bridge = False
                if self._process is not None:
                    try:
                        self._process.terminate()
                    except Exception:
                        pass
                    self._process = None
            return
        self._bridge_miss_count = 0

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
                            (same shape as parameters.ACTIVE_SCHEME)
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
    def _build(self) -> None:
        viz = ttk.LabelFrame(self, text="Now Playing", padding=10)
        viz.pack(fill="x", padx=10, pady=(10, 5))

        plots_row = ttk.Frame(viz)
        plots_row.pack()
        self._build_plots(plots_row)

        disc_row = ttk.Frame(viz)
        disc_row.pack(pady=(12, 0))
        self._build_disc(disc_row)

        self._build_mapping()
        self._build_settings()

        self.status_label = ttk.Label(self, text="", foreground="#c0392b")
        self.status_label.pack(pady=(4, 0))

        ttk.Button(self, text="Back to Manual Control", command=self._on_close).pack(pady=10)

    def _build_disc(self, parent: ttk.Frame) -> None:
        colors = self.colors
        size = parameters.DISC_SIZE
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
        for i in range(parameters.PIXEL_DOT_COUNT):
            color = colors["ACCENT"] if i % 2 == 0 else colors["ACCENT_DARK"]
            dot_id = self.disc_canvas.create_rectangle(0, 0, 0, 0, fill=color, outline="")
            self._pixel_ids.append(dot_id)
        self.disc_canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5,
                                      fill=colors["FG"], outline="")

        # The cover image sits last in the draw order -> automatically covers
        # the pixel dots once a cover is actually set (image=None draws nothing)
        self._cover_image_item = self.disc_canvas.create_image(cx, cy, image=None)
        self._cover_photo = None   # keep a reference, or Tkinter garbage-collects the image
        self._base_cover = None    # circularly masked, unrotated PIL image
        self._disc_angle = 0.0

        self.track_label = ttk.Label(parent, text="", font=("Segoe UI", 9, "bold"),
                                      wraplength=size + 20, justify="center")
        self.track_label.pack(pady=(5, 0))

        have_now_playing_source = parameters.NOWPLAYING_BRIDGE_SCRIPT.exists() or _WIN32_AVAILABLE or _DBUS_AVAILABLE
        if not have_now_playing_source:
            self.track_label.config(text="(no title source available)")
        else:
            self.now_playing = NowPlayingReader(on_update=self._on_now_playing)
            self.now_playing.start()

        self._spin_disc()

    def _spin_disc(self) -> None:
        if not self.winfo_exists():
            return
        self._disc_angle = (self._disc_angle + parameters.SPIN_STEP_DEG) % 360
        cx = cy = parameters.DISC_SIZE / 2
        count = len(self._pixel_ids)
        for i, dot_id in enumerate(self._pixel_ids):
            angle = math.radians(self._disc_angle + i * (360 / count))
            x = cx + parameters.PIXEL_DOT_RADIUS * math.cos(angle)
            y = cy + parameters.PIXEL_DOT_RADIUS * math.sin(angle)
            half = parameters.PIXEL_DOT_SIZE / 2
            self.disc_canvas.coords(dot_id, x - half, y - half, x + half, y + half)

        if _PIL_AVAILABLE and self._base_cover is not None:
            rotated = self._base_cover.rotate(self._disc_angle, resample=Image.BICUBIC)
            self._cover_photo = ImageTk.PhotoImage(rotated)
            self.disc_canvas.itemconfig(self._cover_image_item, image=self._cover_photo)

        self.after(parameters.SPIN_INTERVAL_MS, self._spin_disc)

    def _build_plots(self, parent: ttk.Frame) -> None:
        colors = self.colors
        bar_w, bar_h = parameters.BAR_CANVAS_WIDTH, parameters.BAR_CANVAS_HEIGHT
        wave_w, wave_h = parameters.WAVE_CANVAS_WIDTH, parameters.WAVE_CANVAS_HEIGHT

        spectrum_col = ttk.Frame(parent)
        spectrum_col.pack(side="left", padx=(0, 15))
        ttk.Label(spectrum_col, text="Spectrum", style="CellTitle.TLabel").pack(anchor="w")
        self.bar_canvas = tk.Canvas(spectrum_col, width=bar_w, height=bar_h,
                                     bg=colors["BG_LIGHT"], highlightthickness=0)
        self.bar_canvas.pack()
        bar_width = bar_w / parameters.N_BARS
        for i in range(parameters.N_BARS):
            x0 = i * bar_width + 2
            x1 = x0 + bar_width - 4
            bar_id = self.bar_canvas.create_rectangle(
                x0, bar_h, x1, bar_h, fill=colors["ACCENT"], width=0)
            self._bar_ids.append(bar_id)

        wave_col = ttk.Frame(parent)
        wave_col.pack(side="left")
        ttk.Label(wave_col, text="Waveform", style="CellTitle.TLabel").pack(anchor="w")
        self.wave_canvas = tk.Canvas(wave_col, width=wave_w, height=wave_h,
                                      bg=colors["BG_LIGHT"], highlightthickness=0)
        self.wave_canvas.pack()
        mid_y = wave_h / 2
        self._wave_line = self.wave_canvas.create_line(
            0, mid_y, wave_w, mid_y, fill=colors["ACCENT"], width=1.5, smooth=True)

    def _build_mapping(self) -> None:
        mapping = ttk.LabelFrame(self, text="Channel Mapping", padding=10)
        mapping.pack(fill="x", padx=10, pady=5)

        options = ["None"] + [self.channel_names[c] for c in sorted(self.channel_names)]
        for source in parameters.SOURCES:
            row = ttk.Frame(mapping)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=parameters.SOURCE_LABELS[source], width=8).pack(side="left")

            var = tk.StringVar(value="None")
            self.mapping_vars[source] = var
            cb = ttk.Combobox(row, values=options, textvariable=var, width=24, state="readonly")
            cb.pack(side="left", padx=5)

            meter = ttk.Progressbar(row, orient="horizontal", length=120, maximum=100)
            meter.pack(side="left", padx=5, fill="x", expand=True)
            self.meters[source] = meter

    def _build_settings(self) -> None:
        settings = ttk.LabelFrame(self, text="Settings", padding=10)
        settings.pack(fill="x", padx=10, pady=5)

        ttk.Label(settings, text="Sensitivity:").pack(side="left")
        self.gain_scale = ttk.Scale(settings, from_=0.2, to=5.0, orient="horizontal",
                                     command=self._on_gain_change)
        self.gain_scale.set(self.analyzer.gain)
        self.gain_scale.pack(side="left", padx=5, fill="x", expand=True)

        ttk.Label(settings, text="Smoothing:").pack(side="left")
        self.smooth_scale = ttk.Scale(settings, from_=0.0, to=0.95, orient="horizontal",
                                       command=self._on_smoothing_change)
        self.smooth_scale.set(self.analyzer.smoothing)
        self.smooth_scale.pack(side="left", padx=5, fill="x", expand=True)

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

    def _on_smoothing_change(self, value) -> None:
        self.analyzer.smoothing = float(value)

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
        bar_w, bar_h = parameters.BAR_CANVAS_WIDTH, parameters.BAR_CANVAS_HEIGHT
        bar_width = bar_w / parameters.N_BARS
        for i, level in enumerate(bars):
            x0 = i * bar_width + 2
            x1 = x0 + bar_width - 4
            y1 = bar_h
            y0 = bar_h - level * bar_h
            self.bar_canvas.coords(self._bar_ids[i], x0, y0, x1, y1)

    def _update_waveform(self, waveform) -> None:
        if waveform is None or len(waveform) < 2:
            return
        wave_w, wave_h = parameters.WAVE_CANVAS_WIDTH, parameters.WAVE_CANVAS_HEIGHT
        n = len(waveform)
        mid_y = wave_h / 2
        points = []
        for i, sample in enumerate(waveform):
            x = i / (n - 1) * wave_w
            y = mid_y - sample * mid_y * 0.9
            points.extend((x, y))
        self.wave_canvas.coords(self._wave_line, *points)

    # --------- Now Playing
    def _on_now_playing(self, title: str, artist: str, cover_bytes) -> None:
        self.after(0, self._apply_now_playing, title, artist, cover_bytes)

    def _apply_now_playing(self, title: str, artist: str, cover_bytes) -> None:
        text = f"{title}\n{artist}" if artist else (title or "")
        self.track_label.config(text=text)

        if not (_PIL_AVAILABLE and cover_bytes):
            self._base_cover = None
            return
        try:
            cover_size = parameters.COVER_SIZE
            img = Image.open(io.BytesIO(cover_bytes)).convert("RGBA")
            img = img.resize((cover_size, cover_size), Image.LANCZOS)
            mask = Image.new("L", (cover_size, cover_size), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, cover_size, cover_size), fill=255)
            img.putalpha(mask)
            self._base_cover = img
        except Exception:
            logging.exception("Failed to process cover art")
            self._base_cover = None

    # --------- Closing
    def _on_close(self) -> None:
        self.analyzer.stop()
        if self.now_playing:
            self.now_playing.stop()
        if self._active_channels:
            self.restore_sliders(list(self._active_channels))
        self.on_closed()
        self.destroy()