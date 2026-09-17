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

Die Titel/Interpret-Anzeige liest den Fenstertitel bekannter Media-Player-
Prozesse aus (siehe KNOWN_PLAYER_PROCESSES) -- bewusst KEIN winsdk/winrt, da
dieses Projekt archiviert ist. Vorteil: aktiv gepflegtes pywin32, keine
Build-Toolchain noetig. Nachteil: kein Cover-Art moeglich, und das Format
("Interpret - Titel") ist Player-abhaengig und nicht garantiert.

Threading-Modell:
- Die Audioaufnahme laeuft NICHT in einem eigenen Python-Thread: PyAudioWPatch
  (PortAudio) ruft AudioAnalyzer._audio_callback direkt aus seinem eigenen
  nativen Audio-Thread auf, sobald ein Block bereitsteht.
- Now-Playing-Abfrage laeuft in einem eigenen Daemon-Thread (NowPlayingReader._loop)
- Ergebnisse gehen NICHT direkt in Tkinter, sondern ueber Callbacks nach
  draussen; MusicModeWindow marshallt sie per self.after(0, ...) in den
  GUI-Thread zurueck
"""

import logging
import math
import threading
import time
import tkinter as tk
from collections import deque
from dataclasses import dataclass
from tkinter import ttk

import numpy as np
import pyaudiowpatch as pyaudio

from .controller import apply_dark_titlebar

try:
    import win32gui
    import win32process
    import win32api
    import win32con
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

# Music-Mode-Quellen, die auf DMX-Kanäle gemappt werden können
SOURCES = ("bass", "mid", "treble", "beat", "pitch")
SOURCE_LABELS = {
    "bass": "Bass", "mid": "Mid", "treble": "Treble", "beat": "Beat", "pitch": "Pitch",
}

# Spectrum/Waveform bewusst gleich gross, damit sie symmetrisch nebeneinander sitzen
BAR_CANVAS_WIDTH = 280
BAR_CANVAS_HEIGHT = 150
WAVE_CANVAS_WIDTH = 280
WAVE_CANVAS_HEIGHT = 150
DISC_SIZE = 150

# Pixel-Art-"Label" auf der Scheibe (Ersatz fuer echtes Cover-Art, siehe Docstring)
PIXEL_DOT_COUNT = 8
PIXEL_DOT_RADIUS = 22
PIXEL_DOT_SIZE = 6
SPIN_STEP_DEG = 6
SPIN_INTERVAL_MS = 80

# Bekannte Media-Player-Prozesse, deren Fenstertitel nach "Interpret - Titel"
# durchsucht wird. Bei Bedarf einfach ergaenzen.
KNOWN_PLAYER_PROCESSES = {
    "spotify.exe", "vlc.exe", "foobar2000.exe", "wmplayer.exe",
    "musicbee.exe", "itunes.exe", "winamp.exe", "aimp.exe",
}


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

    def __init__(self, on_frame, blocksize: int = BLOCK_SIZE,
                 gain: float = 1.5, smoothing: float = 0.7, n_bars: int = N_BARS):
        self.on_frame = on_frame            # callback(frame: AudioFrame)
        self.blocksize = blocksize
        self.gain = gain                    # Empfindlichkeit, live aenderbar
        self.smoothing = smoothing          # 0..~0.95, hoeher = traeger/ruhiger
        self.n_bars = n_bars
        self.samplerate = SAMPLE_RATE       # Platzhalter, wird in start() durchs echte Geraet ersetzt
        self._channels = 2                  # Platzhalter, wird in start() durchs echte Geraet ersetzt

        self._running = False
        self._pa = None
        self._stream = None
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
    (KNOWN_PLAYER_PROCESSES) und gibt dessen Fenstertitel zurueck, oder None."""
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
        if _get_process_name(pid) in KNOWN_PLAYER_PROCESSES:
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
    """Liest Titel/Interpret aus dem Fenstertitel bekannter Media-Player-Prozesse
    (siehe KNOWN_PLAYER_PROCESSES), z.B. "Interpret - Titel" bei Spotify.
    Windows-only, kein Cover-Art moeglich mit diesem Ansatz, und das Format ist
    Player-abhaengig -- daf uer aber komplett ohne winsdk/winrt (siehe Docstring
    oben). Ohne pywin32 bleibt sie inaktiv, on_update wird dann nie aufgerufen."""

    def __init__(self, on_update, poll_interval: float = 2.0):
        self.on_update = on_update  # callback(title: str, artist: str)
        self.poll_interval = poll_interval
        self._running = False
        self._thread = None
        self._last_raw = None

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
            raw = _find_now_playing_title()
            if raw and raw != self._last_raw:
                self._last_raw = raw
                artist, title = _parse_title(raw)
                self.on_update(title, artist)
            time.sleep(self.poll_interval)


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
        self.disc_canvas = tk.Canvas(parent, width=DISC_SIZE, height=DISC_SIZE,
                                      bg=colors["BG"], highlightthickness=0)
        self.disc_canvas.pack()

        cx = cy = DISC_SIZE / 2
        self.disc_canvas.create_oval(4, 4, DISC_SIZE - 4, DISC_SIZE - 4,
                                      outline=colors["ACCENT_DARK"], width=2)
        for r in range(int(DISC_SIZE / 2) - 8, 24, -12):
            self.disc_canvas.create_oval(cx - r, cy - r, cx + r, cy + r,
                                          outline=colors["ACCENT_DARK"], width=1)

        # Kleine rotierende Pixel-Art-Punkte als "Label" der Schallplatte -- echtes
        # Cover-Art ist ohne winsdk/winrt nicht verfuegbar (siehe Modul-Docstring)
        self._pixel_ids = []
        for i in range(PIXEL_DOT_COUNT):
            color = colors["ACCENT"] if i % 2 == 0 else colors["ACCENT_DARK"]
            dot_id = self.disc_canvas.create_rectangle(0, 0, 0, 0, fill=color, outline="")
            self._pixel_ids.append(dot_id)
        self.disc_canvas.create_oval(cx - 5, cy - 5, cx + 5, cy + 5,
                                      fill=colors["FG"], outline="")
        self._disc_angle = 0.0

        self.track_label = ttk.Label(parent, text="", font=("Segoe UI", 9, "bold"),
                                      wraplength=DISC_SIZE + 20, justify="center")
        self.track_label.pack(pady=(5, 0))

        if not _MEDIA_AVAILABLE:
            self.track_label.config(text="(pywin32 fehlt -> kein Titel)")
        else:
            self.now_playing = NowPlayingReader(on_update=self._on_now_playing)
            self.now_playing.start()

        self._spin_disc()

    def _spin_disc(self) -> None:
        if not self.winfo_exists():
            return
        self._disc_angle = (self._disc_angle + SPIN_STEP_DEG) % 360
        cx = cy = DISC_SIZE / 2
        count = len(self._pixel_ids)
        for i, dot_id in enumerate(self._pixel_ids):
            angle = math.radians(self._disc_angle + i * (360 / count))
            x = cx + PIXEL_DOT_RADIUS * math.cos(angle)
            y = cy + PIXEL_DOT_RADIUS * math.sin(angle)
            half = PIXEL_DOT_SIZE / 2
            self.disc_canvas.coords(dot_id, x - half, y - half, x + half, y + half)
        self.after(SPIN_INTERVAL_MS, self._spin_disc)

    def _build_plots(self, parent: ttk.Frame) -> None:
        colors = self.colors

        spectrum_col = ttk.Frame(parent)
        spectrum_col.pack(side="left", padx=(0, 15))
        ttk.Label(spectrum_col, text="Spectrum", style="CellTitle.TLabel").pack(anchor="w")
        self.bar_canvas = tk.Canvas(spectrum_col, width=BAR_CANVAS_WIDTH, height=BAR_CANVAS_HEIGHT,
                                     bg=colors["BG_LIGHT"], highlightthickness=0)
        self.bar_canvas.pack()
        bar_width = BAR_CANVAS_WIDTH / N_BARS
        for i in range(N_BARS):
            x0 = i * bar_width + 2
            x1 = x0 + bar_width - 4
            bar_id = self.bar_canvas.create_rectangle(
                x0, BAR_CANVAS_HEIGHT, x1, BAR_CANVAS_HEIGHT, fill=colors["ACCENT"], width=0)
            self._bar_ids.append(bar_id)

        wave_col = ttk.Frame(parent)
        wave_col.pack(side="left")
        ttk.Label(wave_col, text="Waveform", style="CellTitle.TLabel").pack(anchor="w")
        self.wave_canvas = tk.Canvas(wave_col, width=WAVE_CANVAS_WIDTH, height=WAVE_CANVAS_HEIGHT,
                                      bg=colors["BG_LIGHT"], highlightthickness=0)
        self.wave_canvas.pack()
        mid_y = WAVE_CANVAS_HEIGHT / 2
        self._wave_line = self.wave_canvas.create_line(
            0, mid_y, WAVE_CANVAS_WIDTH, mid_y, fill=colors["ACCENT"], width=1.5, smooth=True)

    def _build_mapping(self) -> None:
        mapping = ttk.LabelFrame(self, text="Channel Mapping", padding=10)
        mapping.pack(fill="x", padx=10, pady=5)

        options = ["None"] + [self.channel_names[c] for c in sorted(self.channel_names)]
        for source in SOURCES:
            row = ttk.Frame(mapping)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=SOURCE_LABELS[source], width=8).pack(side="left")

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

    # --------- Now Playing
    def _on_now_playing(self, title: str, artist: str) -> None:
        self.after(0, self._apply_now_playing, title, artist)

    def _apply_now_playing(self, title: str, artist: str) -> None:
        text = f"{title}\n{artist}" if artist else (title or "")
        self.track_label.config(text=text)

    # --------- Schliessen
    def _on_close(self) -> None:
        self.analyzer.stop()
        if self.now_playing:
            self.now_playing.stop()
        if self._active_channels:
            self.restore_sliders(list(self._active_channels))
        self.on_closed()
        self.destroy()
