"""
Engine
======

The "logic": a mapping grid turns analysis Features into device states, devices turn into a DMX frame.

    Mapping grid   rows = SOURCE (sub | bass | mids | highs | beat)      columns = what it does to the fixture

        flash columns  (3 states per cell: -  /  ON  /  OFF)
            LED  Derby  Laser      ON  = the device lights up for FLASH_HOLD_MS on every hit of that source
                                   OFF = the device goes dark for FLASH_HOLD_MS on every hit (it is on in between)
        change columns (2 states per cell: -  /  cycle)
            LED pattern, Derby colour, Laser colour, Derby swing, Laser spin
                                   every hit steps to the next pattern / colour / position / direction

    The beat row fires on every `beat_every` beats instead of on a band hit.

    Under the hood every non-empty cell is one Rule (SOURCE + EVENT -> TARGET + ACTION, with hold and minimum gap);
    build_rules() generates them from the grid, so the grid is the only thing the UI edits. Rule, Target and
    LightEngine._fire know nothing about the grid.

    Extra: "Bouncy Bass" -- every bass hit throws the derby motor between BOUNCE_POS_MIN and BOUNCE_POS_MAX. When
    the kicks come faster than the motor can travel (config.BOUNCE_TRAVEL_S) only every 2nd (3rd, ...) kick bounces.

A Look is a grid plus the start state of the devices. The engine knows nothing about audio libraries or the GUI:
process(features) -> {channel: value} for channels 1..9 (channel 1 is always 0).
"""

import itertools
import math
from dataclasses import dataclass, field, replace

from ..fixture import CHANNELS, DERBY_COLOURS, LASER_COLOURS, PATTERN_COUNT, POSITION_MAX, Fixture
from . import config

# --------- Vocabulary of the mapping grid
SOURCES = [("sub", "Sub"), ("bass", "Bass"), ("mids", "Mids"), ("highs", "Highs"), ("beat", "Beat")]
BEAT_OPTIONS = {"1 beat": 1, "2 beats": 2, "1 bar": 4, "2 bars": 8, "4 bars": 16, "8 bars": 32}


@dataclass(frozen=True)
class Column:
    key: str
    label: str
    kind: str                # "switch": - / on / off      "cycle": - / cycle
    target: str              # key into TARGETS
    group: str               # header over the column


COLUMNS = (
    Column("led", "LED", "switch", "led.on", "flash"),
    Column("derby", "Derby", "switch", "derby.on", "flash"),
    Column("laser", "Laser", "switch", "laser.on", "flash"),
    Column("led.pattern", "LED\npattern", "cycle", "led.pattern", "change"),
    Column("derby.colour", "Derby\ncolour", "cycle", "derby.colour", "change"),
    Column("laser.colour", "Laser\ncolour", "cycle", "laser.colour", "change"),
    Column("derby.position", "Derby\nswing", "cycle", "derby.position", "change"),
    Column("laser.rotation", "Laser\nspin", "cycle", "laser.rotation", "change"),
)
COLUMN_BY_KEY = {c.key: c for c in COLUMNS}
STATES = {"switch": ("", "on", "off"), "cycle": ("", "cycle")}


def cell_key(source: str, column: str) -> str:
    return f"{source}.{column}"


_uid = itertools.count(1)


@dataclass
class Rule:
    source: str = "bass"
    event: str = "hit"                  # hit | above | below | beat
    target: str = "led.on"
    action: str = "set"                 # set | toggle | cycle
    values: tuple = ()                  # set: (v,)  toggle: (a, b)  cycle: (v1, v2, ...) or () = all
    threshold: float = 0.6              # above / below
    every_beats: int = 4                # source "beat"
    hold_ms: int = 0
    cooldown_ms: int = 0
    uid: int = field(default_factory=lambda: next(_uid))


# --------- Targets: what a rule can write, with the options it can choose from
@dataclass(frozen=True)
class Target:
    key: str
    label: str
    options: tuple           # every value a "set" / "toggle" can pick
    cycle: tuple             # what "cycle" runs through when the rule names no values
    names: dict              # value -> label


_ON = {True: "On", False: "Off"}
_POSITIONS = (0,) + tuple(range(10, 121, 10)) + (POSITION_MAX,)
_DERBY_CYCLE = tuple(k for k, c in DERBY_COLOURS.items() if not c.auto)

TARGETS = {t.key: t for t in (
    Target("led.on", "LED on/off", (True, False), (True, False), _ON),
    Target("led.pattern", "LED pattern / colour", tuple(range(1, PATTERN_COUNT + 1)),
           tuple(range(1, PATTERN_COUNT + 1)), {n: f"Pattern {n}" for n in range(1, PATTERN_COUNT + 1)}),
    Target("derby.on", "Derby on/off", (True, False), (True, False), _ON),
    Target("derby.colour", "Derby colour", tuple(DERBY_COLOURS), _DERBY_CYCLE,
           {k: c.label for k, c in DERBY_COLOURS.items()}),
    Target("derby.position", "Derby position", _POSITIONS, (10, 40, 70, 100, POSITION_MAX),
           {p: ("Neutral (0)" if p == 0 else f"Position {p}") for p in _POSITIONS}),
    Target("laser.on", "Laser on/off", (True, False), (True, False), _ON),
    Target("laser.colour", "Laser colour", tuple(LASER_COLOURS), tuple(LASER_COLOURS),
           {k: c.label for k, c in LASER_COLOURS.items()}),
    Target("laser.rotation", "Laser rotation", ("cw", "ccw", "stop"), ("cw", "ccw"),
           {"cw": "Clockwise", "ccw": "Counter-clockwise", "stop": "Stop"}),
)}


def value_label(target: str, value) -> str:
    return TARGETS[target].names.get(value, str(value))


# --------- Looks
@dataclass
class Look:
    """A mapping grid plus the start state of the devices. The UI edits grid / beat_every directly."""
    name: str
    description: str = ""
    grid: dict = field(default_factory=dict)          # "bass.led" -> "on" | "off" | "cycle"  (missing = nothing)
    beat_every: int = 4                               # the beat row fires every this many beats
    initial: dict = field(default_factory=dict)       # target key -> value at the start of every song

    def copy(self, **changes) -> "Look":
        return replace(self, grid=dict(self.grid), initial=dict(self.initial), **changes)


def _grid(*cells) -> dict:
    """_grid("bass.led=on", "beat.led.pattern=cycle") -> {"bass.led": "on", "beat.led.pattern": "cycle"}"""
    return dict(cell.split("=") for cell in cells)


# One-click starting points, shown as buttons in Music Mode. Everything can be changed afterwards.
QUICK_LOOKS = [
    Look("Kick Flash", "LED flashes on every bass hit, its pattern changes every 2 bars; the derby swings on the sub.",
         grid=_grid("bass.led=on", "beat.led.pattern=cycle", "beat.derby.colour=cycle",
                    "sub.derby.position=cycle", "beat.laser.rotation=cycle"),
         beat_every=8,
         initial={"led.pattern": 3, "derby.on": True, "derby.colour": "blue", "laser.on": True,
                  "laser.colour": "green", "laser.rotation": "cw"}),
    Look("Colour Pulse", "Every bass hit flashes the LED and steps the derby to the next colour; highs flip the laser colour.",
         grid=_grid("bass.led=on", "bass.led.pattern=cycle", "bass.derby.colour=cycle", "highs.laser.colour=cycle"),
         initial={"derby.on": True, "derby.colour": "red", "laser.on": True, "laser.rotation": "cw"}),
    Look("Sub Swing", "Sub hits swing the derby between two positions, bass flashes it, mids flash the laser.",
         grid=_grid("sub.derby.position=cycle", "bass.derby=on", "mids.laser=on", "beat.led.pattern=cycle"),
         beat_every=8,
         initial={"led.on": True, "led.pattern": 5, "derby.colour": "white", "laser.colour": "red_green",
                  "laser.rotation": "ccw"}),
    Look("Ambient", "No reaction to single hits: steady light, slow changes every 4 bars.",
         grid=_grid("beat.led.pattern=cycle", "beat.derby.colour=cycle", "beat.laser.rotation=cycle"),
         beat_every=16,
         initial={"led.on": True, "led.pattern": 3, "derby.on": True, "derby.colour": "blue",
                  "laser.on": True, "laser.colour": "green", "laser.rotation": "cw"}),
]
BY_NAME = {look.name: look for look in QUICK_LOOKS}
DEFAULT = QUICK_LOOKS[0]
CUSTOM = "Custom"

# What a "cycle" cell does: (action, values, minimum gap in ms). () = through everything the target offers.
_CYCLES = {
    "led.pattern": ("cycle", (), config.CYCLE_GAP_MS),
    "derby.colour": ("cycle", tuple(config.DERBY_CYCLE_COLOURS), config.CYCLE_GAP_MS),
    "laser.colour": ("cycle", tuple(config.LASER_CYCLE_COLOURS), config.CYCLE_GAP_MS),
    "derby.position": ("toggle", tuple(config.SWING_POSITIONS), config.SWING_GAP_MS),
    "laser.rotation": ("toggle", ("cw", "ccw"), config.CYCLE_GAP_MS),
}


def build_rules(look: Look) -> list:
    """Every non-empty grid cell becomes one Rule. The uid depends only on the cell, so the cycle / toggle
    counters survive an edit of some other cell."""
    rules = []
    for si, (source, _) in enumerate(SOURCES):
        for ci, column in enumerate(COLUMNS):
            state = look.grid.get(cell_key(source, column.key), "")
            if state not in STATES[column.kind] or not state:
                continue
            common = dict(source=source, event="beat" if source == "beat" else "hit",
                          every_beats=look.beat_every, uid=1000 + si * 50 + ci)
            if column.kind == "switch":
                rules.append(Rule(target=column.target, action="set", values=(state == "on",),
                                  hold_ms=config.FLASH_HOLD_MS, **common))
            else:
                action, values, gap = _CYCLES[column.key]
                rules.append(Rule(target=column.target, action=action, values=values,
                                  cooldown_ms=0 if source == "beat" else gap, **common))
    return rules


def baseline(look: Look) -> dict:
    """Start state: the look's own, except that a device with flash cells rests in the opposite state
    (ON cells -> dark in between, OFF cells -> lit in between)."""
    out = dict(look.initial)
    for device in ("led", "derby", "laser"):
        states = {look.grid.get(cell_key(source, device)) for source, _ in SOURCES}
        if "on" in states:
            out[f"{device}.on"] = False
        elif "off" in states:
            out[f"{device}.on"] = True
    return out


_OFF_FRAME = {ch: 0 for ch in CHANNELS}


@dataclass
class _RuleState:
    last: float = -1e9          # engine time of the last firing
    n: int = 0                  # how often a toggle / cycle has advanced
    above: bool = None          # level state for above / below


class LightEngine:
    """Runs the rules and renders the fixture. process() is called from the audio thread."""

    def __init__(self, look: Look):
        self.look = look
        self.force_blackout = False      # the Blackout button: everything dark, whatever the rules say
        self.bouncy = False              # "Bouncy Bass" checkbox
        self._rules = build_rules(look)
        self._bounce_n = 0
        self._bounce_hi = False
        self._bounce_t = -1e9
        self.fixture = Fixture()
        self._t = 0.0
        self._active = False
        self._rs = {}                    # rule uid -> _RuleState
        self._pending = {}               # (target, rule uid) -> [due time, value before the rule fired]
        self._owner = {}                 # target -> uid of the rule that wrote it last
        self._reset_state()

    def set_look(self, look: Look) -> None:
        self.look = look
        self._rules = build_rules(look)
        self._reset_state()

    def rebuild(self) -> None:
        """The grid was edited: regenerate the rules (cycle counters stay) and re-rest the on/off devices."""
        self._rules = build_rules(self.look)
        for key, value in baseline(self.look).items():
            if key.endswith(".on"):
                self._pending = {k: v for k, v in self._pending.items() if k[0] != key}
                self._apply(key, value)

    # ---- device state
    def _reset_state(self) -> None:
        self.fixture = Fixture()
        self._rs.clear()
        self._pending.clear()
        self._owner.clear()
        self._bounce_n, self._bounce_hi, self._bounce_t = 0, False, -1e9
        for key, value in baseline(self.look).items():
            if key in TARGETS:
                self._apply(key, value)

    def _apply(self, key: str, value) -> None:
        self.fixture.set(key, value)

    def _get(self, key: str):
        return self.fixture.get(key)

    # ---- main entry
    def process(self, f) -> dict:
        """f: analysis.Features. Returns {channel: value} for channels 1..9 (channel 1 is always 0)."""
        self._t += f.dt if f.dt > 0 else 0.02
        if not f.active:
            if self._active:             # music stopped: the next song starts from the look's start state
                self._active = False
                self._reset_state()
            return dict(_OFF_FRAME)
        self._active = True

        self._run_reverts()
        for rule in self._rules:                     # the GUI swaps the whole list, never edits it in place
            state = self._rs.setdefault(rule.uid, _RuleState())
            if not self._fires(rule, state, f):
                continue
            if rule.cooldown_ms and (self._t - state.last) * 1000.0 < rule.cooldown_ms:
                continue
            state.last = self._t
            self._fire(rule, state)
        if self.bouncy and f.hit.get("bass"):
            self._bounce(f)
        return dict(_OFF_FRAME) if self.force_blackout else self.fixture.dmx()

    # ---- Bouncy Bass
    def _bounce(self, f) -> None:
        """Throw the derby between its two end positions on the kick -- but only as often as the motor can follow:
        if the kicks come faster than config.BOUNCE_TRAVEL_S, every 2nd (3rd, ...) kick bounces."""
        interval = f.kick_interval if f.kick_interval > 0 else 60.0 / max(f.bpm, 1.0)
        stride = max(1, math.ceil(config.BOUNCE_TRAVEL_S / max(interval, 0.05)))
        self._bounce_n += 1
        if self._bounce_n % stride:
            return
        if self._t - self._bounce_t < 0.8 * config.BOUNCE_TRAVEL_S:          # jitter guard
            return
        self._bounce_t = self._t
        self._bounce_hi = not self._bounce_hi
        self._apply("derby.position", config.BOUNCE_POS_MAX if self._bounce_hi else config.BOUNCE_POS_MIN)
        self._owner["derby.position"] = -1

    # ---- rules
    def _fires(self, rule: Rule, state: _RuleState, f) -> bool:
        if rule.source == "beat":
            return f.beat_tick and f.beat_index % max(1, rule.every_beats) == 0
        if rule.event == "hit":
            return bool(f.hit.get(rule.source))
        if rule.event not in ("above", "below"):
            return False
        level = f.level.get(rule.source, 0.0)
        if state.above is None:
            state.above = False
        if not state.above and level >= rule.threshold:
            state.above = True
            return rule.event == "above"
        if state.above and level < rule.threshold - config.LEVEL_HYSTERESIS:
            state.above = False
            return rule.event == "below"
        return False

    def _fire(self, rule: Rule, state: _RuleState) -> None:
        target = TARGETS.get(rule.target)
        if target is None:
            return
        if rule.action == "toggle":
            seq = rule.values if len(rule.values) >= 2 else target.options[:2]
            value = seq[state.n % 2]
            state.n += 1
        elif rule.action == "cycle":
            seq = rule.values or target.cycle
            value = seq[state.n % len(seq)]
            state.n += 1
        else:
            value = rule.values[0] if rule.values else target.options[0]

        if rule.hold_ms > 0:
            key = (rule.target, rule.uid)
            due = self._t + rule.hold_ms / 1000.0
            pending = self._pending.get(key)
            if pending:
                pending[0] = due                     # firing again while held: just extend, keep the old value
            else:
                self._pending[key] = [due, self._get(rule.target)]
        self._apply(rule.target, value)
        self._owner[rule.target] = rule.uid

    def _run_reverts(self) -> None:
        for key, (due, before) in list(self._pending.items()):
            if self._t < due:
                continue
            del self._pending[key]
            target, uid = key
            if self._owner.get(target) == uid:       # only if nobody wrote the target in between
                self._apply(target, before)
                self._owner.pop(target, None)

    # ---- state for the UI
    def preview(self) -> dict:
        """What the fixture is doing right now (None = dark)."""
        dark = not self._active or self.force_blackout
        f = self.fixture
        return dict(
            led=None if dark or not f.led.on else f.led.pattern,
            derby=None if dark or not f.derby.on else f.derby.colour,
            derby_position=f.derby.position,
            laser=None if dark or not f.laser.on else f.laser.colour,
            laser_rotation=f.laser.rotation,
            blackout=self.force_blackout)
