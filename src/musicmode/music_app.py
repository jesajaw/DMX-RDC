"""
Music Mode (app / UI)
=====================

The Music Mode window. It contains no analysis and no light logic -- it only wires the pieces together:

    audio_source.LoopbackSource  ->  analysis.Analyzer  ->  engine.LightEngine  ->  controller.set_many(values, MUSIC)
                                                  \\-> this window (visualisation, status)
    nowplaying.NowPlayingReader  ->  this window (title / artist / spinning cover)

Left: the round cover in the middle with the four frequency bands around it (Sub / Bass / Mids / Highs),
each as an arc of radial bars (bar length = amplitude, mirrored left / right) that flashes on a hit,
plus the oscilloscope. Right: live status with band meters, the look chips, the rule list
("when this happens -> do this") and the sensitivity slider.

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
from ..config import ACTIVE_SCHEME
from ..controller import MUSIC, DMXController, apply_dark_titlebar
from . import config, engine
from .analysis import BAND_LABELS, BAND_NAMES, BAND_RANGES, Analyzer, Features
from .audio_source import LoopbackSource
from .engine import LightEngine, Rule
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


class ScrollFrame(ttk.Frame):
    """A frame with a vertical scrollbar; put the content into .inner."""

    def __init__(self, parent, height: int, bg: str):
        super().__init__(parent, style="Plain.Card.TFrame")
        self.canvas = tk.Canvas(self, height=height, bg=bg, highlightthickness=0)
        bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        self.canvas.pack(side="left", fill="both", expand=True)
        bar.pack(side="right", fill="y")
        self.inner = ttk.Frame(self.canvas, style="Plain.Card.TFrame")
        window = self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda e: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", lambda e: self.canvas.itemconfigure(window, width=e.width))
        for widget in (self.canvas, self.inner):
            widget.bind("<Enter>", lambda e: self.canvas.bind_all("<MouseWheel>", self._wheel))
            widget.bind("<Leave>", lambda e: self.canvas.unbind_all("<MouseWheel>"))

    def _wheel(self, event) -> None:
        self.canvas.yview_scroll(-1 if event.delta > 0 else 1, "units")


class RuleRow:
    """One editable rule: SOURCE EVENT  ->  TARGET ACTION VALUE, plus hold and minimum gap."""

    def __init__(self, parent, rule: Rule, on_change, on_delete):
        self.rule, self.on_change, self.on_delete = rule, on_change, on_delete
        self.frame = ttk.Frame(parent, style="Plain.Card.TFrame")
        self.render()

    def _changed(self) -> None:
        self.on_change()

    def _line(self, row: int) -> ttk.Frame:
        line = ttk.Frame(self.frame, style="Plain.Card.TFrame")
        line.grid(row=row, column=0, sticky="ew", pady=1)
        return line

    def _pair(self) -> tuple:
        target = engine.TARGETS[self.rule.target]
        return tuple(self.rule.values) if len(self.rule.values) >= 2 else tuple(target.options[:2])

    def render(self) -> None:
        for child in self.frame.winfo_children():
            child.destroy()
        rule = self.rule
        target = engine.TARGETS[rule.target]
        value_options = [(target.names.get(v, str(v)), v) for v in target.options]

        # line 1: when
        l1 = self._line(0)
        _combo(l1, [(label, key) for key, label in engine.SOURCES], rule.source, 7, self._set_source).pack(side="left")
        if rule.source == "beat":
            ttk.Label(l1, text="every", style="Card.TLabel").pack(side="left", padx=(8, 4))
            _combo(l1, list(engine.BEAT_OPTIONS.items()), rule.every_beats, 8, self._set_every).pack(side="left")
        else:
            _combo(l1, [(label, key) for key, label in engine.BAND_EVENTS], rule.event, 12,
                   self._set_event).pack(side="left", padx=(6, 0))
            if rule.event in ("above", "below"):
                _combo(l1, [(f"{int(v * 100)} %", v) for v in engine.THRESHOLD_OPTIONS], rule.threshold, 6,
                       self._set_threshold).pack(side="left", padx=(6, 0))
        ttk.Label(l1, text="\u2192", style="CardMuted.TLabel").pack(side="left", padx=8)
        _combo(l1, [(t.label, key) for key, t in engine.TARGETS.items()], rule.target, 19,
               self._set_target).pack(side="left")

        # line 2: do
        l2 = self._line(1)
        _combo(l2, [(label, key) for key, label in engine.ACTIONS], rule.action, 12, self._set_action).pack(side="left")
        if rule.action == "set":
            current = rule.values[0] if rule.values else target.options[0]
            _combo(l2, value_options, current, 20, lambda v: self._set_values((v,))).pack(side="left", padx=(6, 0))
        elif rule.action == "toggle":
            a, b = self._pair()
            ttk.Label(l2, text="A", style="CardMuted.TLabel").pack(side="left", padx=(8, 3))
            _combo(l2, value_options, a, 16, lambda v: self._set_values((v, self._pair()[1]))).pack(side="left")
            ttk.Label(l2, text="B", style="CardMuted.TLabel").pack(side="left", padx=(8, 3))
            _combo(l2, value_options, b, 16, lambda v: self._set_values((self._pair()[0], v))).pack(side="left")
        else:
            seq = rule.values or target.cycle
            text = "through all" if not rule.values else " \u2192 ".join(target.names.get(v, str(v)) for v in seq)
            ttk.Label(l2, text=text[:46], style="CardMuted.TLabel").pack(side="left", padx=(8, 0))
        ttk.Button(l2, text="\u2715", width=3, command=lambda: self.on_delete(self)).pack(side="right")

        # line 3: timing
        l3 = self._line(2)
        ttk.Label(l3, text="hold", style="CardMuted.TLabel").pack(side="left")
        _combo(l3, [("off" if ms == 0 else f"{ms} ms", ms) for ms in engine.HOLD_OPTIONS_MS], rule.hold_ms, 7,
               self._set_hold).pack(side="left", padx=(4, 14))
        ttk.Label(l3, text="min. gap", style="CardMuted.TLabel").pack(side="left")
        _combo(l3, [("none" if ms == 0 else f"{ms} ms", ms) for ms in engine.GAP_OPTIONS_MS], rule.cooldown_ms, 7,
               self._set_gap).pack(side="left", padx=(4, 0))

    # ---- edits (rules are changed in place; the engine picks the change up on the next block)
    def _set_source(self, key) -> None:
        self.rule.source = key
        if key == "beat":
            self.rule.event = "beat"
        elif self.rule.event == "beat":
            self.rule.event = "hit"
        self.render()
        self._changed()

    def _set_event(self, key) -> None:
        self.rule.event = key
        self.render()
        self._changed()

    def _set_threshold(self, value) -> None:
        self.rule.threshold = float(value)
        self._changed()

    def _set_every(self, beats) -> None:
        self.rule.every_beats = int(beats)
        self._changed()

    def _set_target(self, key) -> None:
        self.rule.target = key
        self.rule.values = ()
        self.render()
        self._changed()

    def _set_action(self, key) -> None:
        self.rule.action = key
        self.rule.values = ()
        self.render()
        self._changed()

    def _set_values(self, values) -> None:
        self.rule.values = tuple(values)
        self._changed()

    def _set_hold(self, ms) -> None:
        self.rule.hold_ms = int(ms)
        self._changed()

    def _set_gap(self, ms) -> None:
        self.rule.cooldown_ms = int(ms)
        self._changed()


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
        self._build_tuning(right, 3)
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
        self._n_bars = len(thetas)
        self._slots = 2 * self._n_bars                       # first half: right side, second: left side (mirror)
        self._slot_bar = [k % self._n_bars for k in range(self._slots)]
        self._dirs = []
        for k in range(self._slots):
            th = math.radians(thetas[k % self._n_bars])
            self._dirs.append((math.sin(th) if k < self._n_bars else -math.sin(th), -math.cos(th)))
        self._peaks = [0.0] * self._n_bars
        self._bar_cache = [None] * self._slots           # last drawn (bar end, peak end, start radius) per slot

        # items: bars + peak caps, then the hit arcs; created first so the disc is drawn on top
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
        self._band_labels = {}
        for name in BAND_NAMES:
            lo_hz, hi_hz = BAND_RANGES[name]
            self._band_labels[name] = canvas.create_text(
                0, 0, text=f"{BAND_LABELS[name].upper()}\n{_freq_text(lo_hz)}\u2013{_freq_text(hi_hz)} Hz",
                fill=colors["MUTED"], font=(theme.FONT, 8, "bold"), justify="center")

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
        self._wave_glow = self.wave_canvas.create_line(0, 0, 0, 0, fill=colors["ACCENT_DARK"], width=5)
        self._wave_line = self.wave_canvas.create_line(0, 0, 0, 0, fill=colors["ACCENT2"], width=1.5)
        self._wave_mid = self.wave_canvas.create_line(0, 0, 0, 0, fill=colors["LINE"])
        self.wave_canvas.bind("<Configure>", self._on_wave_resize)

        if now_playing_available():            # otherwise: just no title / cover
            self.now_playing = NowPlayingReader(on_update=self._on_now_playing, on_playing=self._on_playing)
            self.now_playing.start()

        self._layout_viz()
        self._spin_disc()

    def _on_wave_resize(self, event) -> None:
        self._wave_w = max(10, event.width)
        mid = event.height / 2
        self.wave_canvas.coords(self._wave_mid, 0, mid, self._wave_w, mid)

    def _layout_viz(self) -> None:
        """(Re)computes the geometry of the disc and the band ring for the current canvas size."""
        canvas = self.disc_canvas
        w, h = canvas.winfo_width(), canvas.winfo_height()
        if w < 60 or h < 60:
            w = h = config.VIZ_MIN_SIZE
        cx, cy = w / 2, h / 2
        r_disc = config.DISC_SIZE / 2
        r_in = r_disc + config.RING_GAP
        max_len = max(18.0, min(w, h) / 2 - r_in - config.BAND_LABEL_MARGIN)
        width = max(2.0, 2 * math.pi * (r_in + 8) / self._slots * 0.55)
        self._geo = dict(cx=cx, cy=cy, r_in=r_in, max_len=max_len)

        for item, radius in zip(self._ring_ovals, self._disc_radii):
            canvas.coords(item, cx - radius, cy - radius, cx + radius, cy + radius)
        canvas.coords(self._center_dot, cx - 5, cy - 5, cx + 5, cy + 5)
        canvas.coords(self._cover_image_item, cx, cy)
        for k in range(self._slots):
            canvas.itemconfig(self._bar_ids[k], width=width)
            canvas.itemconfig(self._peak_ids[k], width=width)

        r_arc = r_in - 6
        r_label = r_in + max_len + 22
        for name in BAND_NAMES:
            for item in self._arc_ids[name]:
                canvas.coords(item, cx - r_arc, cy - r_arc, cx + r_arc, cy + r_arc)
            lo, hi = self._arc_span[name]
            mid = math.radians((lo + hi) / 2)
            canvas.coords(self._band_labels[name], cx + math.sin(mid) * r_label, cy - math.cos(mid) * r_label)
        self._place_dots()
        self._bar_cache = [None] * self._slots           # geometry changed -> every bar has to be redrawn
        if self._last_flat is not None:
            self._draw_bars()

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
        self.section_label = ttk.Label(box, text="", style="Section.TLabel")
        self.section_label.grid(row=0, column=1, rowspan=2, sticky="e")

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
                                          fill=colors["MUTED"], font=(theme.FONT, 8, "bold"))
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
                                            font=(theme.FONT, 8, "bold"))
            self._preview_texts[key] = self.preview_canvas.create_text(x + 24 + 8 * len(text) + 6, 13, text="",
                                                                       anchor="w", fill=colors["FG"],
                                                                       font=(theme.FONT, 8))

    # --------- Right side: quick looks
    def _build_looks(self, parent: ttk.Frame, row: int) -> None:
        box = self._card(parent, "Look", row)
        self.look_var = tk.StringVar(value=self.look.name)
        buttons = ttk.Frame(box, style="Plain.Card.TFrame")
        buttons.grid(row=0, column=0, sticky="ew")
        for i, look in enumerate(engine.QUICK_LOOKS):
            buttons.columnconfigure(i % 3, weight=1, uniform="looks")
            ttk.Radiobutton(buttons, text=look.name, value=look.name, variable=self.look_var,
                            style="Look.Toolbutton", command=lambda n=look.name: self._load_look(n)
                            ).grid(row=i // 3, column=i % 3, sticky="ew", padx=2, pady=2)
        self.look_desc = ttk.Label(box, text="", style="Hint.TLabel", wraplength=470, justify="left")
        self.look_desc.grid(row=1, column=0, sticky="w", pady=(6, 0))

    # --------- Right side: the rules
    def _build_rules(self, parent: ttk.Frame, row: int) -> None:
        box = self._card(parent, "When this happens  \u2192  do this", row)
        box.columnconfigure(0, weight=1)
        self.rule_scroll = ScrollFrame(box, height=250, bg=self.colors["BG_LIGHT"])
        self.rule_scroll.grid(row=0, column=0, sticky="ew")
        self.rule_scroll.inner.columnconfigure(0, weight=1)
        ttk.Button(box, text="+  Add rule", command=self._add_rule).grid(row=1, column=0, sticky="w", pady=(8, 0))

    def _rebuild_rules(self) -> None:
        for row in self.rows:
            row.frame.destroy()
        for child in self.rule_scroll.inner.winfo_children():
            child.destroy()
        self.rows = []
        for i, rule in enumerate(self.look.rules):
            if i:
                ttk.Separator(self.rule_scroll.inner, orient="horizontal").grid(
                    row=2 * i - 1, column=0, sticky="ew", pady=6)
            row = RuleRow(self.rule_scroll.inner, rule, self._on_rule_edit, self._delete_rule)
            row.frame.grid(row=2 * i, column=0, sticky="ew")
            self.rows.append(row)

    def _add_rule(self) -> None:
        self.look.rules.append(Rule(source="bass", event="hit", target="led.on", action="set",
                                    values=(True,), hold_ms=120))
        self._rebuild_rules()
        self._mark_custom()
        self.after(50, lambda: self.rule_scroll.canvas.yview_moveto(1.0))

    def _delete_rule(self, row: RuleRow) -> None:
        if row.rule in self.look.rules:
            self.look.rules.remove(row.rule)
        self._rebuild_rules()
        self._mark_custom()

    def _on_rule_edit(self) -> None:
        self._mark_custom()

    # --------- Right side: sliders
    def _build_tuning(self, parent: ttk.Frame, row: int) -> None:
        box = self._card(parent, "Tuning", row, pady=(0, 0))
        box.columnconfigure(0, weight=1)
        head = ttk.Frame(box, style="Plain.Card.TFrame")
        head.grid(row=0, column=0, sticky="ew")
        ttk.Label(head, text="Sensitivity", style="Card.TLabel").pack(side="left")
        value = ttk.Label(head, text=f"{self.analyzer.gain:.1f}\u00d7", style="Value.TLabel")
        value.pack(side="right")
        scale = ttk.Scale(box, from_=0.2, to=5.0, orient="horizontal", style="Card.Horizontal.TScale")
        scale.grid(row=1, column=0, sticky="ew", pady=(3, 0))
        scale.set(self.analyzer.gain)
        scale.configure(command=lambda v, lab=value: self._on_sensitivity(float(v), lab))

    def _on_sensitivity(self, value: float, label) -> None:
        label.config(text=f"{value:.1f}\u00d7")
        self.analyzer.gain = value

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
            self._rebuild_rules()
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
            self._set_text(self.lock_label, "lock", "tempo locked" if f.locked else
                           ("following kicks" if f.tempo_known else "listening..."))
        device = self.source.device_name
        self._set_text(self.section_label, "device", (device[:30] + "\u2026" if len(device) > 31 else device)
                       if device else ("no signal" if not f.active else ""))
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
        if waveform is None or len(waveform) < 2:
            return
        wave_w = self._wave_w
        mid_y = (self.wave_canvas.winfo_height() or config.WAVE_CANVAS_HEIGHT) / 2
        scale = mid_y * 0.9
        n = len(waveform)
        xs = np.linspace(0.0, wave_w, n)
        ys = mid_y - np.clip(np.asarray(waveform, dtype=np.float64) * config.WAVE_DISPLAY_GAIN, -1.0, 1.0) * scale
        points = np.empty(2 * n)
        points[0::2], points[1::2] = xs, ys
        points = points.tolist()
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
        self.source.stop()
        if self.now_playing:
            self.now_playing.stop()
        self.controller.release(MUSIC)               # the manual setup goes back on the wire
        self.on_closed()
        self.destroy()
