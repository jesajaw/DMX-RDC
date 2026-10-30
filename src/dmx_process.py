# partial implementation by ai
import multiprocessing, queue, signal, time
from . import config

MANUAL, MUSIC = "manual", "music"
MODE_MANUAL, MODE_MUSIC = 0, 1


class SharedDMX:
    def __init__(self, ctx=None):
        ctx = ctx or multiprocessing.get_context("spawn")
        size = config.UNIVERSE_SIZE
        self.manual = ctx.RawArray("B", size)
        self.music = ctx.RawArray("B", size)        # written by the analysis process
        self.mode = ctx.RawValue("B", MODE_MANUAL)  # which of the two goes out
        self.connected = ctx.RawValue("B", 0)       # set by the DMX process
        self.dmx_beat = ctx.RawValue("Q", 0)        # frames sent so far (DMX process)
        self.music_beat = ctx.RawValue("Q", 0)      # loop passes of the analysis process

    @staticmethod
    def _store(array, channel: int, value) -> None:
        if 1 <= channel < config.UNIVERSE_SIZE:      # channel 0 is the DMX start code, always 0
            array[channel] = 0 if value < 0 else 255 if value > 255 else int(value)

    def write_manual(self, channel: int, value) -> None:
        self._store(self.manual, channel, value)

    def write_music(self, values: dict) -> None:
        for channel, value in values.items():
            self._store(self.music, channel, value)

    def clear_music(self) -> None:
        for channel in range(1, config.UNIVERSE_SIZE):
            self.music[channel] = 0

    def live(self, channel: int) -> int:
        return (self.music if self.mode.value == MODE_MUSIC else self.manual)[channel]

    def frame(self) -> bytes:
        source = self.music if self.mode.value == MODE_MUSIC else self.manual
        return bytes(source[:config.SEND_SLOTS])

def _open_port(port: str):
    import serial
    return serial.serial_for_url(port, baudrate=250000, bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_TWO)


def _write_frame(ser, frame: bytes) -> None:
    ser.break_condition = True
    time.sleep(0.0001)
    ser.break_condition = False
    time.sleep(0.000012)
    ser.write(frame)


def _close_port(ser) -> None:
    try:
        _write_frame(ser, bytes(config.SEND_SLOTS))
    except Exception:
        pass
    try:
        ser.close()
    except Exception:
        pass


def dmx_main(shared: SharedDMX, cmd_q, evt_q) -> None:
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    evt_q.cancel_join_thread()
    parent = multiprocessing.parent_process()
    ser = None
    next_frame = 0.0
    last_parent_check = 0.0
    try:
        while True:
            now = time.monotonic()
            if parent is not None and now - last_parent_check > 0.5:
                last_parent_check = now
                if not parent.is_alive():
                    break

            wait = max(0.0, next_frame - now) if ser is not None else 0.2
            try:
                cmd = cmd_q.get(timeout=wait)
            except queue.Empty:
                cmd = None
            except (EOFError, OSError):
                break

            if cmd is not None:
                op = cmd[0]
                if op == "quit":
                    break
                if op == "connect":
                    if ser is not None:
                        _close_port(ser)
                        ser = None
                    shared.connected.value = 0
                    try:
                        ser = _open_port(cmd[1])
                    except Exception as error:
                        evt_q.put(("connect_failed", cmd[1], str(error) or type(error).__name__))
                    else:
                        shared.connected.value = 1
                        next_frame = 0.0
                        evt_q.put(("connected", cmd[1]))
                elif op == "disconnect":
                    if ser is not None:
                        _close_port(ser)
                        ser = None
                    shared.connected.value = 0
                    evt_q.put(("disconnected",))
                continue

            if ser is None:
                continue
            try:
                _write_frame(ser, shared.frame())
            except Exception as error:
                try:
                    ser.close()
                except Exception:
                    pass
                ser = None
                shared.connected.value = 0
                evt_q.put(("lost", str(error) or type(error).__name__))
                continue
            shared.dmx_beat.value += 1
            next_frame = time.monotonic() + config.SEND_INTERVAL_S
    finally:
        if ser is not None:
            _close_port(ser)
        shared.connected.value = 0
