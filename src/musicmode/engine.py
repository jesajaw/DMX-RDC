"""
Engine
======

The "logic": rules turn analysis Features into device states, devices turn into a DMX frame.

    Rule = SOURCE + EVENT  ->  TARGET + ACTION

    source   sub | bass | mids | highs        (a band)     or   beat
    event    hit                              the band had an onset
             above / below                    the band's level crosses `threshold` (0..1, with hysteresis)
             (beat)                           every `every_beats` beats
    target   led.on  led.pattern  derby.on  derby.colour  derby.position  laser.on  laser.colour  laser.rotation
    action   set     -> values[0]
             toggle  -> values[0], values[1], values[0], ...     (e.g. position 0 / 127)
             cycle   -> through values (or all of the target's cycle options when `values` is empty)
    extras   hold_ms      after this long the target goes back to what it was (a blink); 0 = stays
             cooldown_ms  minimum time between two firings of this rule (e.g. for the slow derby position)

Every rule works on its own and every target can be driven by any number of rules; when two rules
write the same target the later write wins. "BASS -> ON" and "BASS -> OFF" are simply two rules.

A Look is a list of rules plus the start state of the devices. The engine knows nothing about audio
libraries or the GUI: process(features) -> {channel: value} for channels 1..9 (channel 1 is always 0).
"""

import itertools
from dataclasses import dataclass, field, replace

from ..fixture import CHANNELS, DERBY_COLOURS, LASER_COLOURS, PATTERN_COUNT, POSITION_MAX, Fixture
from . import config

# --------- Rule vocabulary (key, label) -- what the UI offers
SOURCES = [("sub", "Sub"), ("bass", "Bass"), ("mids", "Mids"), ("highs", "Highs"), ("beat", "Beat")]
BAND_EVENTS = [("hit", "hit"), ("above", "rises above"), ("below", "falls below")]
ACTIONS = [("set", "set"), ("toggle", "toggle A \u21c4 B"), ("cycle", "cycle")]
BEAT_OPTIONS = {"1 beat": 1, "2 beats": 2, "1 bar": 4, "2 bars": 8, "4 bars": 16, "8 bars": 32}
HOLD_OPTIONS_MS = (0, 60, 100, 150, 250, 500, 1000)
GAP_OPTIONS_MS = (0, 100, 250, 500, 1000)
THRESHOLD_OPTIONS = (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)

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
    """Rules plus the start state of the devices. The UI edits these fields directly."""
    name: str
    description: str = ""
    rules: list = field(default_factory=list)
    initial: dict = field(default_factory=dict)       # target key -> value at the start of every song

    def copy(self, **changes) -> "Look":
        return replace(self, rules=[replace(r) for r in self.rules], initial=dict(self.initial), **changes)


def _r(source, event, target, action="set", *values, thr=0.6, every=4, hold=0, gap=0) -> Rule:
    return Rule(source=source, event=event, target=target, action=action, values=tuple(values),
                threshold=thr, every_beats=every, hold_ms=hold, cooldown_ms=gap)


# One-click starting points, shown as chips in Music Mode. Everything can be changed afterwards.
QUICK_LOOKS = [
    Look("Kick Flash", "LED flashes on every bass hit, its pattern changes every bar; the derby swings on the sub.",
         rules=[
             _r("bass", "hit", "led.on", "set", True, hold=120),
             _r("beat", "beat", "led.pattern", "cycle", every=4),
             _r("beat", "beat", "derby.colour", "cycle", "blue", "white", "red_blue", every=4),
             _r("sub", "hit", "derby.position", "toggle", 10, 110, gap=300),
             _r("beat", "beat", "laser.rotation", "toggle", "cw", "ccw", every=16),
         ],
         initial={"led.pattern": 3, "derby.on": True, "derby.colour": "blue", "laser.on": True,
                  "laser.colour": "green", "laser.rotation": "cw"}),
    Look("Colour Pulse", "Every bass hit flashes the LED and steps the derby to the next colour; highs flip the laser colour.",
         rules=[
             _r("bass", "hit", "led.on", "set", True, hold=100),
             _r("bass", "hit", "led.pattern", "cycle"),
             _r("bass", "hit", "derby.colour", "cycle", "red", "blue", "green", "white"),
             _r("highs", "hit", "laser.colour", "toggle", "green", "red", gap=250),
         ],
         initial={"derby.on": True, "derby.colour": "red", "laser.on": True, "laser.rotation": "cw"}),
    Look("Sub Swing", "Sub hits swing the derby between two positions, bass flashes it, the laser follows the mids.",
         rules=[
             _r("sub", "hit", "derby.position", "toggle", 0, 127, gap=250),
             _r("bass", "hit", "derby.on", "set", True, hold=140),
             _r("mids", "above", "laser.on", "set", True, thr=0.6),
             _r("mids", "below", "laser.on", "set", False, thr=0.6),
             _r("beat", "beat", "led.pattern", "cycle", every=8),
         ],
         initial={"led.on": True, "led.pattern": 5, "derby.colour": "white", "laser.colour": "red_green",
                  "laser.rotation": "ccw"}),
    Look("Ambient", "No reaction to single hits: steady light, slow changes every 2 to 4 bars.",
         rules=[
             _r("beat", "beat", "led.pattern", "cycle", every=8),
             _r("beat", "beat", "derby.colour", "cycle", "blue", "green_blue", "red_blue", "white", every=16),
             _r("beat", "beat", "laser.rotation", "toggle", "cw", "ccw", every=32),
         ],
         initial={"led.on": True, "led.pattern": 3, "derby.on": True, "derby.colour": "blue",
                  "laser.on": True, "laser.colour": "green", "laser.rotation": "cw"}),
]
BY_NAME = {look.name: look for look in QUICK_LOOKS}
DEFAULT = QUICK_LOOKS[0]
CUSTOM = "Custom"

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
        self.fixture = Fixture()
        self._t = 0.0
        self._active = False
        self._rs = {}                    # rule uid -> _RuleState
        self._pending = {}               # (target, rule uid) -> [due time, value before the rule fired]
        self._owner = {}                 # target -> uid of the rule that wrote it last
        self._reset_state()

    def set_look(self, look: Look) -> None:
        self.look = look
        self._reset_state()

    # ---- device state
    def _reset_state(self) -> None:
        self.fixture = Fixture()
        self._rs.clear()
        self._pending.clear()
        self._owner.clear()
        for key, value in self.look.initial.items():
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
        for rule in tuple(self.look.rules):          # snapshot: the GUI may edit the list meanwhile
            state = self._rs.setdefault(rule.uid, _RuleState())
            if not self._fires(rule, state, f):
                continue
            if rule.cooldown_ms and (self._t - state.last) * 1000.0 < rule.cooldown_ms:
                continue
            state.last = self._t
            self._fire(rule, state)
        return dict(_OFF_FRAME) if self.force_blackout else self.fixture.dmx()

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
