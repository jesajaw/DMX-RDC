"""
DMX Controller & Presets
=========================

Non-UI layer: DMXController is the single interface to the lights (serial DMX512 link to a USB-DMX
adapter + send thread; the manual window and Music Mode both write through it), PresetManager keeps
saved channel presets on disk.

Also contains a few small Windows-only display helpers (DPI awareness, dark
title bar). They live here rather than in app.py so musicmode/music_app.py can
import them too without creating a circular import with app.py (app.py imports
MusicModeWindow from music_app.py). All of them are no-ops on non-Windows.
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import sys
import threading
import time
from pathlib import Path

import serial

from . import config, fixture


def _is_windows() -> bool:
    return sys.platform == "win32"


# Only touch ctypes.windll on Windows -- it doesn't exist on other platforms,
# so referencing it unconditionally at import time would crash the whole
# package on Linux/macOS before any platform check even runs.
if _is_windows():
    _user32 = ctypes.windll.user32
    _dwmapi = ctypes.windll.dwmapi

    _user32.GetParent.argtypes = [wintypes.HWND]
    _user32.GetParent.restype = wintypes.HWND

    _dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    _dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long  # HRESULT
else:
    _user32 = None
    _dwmapi = None


MANUAL, MUSIC = "manual", "music"


class DMXController:
    """The one interface to the lights. The manual window and Music Mode both write through it.

    - set(channel, value, source) / set_many(values, source): the buffer that goes out on the wire.
    - Every manual change is also remembered as the *manual state*. While another source (Music Mode) has
      acquire()d the lights, manual writes only update that remembered state; release() puts it back on the
      wire. So opening Music Mode and closing it again leaves the manual setup exactly as it was.
    - connect() / disconnect() own the serial port and the send thread (a frame is sent over and over, as DMX needs).
    Writes are plain item assignments on a bytearray, so they are safe from any thread (the audio thread writes
    from the capture callback, the GUI from the Tk thread).
    """

    def __init__(self, on_lost=None):
        self.on_lost = on_lost                      # callback(error), called from the send thread
        self._live = bytearray(config.UNIVERSE_SIZE)
        self._manual = bytearray(config.UNIVERSE_SIZE)
        self._owner: str | None = None              # None = manual, else the source that acquired the lights
        self._ser = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    # ---- writing
    def set(self, channel: int, value: int, source: str = MANUAL) -> None:
        if not 1 <= channel < config.UNIVERSE_SIZE:
            return
        value = 0 if value < 0 else 255 if value > 255 else int(value)
        if source == MANUAL:
            self._manual[channel] = value
            if self._owner is None:
                self._live[channel] = value
        elif source == self._owner:
            self._live[channel] = value

    def set_many(self, values: dict, source: str = MANUAL) -> None:
        for channel, value in values.items():
            self.set(channel, value, source)

    def get(self, channel: int) -> int:
        """What is on the wire for this channel right now."""
        return self._live[channel]

    def manual_values(self) -> dict:
        return {ch: self._manual[ch] for ch in fixture.CHANNELS}

    def blackout(self) -> None:
        """Manual blackout: every channel to 0 (also the remembered manual state)."""
        for ch in fixture.CHANNELS:
            self.set(ch, 0)

    # ---- who drives the lights
    def acquire(self, source: str) -> None:
        """`source` takes over the output; it starts dark, manual writes no longer reach the wire."""
        self._owner = source
        for ch in fixture.CHANNELS:
            self._live[ch] = 0

    def release(self, source: str) -> None:
        """Hands the output back to the manual state."""
        if self._owner != source:
            return
        self._owner = None
        for ch in fixture.CHANNELS:
            self._live[ch] = self._manual[ch]

    # ---- connection
    @property
    def connected(self) -> bool:
        return self._ser is not None

    def connect(self, port: str) -> None:
        """Opens the port (can block -> call from a worker thread) and starts sending. Raises on failure."""
        if self.connected:
            return
        self._ser = serial.Serial(port=port, baudrate=250000, bytesize=serial.EIGHTBITS,
                                  parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_TWO)
        self._stop.clear()
        self._thread = threading.Thread(target=self._send_loop, args=(self._ser,), daemon=True)
        self._thread.start()

    def disconnect(self) -> None:
        """Stops sending, sends one all-zero frame, closes the port."""
        ser, self._ser = self._ser, None
        if ser is None:
            return
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        for ch in range(1, config.UNIVERSE_SIZE):
            self._live[ch] = 0
        try:
            self._write_frame(ser)
            ser.close()
        except Exception:
            pass

    def _write_frame(self, ser) -> None:
        # one DMX frame, including break / mark-after-break
        ser.break_condition = True
        time.sleep(0.0001)
        ser.break_condition = False
        time.sleep(0.000012)
        ser.write(self._live[:config.SEND_SLOTS])

    def _send_loop(self, ser) -> None:
        while not self._stop.is_set():
            try:
                self._write_frame(ser)
            except (serial.SerialException, OSError) as error:
                self._ser = None                    # the link is gone
                try:
                    ser.close()
                except Exception:
                    pass
                if self.on_lost is not None:
                    self.on_lost(error)
                return
            self._stop.wait(config.SEND_INTERVAL_S)


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


# Windows-only visual fixes tkinter doesn't handle by itself: DPI awareness (fixes
# blurry/blocky text on HiDPI displays) and a dark title bar to match the theme.
# Both are no-ops on non-Windows.
def enable_dpi_awareness() -> None:
    if not _is_windows():
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # PROCESS_SYSTEM_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # fallback for older Windows
        except Exception:
            pass


def apply_dark_titlebar(window) -> None:
    if not _is_windows():
        return
    window.update_idletasks()
    hwnd = _user32.GetParent(window.winfo_id())
    for attribute in (20, 19):  # DWMWA_USE_IMMERSIVE_DARK_MODE: 20 (Win10 2004+), 19 (older)
        value = ctypes.c_int(1)
        result = _dwmapi.DwmSetWindowAttribute(hwnd, attribute, ctypes.byref(value), ctypes.sizeof(value))
        if result == 0:
            break


def force_dark_titlebar(window) -> None:
    if not _is_windows():
        return
    window.update()
    try:
        hwnd = _user32.GetParent(window.winfo_id())
        rendering_policy = ctypes.c_int(2)
        _dwmapi.DwmSetWindowAttribute(hwnd, 20, ctypes.byref(rendering_policy), ctypes.sizeof(rendering_policy))
    except Exception:
        pass
