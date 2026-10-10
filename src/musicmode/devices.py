"""
Devices
=======

Static foundation of the Light Engine: what the three outputs of the Razor Derby can do
and which DMX value each state maps to. No audio, no rules, no timing in here.

    LED    Ch6          on/off, pattern 1..18 (every pattern is a colour programme)
    Derby  Ch3 + Ch5    on/off, colour, position (0 = neutral, motor not in use)
    Laser  Ch7 + Ch9    on/off, colour, rotation direction (stop / cw / ccw)

Never driven: Ch1 (show select, stays 0 = manual mode), Ch2 (speed / sound),
Ch4 and Ch8 (strobe). They are part of the output frame as a fixed 0.

Every state maps to one fixed number in the middle of the range the manual gives for it.
Speeds *inside* a range (pattern speed, laser rotation speed) are not controlled:
each has one tunable constant in config.py.
"""

from dataclasses import dataclass, field

from . import config

# --------- Channels (9-channel DMX mode)
CH_MODE = 1             # show select: always 0
CH_SHOW_SPEED = 2       # not used
CH_DERBY_COLOUR = 3
CH_DERBY_STROBE = 4     # not used
CH_DERBY_MOTOR = 5
CH_LED_PATTERN = 6
CH_LASER_COLOUR = 7
CH_LASER_STROBE = 8     # not used
CH_LASER_ROTATION = 9
CHANNELS = tuple(range(1, 10))
UNUSED_CHANNELS = (CH_MODE, CH_SHOW_SPEED, CH_DERBY_STROBE, CH_LASER_STROBE)


def _clamp(x: int, lo: int, hi: int) -> int:
    return lo if x < lo else hi if x > hi else x


@dataclass(frozen=True)
class Colour:
    key: str
    label: str
    dmx: int
    auto: bool = False       # fixture changes colour by itself (speed unknown)


def _table(*colours: Colour) -> dict:
    return {c.key: c for c in colours}


# --------- Derby colours (Ch3). 0 = off. dmx = middle of the manual's range for that colour.
DERBY_COLOURS = _table(
    Colour("red",         "Red",            13),     #   6..20
    Colour("green",       "Green",          28),     #  21..35
    Colour("blue",        "Blue",           43),     #  36..50
    Colour("white",       "White",          58),     #  51..65
    Colour("red_green",   "Red + Green",    73),     #  66..80
    Colour("red_blue",    "Red + Blue",     88),     #  81..95
    Colour("red_white",   "Red + White",   103),     #  96..110
    Colour("green_blue",  "Green + Blue",  118),     # 111..125
    Colour("green_white", "Green + White", 133),     # 126..140
    Colour("blue_white",  "Blue + White",  148),     # 141..155
    Colour("rgb",         "R + G + B",     163),     # 156..170
    Colour("rgw",         "R + G + W",     178),     # 171..185
    Colour("gbw",         "G + B + W",     193),     # 186..200
    Colour("rgbw",        "RGBW (all)",    208),     # 201..215
    Colour("auto4",       "Auto (4 colours)", 223, auto=True),   # 216..230
    Colour("auto7",       "Auto (7 colours)", 243, auto=True),   # 231..255
)

# --------- Laser colours (Ch7). 0 = off.
# The strobe variants (130..255) are left out on purpose: their strobe speed is not controllable.
LASER_COLOURS = _table(
    Colour("red",       "Red",           30),        #  10..49
    Colour("green",     "Green",         70),        #  50..89
    Colour("red_green", "Red + Green",  110),        #  90..129
)

# --------- LED patterns (Ch6). 0..9 = blackout. Pattern n owns the values 10n .. 10n+9
# (pattern 18 owns 180..255). Inside its range the value only changes the pattern speed,
# which we do not control -> one fixed offset for all patterns (0..9).
PATTERN_COUNT = 18
PATTERN_SPEED_OFFSET = config.PATTERN_SPEED_OFFSET


def pattern_dmx(n: int) -> int:
    return 10 * _clamp(int(n), 1, PATTERN_COUNT) + PATTERN_SPEED_OFFSET


# --------- Derby position (Ch5). 0 = neutral (motor not in use), 1..127 = fixed position.
POSITION_NEUTRAL = 0
POSITION_MIN = 1
POSITION_MAX = 127

# --------- Laser rotation (Ch9). Direction only; the speed inside the range is not controlled.
LASER_ROTATION = {
    "stop": 0,        #   0..4   (also 128..133)
    "cw":   config.LASER_CW_VALUE,       #   5..127
    "ccw":  config.LASER_CCW_VALUE,      # 134..255
}


# --------- The three devices. Each holds its state and renders it to {channel: value}.
@dataclass
class Led:
    on: bool = False
    pattern: int = 1                     # 1..PATTERN_COUNT; kept while the LED is off

    def set_pattern(self, n: int) -> None:
        self.pattern = _clamp(int(n), 1, PATTERN_COUNT)

    def dmx(self) -> dict:
        return {CH_LED_PATTERN: pattern_dmx(self.pattern) if self.on else 0}


@dataclass
class Derby:
    on: bool = False                     # off = colour channel 0; the motor is independent of it
    colour: str = "red"
    position: int = POSITION_NEUTRAL     # 0 = neutral, else POSITION_MIN..POSITION_MAX

    def set_colour(self, key: str) -> None:
        if key not in DERBY_COLOURS:
            raise ValueError(f"unknown Derby colour: {key!r}")
        self.colour = key

    def set_position(self, pos: int) -> None:
        pos = int(pos)
        self.position = POSITION_NEUTRAL if pos <= 0 else _clamp(pos, POSITION_MIN, POSITION_MAX)

    def dmx(self) -> dict:
        return {CH_DERBY_COLOUR: DERBY_COLOURS[self.colour].dmx if self.on else 0,
                CH_DERBY_MOTOR: self.position}


@dataclass
class Laser:
    on: bool = False
    colour: str = "green"
    rotation: str = "stop"               # stop | cw | ccw

    def set_colour(self, key: str) -> None:
        if key not in LASER_COLOURS:
            raise ValueError(f"unknown Laser colour: {key!r}")
        self.colour = key

    def set_rotation(self, direction: str) -> None:
        if direction not in LASER_ROTATION:
            raise ValueError(f"unknown Laser rotation: {direction!r}")
        self.rotation = direction

    def dmx(self) -> dict:
        return {CH_LASER_COLOUR: LASER_COLOURS[self.colour].dmx if self.on else 0,
                CH_LASER_ROTATION: LASER_ROTATION[self.rotation] if self.on else 0}


@dataclass
class Fixture:
    """All three devices; dmx() is the complete 9-channel frame."""
    led: Led = field(default_factory=Led)
    derby: Derby = field(default_factory=Derby)
    laser: Laser = field(default_factory=Laser)

    def dmx(self) -> dict:
        frame = {ch: 0 for ch in CHANNELS}           # unused channels (incl. Ch1) stay 0
        for device in (self.led, self.derby, self.laser):
            frame.update(device.dmx())
        return frame

