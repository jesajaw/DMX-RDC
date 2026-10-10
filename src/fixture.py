"""
The ONE place that knows the Varytec Razor Derby in its 9-channel mode (see the manual in the project root).
The manual window, Music Mode and the controller all take their channel names, device groups, colour ranges and value texts from here -- nothing about the channel layout is defined anywhere else.

    Derby   Ch3 colour, Ch4 strobe, Ch5 motor
    LED     Ch6 pattern
    Laser   Ch7 colour, Ch8 strobe, Ch9 rotation
    Auto    Ch1 show select, Ch2 speed

Three parts:
    1. the channel table                    (names, which device a channel belongs to, describe(channel, value))
    2. colour / pattern / position tables   (one range per entry; the DMX value sent is the middle of it)
    3. the device states                    (Led, Derby, Laser, Fixture) that render themselves to {channel: value}; this is what Music Mode's engine drives

"""

from dataclasses import dataclass, field

CHANNEL_COUNT = 9
CHANNELS = tuple(range(1, CHANNEL_COUNT + 1))

CH_AUTO, CH_SPEED = 1, 2
CH_DERBY_COLOUR, CH_DERBY_STROBE, CH_DERBY_MOTOR = 3, 4, 5
CH_LED_PATTERN = 6
CH_LASER_COLOUR, CH_LASER_STROBE, CH_LASER_ROTATION = 7, 8, 9


def _clamp(x: int, low: int, high: int) -> int:
    return low if x < low else high if x > high else x


# =====================================================================================
# 1. Channels and devices
# number -> short name
# =====================================================================================

CHANNEL_NAMES = {
    1: "Show Select", 2: "Speed",
    3: "Color", 4: "Strobe", 5: "Motor",
    6: "Pattern",
    7: "Mode", 8: "Strobe", 9: "Rotation",
}


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    channels: tuple


AUTO = Group("auto", "Automatic", (CH_AUTO, CH_SPEED))
DEVICES = (
    Group("derby", "Derby", (CH_DERBY_COLOUR, CH_DERBY_STROBE, CH_DERBY_MOTOR)),
    Group("led", "LED", (CH_LED_PATTERN,)),
    Group("laser", "Laser", (CH_LASER_COLOUR, CH_LASER_STROBE, CH_LASER_ROTATION)),
)


def channel_title(channel: int) -> str:
    return f"{channel} \u00b7 {CHANNEL_NAMES[channel]}"


# =====================================================================================
# 2. Value tables
# =====================================================================================
@dataclass(frozen=True)
class Colour:
    key: str
    label: str
    low: int # first value...
    high: int # last value for the dmx setting
    auto: bool = False # the fixture changes colour by itself (speed not controllable, may needs to be measured)

    @property
    def dmx(self) -> int:
        # value we send: middle of the settings range
        return (self.low + self.high + 1) // 2


def _colours(first: int, entries) -> dict:
    # entries: (key, label, last value of the range[, auto]); every range starts after the previous one
    out, low = {}, first
    for key, label, high, *auto in entries:
        out[key] = Colour(key, label, low, high, bool(auto and auto[0]))
        low = high + 1
    return out


# Derby colour (Ch3): 0..5 = off
DERBY_COLOURS = _colours(6, (
    ("red", "Red", 20), ("green", "Green", 35), ("blue", "Blue", 50), ("white", "White", 65),
    ("red_green", "Red + Green", 80), ("red_blue", "Red + Blue", 95), ("red_white", "Red + White", 110),
    ("green_blue", "Green + Blue", 125), ("green_white", "Green + White", 140), ("blue_white", "Blue + White", 155),
    ("rgb", "Red + Green + Blue", 170), ("rgw", "Red + Green + White", 185), ("gbw", "Green + Blue + White", 200),
    ("rgbw", "RGBW (All)", 215),
    ("auto4", "Auto Color (4)", 230, True), ("auto7", "Auto Color (7)", 255, True),
))

# Laser mode (Ch7): 0..9 = off. The strobe variants (130..255) are not selectable: their speed is not controllable.
LASER_COLOURS = _colours(10, (("red", "Red", 49), ("green", "Green", 89), ("red_green", "Red + Green", 129)))
_LASER_STROBE_STEPS = ((169, "Red + Strobe Green"), (209, "Green + Strobe Red"), (255, "Red + Green (Strobe)"))

# LED pattern (Ch6): 0..9 = blackout. Pattern n owns the values 10n .. 10n+9 (pattern 18: 180..255).
# Inside its range the value only changes the pattern speed, which we do not control -> one fixed offset.
PATTERN_COUNT = 18
PATTERN_SPEED_OFFSET = 5         # 0..9, position inside each pattern's 10-wide range


def pattern_dmx(n: int) -> int:
    return 10 * _clamp(int(n), 1, PATTERN_COUNT) + PATTERN_SPEED_OFFSET


def pattern_of(value: int) -> int:
    # LED pattern number (1..18) a Ch6 value selects; 0 = blackout."""
    return 0 if value <= 9 else min(PATTERN_COUNT, (value - 10) // 10 + 1)


# Derby motor (Ch5): 0 = stopped / neutral, 1..127 = fixed position, 128..255 = rotation speed
POSITION_NEUTRAL, POSITION_MIN, POSITION_MAX = 0, 1, 127

# Laser rotation (Ch9): direction only; the speed inside the range is not controlled.
LASER_CW_VALUE = 66              # clockwise, range 5..127
LASER_CCW_VALUE = 194            # counter-clockwise, range 134..255
LASER_ROTATION = {"stop": 0, "cw": LASER_CW_VALUE, "ccw": LASER_CCW_VALUE}


# =====================================================================================
# describe(): channel value -> readable text
# =====================================================================================
def _steps(*pairs):
    return tuple(pairs)


_SHOW = _steps((9, "Manual (Blackout/Ch.3 active)"), (44, "Derby + Laser + Strobe"), (79, "Derby + Strobe"),
               (114, "Derby + Laser"), (149, "Laser + Strobe"), (184, "Derby Effect"), (219, "Laser Effect"),
               (255, "Strobe Effect"))
_DERBY_COLOUR = _steps((5, "Off"), *((c.high, c.label) for c in DERBY_COLOURS.values()))
_LASER_MODE = _steps((9, "Laser Off"), *((c.high, c.label) for c in LASER_COLOURS.values()), *_LASER_STROBE_STEPS)


def _pick(table, value: int) -> str:
    for high, label in table:
        if value <= high:
            return label
    return table[-1][1]


def _speed(v):    return f"Speed: {int(v / 250 * 100)}%" if v <= 250 else "Sound Control"
def _strobe(v):   return "Strobe Off" if v <= 5 else f"Derby Strobe Rate: {int(v / 255 * 100)}%"
def _motor(v):    return ("Motor Stopped" if v == 0 else f"Manual Position: {v}" if v <= 127
                          else f"Rotation Speed: {int((v - 128) / 127 * 100)}%")
def _pattern(v):  return "Blackout" if v <= 9 else f"Pattern {pattern_of(v)}"
def _lstrobe(v):  return ("Laser Strobe Off" if v <= 9 else f"Laser Strobe Rate: {int(v / 254 * 100)}%" if v <= 254
                          else "Sound-Controlled Strobe")
def _rotation(v): return "Stopped" if v <= 4 or 127 < v <= 133 else "Rotation CW" if v <= 127 else "Rotation CCW"


_DESCRIBE = {
    1: lambda v: _pick(_SHOW, v), 2: _speed,
    3: lambda v: _pick(_DERBY_COLOUR, v), 4: _strobe, 5: _motor,
    6: _pattern,
    7: lambda v: _pick(_LASER_MODE, v), 8: _lstrobe, 9: _rotation,
}


def describe(channel: int, value) -> str:
    v = _clamp(int(float(value)), 0, 255)
    fn = _DESCRIBE.get(channel)
    return f"{v} | {fn(v)}" if fn else str(v)


# =====================================================================================
# 3. Device states
# =====================================================================================
@dataclass
class Led:
    on: bool = False
    pattern: int = 1 # 1..PATTERN_COUNT; kept while the LED is off

    def set(self, attr: str, value) -> None:
        if attr == "on":
            self.on = bool(value)
        elif attr == "pattern":
            self.pattern = _clamp(int(value), 1, PATTERN_COUNT)
        else:
            raise KeyError(attr)

    def dmx(self) -> dict:
        return {CH_LED_PATTERN: pattern_dmx(self.pattern) if self.on else 0}


@dataclass
class Derby:
    on: bool = False                     # off = colour channel 0; the motor is independent of it
    colour: str = "red"
    position: int = POSITION_NEUTRAL     # 0 = neutral, else POSITION_MIN..POSITION_MAX

    def set(self, attr: str, value) -> None:
        if attr == "on":
            self.on = bool(value)
        elif attr == "colour":
            if value not in DERBY_COLOURS:
                raise ValueError(f"unknown Derby colour: {value!r}")
            self.colour = value
        elif attr == "position":
            value = int(value)
            self.position = POSITION_NEUTRAL if value <= 0 else _clamp(value, POSITION_MIN, POSITION_MAX)
        else:
            raise KeyError(attr)

    def dmx(self) -> dict:
        return {CH_DERBY_COLOUR: DERBY_COLOURS[self.colour].dmx if self.on else 0,
                CH_DERBY_MOTOR: self.position}


@dataclass
class Laser:
    on: bool = False
    colour: str = "green"
    rotation: str = "stop"               # stop | cw | ccw

    def set(self, attr: str, value) -> None:
        if attr == "on":
            self.on = bool(value)
        elif attr == "colour":
            if value not in LASER_COLOURS:
                raise ValueError(f"unknown Laser colour: {value!r}")
            self.colour = value
        elif attr == "rotation":
            if value not in LASER_ROTATION:
                raise ValueError(f"unknown Laser rotation: {value!r}")
            self.rotation = value
        else:
            raise KeyError(attr)

    def dmx(self) -> dict:
        return {CH_LASER_COLOUR: LASER_COLOURS[self.colour].dmx if self.on else 0,
                CH_LASER_ROTATION: LASER_ROTATION[self.rotation] if self.on else 0}


@dataclass
class Fixture:
    # all three devices. set("derby.colour", "blue") / get(...) address one property, dmx() is the whole frame
    led: Led = field(default_factory=Led)
    derby: Derby = field(default_factory=Derby)
    laser: Laser = field(default_factory=Laser)

    def set(self, key: str, value) -> None:
        device, attr = key.split(".")
        getattr(self, device).set(attr, value)

    def get(self, key: str):
        device, attr = key.split(".")
        return getattr(getattr(self, device), attr)

    def dmx(self) -> dict:
        frame = {ch: 0 for ch in CHANNELS} # channels nobody drives (Ch1, 2, 4, 8) stay 0
        for device in (self.led, self.derby, self.laser):
            frame.update(device.dmx())
        return frame
