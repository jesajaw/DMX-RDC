"""
DMX Derby Controller
=====================

Tkinter GUI to control a Razor Derby over a USB-DMX adapter
(RS485, 250000 baud, 2 stop bits).

Split across these files:
- config.py:     central parameters class, single source of all constants
- controller.py: DMX serial controller, preset persistence, platform helpers
- musicmode.py:  audio analysis + Music Mode window
- lightengine.py: tempo + song-structure engine and the simple "when -> do" looks of Music Mode
- theme.py:       every ttk style (colours come from config.py)
- ui.py (this file): main window (DMXUI), dialogs

The entry point does NOT live here but in main.py at the project root --
this file only provides DMXUI (and the dialog helpers), without starting a
Tk root/mainloop itself.

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

import serial.tools.list_ports
import tkinter as tk
from tkinter import ttk

from . import config, theme
from .controller import Controller, PresetManager, apply_dark_titlebar, force_dark_titlebar
from .musicmode import MusicModeWindow


class DMXUI:
    def __init__(self, root: tk.Tk):
        self.root = root
        apply_dark_titlebar(root)
        self.root.title("DMX Derby Controller")
        self.root.configure(bg=config.COLOR_BG)

        self.dmx: Controller | None = None
        self.is_sending = False
        self.channel_labels: dict[int, ttk.Label] = {}
        self.sliders: dict[int, ttk.Scale] = {}
        self.presets = PresetManager(config.PRESETS_DIR)
        self._music_last: dict[int, int] = {}   # last value Music Mode wrote per channel

        self._setup_style()
        self.root.columnconfigure(0, weight=1)
        self.root.rowconfigure(1, weight=1)
        self._build_header()
        self._build_channel_grid()
        self._size_to_content()

        self._open_music_mode()

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
        theme.apply_theme(self.root)

    # --------- UI
    GROUPS = (
        ("Fixture", (1, 2)),
        ("Derby  \u00b7  LED", (3, 4, 5)),
        ("Laser", (6, 7, 8, 9)),
    )

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
        scheme = config.ACTIVE_SCHEME
        self.conn_canvas.itemconfig(self._conn_dot, fill="#4cc38a" if connected else scheme["MUTED"])
        self.conn_label.config(text=f"Connected  \u00b7  {port}" if connected else "Disconnected")

    def _build_channel_grid(self) -> None:
        # Three columns (Fixture / Derby / Laser), channels stacked inside them. Everything stretches with
        # the window; the free slots under Fixture and Derby hold the presets and the Music Mode entry.
        grid = ttk.Frame(self.root, padding=(18, 8, 18, 18))
        grid.grid(row=1, column=0, sticky="nsew")
        for col in range(3):
            grid.columnconfigure(col, weight=1, uniform="groups")
        for row in range(1, 5):
            grid.rowconfigure(row, weight=1, uniform="cells")

        for col, (title, channels) in enumerate(self.GROUPS):
            ttk.Label(grid, text=title.upper(), style="Subtitle.TLabel", font=(theme.FONT, 9, "bold"),
                      foreground=config.ACTIVE_SCHEME["ACCENT2"]).grid(row=0, column=col, sticky="w",
                                                                       padx=6, pady=(0, 6))
            for i, channel in enumerate(channels):
                self._build_cell(grid, i + 1, col, channel, config.CHANNEL_NAMES[channel - 1])

        self._build_preset_card(grid, row=3, col=0, rowspan=2)
        self._build_music_card(grid, row=4, col=1)

    def _build_cell(self, parent: ttk.Frame, row: int, col: int, channel: int, name: str) -> None:
        cell = ttk.Frame(parent, padding=(14, 10), style="Card.TFrame")
        cell.grid(row=row, column=col, padx=6, pady=5, sticky="nsew")
        cell.columnconfigure(0, weight=1)

        ttk.Label(cell, text=name, style="CellTitle.TLabel").grid(row=0, column=0, sticky="w")
        status = ttk.Label(cell, text="---", style="Status.TLabel", anchor="w")
        status.grid(row=1, column=0, sticky="ew", pady=(2, 6))
        self.channel_labels[channel] = status

        slider = ttk.Scale(cell, from_=0, to=255, orient="horizontal", style="Card.Horizontal.TScale",
                           command=lambda v, c=channel: self.on_slider_change(c, v))
        slider.set(0)
        slider.grid(row=2, column=0, sticky="ew")
        self.sliders[channel] = slider

        self.update_display(channel, 0)

    def _build_preset_card(self, parent: ttk.Frame, row: int, col: int, rowspan: int) -> None:
        card = ttk.Frame(parent, padding=(14, 10), style="Card.TFrame")
        card.grid(row=row, column=col, rowspan=rowspan, padx=6, pady=5, sticky="nsew")
        card.columnconfigure(0, weight=1)
        card.columnconfigure(1, weight=1)
        card.columnconfigure(2, weight=1)
        ttk.Label(card, text="PRESETS", style="CardTitle.TLabel").grid(row=0, column=0, columnspan=3, sticky="w")

        self.preset_cb = ttk.Combobox(card, values=self.presets.list_presets(), state="readonly")
        self.preset_cb.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        if self.preset_cb["values"]:
            self.preset_cb.current(0)

        ttk.Button(card, text="Load", command=self.load_preset).grid(row=2, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(card, text="Save As...", command=self.save_preset_as).grid(row=2, column=1, sticky="ew", padx=4)
        ttk.Button(card, text="Delete", command=self.delete_preset).grid(row=2, column=2, sticky="ew", padx=(4, 0))

    def _build_music_card(self, parent: ttk.Frame, row: int, col: int) -> None:
        card = ttk.Frame(parent, padding=(14, 10), style="Card.TFrame")
        card.grid(row=row, column=col, padx=6, pady=5, sticky="nsew")
        card.columnconfigure(0, weight=1)
        ttk.Label(card, text="MUSIC MODE", style="CardTitle.TLabel").grid(row=0, column=0, sticky="w")
        ttk.Label(card, text="Lights that react to the music: bass hits, colour changes, strobe.",
                  style="CardMuted.TLabel", wraplength=240, justify="left").grid(row=1, column=0, sticky="w", pady=(6, 8))
        ttk.Button(card, text="\U0001f3b5  Open Music Mode", command=self._open_music_mode,
                   style="Accent.TButton").grid(row=2, column=0, sticky="ew")

    def _open_music_mode(self) -> None:
        channel_names = {ch: config.CHANNEL_NAMES[ch - 1] for ch in range(1, config.CHANNEL_COUNT + 1)}
        window = MusicModeWindow(
            self.root,
            channel_names=channel_names,
            set_channel_value=self._music_set_channel,
            restore_sliders=self._music_restore_sliders,
            on_closed=self._on_music_mode_closed,
            colors=config.ACTIVE_SCHEME,
            is_connected=lambda: self.dmx is not None,
        )
        window.update_idletasks()
        self.root.withdraw()

    def _on_music_mode_closed(self) -> None:
        self.root.deiconify()

    def _music_set_channel(self, channel: int, value: int) -> None:
        # Music Mode drives the channel. The value goes straight to the DMX buffer -- it must not depend on the
        # slider accepting a programmatic set() while it is disabled. The slider only mirrors the value.
        slider = self.sliders.get(channel)
        if slider is None:
            return
        value = int(value)
        if self.dmx:
            self.dmx.set_channel(channel, value)
        if self._music_last.get(channel) == value:
            return                                   # nothing new for the display
        self._music_last[channel] = value
        self.update_display(channel, value)
        slider.state(["!disabled"])                  # a disabled ttk.Scale may ignore set()
        slider.set(value)                            # -> on_slider_change (display + dmx, same value)
        slider.state(["disabled"])                   # locked while Music Mode owns the channel

    def _music_restore_sliders(self, channels: list[int]) -> None:
        for channel in channels:
            self._music_last.pop(channel, None)
            slider = self.sliders.get(channel)
            if slider is not None:
                slider.state(["!disabled"])

    def _size_to_content(self) -> None:
        # the content decides the minimum size; the window can grow from there and everything scales with it
        self.root.update_idletasks()
        width = self.root.winfo_reqwidth()
        height = self.root.winfo_reqheight()
        self.root.minsize(width, height)
        self.root.geometry(f"{max(width, 1020)}x{max(height, 640)}")

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
            self._set_connection_state(False)

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
        self._set_connection_state(True, self.port_cb.get())

    def _connect_failed(self, error: Exception, port: str) -> None:
        self.btn_connect.config(state="normal")
        show_error(self.root, "Error", f"Could not open {port}:\n{error}")

    def _connection_lost(self, error: Exception) -> None:
        self.is_sending = False
        self.dmx = None
        self.btn_connect.config(text="Connect", state="normal")
        self._set_connection_state(False)
        show_error(self.root, "Connection Lost", f"DMX connection interrupted:\n{error}")

    def _send_loop(self) -> None:
        # Background send cycle -- exits and reports on write failure (unplug) instead of failing silently
        while self.is_sending:
            try:
                self.dmx.send()
            except (serial.SerialException, OSError) as e:
                self.root.after(0, self._connection_lost, e)
                return
            time.sleep(config.SEND_INTERVAL_S)


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
