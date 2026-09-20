"""
Music Mode
==========

Eigenes Fenster (MusicModeWindow), das die Systemsounds (WASAPI-Loopback des
aktuellen Ausgabegeräts) in Echtzeit analysiert, als Spektrum + Oszilloskop
visualisiert, Bass/Mid/Treble/Beat/Pitch auf DMX-Kanäle mappen kann und dazu
optional (Windows) Titel/Interpret des aktuell spielenden Tracks anzeigt.

Das Hauptfenster (DMXUI) wird beim Öffnen versteckt (root.withdraw()) und dient
nur noch als Backend: Verbindung/Send-Loop laufen unverändert weiter, Kanalwerte
werden über die vom Hauptfenster übergebenen Callbacks gesetzt.

Abhängigkeiten:
    pip install PyAudioWPatch numpy
    pip install pywin32   # optional, nur fuer Titel/Interpret (Windows-only)

Hinweis Plattform:
- Windows: Loopback-Audio laeuft ohne weitere Einrichtung (WASAPI-Loopback des
  Default-Ausgabegeraets, per PyAudioWPatch/PortAudio).
- macOS/Linux: keine WASAPI-Loopback -- AudioAnalyzer.start() liefert dann
  einen Fehler (ueber on_frame(AudioFrame(error=...))), der Rest der UI
  bleibt aber benutzbar. Kein Titel/Interpret.

Die Titel/Interpret/Cover-Anzeige nutzt primaer NowPlayingBridge.exe, einen
kleinen C#/.NET-Hintergrundprozess (siehe src/Program.cs),
der first-party WinRT anspricht -- dieselbe SMTC-Quelle wie die Windows-
Lautstaerke-Vorschau. Grund: winsdk/winrt (die Python-WinRT-Bindungen) sind
archiviert und haben fuer neuere Python-Versionen keine fertigen Wheels mehr.
.NET hat WinRT-Unterstuetzung dagegen first-party und aktiv gepflegt. Python
selbst spricht dabei kein COM/WinRT -- es startet die .exe als Subprozess und
liest deren JSON-/Cover-Ausgabedateien per stinknormalem Datei-I/O.
Ist NowPlayingBridge.exe noch nicht gebaut (siehe Docstring dort), faellt
NowPlayingReader automatisch auf eine reine Fenstertitel-Heuristik zurueck
(parameters.KNOWN_PLAYER_PROCESSES, z.B. "Interpret - Titel" bei Spotify) --
dann gibt's Titel/Interpret, aber kein Cover. Ohne Cover zeigt die Scheibe
eine kleine rotierende Pixel-Art-Animation statt eines leeren Kreises.

Threading-Modell:
- Die Audioaufnahme laeuft NICHT in einem eigenen Python-Thread: PyAudioWPatch
  (PortAudio) ruft AudioAnalyzer._audio_callback direkt aus seinem eigenen
  nativen Audio-Thread auf, sobald ein Block bereitsteht.
- Now-Playing-Abfrage laeuft in einem eigenen Daemon-Thread (NowPlayingReader._loop),
  der entweder NowPlayingBridge.exe pollt oder (Fallback) Fenstertitel scannt.
- Ergebnisse gehen NICHT direkt in Tkinter, sondern ueber Callbacks nach
  draussen; MusicModeWindow marshallt sie per self.after(0, ...) in den
  GUI-Thread zurueck
"""

import io
import json
import logging
import math
import subprocess
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from tkinter import ttk

import numpy as np
import pyaudiowpatch as pyaudio

from .config import parameters
from .controller import apply_dark_titlebar

try:
    from PIL import Image, ImageDraw, ImageTk
    _PIL_AVAILABLE = True
except Exception:
    _PIL_AVAILABLE = False

try:
    import win32gui
    import win32process
    import win32api
    import win32con
    _MEDIA_AVAILABLE = True
except Exception:
    _MEDIA_AVAILABLE = False


@dataclass
class AudioFrame:
    """Ein Analyseergebnis fuer einen Audio-Block."""
    bass: float = 0.0
    mid: float = 0.0
    treble: float = 0.0
    beat: float = 0.0
    pitch: float = 0.0
    bars: np.ndarray = None
    waveform: np.ndarray = None
    error: Exception = None


class AudioAnalyzer:
    """Nimmt System-Loopback-Audio auf und berechnet daraus mehrere Live-Kennzahlen:
    - bass/mid/treble: geglaettete Energie in drei Frequenzbaendern (0..1)
    - beat: kurzer, abklingender Puls bei ploetzlichem Energieanstieg
      (einfache Onset-Erkennung, kein echtes BPM-Tracking)
    - pitch: normalisierter Spektralschwerpunkt (0=dumpf/bassig, 1=hell/hochfrequent) --
      eher fuer kontinuierliche Rotations-/Speed-Parameter geeignet als eine Bandenergie
    - bars: Spektrum in log-verteilten Baendern, fuers Balken-Display
    - waveform: kurzer Ausschnitt der Rohsamples, fuers Oszilloskop-Display

    Nutzt PyAudioWPatch (dedizierter WASAPI-Loopback-Fork von PyAudio) statt
    sounddevice: dessen WasapiSettings(loopback=True) existiert schlicht nicht
    als High-Level-API -- das war ein Fehler meinerseits. PortAudio/PyAudio
    ruft _audio_callback direkt aus seinem eigenen nativen Audio-Thread auf,
    sobald ein Block bereitsteht -- kein eigener threading.Thread noetig.
    (Davor: soundcard, das auf manchen Geraeten mit STATUS_HEAP_CORRUPTION
    abstuerzte.)
    """

    def __init__(self, on_frame, blocksize: int = parameters.BLOCK_SIZE,
                 gain: float = 1.5, smoothing: float = 0.7, n_bars: int = parameters.N_BARS):
        self.on_frame = on_frame            # callback(frame: AudioFrame)
        self.blocksize = blocksize
        self.gain = gain                    # Empfindlichkeit, live aenderbar
        self.smoothing = smoothing          # 0..~0.95, hoeher = traeger/ruhiger
        self.n_bars = n_bars
        self.samplerate = parameters.SAMPLE_RATE  # Platzhalter, wird in start() durchs echte Geraet ersetzt
        self._channels = 2                  # Platzhalter, wird in start() durchs echte Geraet ersetzt

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
        try:
            wasapi_info = p.get_host_api_info_by_type(pyaudio.paWASAPI)
        except OSError as e:
            raise RuntimeError("WASAPI ist auf diesem System nicht verfuegbar") from e

        default_speakers = p.get_device_info_by_index(wasapi_info["defaultOutputDevice"])
        if default_speakers["isLoopbackDevice"]:
            return default_speakers

        for loopback in p.get_loopback_device_info_generator():
            if default_speakers["name"] in loopback["name"]:
                return loopback

        raise RuntimeError("Kein passendes WASAPI-Loopback-Geraet gefunden")

    def _audio_callback(self, in_data, frame_count, time_info, status):
        # Laeuft im nativen PortAudio-Thread, nicht in einem von uns gestarteten Thread
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

        # Beat/Onset: Gesamtenergie vs. ihr gleitender Mittelwert der letzten ~1s
        total_energy = float(np.sqrt(np.mean(spectrum ** 2)))
        avg_energy = float(np.mean(self._energy_history)) if self._energy_history else 0.0
        self._energy_history.append(total_energy)
        is_onset = (avg_energy > 0 and total_energy > avg_energy * parameters.BEAT_THRESHOLD_RATIO
                    and total_energy > parameters.BEAT_MIN_ENERGY)
        self._beat_level = max(1.0 if is_onset else 0.0, self._beat_level * parameters.BEAT_DECAY)

        # Pitch: normalisierter Spektralschwerpunkt (0=bassig, 1=hell)
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
    """Sucht unter den sichtbaren Top-Level-Fenstern eines bekannten Media-Players
    (parameters.KNOWN_PLAYER_PROCESSES) und gibt dessen Fenstertitel zurueck, oder None."""
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
    # Gaengiges Format vieler Player: "Interpret - Titel"
    if " - " in raw:
        artist, _, title = raw.partition(" - ")
        return artist.strip(), title.strip()
    return "", raw.strip()


class NowPlayingReader:
    """Liefert Titel/Interpret/Cover des aktuell spielenden Tracks.

    Primaer: startet NowPlayingBridge.exe (C#/.NET, first-party WinRT) als
    Hintergrundprozess und pollt deren JSON-/Cover-Ausgabedateien -- liefert
    Titel, Interpret UND Cover-Art (dieselbe Quelle wie die Windows-Vorschau).

    Fallback (falls parameters.NOWPLAYING_BRIDGE_EXE nicht existiert, also noch
    nicht gebaut wurde): reine Fenstertitel-Heuristik ueber bekannte Media-
    Player-Prozesse (parameters.KNOWN_PLAYER_PROCESSES), z.B. "Interpret -
    Titel" bei Spotify -- liefert nur Titel/Interpret, kein Cover. Ohne pywin32
    bleibt auch dieser Fallback inaktiv, on_update wird dann nie aufgerufen.
    """

    def __init__(self, on_update, poll_interval: float = 1.0):
        self.on_update = on_update  # callback(title: str, artist: str, cover_bytes: bytes | None)
        self.poll_interval = poll_interval
        self._running = False
        self._thread = None
        self._process = None
        self._last_signature = None
        self._use_bridge = parameters.NOWPLAYING_BRIDGE_EXE.exists()

    def start(self) -> None:
        if self._running:
            return
        if not self._use_bridge and not _MEDIA_AVAILABLE:
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

    def _start_bridge_process(self) -> None:
        cache_dir = parameters.NOWPLAYING_CACHE_DIR
        cache_dir.mkdir(exist_ok=True)
        try:
            self._process = subprocess.Popen(
                [str(parameters.NOWPLAYING_BRIDGE_EXE), str(cache_dir), str(int(self.poll_interval * 1000))],
                creationflags=subprocess.CREATE_NO_WINDOW,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except Exception:
            logging.exception("NowPlayingBridge.exe konnte nicht gestartet werden")
            self._process = None
            self._use_bridge = False

    def _loop(self) -> None:
        while self._running:
            if self._use_bridge:
                self._poll_bridge_files()
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
            return

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


class MusicModeWindow(tk.Toplevel):
    """Eigenstaendiges Fenster: Spektrum + Oszilloskop nebeneinander, Scheibe mit
    Titel/Interpret darunter, Kanal-Mapping und Start/Stop-Settings."""

    def __init__(self, parent: tk.Tk, channel_names: dict, set_channel_value,
                 restore_sliders, on_closed, colors: dict):
        """
        channel_names:     {channel_nr: "1: Show Select", ...}
        set_channel_value: callback(channel: int, value: int) -> None
        restore_sliders:   callback(channels: list[int]) -> None
        on_closed:         callback() -> None, wird beim Schliessen dieses Fensters
                            aufgerufen (Hauptfenster soll sich dann wieder zeigen)
        colors:             dict mit BG/BG_LIGHT/FG/ACCENT/ACCENT_DARK/STATUS_TEXT
                            (gleiche Form wie parameters.ACTIVE_SCHEME)
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

    # --------- Aufbau
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

        # Kleine rotierende Pixel-Art-Punkte als Fallback-"Label", solange kein
        # echtes Cover vorliegt (siehe Modul-Docstring)
        self._pixel_ids = []
        for i in range(parameters.PIXEL_DOT_COUNT):
            color = colors["ACCENT"] if i % 2 == 0 else colors["ACCENT_DARK"]
            dot_id = self.disc_canvas.create_rectangle(0, 0, 0, 0, fill=color, outline="")
            self._pixel_ids.append(dot_id)
        self.disc_canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5,
                                      fill=colors["FG"], outline="")

        # Cover-Image liegt zuletzt im Zeichen-Stapel -> ueberdeckt die Pixel-
        # Punkte automatisch, sobald ein Cover gesetzt wird (image=None zeichnet nichts)
        self._cover_image_item = self.disc_canvas.create_image(cx, cy, image=None)
        self._cover_photo = None   # Referenz halten, sonst raeumt Tkinter das Bild weg
        self._base_cover = None    # zirkulaer maskiertes, ungedrehtes PIL-Image
        self._disc_angle = 0.0

        self.track_label = ttk.Label(parent, text="", font=("Segoe UI", 9, "bold"),
                                      wraplength=size + 20, justify="center")
        self.track_label.pack(pady=(5, 0))

        if not _MEDIA_AVAILABLE and not parameters.NOWPLAYING_BRIDGE_EXE.exists():
            self.track_label.config(text="(NowPlayingBridge.exe fehlt, pywin32 fehlt -> kein Titel)")
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

    # --------- Settings-Callbacks
    def _on_gain_change(self, value) -> None:
        self.analyzer.gain = float(value)

    def _on_smoothing_change(self, value) -> None:
        self.analyzer.smoothing = float(value)

    # --------- Audio-Frames (Worker-Thread -> GUI-Thread)
    def _on_frame(self, frame: AudioFrame) -> None:
        self.after(0, self._apply_frame, frame)

    def _apply_frame(self, frame: AudioFrame) -> None:
        if frame.error is not None:
            logging.error("Music Mode Audiofehler", exc_info=frame.error)
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

        # Mapping kann waehrend des Laufs geaendert werden -> Slider freigeben,
        # die gerade nicht mehr gemappt sind
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
            logging.exception("Cover konnte nicht verarbeitet werden")
            self._base_cover = None

    # --------- Schliessen
    def _on_close(self) -> None:
        self.analyzer.stop()
        if self.now_playing:
            self.now_playing.stop()
        if self._active_channels:
            self.restore_sliders(list(self._active_channels))
        self.on_closed()
        self.destroy()
