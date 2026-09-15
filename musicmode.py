"""
Music Mode
==========

Eigenes Fenster (MusicModeWindow), das die Systemsounds (WASAPI-Loopback des
aktuellen Ausgabegeräts) in Echtzeit analysiert, als Spektrum + Oszilloskop
visualisiert, Bass/Mid/Treble/Beat/Pitch auf DMX-Kanäle mappen kann und dazu
optional (Windows) Titel/Cover des aktuell spielenden Tracks auf einer
rotierenden Scheibe anzeigt.

Das Hauptfenster (DMXUI) wird beim Öffnen versteckt (root.withdraw()) und dient
nur noch als Backend: Verbindung/Send-Loop laufen unverändert weiter, Kanalwerte
werden über die vom Hauptfenster übergebenen Callbacks gesetzt.

Abhängigkeiten:
    pip install sounddevice numpy pillow
    pip install winsdk   # optional, nur fuer Titel/Cover (Windows-only)

Hinweis Plattform:
- Windows: Loopback-Audio laeuft ohne weitere Einrichtung (WASAPI-Loopback des
  Default-Ausgabegeraets, per sounddevice/PortAudio). Titel/Cover ebenfalls
  nur unter Windows (winsdk).
- macOS/Linux: keine WASAPI-Loopback -- AudioAnalyzer.start() liefert dann
  einen Fehler (ueber on_frame(AudioFrame(error=...))), der Rest der UI
  bleibt aber benutzbar. Kein Titel/Cover.

Die winsdk-Integration ist best-effort: die exakte Python-Projektion der
WinRT-Medien-API kann sich je nach installierter winsdk-Version leicht
unterscheiden (v.a. beim Auslesen der Thumbnail-Bytes). Schlaegt sie fehl,
bleibt einfach Titel/Cover leer -- der Rest von Music Mode funktioniert normal.

Threading-Modell:
- Die Audioaufnahme laeuft NICHT in einem eigenen Python-Thread: sounddevice
  (PortAudio) ruft AudioAnalyzer._audio_callback direkt aus seinem eigenen
  nativen Audio-Thread auf, sobald ein Block bereitsteht.
- Now-Playing-Abfrage laeuft in einem eigenen Daemon-Thread (NowPlayingReader._loop)
- Ergebnisse gehen NICHT direkt in Tkinter, sondern ueber Callbacks nach
  draussen; MusicModeWindow marshallt sie per self.after(0, ...) in den
  GUI-Thread zurueck
"""

import io
import logging
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from tkinter import ttk

import numpy as np
import sounddevice as sd

from controller import apply_dark_titlebar

try:
    from PIL import Image, ImageDraw, ImageTk
    _PIL_AVAILABLE = True
except Exception:
    _PIL_AVAILABLE = False

try:
    import asyncio

    from winsdk.windows.media.control import (
        GlobalSystemMediaTransportControlsSessionManager as MediaManager,
    )
    from winsdk.windows.storage.streams import DataReader
    _MEDIA_AVAILABLE = True
except Exception:
    _MEDIA_AVAILABLE = False


SAMPLE_RATE = 48000                  # Platzhalter, wird beim Start durchs echte Geraet ersetzt
BLOCK_SIZE = 1024
N_BARS = 24
WAVE_POINTS = 160
BAR_FREQ_RANGE = (20, 16000)         # log-verteilte Grenzen fuers Spektrum
ENERGY_HISTORY_LEN = 43              # ~1s bei ~21ms/Block, fuer Beat-Erkennung
BEAT_THRESHOLD_RATIO = 1.3           # Energie muss X-fach ueber dem Mittel liegen
BEAT_MIN_ENERGY = 0.02               # Mindestenergie, damit Stille keinen Beat ausloest
BEAT_DECAY = 0.75                    # Abklingfaktor des Beat-Pulses pro Block
PITCH_REFERENCE_HZ = 4000.0          # Normalisierungsreferenz fuer den Spektralschwerpunkt

BAND_RANGES = {
    "bass": (20, 250),
    "mid": (250, 4000),
    "treble": (4000, 16000),
}

# Kanal-Kategorien: welcher DMX-Kanal ist fuer welche Art von Steuerung gedacht
CHANNEL_CATEGORIES = {
    1: "mode",     # Show Select
    2: "speed",    # Speed
    3: "color",    # Derby Color
    4: "strobe",   # Derby Strobe
    5: "speed",    # Derby Motor
    6: "pattern",  # Pattern
    7: "mode",     # Laser Mode
    8: "strobe",   # Laser Strobe
    9: "speed",    # Laser Rotation
}

SOURCES = ("bass", "mid", "treble", "beat", "pitch")
SOURCE_INFO = {
    "bass":   {"label": "Bass",   "hint": "empfohlen: Farbe/Pattern",   "recommended": {"color", "pattern"}},
    "mid":    {"label": "Mid",    "hint": "empfohlen: Farbe/Pattern",   "recommended": {"color", "pattern"}},
    "treble": {"label": "Treble", "hint": "empfohlen: Farbe/Pattern",   "recommended": {"color", "pattern"}},
    "beat":   {"label": "Beat",   "hint": "empfohlen: Strobe",          "recommended": {"strobe"}},
    "pitch":  {"label": "Pitch",  "hint": "empfohlen: Speed/Rotation",  "recommended": {"speed"}},
}

BAR_CANVAS_WIDTH = 260
BAR_CANVAS_HEIGHT = 120
WAVE_CANVAS_WIDTH = 200
WAVE_CANVAS_HEIGHT = 120
DISC_SIZE = 150
COVER_SIZE = 104


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

    Nutzt sounddevice (PortAudio) mit WASAPI-Loopback: PortAudio ruft
    _audio_callback direkt aus seinem eigenen nativen Audio-Thread auf, sobald
    ein Block bereitsteht -- kein eigener threading.Thread noetig. (Vorher:
    soundcard/soundcard-WASAPI, das auf manchen Geraeten mit
    STATUS_HEAP_CORRUPTION abstuerzte.)
    """

    def __init__(self, on_frame, blocksize: int = BLOCK_SIZE,
                 gain: float = 1.5, smoothing: float = 0.7, n_bars: int = N_BARS):
        self.on_frame = on_frame            # callback(frame: AudioFrame)
        self.blocksize = blocksize
        self.gain = gain                    # Empfindlichkeit, live aenderbar
        self.smoothing = smoothing          # 0..~0.95, hoeher = traeger/ruhiger
        self.n_bars = n_bars
        self.samplerate = SAMPLE_RATE       # Platzhalter, wird in start() durchs echte Geraet ersetzt

        self._running = False
        self._stream: sd.InputStream | None = None
        self._levels = {"bass": 0.0, "mid": 0.0, "treble": 0.0}
        self._bar_levels = np.zeros(n_bars)
        self._bar_edges = np.geomspace(BAR_FREQ_RANGE[0], BAR_FREQ_RANGE[1], n_bars + 1)
        self._window = np.hanning(blocksize)
        self._energy_history = deque(maxlen=ENERGY_HISTORY_LEN)
        self._beat_level = 0.0
        self._pitch_level = 0.0

    def start(self) -> None:
        if self._running:
            return
        try:
            device_index, samplerate, channels = self._resolve_loopback_device()
            wasapi_settings = sd.WasapiSettings(loopback=True)
            self._stream = sd.InputStream(
                device=device_index, channels=channels, samplerate=samplerate,
                blocksize=self.blocksize, dtype="float32",
                extra_settings=wasapi_settings, callback=self._audio_callback,
            )
            self._stream.start()
        except Exception as e:
            self._stream = None
            self.on_frame(AudioFrame(error=e))
            return
        self.samplerate = samplerate
        self._running = True

    def stop(self) -> None:
        self._running = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    @staticmethod
    def _resolve_loopback_device() -> tuple[int, int, int]:
        # Muss ueber die WASAPI-Hostapi laufen (nicht sd.default.device), sonst
        # greift extra_settings=WasapiSettings(loopback=True) nicht
        hostapis = sd.query_hostapis()
        wasapi_idx = next((i for i, api in enumerate(hostapis) if api["name"] == "Windows WASAPI"), None)
        if wasapi_idx is None:
            raise RuntimeError("WASAPI-Hostapi nicht gefunden (kein Windows?)")

        output_idx = hostapis[wasapi_idx]["default_output_device"]
        if output_idx is None or output_idx < 0:
            raise RuntimeError("Kein WASAPI-Standardausgabegeraet gefunden")

        info = sd.query_devices(output_idx)
        return output_idx, int(info["default_samplerate"]), info["max_output_channels"]

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status) -> None:
        # Laeuft im nativen PortAudio-Thread, nicht in einem von uns gestarteten Thread
        mono = indata.mean(axis=1) if indata.ndim > 1 else indata
        self._process(mono)

    def _process(self, samples: np.ndarray) -> None:
        window = self._window if len(samples) == len(self._window) else np.hanning(len(samples))
        windowed = samples * window
        spectrum = np.abs(np.fft.rfft(windowed)) / len(windowed)
        freqs = np.fft.rfftfreq(len(windowed), d=1.0 / self.samplerate)

        for name, (lo, hi) in BAND_RANGES.items():
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
        is_onset = (avg_energy > 0 and total_energy > avg_energy * BEAT_THRESHOLD_RATIO
                    and total_energy > BEAT_MIN_ENERGY)
        self._beat_level = max(1.0 if is_onset else 0.0, self._beat_level * BEAT_DECAY)

        # Pitch: normalisierter Spektralschwerpunkt (0=bassig, 1=hell)
        magnitude_sum = float(np.sum(spectrum))
        centroid = float(np.sum(freqs * spectrum) / magnitude_sum) if magnitude_sum > 0 else 0.0
        pitch_norm = min(1.0, centroid / PITCH_REFERENCE_HZ)
        self._pitch_level = self.smoothing * self._pitch_level + (1 - self.smoothing) * pitch_norm

        step = max(1, len(samples) // WAVE_POINTS)
        waveform = np.clip(samples[::step], -1.0, 1.0)

        self.on_frame(AudioFrame(
            bass=self._levels["bass"], mid=self._levels["mid"], treble=self._levels["treble"],
            beat=self._beat_level, pitch=self._pitch_level,
            bars=self._bar_levels.copy(), waveform=waveform,
        ))


class NowPlayingReader:
    """Liest Titel/Interpret/Cover der aktuell unter Windows spielenden Medien-Session
    ueber die WinRT GlobalSystemMediaTransportControlsSessionManager-API (winsdk-Paket).
    Windows-only und best-effort: ohne winsdk oder bei API-Aenderungen bleibt sie inaktiv,
    on_update wird dann einfach nie aufgerufen."""

    def __init__(self, on_update, poll_interval: float = 2.0):
        self.on_update = on_update  # callback(title: str, artist: str, cover_bytes: bytes | None)
        self.poll_interval = poll_interval
        self._running = False
        self._thread = None
        self._last_key = None

    def start(self) -> None:
        if not _MEDIA_AVAILABLE or self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False

    def _loop(self) -> None:
        while self._running:
            try:
                result = asyncio.run(self._fetch())
            except Exception:
                result = None
            if result is not None:
                title, artist, cover_bytes = result
                key = (title, artist)
                if key != self._last_key:
                    self._last_key = key
                    self.on_update(title, artist, cover_bytes)
            time.sleep(self.poll_interval)

    async def _fetch(self):
        manager = await MediaManager.request_async()
        session = manager.get_current_session()
        if session is None:
            return None
        props = await session.try_get_media_properties_async()
        title = props.title or ""
        artist = props.artist or ""

        cover_bytes = None
        thumb_ref = props.thumbnail
        if thumb_ref is not None:
            try:
                stream = await thumb_ref.open_read_async()
                size = stream.size
                reader = DataReader(stream)
                await reader.load_async(size)
                buf = bytearray(size)
                reader.read_bytes(buf)
                cover_bytes = bytes(buf)
            except Exception:
                cover_bytes = None

        return title, artist, cover_bytes


class MusicModeWindow(tk.Toplevel):
    """Eigenstaendiges Fenster: Spektrum + Oszilloskop, rotierende Cover-Scheibe,
    Kanal-Mapping (mit Empfehlungs-Markierung) und Start/Stop-Settings."""

    def __init__(self, parent: tk.Tk, channel_names: dict, set_channel_value,
                 restore_sliders, on_closed, colors: dict):
        """
        channel_names:     {channel_nr: "1: Show Select", ...}
        set_channel_value: callback(channel: int, value: int) -> None
        restore_sliders:   callback(channels: list[int]) -> None
        on_closed:         callback() -> None, wird beim Schliessen dieses Fensters
                            aufgerufen (Hauptfenster soll sich dann wieder zeigen)
        colors:             dict mit BG/BG_LIGHT/FG/ACCENT/ACCENT_DARK/STATUS_TEXT
                            (gleiche Form wie die Scheme-Dicts im Hauptfenster)
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

        disc_col = ttk.Frame(viz)
        disc_col.pack(side="left", padx=(0, 15))
        self._build_disc(disc_col)

        plots_col = ttk.Frame(viz)
        plots_col.pack(side="left", fill="both", expand=True)
        self._build_plots(plots_col)

        self._build_mapping()
        self._build_settings()

        self.status_label = ttk.Label(self, text="", foreground="#c0392b")
        self.status_label.pack(pady=(4, 0))

        ttk.Button(self, text="Back to Manual Control", command=self._on_close).pack(pady=10)

    def _build_disc(self, parent: ttk.Frame) -> None:
        colors = self.colors
        self.disc_canvas = tk.Canvas(parent, width=DISC_SIZE, height=DISC_SIZE,
                                      bg=colors["BG"], highlightthickness=0)
        self.disc_canvas.pack()

        cx = cy = DISC_SIZE / 2
        self.disc_canvas.create_oval(4, 4, DISC_SIZE - 4, DISC_SIZE - 4,
                                      outline=colors["ACCENT_DARK"], width=2)
        for r in range(int(DISC_SIZE / 2) - 8, 24, -12):
            self.disc_canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                          outline=colors["ACCENT_DARK"], width=1)
        self._cover_image_item = self.disc_canvas.create_image(cx, cy, image=None)
        self._cover_photo = None   # Referenz halten, sonst raeumt Tkinter das Bild weg
        self._base_cover = None    # zirkulaer maskiertes, ungedrehtes PIL-Image
        self._disc_angle = 0.0

        self.track_label = ttk.Label(parent, text="", font=("Segoe UI", 9, "bold"),
                                      wraplength=DISC_SIZE + 20, justify="center")
        self.track_label.pack(pady=(5, 0))

        if not _PIL_AVAILABLE:
            self.track_label.config(text="(Pillow fehlt -> kein Cover)")
        elif not _MEDIA_AVAILABLE:
            self.track_label.config(text="(winsdk fehlt -> kein Titel/Cover)")
        else:
            self.now_playing = NowPlayingReader(on_update=self._on_now_playing)
            self.now_playing.start()

        self._spin_disc()

    def _build_plots(self, parent: ttk.Frame) -> None:
        colors = self.colors

        ttk.Label(parent, text="Spectrum", style="CellTitle.TLabel").pack(anchor="w")
        self.bar_canvas = tk.Canvas(parent, width=BAR_CANVAS_WIDTH, height=BAR_CANVAS_HEIGHT,
                                     bg=colors["BG_LIGHT"], highlightthickness=0)
        self.bar_canvas.pack(pady=(0, 5))
        bar_width = BAR_CANVAS_WIDTH / N_BARS
        for i in range(N_BARS):
            x0 = i * bar_width + 2
            x1 = x0 + bar_width - 4
            bar_id = self.bar_canvas.create_rectangle(
                x0, BAR_CANVAS_HEIGHT, x1, BAR_CANVAS_HEIGHT, fill=colors["ACCENT"], width=0)
            self._bar_ids.append(bar_id)

        ttk.Label(parent, text="Waveform", style="CellTitle.TLabel").pack(anchor="w")
        self.wave_canvas = tk.Canvas(parent, width=WAVE_CANVAS_WIDTH, height=WAVE_CANVAS_HEIGHT,
                                      bg=colors["BG_LIGHT"], highlightthickness=0)
        self.wave_canvas.pack()
        mid_y = WAVE_CANVAS_HEIGHT / 2
        self._wave_line = self.wave_canvas.create_line(
            0, mid_y, WAVE_CANVAS_WIDTH, mid_y, fill=colors["ACCENT"], width=1.5, smooth=True)

    def _build_mapping(self) -> None:
        mapping = ttk.LabelFrame(self, text="Channel Mapping", padding=10)
        mapping.pack(fill="x", padx=10, pady=5)

        for source in SOURCES:
            info = SOURCE_INFO[source]
            row = ttk.Frame(mapping)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=info["label"], width=8).pack(side="left")

            var = tk.StringVar(value="None")
            self.mapping_vars[source] = var
            cb = ttk.Combobox(row, values=self._options_for(source), textvariable=var,
                               width=24, state="readonly")
            cb.pack(side="left", padx=5)

            meter = ttk.Progressbar(row, orient="horizontal", length=90, maximum=100)
            meter.pack(side="left", padx=5, fill="x", expand=True)
            self.meters[source] = meter

            ttk.Label(row, text=info["hint"], style="Status.TLabel").pack(side="left", padx=5)

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

    # --------- Mapping-Optionen mit Empfehlungs-Markierung
    def _options_for(self, source: str) -> list:
        recommended = SOURCE_INFO[source]["recommended"]
        starred, rest = [], []
        for ch in sorted(self.channel_names):
            label = self.channel_names[ch]
            if CHANNEL_CATEGORIES.get(ch) in recommended:
                starred.append(f"\u2605 {label}")
            else:
                rest.append(label)
        return ["None", *starred, *rest]

    def _channel_for(self, source: str):
        label = self.mapping_vars[source].get()
        if label == "None":
            return None
        label = label.removeprefix("\u2605 ")
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
        bar_width = BAR_CANVAS_WIDTH / N_BARS
        for i, level in enumerate(bars):
            x0 = i * bar_width + 2
            x1 = x0 + bar_width - 4
            y1 = BAR_CANVAS_HEIGHT
            y0 = BAR_CANVAS_HEIGHT - level * BAR_CANVAS_HEIGHT
            self.bar_canvas.coords(self._bar_ids[i], x0, y0, x1, y1)

    def _update_waveform(self, waveform) -> None:
        if waveform is None or len(waveform) < 2:
            return
        n = len(waveform)
        mid_y = WAVE_CANVAS_HEIGHT / 2
        points = []
        for i, sample in enumerate(waveform):
            x = i / (n - 1) * WAVE_CANVAS_WIDTH
            y = mid_y - sample * mid_y * 0.9
            points.extend((x, y))
        self.wave_canvas.coords(self._wave_line, *points)

    # --------- Now Playing / Cover-Scheibe
    def _spin_disc(self) -> None:
        if not self.winfo_exists():
            return
        self._disc_angle = (self._disc_angle - 4) % 360
        if _PIL_AVAILABLE and self._base_cover is not None:
            rotated = self._base_cover.rotate(self._disc_angle, resample=Image.BICUBIC)
            self._cover_photo = ImageTk.PhotoImage(rotated)
            self.disc_canvas.itemconfig(self._cover_image_item, image=self._cover_photo)
        self.after(80, self._spin_disc)

    def _on_now_playing(self, title: str, artist: str, cover_bytes) -> None:
        self.after(0, self._apply_now_playing, title, artist, cover_bytes)

    def _apply_now_playing(self, title: str, artist: str, cover_bytes) -> None:
        text = f"{title}\n{artist}" if artist else (title or "")
        self.track_label.config(text=text)

        if not (_PIL_AVAILABLE and cover_bytes):
            return
        try:
            img = Image.open(io.BytesIO(cover_bytes)).convert("RGBA")
            img = img.resize((COVER_SIZE, COVER_SIZE), Image.LANCZOS)
            mask = Image.new("L", (COVER_SIZE, COVER_SIZE), 0)
            ImageDraw.Draw(mask).ellipse((0, 0, COVER_SIZE, COVER_SIZE), fill=255)
            img.putalpha(mask)
            self._base_cover = img
        except Exception:
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