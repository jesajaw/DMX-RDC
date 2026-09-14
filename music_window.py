"""
Music Mode
==========

Eigenes Fenster (MusicModeWindow), das die Systemsounds (WASAPI-Loopback des
aktuellen Ausgabegeräts) in Echtzeit analysiert, als Spektrum visualisiert und
Bass-/Mitten-/Höhen-Pegel auf beliebige DMX-Kanäle mappen kann.

Das Hauptfenster (DMXUI) wird beim Öffnen versteckt (root.withdraw()) und dient
nur noch als Backend: Verbindung/Send-Loop laufen unverändert weiter, Kanalwerte
werden über die vom Hauptfenster übergebenen Callbacks gesetzt.

Abhängigkeiten:
    pip install soundcard numpy

Hinweis Plattform:
- Windows: läuft ohne weitere Einrichtung (WASAPI-Loopback des Default-Ausgabegeräts).
- macOS: braucht i.d.R. ein virtuelles Loopback-Device (z.B. BlackHole),
  da CoreAudio kein natives Output-Loopback kennt.
- Linux: nutzt den PulseAudio "Monitor"-Source des Ausgabegeräts.

Threading-Modell:
- Aufnahme/FFT läuft in einem eigenen Daemon-Thread (AudioAnalyzer._loop)
- Ergebnisse gehen NICHT direkt in Tkinter, sondern über das on_levels-Callback
  nach draußen; MusicModeWindow marshallt sie per self.after(0, ...) in den
  GUI-Thread zurück
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import ttk

import numpy as np
import soundcard as sc

SAMPLE_RATE = 48000
BLOCK_SIZE = 1024
N_BARS = 24
BAR_FREQ_RANGE = (20, 16000)  # log-verteilte Grenzen fuers Spektrum

BAND_RANGES = {
    "bass": (20, 250),
    "mid": (250, 4000),
    "treble": (4000, 16000),
}


class AudioAnalyzer:
    """Nimmt System-Loopback-Audio auf, liefert Bass/Mid/Treble (0..1) + Spektrum-Balken."""

    def __init__(self, on_levels, samplerate: int = SAMPLE_RATE, blocksize: int = BLOCK_SIZE,
                 gain: float = 1.5, smoothing: float = 0.7, n_bars: int = N_BARS):
        self.on_levels = on_levels          # callback(bass, mid, treble, bars, error=None)
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.gain = gain                    # Empfindlichkeit, live änderbar
        self.smoothing = smoothing          # 0..~0.95, höher = träger/ruhiger
        self.n_bars = n_bars

        self._running = False
        self._thread: threading.Thread | None = None
        self._levels = {"bass": 0.0, "mid": 0.0, "treble": 0.0}
        self._bar_levels = np.zeros(n_bars)
        self._bar_edges = np.geomspace(BAR_FREQ_RANGE[0], BAR_FREQ_RANGE[1], n_bars + 1)
        self._window = np.hanning(blocksize)

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
            self._thread = None

    def _loop(self) -> None:
        try:
            speaker = sc.default_speaker()
            mic = sc.get_microphone(id=str(speaker.name), include_loopback=True)
        except Exception as e:
            self.on_levels(None, None, None, None, error=e)
            return

        try:
            with mic.recorder(samplerate=self.samplerate, blocksize=self.blocksize) as rec:
                while self._running:
                    data = rec.record(numframes=self.blocksize)
                    mono = data.mean(axis=1) if data.ndim > 1 else data
                    self._process(mono)
        except Exception as e:
            if self._running:
                self.on_levels(None, None, None, None, error=e)

    def _process(self, samples: np.ndarray) -> None:
        windowed = samples * self._window
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

        self.on_levels(self._levels["bass"], self._levels["mid"], self._levels["treble"],
                        self._bar_levels.copy())


class MusicModeWindow(tk.Toplevel):
    """Eigenständiges Fenster: Spektrum-Visualisierung + Kanal-Mapping + Start/Stop-Settings."""

    BANDS = ("bass", "mid", "treble")
    CANVAS_WIDTH = 440
    CANVAS_HEIGHT = 140

    def __init__(self, parent: tk.Tk, channel_names: dict[int, str],
                 set_channel_value, restore_sliders, on_closed, colors: dict):
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
        self.configure(bg=colors["BG"])
        self.resizable(False, False)
        self.transient(parent)

        self.channel_names = channel_names
        self.set_channel_value = set_channel_value
        self.restore_sliders = restore_sliders
        self.on_closed = on_closed

        self.analyzer = AudioAnalyzer(on_levels=self._on_levels)
        self.mapping_vars: dict[str, tk.StringVar] = {}
        self.meters: dict[str, ttk.Progressbar] = {}
        self._active_channels: set[int] = set()
        self._bar_ids: list[int] = []

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # das Fenster selbst repraesentiert "Music Mode aktiv" -> sofort starten
        self.analyzer.start()

    def _build(self) -> None:
        colors = self.colors

        viz = ttk.LabelFrame(self, text="Now Playing", padding=10)
        viz.pack(fill="x", padx=10, pady=(10, 5))

        self.canvas = tk.Canvas(viz, width=self.CANVAS_WIDTH, height=self.CANVAS_HEIGHT,
                                 bg=colors["BG_LIGHT"], highlightthickness=0)
        self.canvas.pack()
        bar_width = self.CANVAS_WIDTH / N_BARS
        for i in range(N_BARS):
            x0 = i * bar_width + 2
            x1 = x0 + bar_width - 4
            bar_id = self.canvas.create_rectangle(
                x0, self.CANVAS_HEIGHT, x1, self.CANVAS_HEIGHT,
                fill=colors["ACCENT"], width=0,
            )
            self._bar_ids.append(bar_id)

        mapping = ttk.LabelFrame(self, text="Channel Mapping", padding=10)
        mapping.pack(fill="x", padx=10, pady=5)

        options = ["None"] + [self.channel_names[c] for c in sorted(self.channel_names)]
        for band in self.BANDS:
            row = ttk.Frame(mapping)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=band.capitalize(), width=8).pack(side="left")

            var = tk.StringVar(value="None")
            self.mapping_vars[band] = var
            cb = ttk.Combobox(row, values=options, textvariable=var, width=22, state="readonly")
            cb.pack(side="left", padx=5)

            meter = ttk.Progressbar(row, orient="horizontal", length=100, maximum=100)
            meter.pack(side="left", padx=5, fill="x", expand=True)
            self.meters[band] = meter

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

        self.status_label = ttk.Label(self, text="", foreground="#c0392b")
        self.status_label.pack(pady=(4, 0))

        ttk.Button(self, text="Back to Manual Control", command=self._on_close).pack(pady=10)

    def _channel_for(self, band: str) -> int | None:
        label = self.mapping_vars[band].get()
        if label == "None":
            return None
        for ch, name in self.channel_names.items():
            if name == label:
                return ch
        return None

    def _on_gain_change(self, value) -> None:
        self.analyzer.gain = float(value)

    def _on_smoothing_change(self, value) -> None:
        self.analyzer.smoothing = float(value)

    def _on_levels(self, bass, mid, treble, bars, error=None) -> None:
        # Laeuft im Audio-Worker-Thread -- Ruecksprung in den GUI-Thread
        self.after(0, self._apply_levels, bass, mid, treble, bars, error)

    def _apply_levels(self, bass, mid, treble, bars, error) -> None:
        if error is not None:
            self.status_label.config(text=f"Audio error: {error}")
            self._on_close()
            return

        self._update_bars(bars)

        levels = {"bass": bass, "mid": mid, "treble": treble}
        current_channels: set[int] = set()
        for band, level in levels.items():
            self.meters[band]["value"] = level * 100
            channel = self._channel_for(band)
            if channel is not None:
                current_channels.add(channel)
                self.set_channel_value(channel, int(level * 255))

        # Mapping kann waehrend des Laufs geaendert werden -> Slider freigeben,
        # die gerade nicht mehr gemappt sind
        freed = self._active_channels - current_channels
        if freed:
            self.restore_sliders(list(freed))
        self._active_channels = current_channels

    def _update_bars(self, bars: np.ndarray) -> None:
        bar_width = self.CANVAS_WIDTH / N_BARS
        for i, level in enumerate(bars):
            x0 = i * bar_width + 2
            x1 = x0 + bar_width - 4
            y1 = self.CANVAS_HEIGHT
            y0 = self.CANVAS_HEIGHT - level * self.CANVAS_HEIGHT
            self.canvas.coords(self._bar_ids[i], x0, y0, x1, y1)

    def _on_close(self) -> None:
        self.analyzer.stop()
        if self._active_channels:
            self.restore_sliders(list(self._active_channels))
        self.on_closed()
        self.destroy()
