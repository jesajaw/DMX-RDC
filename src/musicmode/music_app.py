"""
Music Mode (app / UI)
=====================

The Music Mode window. It contains no analysis and no light logic -- it only wires the pieces together:

    audio_source.LoopbackSource  ->  analysis.Analyzer  ->  engine.LightEngine  ->  controller.set_many(values, MUSIC)
                                                  \\-> this window (visualisation, status)
    nowplaying.NowPlayingReader  ->  this window (title / artist / spinning cover) + the song key for the analysis cache

Left: the round cover in the middle. Around it the waveform as a closed ring (with two fading echoes) and, further
out, the four frequency bands (Sub / Bass / Mids / Highs) as radial bars (bar length = amplitude, mirrored left /
right) whose arcs flash on a hit. No text on it: hover the mouse over the visual and the axes fade in -- dB rings,
Hz ticks and a read-out of the frequency / level under the cursor.

Right: live status (BPM + re-check button, band meters), the looks as one row of buttons, the mapping grid (band x
what it does, one click per cell), a few extras as checkboxes and the tuning sliders. The audio device is shown as
a small icon (speaker / headphones / headset) next to "DMX connected" in the footer.

The main window (DMXUI) is hidden while this is open. Both windows talk to the lights through the same
DMXController: this window acquire()s it (manual sliders no longer reach the wire, the output starts dark)
and release()s it on close, which puts the manual setup back exactly as it was. The engine writes the whole
9-channel frame every block (channels it never drives, like Ch1, are simply 0).

Threading: audio blocks arrive on the capture thread; analysis, engine and the DMX write run right there
so the lights never wait for the GUI. The GUI only gets the newest frame (config.UI_FPS times a second at
most, nothing at all while minimised) and marshals everything else back onto its own thread with self.after.
"""

import io
import logging
import math
import time
import tkinter as tk
from tkinter import ttk

import numpy as np

from .. import theme
from ..config import ACTIVE_SCHEME, FONT, MONO
from ..controller import MUSIC, DMXController, apply_dark_titlebar
from . import config, engine
from .analysis import BAND_LABELS, BAND_NAMES, BAND_RANGES, Analyzer, Features
from .audio_source import LoopbackSource
from .engine import LightEngine
from .nowplaying import NowPlayingReader, now_playing_available

try:
    from PIL import Image, ImageDraw, ImageTk
    _PIL_AVAILABLE = True
except Exception:
    _PIL_AVAILABLE = False


# --------- small helpers
_BASE_RGB = dict(red=(255, 59, 78), green=(45, 255, 122), blue=(61, 123, 255), white=(244, 246, 255))
_MIXES = {"rgb": ("red", "green", "blue"), "rgw": ("red", "green", "white"),
          "gbw": ("green", "blue", "white"), "rgbw": ("red", "green", "blue", "white")}


def preview_colour(key: str) -> str:
    """Rough on-screen colour for a derby / laser colour key (additive: channel-wise max)."""
    if key.startswith("auto"):
        return "#e8e8e8"
    parts = _MIXES.get(key) or key.split("_")
    rgb = [max(_BASE_RGB[p][i] for p in parts if p in _BASE_RGB) for i in range(3)]
    return "#%02x%02x%02x" % tuple(rgb)


def _freq_text(hz: int) -> str:
    return f"{hz // 1000}k" if hz >= 1000 else str(hz)


def _combo(parent, options, current, width, command):
    """Read-only combobox over [(label, value), ...]; calls command(value) on selection."""
    labels = [label for label, _ in options]
    lookup = dict(options)
    var = tk.StringVar()
    for label, value in options:
        if value == current:
            var.set(label)
            break
    else:
        numeric = [(abs(value - current), label) for label, value in options
                   if isinstance(value, (int, float)) and isinstance(current, (int, float))
                   and not isinstance(value, bool)]
        var.set(min(numeric)[1] if numeric else (labels[0] if labels else ""))
    box = ttk.Combobox(parent, values=labels, textvariable=var, state="readonly", width=width)
    box.bind("<<ComboboxSelected>>", lambda e: command(lookup[var.get()]))
    return box


def _hz_text(hz: float) -> str:
    if hz >= 1000:
        return f"{hz / 1000:.1f}".rstrip("0").rstrip(".") + " kHz"
    return f"{hz:.0f} Hz"


def _device_kind(name: str) -> str:
    """Rough guess what the capture device is, from its name: speaker | headphones | headset."""
    n = (name or "").lower()
    if any(w in n for w in ("headset", "hands-free", "handsfree", "hfp")):
        return "headset"
    if any(w in n for w in ("headphone", "kopfh", "airpods", "buds", "earphone", "earbud", "wh-1000", "wf-1000",
                            "bluetooth", "arctis", "hyperx", "cloud")):
        return "headphones"
    return "speaker"


class MappingGrid:
    """The mapping: one row per source (band / beat), one column per thing it can do. One click per cell.

    flash columns   - / ON / OFF     (LED, Derby, Laser)
    change columns  - / cycle        (LED pattern, Derby colour, Laser colour, Derby swing, Laser spin)
    Left click steps forward, right click steps back."""

    def __init__(self, parent, colors: dict, look_getter, on_change):
        self.colors, self.look_getter, self.on_change = colors, look_getter, on_change
        self.ink = theme.on_accent(colors)
        self.frame = ttk.Frame(parent, style="Plain.Card.TFrame")
        self._cells = {}
        self._beat_combo = None
        f = self.frame
        groups = {}
        for ci, col in enumerate(engine.COLUMNS):
            groups.setdefault(col.group, []).append(ci)
        titles = {"flash": "HIT  =  FLASH", "change": "HIT  =  CHANGE"}
        for group, cis in groups.items():
            ttk.Label(f, text=titles[group], style="Group.TLabel").grid(
                row=0, column=1 + cis[0], columnspan=len(cis), pady=(0, 2))
        for ci, col in enumerate(engine.COLUMNS):
            f.columnconfigure(1 + ci, weight=1, uniform="cell")
            ttk.Label(f, text=col.label, style="CardMuted.TLabel", justify="center").grid(row=1, column=1 + ci)
        for ri, (source, label) in enumerate(engine.SOURCES):
            ttk.Label(f, text=label.upper(), style="Card.TLabel").grid(row=2 + ri, column=0, sticky="w", padx=(0, 8))
            for ci, col in enumerate(engine.COLUMNS):
                cell = tk.Label(f, width=5, cursor="hand2", font=(FONT, 8, "bold"))
                cell.grid(row=2 + ri, column=1 + ci, padx=2, pady=2, ipady=3, sticky="ew")
                cell.bind("<Button-1>", lambda e, s=source, c=col: self._step(s, c, +1))
                cell.bind("<Button-3>", lambda e, s=source, c=col: self._step(s, c, -1))
                self._cells[(source, col.key)] = cell
        self._beat_row = 2 + [src for src, _ in engine.SOURCES].index("beat")

    def _state(self, source: str, col) -> str:
        return self.look_getter().grid.get(engine.cell_key(source, col.key), "")

    def _paint(self, source: str, col) -> None:
        c, state = self.colors, self._state(source, col)
        cell = self._cells[(source, col.key)]
        if state == "on":
            cell.config(text="ON", bg=c["ACCENT"], fg=self.ink)
        elif state == "off":
            cell.config(text="OFF", bg=c["ACCENT_DARK"], fg=c["FG"])
        elif state == "cycle":
            cell.config(text="\u21bb", bg=c["ACCENT"], fg=self.ink)
        else:
            cell.config(text="\u2013", bg=c["BG"], fg=c["MUTED"])

    def _step(self, source: str, col, direction: int) -> None:
        look = self.look_getter()
        states = engine.STATES[col.kind]
        key = engine.cell_key(source, col.key)
        new = states[(states.index(self._state(source, col)) + direction) % len(states)]
        if new:
            look.grid[key] = new
        else:
            look.grid.pop(key, None)
        self._paint(source, col)
        self.on_change()

    def refresh(self) -> None:
        """Repaint every cell for the current look (and rebuild the beat-row selector)."""
        for source, _ in engine.SOURCES:
            for col in engine.COLUMNS:
                self._paint(source, col)
        if self._beat_combo is not None:
            self._beat_combo.destroy()
        look = self.look_getter()
        self._beat_combo = _combo(self.frame, list(engine.BEAT_OPTIONS.items()), look.beat_every, 7, self._set_every)
        self._beat_combo.grid(row=self._beat_row, column=1 + len(engine.COLUMNS), padx=(8, 0))

    def _set_every(self, beats) -> None:
        self.look_getter().beat_every = int(beats)
        self.on_change()


class MusicModeWindow(tk.Toplevel):
    """Music Mode window (see the module docstring)."""

    def __init__(self, parent: tk.Tk, controller: DMXController, on_closed):
        """
        controller: the shared DMXController (manual window and Music Mode write through the same one)
        on_closed:  callback() -> None, called when this window closes (the main window shows itself again)
        """
        super().__init__(parent)
        self.title("Music Mode")
        self.colors = colors = ACTIVE_SCHEME
        apply_dark_titlebar(self)
        self.configure(bg=colors["BG"])
        theme.apply_theme(self)
        ttk.Style(self).configure("Small.TButton", padding=(10, 3))

        self.controller = controller
        self.on_closed = on_closed
        self._latest = None              # newest features, picked up by the GUI
        self._ui_pending = False
        self._ui_interval_ms = max(1, int(1000 / config.UI_FPS))
        self._last_ui = 0.0
        self._visible = True             # False while minimised: nothing is drawn then
        self._failed = False

        self.analyzer = Analyzer()
        self.look = engine.DEFAULT.copy()
        self.engine = LightEngine(self.look)
        self.source = LoopbackSource(on_block=self._on_block, on_error=self._on_source_error)
        self.now_playing = None
        self.rows = []                   # RuleRow list
        self._loading = False
        self._status_cache = {}
        self._hit_flags = {n: False for n in BAND_NAMES}     # set on the audio thread, cleared by the GUI
        self._glow = {n: 0.0 for n in BAND_NAMES}
        self._last_flat = None
        self._peaks = None
        self._pulse = 0.0
        self._song_key = ""              # "artist - title" of the playing track (analysis cache key)
        self._device_name = None         # last device the footer icon was drawn for
        self._axis_alpha = 0.0           # 0..1, how visible the Hz / dB axes are (fades with the mouse)
        self._axis_target = 0.0
        self._mouse = None               # (x, y) on the visual canvas while the mouse is over it
        self._wave_frame = 0
        self._wave_layers = None

        self._build()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        # the window is as big as its content needs, but can grow (and everything scales with it)
        self.update_idletasks()
        screen_w, screen_h = self.winfo_screenwidth(), self.winfo_screenheight()
        width = min(self.winfo_reqwidth(), screen_w - 40)
        height = min(self.winfo_reqheight(), screen_h - 90)
        self.minsize(width, height)
        self.geometry(f"{max(width, 1240)}x{height}")

        self.bind("<Unmap>", lambda e: self._set_visible(e, False))
        self.bind("<Map>", lambda e: self._set_visible(e, True))
        self.controller.acquire(MUSIC)
        self.source.start()

    def _set_visible(self, event, visible: bool) -> None:
        if event.widget is self:
            self._visible = visible

    # --------- Layout
    def _build(self) -> None:
        self.columnconfigure(0, weight=3, minsize=480)
        self.columnconfigure(1, weight=2, minsize=520)
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
        self._build_mapping(right, 2)
        self._build_extras(right, 3)
        self._build_tuning(right, 4)
        self._load_look(self.look.name)

        footer = ttk.Frame(self)
        footer.grid(row=1, column=0, columnspan=2, sticky="ew", padx=16, pady=(4, 16))
        self.dmx_label = ttk.Label(footer, text="", style="Muted.TLabel")
        self.dmx_label.pack(side="left", padx=(0, 10))
        self._build_device_icon(footer)
        self.status_label = ttk.Label(footer, text="", foreground="#c0392b")
        self.status_label.pack(side="left", padx=(14, 0))
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

    # --------- Left side: now playing + the cover with the frequency bands around it + waveform
    def _build_visual(self, parent: ttk.Frame) -> None:
        colors = self.colors
        head = ttk.Frame(parent)
        head.grid(row=0, column=0, sticky="ew", pady=(0, 8))
        self.track_label = ttk.Label(head, text="", style="Track.TLabel", wraplength=480)
        self.track_label.pack(anchor="w")
        self.artist_label = ttk.Label(head, text="", style="Artist.TLabel", wraplength=480)
        self.artist_label.pack(anchor="w")

        size = config.VIZ_MIN_SIZE
        self.disc_canvas = tk.Canvas(parent, width=size, height=size, bg=colors["BG_LIGHT"],
                                     highlightthickness=1, highlightbackground=colors["LINE"])
        self.disc_canvas.grid(row=1, column=0, sticky="nsew")
        canvas = self.disc_canvas
        bg = colors["BG_LIGHT"]

        # --- band layout. The ring is mirrored left / right; every half runs from the bottom (Sub) up to the
        # top (Highs). theta = angle from the top, clockwise, for the right half.
        n_bands = len(BAND_NAMES)
        span = 180.0 / n_bands
        gap = config.BAND_GAP_DEG
        self._band_colour = {name: theme.lerp_colour(colors["ACCENT"], colors["ACCENT2"], i / max(1, n_bands - 1))
                             for i, name in enumerate(BAND_NAMES)}
        self._arc_span, thetas, band_of = {}, [], []
        for b, name in enumerate(BAND_NAMES):
            hi = 180.0 - b * span - gap / 2
            lo = hi - (span - gap)
            self._arc_span[name] = (lo, hi)
            count = config.BAND_BARS[name]
            for j in range(count):
                thetas.append(hi - (j + 0.5) * (span - gap) / count)
                band_of.append(name)
        self._thetas = thetas
        self._n_bars = len(thetas)
        self._slots = 2 * self._n_bars                       # first half: right side, second: left side (mirror)
        self._slot_bar = [k % self._n_bars for k in range(self._slots)]
        self._dirs = []
        for k in range(self._slots):
            th = math.radians(thetas[k % self._n_bars])
            self._dirs.append((math.sin(th) if k < self._n_bars else -math.sin(th), -math.cos(th)))
        self._peaks = [0.0] * self._n_bars
        self._bar_cache = [None] * self._slots           # last drawn (bar end, peak end, start radius) per slot

        # --- items, bottom to top. 1) the axes (invisible until the mouse is over the visual)
        self._db_rings = [canvas.create_oval(0, 0, 0, 0, outline=bg, dash=(2, 5)) for _ in config.AXIS_DB_STEPS]
        self._ticks, self._tick_lines, self._tick_labels = [], [], []
        for hz in config.AXIS_HZ_TICKS:
            theta = self._freq_theta(hz)
            if theta is None:
                continue
            self._ticks.append((hz, theta))
            self._tick_lines.append((canvas.create_line(0, 0, 0, 0, fill=bg), canvas.create_line(0, 0, 0, 0, fill=bg)))
            suffix = " Hz" if hz in (config.AXIS_HZ_TICKS[0], config.AXIS_HZ_TICKS[-1]) else ""
            self._tick_labels.append(canvas.create_text(0, 0, text=_freq_text(hz) + suffix, fill=bg,
                                                        font=(FONT, 8)))

        # 2) the waveform ring: the live wave plus fading echoes (outermost echo first, so the live one is on top)
        self._wave_items = []
        for i in reversed(range(config.WAVE_RING_LAYERS)):
            colour = theme.lerp_colour(colors["ACCENT2"], bg, min(0.85, 0.45 * i))
            self._wave_items.append(canvas.create_line(0, 0, 0, 0, fill=colour, width=2 if i == 0 else 1,
                                                       joinstyle="round"))
        self._wave_items.reverse()                        # index 0 = the live wave
        self._wave_n = 120                                # points per half circle (the ring is mirrored)
        phis = np.linspace(0.0, 2 * math.pi, 2 * self._wave_n, endpoint=False)
        self._wave_sin, self._wave_cos = np.sin(phis), np.cos(phis)

        # 3) bars + peak caps, then the hit arcs; the disc is drawn on top later
        self._bar_ids, self._peak_ids = [], []
        for k in range(self._slots):
            colour = self._band_colour[band_of[self._slot_bar[k]]]
            self._bar_ids.append(canvas.create_line(0, 0, 0, 0, fill=colour, width=3, capstyle="butt"))
            self._peak_ids.append(canvas.create_line(0, 0, 0, 0, fill=colors["FG"], width=3, capstyle="butt"))
        self._arc_ids = {}
        for name in BAND_NAMES:
            lo, hi = self._arc_span[name]
            dim = theme.lerp_colour(self._band_colour[name], colors["BG_LIGHT"], 0.55)
            right = canvas.create_arc(0, 0, 1, 1, start=90 - hi, extent=hi - lo, style="arc", outline=dim, width=3)
            left = canvas.create_arc(0, 0, 1, 1, start=90 + lo, extent=hi - lo, style="arc", outline=dim, width=3)
            self._arc_ids[name] = (right, left)

        # 4) dB labels above the bars (on a small background so they stay readable)
        self._db_bg = [canvas.create_rectangle(0, 0, 0, 0, fill=bg, outline="", state="hidden")
                       for _ in config.AXIS_DB_STEPS]
        self._db_text = [canvas.create_text(0, 0, text=f"{db} dB" if db == 0 else str(db), fill=bg, font=(MONO, 8))
                         for db in config.AXIS_DB_STEPS]

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

        # 5) the cursor read-out (Hz / dB under the mouse), topmost
        self._cur_ray = canvas.create_line(0, 0, 0, 0, fill=colors["FG"], dash=(3, 3), state="hidden")
        self._cur_dot = canvas.create_oval(0, 0, 0, 0, outline=colors["FG"], width=2, state="hidden")
        self._cur_bg = canvas.create_rectangle(0, 0, 0, 0, fill=colors["BG"], outline=colors["LINE"], state="hidden")
        self._cur_text = canvas.create_text(0, 0, text="", fill=colors["FG"], font=(MONO, 9), state="hidden")
        self._cursor_key = None

        self._geo = {}
        canvas.bind("<Configure>", lambda e: self._layout_viz())
        canvas.bind("<Enter>", lambda e: self._set_axis_target(1.0))
        canvas.bind("<Leave>", lambda e: self._on_mouse_leave())
        canvas.bind("<Motion>", self._on_mouse_move)

        if now_playing_available():            # otherwise: just no title / cover
            self.now_playing = NowPlayingReader(on_update=self._on_now_playing, on_playing=self._on_playing)
            self.now_playing.start()

        self._layout_viz()
        self._spin_disc()



    def _layout_viz(self) -> None:
        """(Re)computes the geometry of the disc, the waveform ring, the band ring and the axes for the canvas size."""
        canvas = self.disc_canvas
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 60 or h < 60:
            w = h = config.VIZ_MIN_SIZE
        cx, cy = w / 2, h / 2
        r_disc = config.DISC_SIZE / 2
        r_in = r_disc + config.RING_GAP
        max_len = max(18.0, min(w, h) / 2 - r_in - config.BAND_LABEL_MARGIN)
        width = max(2.0, 2 * math.pi * (r_in + 8) / self._slots * 0.55)
        self._geo = dict(cx=cx, cy=cy, r_in=r_in, max_len=max_len, r_wave=r_disc + config.WAVE_RING_OFFSET)

        for item, radius in zip(self._ring_ovals, self._disc_radii):
            canvas.coords(item, cx - radius, cy - radius, cx + radius, cy + radius)
        canvas.coords(self._center_dot, cx - 5, cy - 5, cx + 5, cy + 5)
        canvas.coords(self._cover_image_item, cx, cy)
        for k in range(self._slots):
            canvas.itemconfig(self._bar_ids[k], width=width)
            canvas.itemconfig(self._peak_ids[k], width=width)

        r_arc = r_in - 6
        for name in BAND_NAMES:
            for item in self._arc_ids[name]:
                canvas.coords(item, cx - r_arc, cy - r_arc, cx + r_arc, cy + r_arc)

        # axes: dB rings (a bar of level L ends at r_in + 3 + L * max_len), Hz ticks outside the longest bar
        for ring, bg_item, text, db in zip(self._db_rings, self._db_bg, self._db_text, config.AXIS_DB_STEPS):
            r = r_in + 3 + (1.0 + db / config.DB_RANGE_BARS) * max_len
            canvas.coords(ring, cx - r, cy - r, cx + r, cy + r)
            canvas.coords(text, cx, cy - r)
            x0, y0, x1, y1 = canvas.bbox(text)
            canvas.coords(bg_item, x0 - 3, y0 - 1, x1 + 3, y1 + 1)
        r_out = r_in + max_len + 6
        for (hz, theta), (right, left), label in zip(self._ticks, self._tick_lines, self._tick_labels):
            a = math.radians(theta)
            sx, sy = math.sin(a), -math.cos(a)
            canvas.coords(right, cx + sx * r_out, cy + sy * r_out, cx + sx * (r_out + 8), cy + sy * (r_out + 8))
            canvas.coords(left, cx - sx * r_out, cy + sy * r_out, cx - sx * (r_out + 8), cy + sy * (r_out + 8))
            canvas.coords(label, cx + sx * (r_out + 24), cy + sy * (r_out + 24))
        self._apply_axis_alpha()

        self._place_dots()
        self._bar_cache = [None] * self._slots           # geometry changed -> every bar has to be redrawn
        if self._last_flat is not None:
            self._draw_bars()
        self._cursor_key = None

    # ---- Hz <-> angle (the bars are log-spaced inside each band, band after band, bottom = Sub, top = Highs)
    def _freq_theta(self, hz: float):
        for name in BAND_NAMES:
            lo_hz, hi_hz = BAND_RANGES[name]
            if lo_hz <= hz <= hi_hz:
                lo, hi = self._arc_span[name]
                return hi - math.log(hz / lo_hz) / math.log(hi_hz / lo_hz) * (hi - lo)
        return None

    def _theta_freq(self, theta: float) -> float:
        for name in BAND_NAMES:
            lo, hi = self._arc_span[name]
            if lo - 2.0 <= theta <= hi + 2.0:
                lo_hz, hi_hz = BAND_RANGES[name]
                frac = min(1.0, max(0.0, (hi - theta) / (hi - lo)))
                return lo_hz * (hi_hz / lo_hz) ** frac
        return float(BAND_RANGES[BAND_NAMES[0]][0])

    # ---- the dynamic axes
    def _set_axis_target(self, value: float) -> None:
        self._axis_target = value

    def _on_mouse_leave(self) -> None:
        self._axis_target = 0.0
        self._mouse = None

    def _on_mouse_move(self, event) -> None:
        self._axis_target = 1.0
        self._mouse = (event.x, event.y)

    def _apply_axis_alpha(self) -> None:
        canvas, c, a = self.disc_canvas, self.colors, self._axis_alpha
        line = theme.lerp_colour(c["BG_LIGHT"], c["MUTED"], a * 0.8)
        text = theme.lerp_colour(c["BG_LIGHT"], c["MUTED"], a)
        for ring in self._db_rings:
            canvas.itemconfig(ring, outline=line)
        for right, left in self._tick_lines:
            canvas.itemconfig(right, fill=line)
            canvas.itemconfig(left, fill=line)
        for label in self._tick_labels:
            canvas.itemconfig(label, fill=text)
        for bg_item, label in zip(self._db_bg, self._db_text):
            canvas.itemconfig(label, fill=theme.lerp_colour(c["BG_LIGHT"], c["FG"], a * 0.9))
            canvas.itemconfig(bg_item, state="normal" if a > 0.05 else "hidden")

    def _update_axes(self) -> None:
        a, target = self._axis_alpha, self._axis_target
        if a != target:
            step = config.AXIS_FADE_STEP
            self._axis_alpha = a = a + max(-step, min(step, target - a))
            self._apply_axis_alpha()
        self._update_cursor()

    def _update_cursor(self) -> None:
        canvas, geo = self.disc_canvas, self._geo
        items = (self._cur_ray, self._cur_dot, self._cur_bg, self._cur_text)
        mouse = self._mouse
        if not geo or mouse is None or self._axis_alpha < 0.3:
            if self._cursor_key is not None:
                self._cursor_key = None
                for item in items:
                    canvas.itemconfig(item, state="hidden")
            return
        if self._cursor_key == mouse:
            return
        cx, cy, r_in, max_len = geo["cx"], geo["cy"], geo["r_in"], geo["max_len"]
        dx, dy = mouse[0] - cx, mouse[1] - cy
        r = math.hypot(dx, dy)
        if not (r_in - 4 <= r <= r_in + max_len + 30):
            if self._cursor_key is not None:
                for item in items:
                    canvas.itemconfig(item, state="hidden")
            self._cursor_key = None
            return
        self._cursor_key = mouse
        theta = math.degrees(math.atan2(dx, -dy)) % 360.0
        if theta > 180.0:
            theta = 360.0 - theta                        # the ring is mirrored
        level = min(1.0, max(0.0, (r - r_in - 3) / max_len))
        db = -(1.0 - level) * config.DB_RANGE_BARS
        ux, uy = dx / r, dy / r
        canvas.coords(self._cur_ray, cx + ux * r_in, cy + uy * r_in, cx + ux * (r_in + max_len + 4),
                      cy + uy * (r_in + max_len + 4))
        canvas.coords(self._cur_dot, mouse[0] - 4, mouse[1] - 4, mouse[0] + 4, mouse[1] + 4)
        anchor, tx = ("e", mouse[0] - 12) if dx > 0 else ("w", mouse[0] + 12)
        canvas.itemconfig(self._cur_text, text=f"{_hz_text(self._theta_freq(theta))}   {db:.0f} dB", anchor=anchor)
        canvas.coords(self._cur_text, tx, mouse[1] - 14)
        x0, y0, x1, y1 = canvas.bbox(self._cur_text)
        canvas.coords(self._cur_bg, x0 - 4, y0 - 2, x1 + 4, y1 + 2)
        for item in items:
            canvas.itemconfig(item, state="normal")
        canvas.tag_raise(self._cur_ray)
        canvas.tag_raise(self._cur_dot)
        canvas.tag_raise(self._cur_bg)
        canvas.tag_raise(self._cur_text)

    def _place_dots(self) -> None:
        cx, cy = self._geo.get("cx", config.VIZ_MIN_SIZE / 2), self._geo.get("cy", config.VIZ_MIN_SIZE / 2)
        count = len(self._pixel_ids)
        half = config.PIXEL_DOT_SIZE / 2
        for i, dot_id in enumerate(self._pixel_ids):
            angle = math.radians(self._disc_angle + i * (360 / count))
            x = cx + config.PIXEL_DOT_RADIUS * math.cos(angle)
            y = cy + config.PIXEL_DOT_RADIUS * math.sin(angle)
            self.disc_canvas.coords(dot_id, x - half, y - half, x + half, y + half)

    def _spin_disc(self) -> None:
        if not self.winfo_exists():
            return
        if self._playing and self._visible:
            self._disc_angle = (self._disc_angle + config.SPIN_STEP_DEG) % 360
            if not self._cover_frames:
                self._place_dots()                       # the dots are hidden behind a cover anyway
            else:
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
        self.recheck_btn = ttk.Button(box, text="\u21bb  Re-check BPM", style="Small.TButton",
                                      command=self._recheck_tempo)
        self.recheck_btn.grid(row=0, column=1, rowspan=2, sticky="e")

        self.beat_canvas = tk.Canvas(box, width=150, height=22, bg=colors["BG_LIGHT"], highlightthickness=0)
        self.beat_canvas.grid(row=2, column=0, sticky="w", pady=(10, 0))
        self._beat_dots = [self.beat_canvas.create_oval(4 + i * 36, 3, 24 + i * 36, 21,
                                                        outline=colors["ACCENT_DARK"], width=2) for i in range(4)]
        self.bar_label = ttk.Label(box, text="", style="Hint.TLabel")
        self.bar_label.grid(row=2, column=1, sticky="e", pady=(10, 0))

        # band meters: what the rules react to (level bar + a lamp that flashes on a hit)
        row_h, bar_x0, bar_x1 = 22, 60, 380
        self._meter_geo = (row_h, bar_x0, bar_x1)
        self.meter_canvas = tk.Canvas(box, width=420, height=row_h * len(BAND_NAMES) + 4, bg=colors["BG_LIGHT"],
                                      highlightthickness=0)
        self.meter_canvas.grid(row=3, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._meter_fill, self._meter_lamp = {}, {}
        for i, name in enumerate(BAND_NAMES):
            y = 4 + i * row_h
            self.meter_canvas.create_text(4, y + 8, text=BAND_LABELS[name].upper(), anchor="w",
                                          fill=colors["MUTED"], font=(FONT, 8, "bold"))
            self.meter_canvas.create_rectangle(bar_x0, y + 2, bar_x1, y + 14, outline=colors["LINE"])
            self._meter_fill[name] = self.meter_canvas.create_rectangle(bar_x0, y + 2, bar_x0, y + 14,
                                                                        fill=self._band_colour[name], outline="")
            self._meter_lamp[name] = self.meter_canvas.create_oval(bar_x1 + 14, y + 1, bar_x1 + 30, y + 15,
                                                                   outline=colors["LINE"], width=2)

        # what the fixture is being told right now
        self.preview_canvas = tk.Canvas(box, width=420, height=26, bg=colors["BG_LIGHT"], highlightthickness=0)
        self.preview_canvas.grid(row=4, column=0, columnspan=2, sticky="w", pady=(10, 0))
        self._preview_dots, self._preview_texts, self._preview_state = {}, {}, None
        for i, (key, text) in enumerate((("led", "LED"), ("derby", "DERBY"), ("laser", "LASER"))):
            x = 6 + i * 140
            self._preview_dots[key] = self.preview_canvas.create_oval(x, 5, x + 16, 21, outline=colors["LINE"], width=2)
            self.preview_canvas.create_text(x + 24, 13, text=text, anchor="w", fill=colors["MUTED"],
                                            font=(FONT, 8, "bold"))
            self._preview_texts[key] = self.preview_canvas.create_text(x + 24 + 8 * len(text) + 6, 13, text="",
                                                                       anchor="w", fill=colors["FG"],
                                                                       font=(FONT, 8))

    # --------- Right side: quick looks
    def _build_looks(self, parent: ttk.Frame, row: int) -> None:
        box = self._card(parent, "Look", row)
        self.look_var = tk.StringVar(value=self.look.name)
        buttons = ttk.Frame(box, style="Plain.Card.TFrame")
        buttons.grid(row=0, column=0, sticky="ew")
        for i, look in enumerate(engine.QUICK_LOOKS):
            buttons.columnconfigure(i, weight=1, uniform="looks")
            ttk.Radiobutton(buttons, text=look.name, value=look.name, variable=self.look_var,
                            style="Look.Toolbutton", command=lambda n=look.name: self._load_look(n)
                            ).grid(row=0, column=i, sticky="ew", padx=2, pady=2)
        self.look_desc = ttk.Label(box, text="", style="Hint.TLabel", wraplength=490, justify="left")
        self.look_desc.grid(row=1, column=0, sticky="w", pady=(6, 0))

    # --------- Right side: the mapping grid
    def _build_mapping(self, parent: ttk.Frame, row: int) -> None:
        box = self._card(parent, "When this hits  \u2192  do this", row)
        box.columnconfigure(0, weight=1)
        self.grid_widget = MappingGrid(box, self.colors, lambda: self.look, self._on_grid_edit)
        self.grid_widget.frame.grid(row=0, column=0, sticky="ew")
        ttk.Label(box, text="Click a cell:  \u2013  \u2192  ON (flash on hit)  \u2192  OFF (dark on hit).  "
                            "\u21bb steps to the next pattern / colour on every hit.  Right-click goes back.",
                  style="Hint.TLabel", wraplength=490, justify="left").grid(row=1, column=0, sticky="w", pady=(6, 0))

    def _on_grid_edit(self) -> None:
        if self._loading:
            return
        self.engine.rebuild()
        self._mark_custom()

    # --------- Right side: extras (checkboxes)
    def _build_extras(self, parent: ttk.Frame, row: int) -> None:
        box = self._card(parent, "Extras", row)
        box.columnconfigure(0, weight=1)
        self.bouncy_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(box, text="Bouncy Bass", style="Card.TCheckbutton", variable=self.bouncy_var,
                        command=lambda: setattr(self.engine, "bouncy", bool(self.bouncy_var.get()))
                        ).grid(row=0, column=0, sticky="w")
        ttk.Label(box, text=f"Derby motor jumps between position {config.BOUNCE_POS_MIN} and {config.BOUNCE_POS_MAX} "
                            f"on every kick; every 2nd (3rd ...) kick when they come faster than the motor can "
                            f"follow (BOUNCE_TRAVEL_S = {config.BOUNCE_TRAVEL_S} s).",
                  style="Hint.TLabel", wraplength=470, justify="left").grid(row=1, column=0, sticky="w", padx=(22, 0))
        self.learn_var = tk.BooleanVar(value=self.analyzer.learning)
        ttk.Checkbutton(box, text="Learn kick & thresholds per song", style="Card.TCheckbutton",
                        variable=self.learn_var,
                        command=lambda: setattr(self.analyzer, "learning", bool(self.learn_var.get()))
                        ).grid(row=2, column=0, sticky="w", pady=(6, 0))

    # --------- Right side: sliders
    def _build_tuning(self, parent: ttk.Frame, row: int) -> None:
        box = self._card(parent, "Tuning", row, pady=(0, 0))
        box.columnconfigure(0, weight=1)
        self._slider(box, 0, "Sensitivity", 0.2, 5.0, self.analyzer.gain, lambda v: f"{v:.1f}\u00d7",
                     lambda v: setattr(self.analyzer, "gain", v))
        self._slider(box, 2, "Smoothness", 0.0, 0.95, self.analyzer.smoothing, lambda v: f"{round(v / 0.95 * 100)} %",
                     lambda v: setattr(self.analyzer, "smoothing", v), pady=(8, 0))

    def _slider(self, box, row: int, title: str, low: float, high: float, value: float, fmt, apply, pady=(0, 0)) -> None:
        head = ttk.Frame(box, style="Plain.Card.TFrame")
        head.grid(row=row, column=0, sticky="ew", pady=pady)
        ttk.Label(head, text=title, style="Card.TLabel").pack(side="left")
        label = ttk.Label(head, text=fmt(value), style="Value.TLabel")
        label.pack(side="right")
        scale = ttk.Scale(box, from_=low, to=high, orient="horizontal", style="Card.Horizontal.TScale")
        scale.grid(row=row + 1, column=0, sticky="ew", pady=(3, 0))
        scale.set(value)
        scale.configure(command=lambda v: (label.config(text=fmt(float(v))), apply(float(v))))



    # --------- Looks handling
    def _load_look(self, name: str) -> None:
        preset = engine.BY_NAME.get(name)
        if preset is None:
            return
        self._loading = True
        try:
            self.look = preset.copy()
            self.engine.set_look(self.look)
            self.look_var.set(preset.name)
            self.look_desc.config(text=preset.description)
            self.grid_widget.refresh()
        finally:
            self._loading = False

    def _mark_custom(self) -> None:
        if self._loading:
            return
        self.look.name = engine.CUSTOM
        self.look_var.set(engine.CUSTOM)
        self.look_desc.config(text="Your own setup. Pick a look above to start over from a ready-made one.")

    def _on_blackout(self) -> None:
        self.engine.force_blackout = bool(self.blackout_var.get())

    # --------- Footer: the audio device as a small symbol
    def _build_device_icon(self, parent: ttk.Frame) -> None:
        self.device_canvas = tk.Canvas(parent, width=30, height=24, bg=self.colors["BG"], highlightthickness=0)
        self.device_canvas.pack(side="left")
        self._tip = None
        self.device_canvas.bind("<Enter>", self._show_device_tip)
        self.device_canvas.bind("<Leave>", self._hide_device_tip)
        self._draw_device_icon("")

    def _draw_device_icon(self, name: str) -> None:
        """Speaker box, headphones or headset -- guessed from the capture device's name; dim while there is none."""
        canvas, colors = self.device_canvas, self.colors
        canvas.delete("all")
        col = colors["ACCENT2"] if name else colors["LINE"]
        kind = _device_kind(name)
        if kind == "speaker":
            canvas.create_rectangle(7, 2, 23, 22, outline=col, width=2)
            canvas.create_oval(11, 9, 19, 17, outline=col, width=2)
            canvas.create_oval(13, 4, 17, 8, outline=col, width=1)
        else:
            canvas.create_arc(5, 2, 25, 22, start=0, extent=180, style="arc", outline=col, width=2)
            canvas.create_rectangle(4, 11, 9, 19, outline=col, fill=col)
            canvas.create_rectangle(21, 11, 26, 19, outline=col, fill=col)
            if kind == "headset":
                canvas.create_line(6, 19, 8, 22, 15, 22, fill=col, width=2, smooth=True)
                canvas.create_oval(14, 20, 18, 24, outline=col, fill=col)
        self._tip_text = name or "no audio device yet (waiting for sound)"

    def _show_device_tip(self, event) -> None:
        self._hide_device_tip()
        tip = self._tip = tk.Toplevel(self)
        tip.wm_overrideredirect(True)
        tip.wm_geometry(f"+{event.x_root + 12}+{event.y_root - 30}")
        tk.Label(tip, text=self._tip_text, bg=self.colors["BG_LIGHT"], fg=self.colors["FG"], font=(FONT, 9),
                 relief="solid", borderwidth=1, padx=6, pady=2).pack()

    def _hide_device_tip(self, event=None) -> None:
        if self._tip is not None:
            self._tip.destroy()
            self._tip = None

    # --------- Tempo re-check
    def _recheck_tempo(self) -> None:
        """Forget tempo and bar count and listen again (the bar counter restarts with the new lock)."""
        self.analyzer.request_tempo_recheck()
        self._set_text(self.lock_label, "lock", "re-checking tempo...")
        self._set_text(self.bar_label, "bar", "")
        self._status_cache["beat"] = None

    # --------- Audio blocks
    # The lights are computed and written to the DMX buffer right here on the capture thread: no Tk event queue,
    # no drawing in between. The GUI only gets the newest frame, at most ~45 times a second.
    def _on_source_error(self, error: Exception) -> None:
        self._call_gui(self._show_error, error)

    def _on_block(self, samples: np.ndarray, samplerate: int) -> None:
        try:
            features = self.analyzer.process(samples, samplerate)
            values = self.engine.process(features)         # channels 1..9, channel 1 is always 0
        except Exception as e:
            if not self._failed:                            # log once, never spam from the audio thread
                self._failed = True
                logging.exception("Music Mode processing failed")
                self._call_gui(self.status_label.config, text=f"Light engine error: {e}")
            return
        self.controller.set_many(values, MUSIC)
        for name in BAND_NAMES:
            if features.hit[name]:
                self._hit_flags[name] = True
        self._latest = features
        if not self._ui_pending:
            self._ui_pending = True
            wait = self._ui_interval_ms - (time.monotonic() - self._last_ui) * 1000
            self._call_gui(self._ui_update, delay=max(0, int(wait)))

    def _call_gui(self, fn, *args, delay: int = 0, **kwargs) -> None:
        try:
            self.after(delay, lambda: fn(*args, **kwargs))
        except (tk.TclError, RuntimeError):              # window is closing
            pass

    def _show_error(self, error: Exception) -> None:
        logging.error("Music Mode audio error", exc_info=error)
        self.status_label.config(text=f"Audio error: {error}")

    def _ui_update(self) -> None:
        self._ui_pending = False
        now = time.monotonic()
        dt, self._last_ui = now - self._last_ui, now
        features = self._latest
        if features is None or not self._visible:
            return
        step = min(dt, 0.25) / config.UI_REF_DT          # decays are tuned per UI_REF_DT -> same look at any FPS

        flags, self._hit_flags = self._hit_flags, {n: False for n in BAND_NAMES}
        decay = config.HIT_GLOW_DECAY ** step
        for name in BAND_NAMES:
            self._glow[name] = 1.0 if flags[name] else (self._glow[name] * decay if self._glow[name] > 0.02 else 0.0)
        self._pulse = max(self._glow["bass"], self._glow["sub"]) * 6.0

        self._update_bars(features.bars, config.SPECTRUM_PEAK_DECAY * step)
        self._update_arcs()
        self._update_waveform(features.waveform)
        self._update_axes()
        self._update_status(features)

    def _set_text(self, widget, key: str, text: str) -> None:
        if self._status_cache.get(key) != text:      # only touch Tk when something actually changed
            self._status_cache[key] = text
            widget.config(text=text)

    def _update_status(self, f: Features) -> None:
        eng = self.engine
        if not f.active:
            self._set_text(self.bpm_label, "bpm", "-- BPM")
            self._set_text(self.lock_label, "lock", "no music")
        else:
            self._set_text(self.bpm_label, "bpm", f"{f.bpm:.1f} BPM" if f.tempo_known else "-- BPM")
            state = "tempo locked" if f.locked else ("following kicks" if f.tempo_known else "listening...")
            if f.kick_hz:
                state += f"  \u00b7  kick \u2248 {f.kick_hz:.0f} Hz" + ("" if f.kick_learned else " (learning)")
            self._set_text(self.lock_label, "lock", state)
        if self.source.device_name != self._device_name:
            self._device_name = self.source.device_name
            self._draw_device_icon(self._device_name)
        connected = self.controller.connected         # is anything going to reach the fixture at all?
        self._set_text(self.dmx_label, "dmx", "\u25cf DMX connected" if connected else
                       "\u25cb DMX NOT connected \u2013 go back to manual control and click Connect")
        colour = "#4cc38a" if connected else "#e6a23c"
        if self._status_cache.get("dmx_colour") != colour:
            self._status_cache["dmx_colour"] = colour
            self.dmx_label.config(foreground=colour)

        beat = f.beat_in_bar if (f.active and f.tempo_known) else -1
        if self._status_cache.get("beat") != beat:
            self._status_cache["beat"] = beat
            for i, dot in enumerate(self._beat_dots):
                self.beat_canvas.itemconfig(dot, fill=(self.colors["ACCENT"] if i == beat else self.colors["BG_LIGHT"]))
            self._set_text(self.bar_label, "bar", f"bar {f.bar + 1}" if beat >= 0 else "")

        # band meters
        row_h, x0, x1 = self._meter_geo
        for i, name in enumerate(BAND_NAMES):
            y = 4 + i * row_h
            x = int(x0 + f.level[name] * (x1 - x0))
            if self._status_cache.get(f"meter_{name}") != x:
                self._status_cache[f"meter_{name}"] = x
                self.meter_canvas.coords(self._meter_fill[name], x0, y + 2, x, y + 14)
            lit = self._glow[name] > 0.3
            key = f"lamp_{name}"
            if self._status_cache.get(key) != lit:
                self._status_cache[key] = lit
                self.meter_canvas.itemconfig(self._meter_lamp[name], fill=self._band_colour[name] if lit else "",
                                             outline=self._band_colour[name] if lit else self.colors["LINE"])

        # little fixture preview: what the lights are doing right now
        pv = eng.preview()
        state = (pv["led"], pv["derby"], pv["derby_position"], pv["laser"], pv["laser_rotation"], pv["blackout"])
        if state != self._preview_state:
            self._preview_state = state
            off = "#e74c3c" if pv["blackout"] else self.colors["LINE"]
            canvas = self.preview_canvas
            led_on = pv["led"] is not None
            canvas.itemconfig(self._preview_dots["led"], fill=self.colors["ACCENT"] if led_on else "",
                              outline=self.colors["ACCENT"] if led_on else off)
            canvas.itemconfig(self._preview_texts["led"], text=f"P{pv['led']}" if led_on else "")
            derby = preview_colour(pv["derby"]) if pv["derby"] else None
            canvas.itemconfig(self._preview_dots["derby"], fill=derby or "", outline=derby or off)
            canvas.itemconfig(self._preview_texts["derby"],
                              text=f"pos {pv['derby_position']}" if pv["derby_position"] else "")
            laser = preview_colour(pv["laser"]) if pv["laser"] else None
            canvas.itemconfig(self._preview_dots["laser"], fill=laser or "", outline=laser or off)
            canvas.itemconfig(self._preview_texts["laser"],
                              text=pv["laser_rotation"].upper() if laser and pv["laser_rotation"] != "stop" else "")

    # --------- Visualizer drawing
    def _update_bars(self, bars, peak_fall: float) -> None:
        if bars is None:
            return
        flat = np.concatenate([bars[name] for name in BAND_NAMES]).tolist()
        self._last_flat = flat
        peaks = self._peaks
        for i, level in enumerate(flat):
            # peak cap: jumps up with the bar, then falls slowly
            peaks[i] = max(level, peaks[i] - peak_fall)
        self._draw_bars()

    def _draw_bars(self) -> None:
        geo, canvas, flat = self._geo, self.disc_canvas, self._last_flat
        if not geo or flat is None:
            return
        cx, cy, max_len = geo["cx"], geo["cy"], geo["max_len"]
        r0 = int(geo["r_in"] + self._pulse)
        cache, peaks, dirs, slot_bar = self._bar_cache, self._peaks, self._dirs, self._slot_bar
        for k in range(self._slots):
            i = slot_bar[k]
            length = int(3 + flat[i] * max_len) & ~1      # even pixels: invisible, but far more cache hits
            p = int(r0 + 5 + peaks[i] * max_len) & ~1
            if cache[k] == (length, p, r0):              # nothing moved by a pixel: spare Tk the redraw
                continue
            cache[k] = (length, p, r0)
            dx, dy = dirs[k]
            canvas.coords(self._bar_ids[k], cx + dx * r0, cy + dy * r0,
                          cx + dx * (r0 + length), cy + dy * (r0 + length))
            canvas.coords(self._peak_ids[k], cx + dx * p, cy + dy * p, cx + dx * (p + 3), cy + dy * (p + 3))

    def _update_arcs(self) -> None:
        """The arc of a band flashes white on a hit and fades back."""
        for name in BAND_NAMES:
            glow = round(self._glow[name] * 8) / 8        # 9 steps are enough, spares Tk redraws
            if self._status_cache.get(f"arc_{name}") == glow:
                continue
            self._status_cache[f"arc_{name}"] = glow
            dim = theme.lerp_colour(self._band_colour[name], self.colors["BG_LIGHT"], 0.55)
            colour = theme.lerp_colour(dim, self.colors["FG"], glow)
            for item in self._arc_ids[name]:
                self.disc_canvas.itemconfig(item, outline=colour)

    def _update_waveform(self, waveform) -> None:
        """The waveform as a closed ring around the disc, mirrored left / right so the ends meet; older frames trail
        behind as dimmer echoes a little further out."""
        if waveform is None or len(waveform) < 2 or not self._geo:
            return
        n = self._wave_n
        wave = np.asarray(waveform, dtype=np.float64)
        wave = np.interp(np.linspace(0.0, len(wave) - 1, n), np.arange(len(wave)), wave)
        wave = np.clip(np.convolve(wave, (0.25, 0.5, 0.25), mode="same") * config.WAVE_DISPLAY_GAIN, -1.0, 1.0)
        layers = self._wave_layers
        if layers is None:
            layers = self._wave_layers = [wave] * config.WAVE_RING_LAYERS
        self._wave_frame += 1
        if self._wave_frame % config.WAVE_ECHO_EVERY == 0:
            layers = self._wave_layers = [layers[0]] + layers[:-1]       # the live wave moves on to the first echo
        layers[0] = wave
        geo, canvas = self._geo, self.disc_canvas
        cx, cy = geo["cx"], geo["cy"]
        for i, (item, layer) in enumerate(zip(self._wave_items, layers)):
            ring = np.concatenate((layer, layer[::-1]))
            r = geo["r_wave"] + i * config.WAVE_ECHO_STEP + ring * config.WAVE_RING_AMP * (1.0 - 0.25 * i)
            xs, ys = cx + self._wave_sin * r, cy - self._wave_cos * r
            pts = np.empty(2 * len(xs) + 2)
            pts[0:-2:2], pts[1:-2:2] = xs, ys
            pts[-2], pts[-1] = xs[0], ys[0]                              # close the ring
            canvas.coords(item, *pts.tolist())

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
        key = " - ".join(part.strip() for part in (artist, title) if part and part.strip())
        if key != self._song_key:                        # a different track: the analysis switches to its cached values
            self._song_key = key
            self.analyzer.request_song(key or None)

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
        self._hide_device_tip()
        self.source.stop()
        self.analyzer.flush()                        # learned kick frequencies etc. go to song_profiles.json
        if self.now_playing:
            self.now_playing.stop()
        self.controller.release(MUSIC)               # the manual setup goes back on the wire
        self.on_closed()
        self.destroy()
