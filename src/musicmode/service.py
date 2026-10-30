import logging
import multiprocessing
import queue
import signal
import threading
import time
from dataclasses import dataclass

from . import config, engine
from .analysis import BAND_NAMES, Analyzer, Features
from .audio_source import LoopbackSource
from .nowplaying import NowPlayingReader, now_playing_available

SNAPSHOT_INTERVAL_S = 0.8 / config.UI_FPS      # a little faster than the GUI polls, it takes the newest one
SOURCE_RETRY_S = 5.0                           # try to open the audio device again this often while it is not open
MAX_BACKLOG_BLOCKS = 25                        # ~0.5 s: further behind than this -> skip ahead instead of lagging


@dataclass
class Snapshot:
    """What the GUI needs to draw one frame (small and picklable)."""
    features: Features
    hits: dict            # band -> a hit happened since the previous snapshot (so no hit is lost between frames)
    preview: dict         # engine.preview(): what the fixture is doing right now
    device: str           # audio device currently in use ("" = none yet)


# ======================================================================================================
# Analysis process
# ======================================================================================================
def default_source_factory(on_block, on_error):
    return LoopbackSource(on_block=on_block, on_error=on_error)


def analysis_main(shared, cmd_q, evt_q, init: dict, source_factory=default_source_factory) -> None:
    """Entry point of the analysis process."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)     # Ctrl+C belongs to the UI process, it shuts us down cleanly
    evt_q.cancel_join_thread()                       # never block at exit because the UI stopped reading
    logging.basicConfig(level=logging.WARNING, format="[analysis] %(levelname)s %(message)s")
    worker = _Worker(shared, cmd_q, evt_q, init, source_factory)
    try:
        worker.run()
    except BaseException as error:                   # last words, then the UI restarts us
        logging.exception("Music analysis process crashed")
        worker.send(("error", f"Music analysis crashed: {error!r}"))
        raise


class _Worker:
    def __init__(self, shared, cmd_q, evt_q, init: dict, source_factory):
        self.shared, self.cmd_q, self.evt_q = shared, cmd_q, evt_q
        self.parent = multiprocessing.parent_process()
        self.analyzer = Analyzer()
        self.analyzer.gain = init["gain"]
        self.analyzer.smoothing = init["smoothing"]
        self.analyzer.learning = init["learning"]
        self.engine = engine.LightEngine(init["look"])
        self.engine.bouncy = init["bouncy"]
        self.engine.force_blackout = init["blackout"]
        self.blocks: queue.Queue = queue.Queue(maxsize=200)
        self.source = source_factory(self._enqueue, self._on_source_error)
        self.now_playing = None
        self._stop = False
        self._failed = False
        self._last_error = ""
        self._hits = {n: False for n in BAND_NAMES}
        self._last_snapshot = 0.0
        self._last_source_try = time.monotonic()
        self._last_parent_check = 0.0

    # ---- output to the GUI (never blocks)
    def send(self, event) -> bool:
        try:
            self.evt_q.put_nowait(event)
            return True
        except queue.Full:
            return False
        except Exception:
            logging.debug("could not send %r", event[0], exc_info=True)
            return False

    def _send_error(self, text: str) -> None:
        if text != self._last_error:                 # one message per problem, not one per block
            self._last_error = text
            self.send(("error", text))

    # ---- audio side (called from the PortAudio callback / the silence watchdog)
    def _enqueue(self, samples, samplerate) -> None:
        try:
            self.blocks.put_nowait((samples, samplerate))
        except queue.Full:                           # the analysis fell behind: drop the oldest block
            try:
                self.blocks.get_nowait()
                self.blocks.put_nowait((samples, samplerate))
            except (queue.Empty, queue.Full):
                pass

    def _on_source_error(self, error: Exception) -> None:
        self._send_error(f"Audio error: {error}")

    # ---- now playing (reader thread)
    def _on_now_playing(self, title: str, artist: str, cover_bytes) -> None:
        key = " - ".join(part.strip() for part in (artist, title) if part and part.strip())
        self.analyzer.request_song(key or None)      # the analysis switches to this track's cached values
        self.send(("nowplaying", title, artist, cover_bytes))

    def _on_playing(self, playing: bool) -> None:
        self.send(("playing", playing))

    # ---- commands from the GUI
    def _pump_commands(self) -> None:
        while True:
            try:
                cmd = self.cmd_q.get_nowait()
            except queue.Empty:
                return
            except (EOFError, OSError):
                self._stop = True
                return
            op = cmd[0]
            try:
                if op == "stop":
                    self._stop = True
                elif op == "set":                    # ("set", name, value)
                    _, name, value = cmd
                    if name in ("gain", "smoothing", "learning"):
                        setattr(self.analyzer, name, value)
                    elif name in ("bouncy", "force_blackout"):
                        setattr(self.engine, name, value)
                elif op == "look":                   # ("look", look): the GUI picked a ready-made look
                    self.engine.set_look(cmd[1])
                elif op == "rebuild":                # ("rebuild", look): a grid cell was edited
                    self.engine.look = cmd[1]
                    self.engine.rebuild()
                elif op == "tempo":
                    self.analyzer.request_tempo_recheck()
            except Exception:
                logging.exception("bad command %r", op)

    # ---- main loop
    def run(self) -> None:
        self.source.start()
        if now_playing_available():
            self.now_playing = NowPlayingReader(on_update=self._on_now_playing, on_playing=self._on_playing)
            self.now_playing.start()
        try:
            while not self._stop:
                self._pump_commands()
                self.shared.music_beat.value += 1    # "I am alive and looping" for the GUI's watchdog
                now = time.monotonic()
                if self.parent is not None and now - self._last_parent_check > 0.5:
                    self._last_parent_check = now
                    if not self.parent.is_alive():
                        break
                if not self.source.running and now - self._last_source_try > SOURCE_RETRY_S:
                    self._last_source_try = now      # e.g. PulseAudio was not up yet, Bluetooth speaker came later
                    self.source.start()
                try:
                    samples, rate = self.blocks.get(timeout=0.05)
                except queue.Empty:
                    continue
                if self.blocks.qsize() > MAX_BACKLOG_BLOCKS:
                    while self.blocks.qsize() > 3:   # skip ahead, old audio is worthless for live lights
                        try:
                            samples, rate = self.blocks.get_nowait()
                        except queue.Empty:
                            break
                self._process(samples, rate)
        finally:
            self._shutdown()

    def _process(self, samples, rate) -> None:
        try:
            features = self.analyzer.process(samples, rate)
            values = self.engine.process(features)    # channels 1..9, channel 1 is always 0
        except Exception as error:
            if not self._failed:                     # log once, never spam
                self._failed = True
                logging.exception("Music Mode processing failed")
                self._send_error(f"Light engine error: {error}")
            return
        self.shared.write_music(values)               # straight to the DMX process, the GUI is not involved
        for name in BAND_NAMES:
            if features.hit[name]:
                self._hits[name] = True
        now = time.monotonic()
        if now - self._last_snapshot >= SNAPSHOT_INTERVAL_S:
            snapshot = Snapshot(features, dict(self._hits), self.engine.preview(), self.source.device_name)
            if self.send(("frame", snapshot)):        # a dropped snapshot keeps its hits for the next one
                self._hits = {n: False for n in BAND_NAMES}
            self._last_snapshot = now

    def _shutdown(self) -> None:
        self.shared.clear_music()
        for step in (self.source.stop, self.analyzer.flush,
                     (self.now_playing.stop if self.now_playing else (lambda: None))):
            try:
                step()
            except Exception:
                logging.debug("shutdown step failed", exc_info=True)


# ======================================================================================================
# UI side
# ======================================================================================================
class AnalysisService:
    """The GUI's handle on the analysis process. Every method returns immediately; poll() must be called regularly
    from the GUI thread and is where all callbacks fire."""

    HANG_S = 4.0                 # alive but no progress for this long -> restart
    STARTUP_GRACE_S = 20.0       # spawning + importing numpy can take a while on a cold disk
    MAX_RESTARTS = 3
    RESTART_WINDOW_S = 60.0

    def __init__(self, shared, look, on_error=None, on_status=None, on_now_playing=None, on_playing=None,
                 source_factory=default_source_factory):
        self.on_error = on_error                    # callback(text): problem worth showing in the status line
        self.on_status = on_status                  # callback(text): "restarted ..." style information
        self.on_now_playing = on_now_playing        # callback(title, artist, cover_bytes)
        self.on_playing = on_playing                # callback(playing: bool)
        self._shared = shared
        self._source_factory = source_factory
        self._ctx = multiprocessing.get_context("spawn")
        # the settings the worker has to know; also what a restarted worker is started with
        self.state = dict(look=look.copy(), gain=config.GAIN_DEFAULT, smoothing=config.LEVEL_SMOOTHING,
                          learning=config.LEARN_DEFAULT, bouncy=False, blackout=False)
        self._proc = None
        self._cmd_q = None
        self._evt_q = None
        self._started = 0.0
        self._beat = 0
        self._beat_t = 0.0
        self._seen_beat = False
        self._restarts: list[float] = []
        self._stopped = True

    # ---- lifecycle
    def start(self) -> None:
        self._stopped = False
        self._spawn()

    def _spawn(self) -> None:
        self._shared.clear_music()
        self._cmd_q = self._ctx.Queue()
        self._evt_q = self._ctx.Queue(maxsize=64)
        self._cmd_q.cancel_join_thread()
        init = dict(self.state, look=self.state["look"].copy())
        self._proc = self._ctx.Process(target=analysis_main, name="music-analysis", daemon=True,
                                       args=(self._shared, self._cmd_q, self._evt_q, init, self._source_factory))
        self._proc.start()
        self._started = self._beat_t = time.monotonic()
        self._beat = self._shared.music_beat.value
        self._seen_beat = False

    def stop(self) -> None:
        """Asks the worker to finish (it saves the learned song profiles) and returns immediately. A background
        thread makes sure it is really gone."""
        self._stopped = True
        proc, self._proc = self._proc, None
        if proc is None:
            return
        try:
            self._cmd_q.put(("stop",))
        except Exception:
            pass

        def reap() -> None:
            proc.join(timeout=3.0)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=1.0)
            if proc.is_alive():
                proc.kill()
            self._shared.clear_music()

        threading.Thread(target=reap, name="analysis-reaper", daemon=True).start()

    # ---- settings (GUI -> worker)
    def _send(self, *cmd) -> None:
        if self._proc is not None:
            try:
                self._cmd_q.put(cmd)
            except Exception:
                logging.debug("could not send %r", cmd[0], exc_info=True)

    def _set(self, name: str, value, key: str | None = None) -> None:
        self.state[key or name] = value
        self._send("set", name, value)

    def set_gain(self, value: float) -> None:
        self._set("gain", float(value))

    def set_smoothing(self, value: float) -> None:
        self._set("smoothing", float(value))

    def set_learning(self, value: bool) -> None:
        self._set("learning", bool(value))

    def set_bouncy(self, value: bool) -> None:
        self._set("bouncy", bool(value))

    def set_blackout(self, value: bool) -> None:
        self._set("force_blackout", bool(value), key="blackout")

    def set_look(self, look) -> None:
        """A ready-made look was picked (device state resets)."""
        self.state["look"] = look.copy()
        self._send("look", look.copy())

    def rebuild(self, look) -> None:
        """A grid cell / beat setting of the current look was edited (cycle counters stay)."""
        self.state["look"] = look.copy()
        self._send("rebuild", look.copy())

    def recheck_tempo(self) -> None:
        self._send("tempo")

    # ---- results (worker -> GUI)
    def poll(self):
        """Delivers events and watches the process. Returns the newest Snapshot since the last call, or None."""
        if self._proc is None:
            return None
        newest = None
        hits = {n: False for n in BAND_NAMES}
        while True:
            try:
                event = self._evt_q.get_nowait()
            except (queue.Empty, EOFError, OSError):
                break
            kind = event[0]
            if kind == "frame":
                newest = event[1]
                for name in BAND_NAMES:
                    hits[name] = hits[name] or newest.hits[name]
            elif kind == "error" and self.on_error:
                self.on_error(event[1])
            elif kind == "nowplaying" and self.on_now_playing:
                self.on_now_playing(event[1], event[2], event[3])
            elif kind == "playing" and self.on_playing:
                self.on_playing(event[1])
        if newest is not None:
            newest.hits = hits                       # hits of snapshots that were skipped are not lost
        self._watch()
        return newest

    def _watch(self) -> None:
        if self._proc is None:
            return
        now = time.monotonic()
        beat = self._shared.music_beat.value
        if beat != self._beat:
            self._beat, self._beat_t, self._seen_beat = beat, now, True
        if not self._proc.is_alive():
            self._restart(f"process exited with code {self._proc.exitcode}")
        elif now - self._beat_t > (self.HANG_S if self._seen_beat else self.STARTUP_GRACE_S):
            self._restart("it stopped responding")

    def _restart(self, reason: str) -> None:
        logging.error("Music analysis process restarted: %s", reason)
        proc = self._proc
        try:
            if proc.is_alive():
                proc.kill()
            proc.join(timeout=1.0)
        except Exception:
            pass
        for q in (self._cmd_q, self._evt_q):
            try:
                q.close()
                q.cancel_join_thread()
            except Exception:
                pass
        now = time.monotonic()
        self._restarts = [t for t in self._restarts if now - t < self.RESTART_WINDOW_S] + [now]
        if len(self._restarts) > self.MAX_RESTARTS:
            self._proc = None
            if self.on_error:
                self.on_error(f"Music analysis keeps failing ({reason}). Close and reopen Music Mode.")
            return
        self._spawn()
        if self.on_status:
            self.on_status(f"Music analysis restarted ({reason})")
