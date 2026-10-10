"""
DMX Derby Controller
=====================

Tkinter GUI to control a Razor Derby over a USB-DMX adapter
(RS485, 250000 baud, 2 stop bits).

Split across these files:
- fixture.py:    the fixture itself: channel names, device groups, colour tables, value texts, device states
- controller.py: DMXController (serial link + send thread; the ONE interface both windows write through),
                 preset persistence, platform helpers
- config.py:     DMX timing and theme colours
- theme.py:      every ttk style (colours come from config.py)
- app.py (this file): main window (DMXUI), dialogs
- musicmode/:    Music Mode, one job per file
    config.py        every Music Mode tunable
    audio_source.py  loopback capture
    analysis.py      bands, hits, tempo, beat clock
    engine.py        rules ("when this happens -> do this") -> device states -> DMX frame
    music_app.py     the Music Mode window
    nowplaying.py    title / artist / cover (+ NowPlayingBridge.ps1, nowplaying_cache/)

Layout: three device columns (Derby | LED | Laser) with their channels, and a bottom row with the fixture's
automatic channels (Ch1 / Ch2), the Music Mode entry and the presets.

The entry point does NOT live here but in main.py at the project root --
this file only provides DMXUI (and the dialog helpers), without starting a
Tk root/mainloop itself.

Threading model
----------------
- Connecting (opening a COM port can block) runs in a worker thread
- The DMX send cycle runs in the controller's own thread while connected; a lost link is reported back through
  root.after(...), since Tkinter widgets must only be touched from the main thread
- This checks the USB link to the adapter, not the DMX cable itself -- DMX512 is unidirectional and gives
  no feedback from the fixture end.
"""

import json
import threading

import serial.tools.list_ports
import tkinter as tk
from tkinter import ttk

from . import config, fixture, theme
from .controller import DMXController, PresetManager, apply_dark_titlebar, force_dark_titlebar
from .musicmode.music_app import MusicModeWindow


class DMXUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        apply_dark_titlebar(root)
        self.root.title("DMX Derby Controller")
        self.root.configure(bg=config.COLOR_BG)

        self.ctl = DMXController(on_lost=lambda error: self.root.after(0, self._connection_lost, error))
        self.presets = PresetManager(config.PRESETS_DIR)
        self.channel_labels: dict[int, ttk.Label] = {}
        self.sliders: dict[int, ttk.Scale] = {}
        self._shown: dict[int, str] = {}                 # text currently shown per channel
        self._pattern_var = tk.IntVar(value=0)           # LED pattern chips (0 = off)

        theme.apply_theme(self.root)
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self._build_header()
        self._build_body()
        self._size_to_content()

    # --------- header
    def _build_header(self) -> None:
        bar = ttk.Frame(self.root, padding=(18, 14, 18, 6))
        bar.grid(row=0, column=0, sticky="ew")
        bar.columnconfigure(1, weight=1)

        titles = ttk.Frame(bar)
        titles.grid(row=0, column=0, sticky="w")
        ttk.Label(titles, text="DMX DERBY CONTROLLER", style="Title.TLabel").pack(anchor="w")
        ttk.Label(titles, text="Varytec Razor Derby  \u00b7  9-channel mode", style="Subtitle.TLabel").pack(anchor="w")

        controls = ttk.Frame(bar)
        controls.grid(row=0, column=2, sticky="e")

        self.conn_canvas = tk.Canvas(controls, width=14, height=14, bg=config.COLOR_BG, highlightthickness=0)
        self.conn_canvas.pack(side="left")
        self._conn_dot = self.conn_canvas.create_oval(2, 2, 12, 12, fill=config.ACTIVE_SCHEME["MUTED"], outline="")
        self.conn_label = ttk.Label(controls, text="Disconnected", style="Muted.TLabel", width=16)
        self.conn_label.pack(side="left", padx=(6, 14))

        ports = [p.device for p in serial.tools.list_ports.comports()] or ["COM3", "COM4"]
        self.port_cb = ttk.Combobox(controls, values=ports, width=12, state="readonly")
        self.port_cb.pack(side="left", padx=(0, 8))
        self.port_cb.current(0)

        self.btn_connect = ttk.Button(controls, text="Connect", command=self.toggle_connection, style="Accent.TButton")
        self.btn_connect.pack(side="left", padx=(0, 8))
        ttk.Button(controls, text="BLACKOUT", command=self.blackout, style="Blackout.TButton").pack(side="left")

    def _set_connection_state(self, connected: bool, port: str = "") -> None:
        self.conn_canvas.itemconfig(self._conn_dot, fill="#4cc38a" if connected else config.ACTIVE_SCHEME["MUTED"])
        self.conn_label.config(text=f"Connected  \u00b7  {port}" if connected else "Disconnected")
        self.btn_connect.config(text="Disconnect" if connected else "Connect", state="normal")

    # --------- body: Derby | LED | Laser on top, Automatic | Music Mode | Presets below
    def _build_body(self) -> None:
        grid = ttk.Frame(self.root, padding=(18, 8, 18, 18))
        grid.grid(row=1, column=0, sticky="nsew")
        for col in range(3):
            grid.columnconfigure(col, weight=1, uniform="groups")
        for row in (1, 2, 3):
            grid.rowconfigure(row, weight=1, uniform="cells")
        grid.rowconfigure(4, weight=1)

        for col, group in enumerate(fixture.DEVICES):
            ttk.Label(grid, text=group.title.upper(), style="Subtitle.TLabel", font=(theme.FONT, 9, "bold"),
                      foreground=config.ACTIVE_SCHEME["ACCENT2"]).grid(row=0, column=col, sticky="w", padx=6, pady=(0, 6))
            if group.key == "led":
                self._build_led_card(grid, row=1, col=col, rowspan=3, channel=group.channels[0])
                continue
            for i, channel in enumerate(group.channels):
                cell = self._card(grid, row=1 + i, col=col)
                self._channel_block(cell, channel).grid(row=0, column=0, sticky="ew")

        self._build_auto_card(grid, row=4, col=0)
        self._build_music_card(grid, row=4, col=1)
        self._build_preset_card(grid, row=4, col=2)

    def _card(self, parent, row: int, col: int, rowspan: int = 1) -> ttk.Frame:
        card = ttk.Frame(parent, padding=(14, 10), style="Card.TFrame")
        card.grid(row=row, column=col, rowspan=rowspan, padx=6, pady=5, sticky="nsew")
        card.columnconfigure(0, weight=1)
        return card

    def _channel_block(self, parent, channel: int) -> ttk.Frame:
        """Title, readable state and slider of one channel."""
        block = ttk.Frame(parent, style="Plain.Card.TFrame")
        block.columnconfigure(0, weight=1)
        ttk.Label(block, text=fixture.channel_title(channel), style="CellTitle.TLabel").grid(row=0, column=0, sticky="w")
        status = ttk.Label(block, text="---", style="Status.TLabel", anchor="w")
        status.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        self.channel_labels[channel] = status

        slider = ttk.Scale(block, from_=0, to=255, orient="horizontal", style="Card.Horizontal.TScale",
                           command=lambda v, c=channel: self.on_slider_change(c, v))
        slider.set(0)
        slider.grid(row=2, column=0, sticky="ew")
        self.sliders[channel] = slider
        self._show(channel, 0)
        return block

    def _build_led_card(self, parent, row: int, col: int, rowspan: int, channel: int) -> None:
        card = self._card(parent, row, col, rowspan)
        self._channel_block(card, channel).grid(row=0, column=0, sticky="ew")
        ttk.Label(card, text="QUICK PICK", style="CardTitle.TLabel").grid(row=1, column=0, sticky="w", pady=(16, 6))
        chips = ttk.Frame(card, style="Plain.Card.TFrame")
        chips.grid(row=2, column=0, sticky="ew")
        per_row = 6
        for i in range(per_row):
            chips.columnconfigure(i, weight=1, uniform="chips")
        for n in range(1, fixture.PATTERN_COUNT + 1):
            ttk.Radiobutton(chips, text=str(n), value=n, variable=self._pattern_var, width=3, style="Look.Toolbutton",
                            command=lambda n=n: self.sliders[channel].set(fixture.pattern_dmx(n))
                            ).grid(row=(n - 1) // per_row, column=(n - 1) % per_row, sticky="ew", padx=2, pady=2)
        ttk.Radiobutton(chips, text="Off", value=0, variable=self._pattern_var, style="Look.Toolbutton",
                        command=lambda: self.sliders[channel].set(0)
                        ).grid(row=3, column=0, columnspan=per_row, sticky="ew", padx=2, pady=(6, 2))

    def _build_auto_card(self, parent, row: int, col: int) -> None:
        card = self._card(parent, row, col)
        ttk.Label(card, text="AUTOMATIC PROGRAMS", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(card, text="The fixture's own shows. Show Select 0 = control the channels yourself.",
                  style="CardMuted.TLabel", wraplength=300, justify="left").grid(row=1, column=0, sticky="w", pady=(2, 8))
        for i, channel in enumerate(fixture.AUTO.channels):
            self._channel_block(card, channel).grid(row=2 + i, column=0, sticky="ew", pady=(0, 6))

    def _build_music_card(self, parent, row: int, col: int) -> None:
        card = self._card(parent, row, col)
        ttk.Label(card, text="MUSIC MODE", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(card, text="Lights that react to the music: bass hits, colour changes, strobe.",
                  style="CardMuted.TLabel", wraplength=240, justify="left").grid(row=1, column=0, sticky="w", pady=(6, 8))
        ttk.Button(card, text="\U0001f3b5  Open Music Mode", command=self._open_music_mode,
                   style="Accent.TButton").grid(row=2, column=0, sticky="ew")

    def _build_preset_card(self, parent, row: int, col: int) -> None:
        card = self._card(parent, row, col)
        for c in range(3):
            card.columnconfigure(c, weight=1)
        ttk.Label(card, text="PRESETS", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")

        self.preset_cb = ttk.Combobox(card, values=self.presets.list_presets(), state="readonly")
        self.preset_cb.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        if self.preset_cb["values"]:
            self.preset_cb.current(0)

        ttk.Button(card, text="Load", command=self.load_preset).grid(row=2, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(card, text="Save As...", command=self.save_preset_as).grid(row=2, column=1, sticky="ew", padx=4)
        ttk.Button(card, text="Delete", command=self.delete_preset).grid(row=2, column=2, sticky="ew", padx=(4, 0))

    def _size_to_content(self) -> None:
        # the content decides the minimum size; the window can grow from there and everything scales with it
        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        height = self.root.winfo_reqheight()
        self.root.minsize(width, height)
        self.root.geometry(f"{max(width, 1020)}x{max(height, 700)}")

    # --------- Music Mode (same controller as the sliders)
    def _open_music_mode(self) -> None:
        window = MusicModeWindow(self.root, self.ctl, on_closed=self.root.deiconify)
        window.update_idletasks()
        self.root.withdraw()

    # --------- Sliders
    def _show(self, channel: int, value: int) -> None:
        text = fixture.describe(channel, value)
        if self._shown.get(channel) != text:             # only touch Tk when something changed
            self._shown[channel] = text
            self.channel_labels[channel].config(text=text)
        if channel == fixture.CH_LED_PATTERN:
            self._pattern_var.set(fixture.pattern_of(value))

    def on_slider_change(self, channel: int, value) -> None:
        value = int(float(value))
        self._show(channel, value)
        self.ctl.set(channel, value)

    def _sync_sliders(self) -> None:
        """Moves every slider to the controller's manual state (after a preset load / blackout)."""
        for channel, value in self.ctl.manual_values().items():
            self.sliders[channel].set(value)
            self._show(channel, value)

    # --------- non-blocking connection
    def toggle_connection(self) -> None:
        if not self.ctl.connected:
            self.btn_connect.config(state="disabled")
            threading.Thread(target=self._connect_worker, args=(self.port_cb.get(),), daemon=True).start()
        else:
            self.ctl.disconnect()
            self._set_connection_state(False)

    def _connect_worker(self, port: str) -> None:
        try:
            self.ctl.connect(port)
        except Exception as e:
            self.root.after(0, self._connect_failed, e, port)
            return
        self.root.after(0, self._set_connection_state, True, port)

    def _connect_failed(self, error: Exception, port: str) -> None:
        self.btn_connect.config(state="normal")
        show_error(self.root, "Error", f"Could not open {port}:\n{error}")

    def _connection_lost(self, error: Exception) -> None:
        self._set_connection_state(False)
        show_error(self.root, "Connection Lost", f"DMX connection interrupted:\n{error}")

    # --------- presets / blackout
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
        self.presets.save(name, self.ctl.manual_values())
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
        self.ctl.set_many(values)
        self._sync_sliders()

    def delete_preset(self) -> None:
        name = self.preset_cb.get()
        if not name:
            return
        if ask_yes_no(self.root, "Delete Preset", f"Delete preset '{name}'?"):
            self.presets.delete(name)
            self.refresh_preset_list()

    def blackout(self) -> None:
        self.ctl.blackout()
        self._sync_sliders()

    def on_close(self) -> None:
        self.ctl.disconnect()
        self.root.destroy()


class ThemedDialog(tk.Toplevel):
    # Themed popups caz the tkinter.messagebox / simpledialog are boring

    def __init__(self, parent: tk.Tk, title: str, message: str, buttons: list[str], with_entry: bool = False):
        super().__init__(parent)
        self.title(title)
        self.configure(bg=config.COLOR_BG)
        self.resizable(False, False)
        self.transient(parent)
        self.grab_set()

        force_dark_titlebar(self)

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
