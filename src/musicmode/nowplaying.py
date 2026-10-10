"""
Now playing
===========

Title / artist / cover art of the track that is currently playing. Entirely optional: if there is no way to
read it on this system (or the source stops delivering), Music Mode simply runs without title and cover.

  Windows: NowPlayingBridge.ps1 (plain PowerShell, no install / build step) is started as a subprocess; it
           writes nowplaying.json + the cover image into config.NOWPLAYING_CACHE_DIR, which is read here.
           Its stdout / stderr go to bridge.log in that folder. If it produces no output within a few
           seconds the reader gives up on it.
  Linux:   MPRIS over D-Bus via `jeepney` -- title, artist and cover art (file:// or http(s):// URL).
           Best-effort, untested against a real D-Bus session.
  Other:   nothing -- now_playing_available() is False and the window shows no track info.

Python itself never speaks COM / WinRT: the bridge does that.
"""

import json
import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import config

_IS_WINDOWS = sys.platform == "win32"

# Linux: MPRIS over D-Bus via jeepney (pure Python, no system dev packages needed)
try:
    if not _IS_WINDOWS:
        from jeepney import DBusAddress, new_method_call
        from jeepney.io.blocking import open_dbus_connection
        _DBUS_AVAILABLE = True
    else:
        _DBUS_AVAILABLE = False
except Exception:
    _DBUS_AVAILABLE = False


def _pick_source() -> str | None:
    """\"ps1\" (Windows + PowerShell + the script), \"mpris\" (Linux + jeepney) or None."""
    if _IS_WINDOWS:
        if config.NOWPLAYING_BRIDGE_SCRIPT.exists() and shutil.which("powershell"):
            return "ps1"
        return None
    return "mpris" if _DBUS_AVAILABLE else None


def now_playing_available() -> bool:
    return _pick_source() is not None


# --------- Linux: MPRIS over D-Bus ---------
def _fetch_mpris_metadata():
    """Queries the first available MPRIS player on the session bus for its current Metadata.
    Returns (title, artist, cover url) or None if no player is registered."""
    conn = open_dbus_connection(bus="SESSION")
    try:
        bus_addr = DBusAddress("/org/freedesktop/DBus", bus_name="org.freedesktop.DBus",
                               interface="org.freedesktop.DBus")
        names_reply = conn.send_and_get_reply(new_method_call(bus_addr, "ListNames"))
        names = [n for n in names_reply.body[0] if n.startswith("org.mpris.MediaPlayer2.")]
        if not names:
            return None

        player_addr = DBusAddress("/org/mpris/MediaPlayer2", bus_name=names[0],
                                  interface="org.freedesktop.DBus.Properties")
        get_msg = new_method_call(player_addr, "Get", "ss", ("org.mpris.MediaPlayer2.Player", "Metadata"))
        metadata = conn.send_and_get_reply(get_msg).body[0][1]      # ("a{sv}", {...}) -> the actual dict

        title = metadata.get("xesam:title", ("s", ""))[1]
        artists = metadata.get("xesam:artist", ("as", []))[1]
        art_url = metadata.get("mpris:artUrl", ("s", ""))[1]
        return title, ", ".join(artists) if artists else "", art_url
    finally:
        conn.close()


def _load_art_bytes(art_url: str):
    if not art_url:
        return None
    try:
        if art_url.startswith("file://"):
            import urllib.parse
            return Path(urllib.parse.unquote(art_url[len("file://"):])).read_bytes()
        if art_url.startswith(("http://", "https://")):
            import urllib.request
            with urllib.request.urlopen(art_url, timeout=2) as response:
                return response.read()
    except Exception:
        return None
    return None


class NowPlayingReader:
    """Provides title / artist / cover art of the currently playing track (see the module docstring)."""

    # consecutive polls the bridge gets to produce its first output file before the reader gives up on it
    BRIDGE_MISS_LIMIT = 10

    def __init__(self, on_update, poll_interval: float = 1.0, on_playing=None):
        self.on_update = on_update          # callback(title: str, artist: str, cover_bytes: bytes | None)
        self.on_playing = on_playing        # optional callback(playing: bool), PowerShell bridge only
        self.poll_interval = poll_interval
        self._source = _pick_source()
        self._stop = threading.Event()
        self._thread = None
        self._process = None
        self._log_file = None
        self._last_signature = None
        self._last_playing = None
        self._misses = 0

    def start(self) -> None:
        if self._thread is not None or self._source is None:
            return
        if self._source == "ps1" and not self._start_bridge():
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._stop_bridge()

    # ---- PowerShell bridge
    def _start_bridge(self) -> bool:
        cache_dir = config.NOWPLAYING_CACHE_DIR
        try:
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._log_file = open(cache_dir / "bridge.log", "w", encoding="utf-8")
            args = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
                    "-File", str(config.NOWPLAYING_BRIDGE_SCRIPT),
                    str(cache_dir), str(int(self.poll_interval * 1000)),
                    str(os.getpid())]       # 3rd arg: the bridge exits when this process is gone
            if config.NOWPLAYING_DEBUG:
                args.append("-RunDebug")
            self._process = subprocess.Popen(args, creationflags=subprocess.CREATE_NO_WINDOW,
                                             stdout=self._log_file, stderr=subprocess.STDOUT)
            return True
        except Exception:
            logging.exception("Failed to start NowPlayingBridge")
            self._stop_bridge()
            self._source = None
            return False

    def _stop_bridge(self) -> None:
        if self._process is not None:
            try:
                self._process.terminate()
                self._process.wait(timeout=2)
            except Exception:
                try:
                    self._process.kill()
                except Exception:
                    pass
            self._process = None
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    # ---- polling
    def _loop(self) -> None:
        poll = self._poll_bridge if self._source == "ps1" else self._poll_mpris
        while not self._stop.is_set() and self._source is not None:
            try:
                poll()
            except Exception:
                logging.debug("now playing poll failed", exc_info=True)
            self._stop.wait(self.poll_interval)

    def _poll_bridge(self) -> None:
        cache_dir = config.NOWPLAYING_CACHE_DIR
        try:
            data = json.loads((cache_dir / "nowplaying.json").read_text(encoding="utf-8-sig"))
        except Exception:
            self._misses += 1
            if self._misses >= self.BRIDGE_MISS_LIMIT:
                logging.warning("NowPlayingBridge produced no output after %d attempts -- running without "
                                "title / cover. Check %s for errors.", self._misses, cache_dir / "bridge.log")
                self._source = None
                self._stop_bridge()
            return
        self._misses = 0

        playing = bool(data.get("playing", True))
        if playing != self._last_playing:
            self._last_playing = playing
            if self.on_playing:
                self.on_playing(playing)

        title, artist = data.get("title", ""), data.get("artist", "")
        has_cover = bool(data.get("hasCover", False))
        signature = (title, artist, has_cover)
        if signature == self._last_signature:
            return
        self._last_signature = signature

        cover_bytes = None
        if has_cover:
            try:
                cover_bytes = (cache_dir / "nowplaying_cover.img").read_bytes()
            except Exception:
                logging.warning("hasCover is true but nowplaying_cover.img can't be read")
        self.on_update(title, artist, cover_bytes)

    def _poll_mpris(self) -> None:
        metadata = _fetch_mpris_metadata()
        if metadata is None:
            return
        title, artist, art_url = metadata
        signature = (title, artist, art_url)
        if signature == self._last_signature:
            return
        self._last_signature = signature
        self.on_update(title, artist, _load_art_bytes(art_url))
