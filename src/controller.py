"""
DMX Controller & Presets
=========================

Non-UI layer: wraps the serial DMX512 link to a USB-DMX adapter and manages
saved channel presets on disk.

Also contains a few small Windows-only display helpers (DPI awareness, dark
title bar). They live here rather than in ui.py so musicmode.py can import
them too without creating a circular import with ui.py (ui.py imports
MusicModeWindow from musicmode.py). All of them are no-ops on non-Windows.
"""

import ctypes
import ctypes.wintypes as wintypes
import json
import sys
import time
from pathlib import Path

import serial

from .config import parameters


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
        self.data = bytearray(parameters.UNIVERSE_SIZE)

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
        for i in range(1, parameters.UNIVERSE_SIZE):
            self.data[i] = 0
        try:
            self.send()
            self.ser.close()
        except Exception:
            pass


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
