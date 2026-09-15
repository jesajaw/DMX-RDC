"""
DMX Derby Controller
=====================

Tkinter GUI to control a Razor Derby over a USB-DMX adapter
(RS485, 250000 baud, 2 stop bits).

Threading model
----------------
- Connecting (serial.Serial(...)) runs in a worker thread since opening a COM port can block
- DMX send cycle runs in its own background thread while connected
- All GUI updates from background threads go through root.after(...), since Tkinter widgets must only be touched from main thread
- Write failures during the send loop (e.g. adapter unplugged) are caught and reported as a connection loss. This checks the USB link to the adapter, not the DMX cable itself -- DMX512 is unidirectional and gives no feedback from the fixture end.
"""

import json
import threading
import time
from pathlib import Path

import serial, serial.tools.list_ports
import tkinter as tk
from tkinter import ttk

import numpy as np
import soundcard as sc
import warnings
warnings.filterwarnings("ignore", module="soundcard")

SAMPLE_RATE = 48000
BLOCK_SIZE = 1024
N_BARS = 24
BAR_FREQ_RANGE = (20, 16000)  # log-verteilte Grenzen fuers Spektrum

BAND_RANGES = {
    "bass": (20, 250),
    "mid": (250, 4000),
    "treble": (4000, 16000),
}

_SCHEMES = {
    "dark_purple": dict(BG="#1e1e24", BG_LIGHT="#2a2a33", FG="#e0dff0", ACCENT="#9b59d9", ACCENT_DARK="#6c3fa0", STATUS_TEXT="#c9a6f5",),
    "dark_blue": dict(BG="#1e1e24", BG_LIGHT="#2a2a33", FG="#e0dff0", ACCENT="#4a90d9", ACCENT_DARK="#2f5f9e", STATUS_TEXT="#a6c9f5",),
    "black_white": dict(BG="#000000", BG_LIGHT="#1a1a1a", FG="#ffffff", ACCENT="#ffffff", ACCENT_DARK="#808080", STATUS_TEXT="#d9d9d9",),
}
COLOR_SCHEME = "dark_purple"

_active = _SCHEMES[COLOR_SCHEME]
COLOR_BG = _active["BG"]
COLOR_BG_LIGHT = _active["BG_LIGHT"]
COLOR_FG = _active["FG"]
COLOR = _active["ACCENT"]
COLOR_DARK = _active["ACCENT_DARK"]
COLOR_STATUS_TEXT = _active["STATUS_TEXT"]

CELL_WIDTH = 260
CELL_HEIGHT = 90
STATUS_LABEL_CHARS = 32

CHANNEL_COUNT = 9
UNIVERSE_SIZE = 513  # channel 0 unused, DMX starts at 1
SEND_INTERVAL_S = 0.03  # ca. 33 Hz

PRESETS_DIR = Path(__file__).parent / "presets" # used for json channel settings


class Controller:
    # wraps the serial DMX512 link to a USB-DMX adapter
    def __init__(self, port: str):
        self.ser = serial.Serial(
            port=port,
            baudrate=250000,
            bytesize=serial.EIGHTBITS,
            parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_TWO,
        )
        self.data = bytearray(UNIVERSE_SIZE)

    def set_channel(self, channel: int, value: int) -> None:
        if 1 <= channel <= 512:
            self.data[channel] = max(0, min(255, value))

    def send(self) -> None:
        # sends DMX frame, including break / mark-after-break
        self.ser.break_condition = True
        time.sleep(0.0001)
        self.ser.break_condition = False
        time.sleep(0.000012)
        self.ser.write(self.data)

    def stop(self) -> None:
        # zeroes all channels, sends once, then closes the port
        for i in range(1, UNIVERSE_SIZE):
            self.data[i] = 0
        try:
            self.send()
            self.ser.close()
        except Exception:
            pass


class DMXUI:
    CHANNEL_NAMES = [
        "1: Show Select",
        "2: Speed",
        "3: Derby Color",
        "4: Derby Strobe",
        "5: Derby Motor",
        "6: Pattern",
        "7: Laser Mode",
        "8: Laser Strobe",
        "9: Laser Rotation",
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("DMX Derby Controller")
        self.root.configure(bg=COLOR_BG)

        self.dmx: Controller | None = None
        self.is_sending = False
        self.channel_labels: dict[int, ttk.Label] = {}
        self.sliders: dict[int, ttk.Scale] = {}
        self.presets = PresetManager(PRESETS_DIR)

        self._setup_style()
        self._build_connection_bar()
        self._build_channel_grid()
        self._build_preset_bar()
        self._size_to_content()

    # channel value -> readable state
    @staticmethod
    def describe(channel: int, value: int) -> str:
        if channel == 1:
            if value <= 9:    return f"{value} | Manual (Blackout/Ch.3 active)"
            if value <= 44:   return f"{value} | Derby + Laser + Strobe"
            if value <= 79:   return f"{value} | Derby + Strobe"
            if value <= 114:  return f"{value} | Derby + Laser"
            if value <= 149:  return f"{value} | Laser + Strobe"
            if value <= 184:  return f"{value} | Derby Effect"
            if value <= 219:  return f"{value} | Laser Effect"
            return f"{value} | Strobe Effect"
        if channel == 2:
            if value <= 250:  return f"{value} | Speed: {int(value / 250 * 100)}%"
            return f"{value} | Sound Control"
        if channel == 3:
            if value <= 5:    return f"{value} | Off"
            if value <= 20:   return f"{value} | Red"
            if value <= 35:   return f"{value} | Green"
            if value <= 50:   return f"{value} | Blue"
            if value <= 65:   return f"{value} | White"
            if value <= 80:   return f"{value} | Red + Green"
            if value <= 95:   return f"{value} | Red + Blue"
            if value <= 110:  return f"{value} | Red + White"
            if value <= 125:  return f"{value} | Green + Blue"
            if value <= 140:  return f"{value} | Green + White"
            if value <= 155:  return f"{value} | Blue + White"
            if value <= 170:  return f"{value} | Red + Green + Blue"
            if value <= 185:  return f"{value} | Red + Green + White"
            if value <= 200:  return f"{value} | Green + Blue + White"
            if value <= 215:  return f"{value} | RGBW (All)"
            if value <= 230:  return f"{value} | Auto Color (4)"
            return f"{value} | Auto Color (7)"
        if channel == 4:
            if value <= 5:    return f"{value} | Strobe Off"
            return f"{value} | Derby Strobe Rate: {int(value / 255 * 100)}%"
        if channel == 5:
            if value == 0:    return f"{value} | Motor Stopped"
            if value <= 127:  return f"{value} | Manual Position: {value}"
            return f"{value} | Rotation Speed: {int((value - 128) / 127 * 100)}%"
        if channel == 6:
            if value <= 9:    return f"{value} | Blackout"
            return f"{value} | Pattern {min(18, (value - 10) // 14 + 1)}"
        if channel == 7:
            if value <= 9:    return f"{value} | Laser Off"
            if value <= 49:   return f"{value} | Red"
            if value <= 89:   return f"{value} | Green"
            if value <= 129:  return f"{value} | Red + Green"
            if value <= 169:  return f"{value} | Red + Strobe Green"
            if value <= 209:  return f"{value} | Green + Strobe Red"
            return f"{value} | Red + Green (Strobe)"
        if channel == 8:
            if value <= 9:    return f"{value} | Laser Strobe Off"
            if value <= 254:  return f"{value} | Laser Strobe Rate: {int(value / 254 * 100)}%"
            return f"{value} | Sound-Controlled Strobe"
        if channel == 9:
            if value <= 4:    return f"{value} | Stopped"
            if value <= 127:  return f"{value} | Rotation CW"
            if value <= 133:  return f"{value} | Stopped"
            return f"{value} | Rotation CCW"
        return f"{value}"

    # --------- theme
    def _setup_style(self) -> None:
        style = ttk.Style()
        style.theme_use("clam")

        style.configure(".", background=COLOR_BG, foreground=COLOR_FG, font=("Segoe UI", 9))
        style.configure("TFrame", background=COLOR_BG)
        style.configure("TLabelframe", background=COLOR_BG, foreground=COLOR_FG, bordercolor=COLOR_DARK)
        style.configure("TLabelframe.Label", background=COLOR_BG, foreground=COLOR)
        style.configure("TLabel", background=COLOR_BG, foreground=COLOR_FG)

        style.configure("TButton", background=COLOR_BG_LIGHT, foreground=COLOR_FG, bordercolor=COLOR_DARK, focusthickness=1, padding=6)
        style.map("TButton", background=[("active", COLOR_DARK), ("pressed", COLOR)], foreground=[("active", COLOR_FG)])

        style.configure("TCombobox", fieldbackground=COLOR_BG_LIGHT, background=COLOR_BG_LIGHT, foreground=COLOR_FG, arrowcolor=COLOR)
        style.map("TCombobox", fieldbackground=[("readonly", COLOR_BG_LIGHT)])
        style.configure("Horizontal.TScale", background=COLOR_BG, troughcolor=COLOR_BG_LIGHT)
        style.configure("TEntry", fieldbackground=COLOR_BG_LIGHT, foreground=COLOR_FG,insertcolor=COLOR_FG)

        style.configure("Blackout.TButton", background=COLOR_DARK, foreground=COLOR_FG)
        style.map("Blackout.TButton", background=[("active", COLOR)])

        style.configure("Cell.TFrame", background=COLOR_BG_LIGHT, bordercolor=COLOR_DARK)
        style.configure("Status.TLabel", background=COLOR_BG_LIGHT, foreground=COLOR_STATUS_TEXT, font=("Consolas", 9))
        style.configure("CellTitle.TLabel", background=COLOR_BG_LIGHT, foreground=COLOR_FG, font=("Segoe UI", 9, "bold"))

    # --------- UI
    def _build_connection_bar(self) -> None:
        bar = ttk.LabelFrame(self.root, text="Connection", padding=10)
        bar.pack(fill="x", padx=10, pady=5)

        ttk.Label(bar, text="Port:").pack(side="left", padx=5)
        ports = [p.device for p in serial.tools.list_ports.comports()] or ["COM3", "COM4"]
        self.port_cb = ttk.Combobox(bar, values=ports, width=15, state="readonly")
        self.port_cb.pack(side="left", padx=5)
        self.port_cb.current(0)

        self.btn_connect = ttk.Button(bar, text="Connect", command=self.toggle_connection)
        self.btn_connect.pack(side="left", padx=5)

        ttk.Button(bar, text="BLACKOUT", command=self.blackout,
                   style="Blackout.TButton").pack(side="right", padx=5)
        ttk.Button(bar, text="🎵 Music Mode", command=self._open_music_mode).pack(side="right", padx=5)

    def _build_channel_grid(self) -> None:
        grid = ttk.LabelFrame(self.root, text="DMX Channels", padding=10)
        grid.pack(fill="both", expand=True, padx=10, pady=5)

        for i in range(CHANNEL_COUNT):
            channel = i + 1
            row, col = divmod(i, 3)
            grid.columnconfigure(col, weight=1)
            self._build_cell(grid, row, col, channel, self.CHANNEL_NAMES[i])

    def _build_cell(self, parent: ttk.Frame, row: int, col: int, channel: int, name: str) -> None:
        cell = ttk.Frame(parent, padding=5, relief="groove", style="Cell.TFrame")
        cell.grid(row=row, column=col, padx=5, pady=5, sticky="nsew")
        cell.pack_propagate(False)
        cell.configure(width=CELL_WIDTH, height=CELL_HEIGHT)

        ttk.Label(cell, text=name, style="CellTitle.TLabel").pack(anchor="w")

        status = ttk.Label(cell, text="---", style="Status.TLabel",
                            width=STATUS_LABEL_CHARS, anchor="w")
        status.pack(anchor="w", pady=(2, 5), fill="x")
        self.channel_labels[channel] = status

        slider = ttk.Scale(cell, from_=0, to=255, orient="horizontal",
                            command=lambda v, c=channel: self.on_slider_change(c, v))
        slider.set(0)
        slider.pack(fill="x", expand=True)
        self.sliders[channel] = slider

        self.update_display(channel, 0)

    def _build_preset_bar(self) -> None:
        bar = ttk.LabelFrame(self.root, text="Presets", padding=10)
        bar.pack(fill="x", padx=10, pady=5)

        self.preset_cb = ttk.Combobox(bar, values=self.presets.list_presets(),
                                       width=20, state="readonly")
        self.preset_cb.pack(side="left", padx=5)
        if self.preset_cb["values"]:
            self.preset_cb.current(0)

        ttk.Button(bar, text="Load", command=self.load_preset).pack(side="left", padx=5)
        ttk.Button(bar, text="Save As...", command=self.save_preset_as).pack(side="left", padx=5)
        ttk.Button(bar, text="Delete", command=self.delete_preset).pack(side="left", padx=5)

    def _open_music_mode(self) -> None:
        channel_names = {ch: self.CHANNEL_NAMES[ch - 1] for ch in range(1, CHANNEL_COUNT + 1)}
        window = MusicModeWindow(
            self.root,
            channel_names=channel_names,
            set_channel_value=self._music_set_channel,
            restore_sliders=self._music_restore_sliders,
            on_closed=self._on_music_mode_closed,
            colors=_active,
        )
        window.update_idletasks()
        self.root.withdraw()

    def _on_music_mode_closed(self) -> None:
        self.root.deiconify()

    def _music_set_channel(self, channel: int, value: int) -> None:
        slider = self.sliders.get(channel)
        if slider is None:
            return
        if str(slider.cget("state")) != "disabled":
            slider.state(["disabled"])
        slider.set(value)  # feuert on_slider_change -> Anzeige + dmx.set_channel

    def _music_restore_sliders(self, channels: list[int]) -> None:
        for channel in channels:
            slider = self.sliders.get(channel)
            if slider is not None:
                slider.state(["!disabled"])

    def _size_to_content(self) -> None:
        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        height = self.root.winfo_reqheight()
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(width, height)

    # --------- Sliders
    def update_display(self, channel: int, value) -> None:
        val_int = int(float(value))
        self.channel_labels[channel].config(text=self.describe(channel, val_int))

    def on_slider_change(self, channel: int, value) -> None:
        val_int = int(float(value))
        self.update_display(channel, val_int)
        if self.dmx:
            self.dmx.set_channel(channel, val_int)


    # --------- non-blocking connection
    def toggle_connection(self) -> None:
        if not self.is_sending:
            self.btn_connect.config(state="disabled")
            port = self.port_cb.get()
            threading.Thread(target=self._connect_worker, args=(port,), daemon=True).start()
        else:
            self.stop_dmx()
            self.btn_connect.config(text="Connect")

    def _connect_worker(self, port: str) -> None:
        try:
            dmx = Controller(port)
            for channel, slider in self.sliders.items():
                dmx.set_channel(channel, int(slider.get()))
            self.root.after(0, self._connect_success, dmx)
        except Exception as e:
            self.root.after(0, self._connect_failed, e, port)

    def _connect_success(self, dmx: Controller) -> None:
        self.dmx = dmx
        self.is_sending = True
        threading.Thread(target=self._send_loop, daemon=True).start()
        self.btn_connect.config(text="Disconnect", state="normal")

    def _connect_failed(self, error: Exception, port: str) -> None:
        self.btn_connect.config(state="normal")
        show_error(self.root, "Error", f"Could not open {port}:\n{error}")

    def _connection_lost(self, error: Exception) -> None:
        self.is_sending = False
        self.dmx = None
        self.btn_connect.config(text="Connect", state="normal")
        show_error(self.root, "Connection Lost", f"DMX connection interrupted:\n{error}")

    def _send_loop(self) -> None:
        # Background send cycle -- exits and reporst on write failure (unplug) instead of failing silently
        while self.is_sending:
            try:
                self.dmx.send()
            except (serial.SerialException, OSError) as e:
                self.root.after(0, self._connection_lost, e)
                return
            time.sleep(SEND_INTERVAL_S)


    # --------- actions
    def refresh_preset_list(self, select: str | None = None) -> None:
        names = self.presets.list_presets()
        self.preset_cb["values"] = names
        if select in names:
            self.preset_cb.set(select)
        elif names:
            self.preset_cb.current(0)
        else:
            self.preset_cb.set("")

    def save_preset_as(self) -> None:
        name = ask_string(self.root, "Save Preset", "Preset name:")
        if not name:
            return
        values = {ch: int(slider.get()) for ch, slider in self.sliders.items()}
        self.presets.save(name, values)
        self.refresh_preset_list(select=name)

    def load_preset(self) -> None:
        name = self.preset_cb.get()
        if not name:
            return
        try:
            values = self.presets.load(name)
        except (OSError, json.JSONDecodeError) as e:
            show_error(self.root, "Error", f"Could not load preset '{name}':\n{e}")
            return
        for ch, val in values.items():
            if ch in self.sliders:
                self.sliders[ch].set(val)
                self.update_display(ch, val)
                if self.dmx:
                    self.dmx.set_channel(ch, val)

    def delete_preset(self) -> None:
        name = self.preset_cb.get()
        if not name:
            return
        if ask_yes_no(self.root, "Delete Preset", f"Delete preset '{name}'?"):
            self.presets.delete(name)
            self.refresh_preset_list()

    def blackout(self) -> None:
        for channel, slider in self.sliders.items():
            slider.set(0)
            self.update_display(channel, 0)
            if self.dmx:
                self.dmx.set_channel(channel, 0)

    def stop_dmx(self) -> None:
        self.is_sending = False
        if self.dmx:
            self.dmx.stop()
            self.dmx = None

    def on_close(self) -> None:
        self.stop_dmx()
        self.root.destroy()

# Reads & writes channel presets, one JSON file per preset, stored in a folder
class PresetManager:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(exist_ok=True)

    def list_presets(self) -> list[str]: # returns preset names (without .json), sorted alphabetically
        return sorted(p.stem for p in self.directory.glob("*.json"))

    def save(self, name: str, values: dict[int, int]) -> None: # writes {channel: value} to <name>.json
        path = self.directory / f"{name}.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(values, f, indent=2)

    def load(self, name: str) -> dict[int, int]: # reads <name>.json back into {channel: value}
        path = self.directory / f"{name}.json"
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(ch): int(val) for ch, val in raw.items()}

    def delete(self, name: str) -> None:
        (self.directory / f"{name}.json").unlink(missing_ok=True)


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
        try:
            sc.default_speaker()
        except Exception as e:
            self.on_levels(None, None, None, None, error=e)
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



class ThemedDialog(tk.Toplevel):
    # Themed popups caz the tkinter.messagebox / simpledialog are boring

    def __init__(self, parent: tk.Tk, title: str, message: str, buttons: list[str], with_entry: bool = False):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=COLOR_BG)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        self.result: str | None = None
        self.entry_value: str | None = None

        ttk.Label(self, text=message, wraplength=280, justify="left").pack(
            padx=20, pady=(20, 10)
        )

        if with_entry:
            self.entry = ttk.Entry(self, width=30)
            self.entry.pack(padx=20, pady=(0, 10))
            self.entry.focus_set()
            self.entry.bind("<Return>", lambda e: self._on_button(buttons[0]))

        btn_row = ttk.Frame(self)
        btn_row.pack(padx=20, pady=(0, 20))
        for label in buttons:
            ttk.Button(btn_row, text=label,
                       command=lambda l=label: self._on_button(l)).pack(side="left", padx=5)

        self.bind("<Escape>", lambda e: self._on_button(None))
        self.protocol("WM_DELETE_WINDOW", lambda: self._on_button(None))

        self.update_idletasks()
        self._center_on(parent)
        self.wait_window(self)

    def _center_on(self, parent: tk.Tk) -> None:
        x = parent.winfo_rootx() + (parent.winfo_width() - self.winfo_width()) // 2
        y = parent.winfo_rooty() + (parent.winfo_height() - self.winfo_height()) // 2
        self.geometry(f"+{x}+{y}")

    def _on_button(self, label: str | None) -> None:
        self.result = label
        if hasattr(self, "entry"):
            self.entry_value = self.entry.get()
        self.grab_release()
        self.destroy()


def show_error(parent: tk.Tk, title: str, message: str) -> None:
    ThemedDialog(parent, title, message, buttons=["OK"])
def ask_yes_no(parent: tk.Tk, title: str, message: str) -> bool:
    dlg = ThemedDialog(parent, title, message, buttons=["Yes", "No"])
    return dlg.result == "Yes"
def ask_string(parent: tk.Tk, title: str, message: str) -> str | None:
    dlg = ThemedDialog(parent, title, message, buttons=["OK", "Cancel"], with_entry=True)
    if dlg.result == "OK" and dlg.entry_value:
        return dlg.entry_value
    return None


def main() -> None:
    root = tk.Tk()
    app = DMXUI(root)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()


if __name__ == "__main__":
    main()