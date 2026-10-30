# partial implementation by ai
import ctypes, json, logging, multiprocessing, queue, sys, time
import ctypes.wintypes as wintypes
from pathlib import Path

from . import fixture
from .dmx_process import MANUAL, MODE_MANUAL, MODE_MUSIC, MUSIC, SharedDMX, dmx_main


def _is_windows() -> bool:
    return sys.platform == "win32"

if _is_windows():
    _user32 = ctypes.windll.user32
    _dwmapi = ctypes.windll.dwmapi

    _user32.GetParent.argtypes = [wintypes.HWND]
    _user32.GetParent.restype = wintypes.HWND

    _dwmapi.DwmSetWindowAttribute.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
    _dwmapi.DwmSetWindowAttribute.restype = ctypes.c_long
else:
    _user32 = None
    _dwmapi = None


class DMXController:
    HANG_S = 3.0
    CONNECT_TIMEOUT_S = 15.0
    MAX_RESTARTS = 3
    RESTART_WINDOW_S = 60.0

    def __init__(self, on_lost=None, on_connected=None, on_connect_failed=None):
        self.on_lost = on_lost
        self.on_connected = on_connected
        self.on_connect_failed = on_connect_failed
        self._ctx = multiprocessing.get_context("spawn")
        self.shared = SharedDMX(self._ctx)
        self._owner: str | None = None
        self._proc = None
        self._cmd_q = None
        self._evt_q = None
        self._port: str | None = None
        self._connecting_since: float | None = None
        self._beat = 0
        self._beat_t = time.monotonic()
        self._restarts: list[float] = []
        self._start_process()

    def _start_process(self) -> None:
        self._cmd_q = self._ctx.Queue()
        self._evt_q = self._ctx.Queue()
        self._cmd_q.cancel_join_thread()
        self._proc = self._ctx.Process(target=dmx_main, args=(self.shared, self._cmd_q, self._evt_q), name="dmx-output", daemon=True)
        self._proc.start()
        self.shared.connected.value = 0
        self._beat = self.shared.dmx_beat.value
        self._beat_t = time.monotonic()

    def _kill_process(self) -> None:
        proc, self._proc = self._proc, None
        if proc is not None:
            try:
                if proc.is_alive():
                    proc.kill()
                proc.join(timeout=1.0)
            except Exception:
                logging.debug("could not stop the DMX process", exc_info=True)
        for q in (self._cmd_q, self._evt_q):
            try:
                q.close()
                q.cancel_join_thread()
            except Exception:
                pass

    def _restart(self, reason: str) -> None:
        port, was_connected = self._port, self.connected or self._connecting_since is not None
        logging.error("DMX output process restarted: %s", reason)
        self._kill_process()
        now = time.monotonic()
        self._restarts = [t for t in self._restarts if now - t < self.RESTART_WINDOW_S] + [now]
        self._connecting_since = None
        self._start_process()
        if not was_connected or port is None:
            return
        if len(self._restarts) > self.MAX_RESTARTS:
            self._port = None
            if self.on_lost:
                self.on_lost(f"The DMX output keeps failing ({reason})")
            return
        self._connecting_since = now
        self._cmd_q.put(("connect", port))

    def poll(self) -> None:
        if self._proc is None:
            return
        while True:
            try:
                event = self._evt_q.get_nowait()
            except (queue.Empty, EOFError, OSError):
                break
            self._handle(event)
            if self._proc is None:
                return

        now = time.monotonic()
        if not self._proc.is_alive():
            self._restart(f"process exited with code {self._proc.exitcode}")
            return
        if self._connecting_since is not None:
            if now - self._connecting_since > self.CONNECT_TIMEOUT_S:
                port, self._port, self._connecting_since = self._port, None, None
                self._kill_process()
                self._start_process()
                if self.on_connect_failed:
                    self.on_connect_failed(port or "", "Opening the port timed out")
            return
        if self.connected:
            beat = self.shared.dmx_beat.value
            if beat != self._beat:
                self._beat, self._beat_t = beat, now
            elif now - self._beat_t > self.HANG_S:
                self._restart("no frame was sent for %.0f s" % self.HANG_S)

    def _handle(self, event) -> None:
        kind = event[0]
        if kind == "connected":
            self._connecting_since = None
            self._beat, self._beat_t = self.shared.dmx_beat.value, time.monotonic()
            if self.on_connected:
                self.on_connected(event[1])
        elif kind == "connect_failed":
            self._connecting_since, self._port = None, None
            if self.on_connect_failed:
                self.on_connect_failed(event[1], event[2])
        elif kind == "lost":
            self._port = None
            if self.on_lost:
                self.on_lost(event[1])
        elif kind == "disconnected":
            self._port = None

    def set(self, channel: int, value: int, source: str = MANUAL) -> None:
        if source == MANUAL:
            self.shared.write_manual(channel, value)
        elif source == self._owner:
            self.shared.write_music({channel: value})

    def set_many(self, values: dict, source: str = MANUAL) -> None:
        for channel, value in values.items():
            self.set(channel, value, source)

    def get(self, channel: int) -> int:
        return self.shared.live(channel)

    def manual_values(self) -> dict:
        return {ch: self.shared.manual[ch] for ch in fixture.CHANNELS}

    def blackout(self) -> None:
        for ch in fixture.CHANNELS:
            self.set(ch, 0)

    def acquire(self, source: str) -> None:
        self._owner = source
        self.shared.clear_music()
        self.shared.mode.value = MODE_MUSIC

    def release(self, source: str) -> None:
        if self._owner != source:
            return
        self._owner = None
        self.shared.mode.value = MODE_MANUAL

    @property
    def connected(self) -> bool:
        return bool(self.shared.connected.value)

    @property
    def connecting(self) -> bool:
        return self._connecting_since is not None

    def connect(self, port: str) -> None:
        if self.connected or self.connecting or self._proc is None:
            return
        self._port = port
        self._connecting_since = time.monotonic()
        self._cmd_q.put(("connect", port))

    def disconnect(self) -> None:
        self._port = None
        self._connecting_since = None
        if self._proc is not None:
            self._cmd_q.put(("disconnect",))

    def close(self) -> None:
        if self._proc is None:
            return
        try:
            self._cmd_q.put(("quit",))
            self._proc.join(timeout=1.5)
        except Exception:
            pass
        self._kill_process()

class PresetManager:
    def __init__(self, directory: Path):
        self.directory = directory
        self.directory.mkdir(exist_ok=True)

    def list_presets(self) -> list[str]:
        return sorted(p.stem for p in self.directory.glob("*.json"))

    def save(self, name: str, values: dict[int, int]) -> None:
        path = self.directory / f"{name}.json"
        with path.open("w", encoding="utf-8") as f:
            json.dump(values, f, indent=2)

    def load(self, name: str) -> dict[int, int]:
        path = self.directory / f"{name}.json"
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
        return {int(ch): int(val) for ch, val in raw.items()}

    def delete(self, name: str) -> None:
        (self.directory / f"{name}.json").unlink(missing_ok=True)

def enable_dpi_awareness() -> None:
    if not _is_windows():
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


def apply_dark_titlebar(window) -> None:
    if not _is_windows():
        return
    window.update_idletasks()
    hwnd = _user32.GetParent(window.winfo_id())
    for attribute in (20, 19):
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