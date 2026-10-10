"""
Audio source
============

Capture only. Delivers raw mono float32 blocks to a callback -- nothing else. The analysis
(analysis.py) never sees PyAudio, WASAPI or PulseAudio, so a different source (microphone,
file, ...) is just another class with the same two methods.

LoopbackSource listens to the system's audio output, whatever device it is played on:

* Windows: every WASAPI loopback device (speakers, headphones, Bluetooth, ...) is opened at once.
  Linux: every PulseAudio / PipeWire "Monitor of <sink>" source.
* The device that carries signal is the one that is forwarded. Another device takes over when it
  is clearly louder or when the current one has been silent for a moment, so switching the
  output device while music plays just continues.
* When nothing arrives for WATCHDOG_S (loopback streams deliver nothing while nothing plays),
  silence is fed in so the lights go dark instead of freezing on the last frame.
* After a stretch of silence the device list is re-scanned (a Bluetooth speaker that was
  connected in the meantime shows up). It is never re-scanned while music plays.

Blocks are forwarded under one lock, so on_block is never called from two threads at once
and the analysis does not need to be thread-safe.
"""

import logging
import sys
import threading
import time
from dataclasses import dataclass

import numpy as np

from . import config

_IS_WINDOWS = sys.platform == "win32"


def _import_pyaudio():
    if _IS_WINDOWS:
        import pyaudiowpatch as pyaudio
    else:
        import pyaudio
    return pyaudio


class AudioSource:
    """Interface: start() begins delivering blocks to on_block(samples, samplerate), stop() ends it."""

    def start(self) -> None:
        raise NotImplementedError

    def stop(self) -> None:
        raise NotImplementedError


@dataclass
class _Tap:
    name: str
    rate: int
    channels: int
    stream: object = None
    rms: float = 0.0
    loud_t: float = -1e9          # last time this device carried signal


class LoopbackSource(AudioSource):
    def __init__(self, on_block, on_error=None):
        self.on_block = on_block              # callback(samples: np.ndarray mono float32, samplerate: int)
        self.on_error = on_error              # callback(exception), only for problems at start
        self.device_name = ""                 # the device currently in use ("" = none yet)
        self._lock = threading.Lock()
        self._pyaudio = None
        self._pa = None
        self._taps = []
        self._current = None
        self._last_forward = time.monotonic()
        self._last_rate = config.SAMPLE_RATE
        self._starved = False
        self._last_scan = 0.0
        self._running = False
        self._thread = None
        self._callback_error_logged = False

    # ---- lifecycle
    def start(self) -> None:
        if self._running:
            return
        try:
            self._pyaudio = _import_pyaudio()
            self._open_all()
        except Exception as e:
            self._close_all()
            if self.on_error:
                self.on_error(e)
            return
        self._running = True
        self._last_forward = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        self._close_all()

    # ---- devices
    def _discover(self, pa) -> list:
        pyaudio = self._pyaudio
        if _IS_WINDOWS:
            try:
                pa.get_host_api_info_by_type(pyaudio.paWASAPI)
            except OSError as e:
                raise RuntimeError("WASAPI is not available on this system") from e
            return list(pa.get_loopback_device_info_generator())
        found = []
        for i in range(pa.get_device_count()):
            info = pa.get_device_info_by_index(i)
            if info.get("maxInputChannels", 0) > 0 and "monitor" in info.get("name", "").lower():
                found.append(info)
        if not found:
            raise RuntimeError("No PulseAudio/PipeWire monitor source found. Make sure PulseAudio, "
                               "or PipeWire with its PulseAudio compatibility layer, is running.")
        return found

    def _open_all(self) -> None:
        pyaudio = self._pyaudio
        self._close_all()
        self._last_scan = time.monotonic()
        pa = pyaudio.PyAudio()
        self._pa = pa
        taps = []
        for info in self._discover(pa):
            try:
                tap = _Tap(name=info["name"], rate=int(info["defaultSampleRate"]),
                           channels=max(1, int(info["maxInputChannels"])))
                tap.stream = pa.open(format=pyaudio.paFloat32, channels=tap.channels, rate=tap.rate,
                                     frames_per_buffer=config.BLOCK_SIZE, input=True,
                                     input_device_index=info["index"],
                                     stream_callback=self._make_callback(tap))
                tap.stream.start_stream()
                taps.append(tap)
            except Exception:
                logging.debug("Could not open audio device %r", info.get("name"), exc_info=True)
        if not taps:
            raise RuntimeError("No loopback / monitor device could be opened")
        with self._lock:
            self._taps = taps
            self._current = None

    def _close_all(self) -> None:
        with self._lock:
            taps, self._taps, self._current = self._taps, [], None
        for tap in taps:
            try:
                tap.stream.stop_stream()
                tap.stream.close()
            except Exception:
                pass
        if self._pa is not None:
            try:
                self._pa.terminate()
            except Exception:
                pass
            self._pa = None

    # ---- blocks
    def _make_callback(self, tap: _Tap):
        pyaudio = self._pyaudio

        def callback(in_data, frame_count, time_info, status):
            try:
                self._handle(tap, np.frombuffer(in_data, dtype=np.float32))
            except Exception:
                if not self._callback_error_logged:       # log once, never spam from the audio thread
                    self._callback_error_logged = True
                    logging.exception("Audio block handling failed")
            return (None, pyaudio.paContinue)
        return callback

    def _handle(self, tap: _Tap, samples: np.ndarray) -> None:
        if tap.channels > 1:
            samples = samples.reshape(-1, tap.channels).mean(axis=1)
        rms = float(np.sqrt(np.mean(samples * samples))) if len(samples) else 0.0
        now = time.monotonic()
        with self._lock:
            tap.rms = rms
            if rms >= config.SOURCE_SWITCH_RMS:
                tap.loud_t = now
            cur = self._current
            if cur is not tap:
                loud = rms >= config.SOURCE_SWITCH_RMS
                if cur is None:
                    take = loud
                else:
                    cur_silent = now - cur.loud_t > config.SOURCE_HOLD_S
                    take = loud and (cur_silent or rms > config.SOURCE_SWITCH_RATIO * cur.rms)
                if not take:
                    return
                self._current = tap
                self.device_name = tap.name
            self._forward(samples, tap.rate, now)

    def _forward(self, samples: np.ndarray, rate: int, now: float) -> None:
        """Caller holds the lock."""
        self._last_forward = now
        self._last_rate = rate
        self._starved = False
        self.on_block(samples, rate)

    # ---- watchdog + re-scan
    def _loop(self) -> None:
        while self._running:
            rate = self._last_rate or config.SAMPLE_RATE
            time.sleep(max(0.005, config.BLOCK_SIZE / rate))
            now = time.monotonic()
            with self._lock:
                since = now - self._last_forward
                if since > config.WATCHDOG_S:
                    self._starved = True
                if self._starved:                            # keep the analysis clock running in silence
                    try:
                        self.on_block(np.zeros(config.BLOCK_SIZE, np.float32), rate)
                    except Exception:
                        logging.debug("on_block failed during silence", exc_info=True)
            if (self._starved and since > config.RESCAN_AFTER_SILENCE_S
                    and now - self._last_scan > config.DEVICE_RESCAN_S):
                try:
                    self._open_all()
                except Exception:
                    logging.debug("Device re-scan failed", exc_info=True)
