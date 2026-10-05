"""
Music Mode programs
===================

Ready-made looks for the LightEngine (see lightengine.py): each Program sets one
behaviour per DMX channel plus a few parameters. Pick one in Music Mode and it is
loaded into the channel table, where every row can still be changed -- the
program then switches to "Custom".

Channel behaviours are listed in lightengine.BEHAVIOURS. Channel 2 (speed) is
not part of a program: it is always BPM x K. The K values below only set the
starting point of the sliders:
    speed = K * BPM / 256   ->  K = 1 at 128 BPM is half of the fixture's range.

Add your own by appending to PRESETS; the order here is the order in the menu.
"""

from .lightengine import Program

CUSTOM = "Custom"

PRESETS = [
    Program(
        name="Techno Club",
        description="Dark and hard. Cold colours that flash on the kick, strobes only on build-ups and "
                    "drops, a green laser and slow rotations. Colours change every 4 bars.",
        behaviours={1: "manual", 3: "kick", 4: "build_drop", 5: "rotate", 6: "cycle",
                    7: "section", 8: "off", 9: "alternate"},
        palette="cold", cycle_beats=16, pattern_pool="1-6",
        k_show=1.0, k_derby=0.8, k_laser=0.6, break_scale=0.4,
    ),
    Program(
        name="Hard Techno / Warehouse",
        description="Aggressive. Red and white, strobes on every kick, laser strobe as well, faster "
                    "rotation. Colours change every 2 bars.",
        behaviours={1: "manual", 3: "kick", 4: "kick", 5: "rotate", 6: "cycle",
                    7: "section", 8: "kick", 9: "alternate"},
        palette="redwhite", cycle_beats=8, pattern_pool="7-12",
        k_show=1.2, k_derby=1.2, k_laser=1.0, break_scale=0.3,
    ),
    Program(
        name="Peak Time Rave",
        description="Everything on. All colours stepping every bar, strobe ramp into the drop on both "
                    "the derby and the laser, all laser patterns.",
        behaviours={1: "manual", 3: "cycle", 4: "build_drop", 5: "rotate", 6: "cycle",
                    7: "cycle", 8: "build_drop", 9: "alternate"},
        palette="rave", cycle_beats=4, pattern_pool="all",
        k_show=1.2, k_derby=1.2, k_laser=1.0, break_scale=0.5,
    ),
    Program(
        name="Build & Drop Show",
        description="Follows the song structure: calm in the break, flickering and strobing in the "
                    "build-up, white flash and full look on the drop.",
        behaviours={1: "manual", 3: "section", 4: "build_drop", 5: "rotate", 6: "cycle",
                    7: "section", 8: "build_drop", 9: "alternate"},
        palette="cold", cycle_beats=16, pattern_pool="13-18",
        k_show=1.0, k_derby=1.0, k_laser=0.8, break_scale=0.3,
    ),
    Program(
        name="Hypnotic / Minimal",
        description="Slow and dark. Blue only, no strobes, a green laser with one steady pattern "
                    "turning slowly. Colour changes every 8 bars.",
        behaviours={1: "manual", 3: "cycle", 4: "off", 5: "rotate", 6: "fixed",
                    7: "green", 8: "off", 9: "cw"},
        palette="violet", cycle_beats=32, pattern_pool="1-6",
        k_show=0.6, k_derby=0.5, k_laser=0.4, break_scale=0.6,
    ),
    Program(
        name="House / Groove",
        description="Warm and relaxed. Colours step every 2 bars, a short strobe burst on the drop only, "
                    "red + green laser cycling its colours.",
        behaviours={1: "manual", 3: "cycle", 4: "drop", 5: "rotate", 6: "cycle",
                    7: "cycle", 8: "off", 9: "alternate"},
        palette="warm", cycle_beats=8, pattern_pool="7-12",
        k_show=0.9, k_derby=0.9, k_laser=0.8, break_scale=0.5,
    ),
    Program(
        name="Fixture Shows (BPM-locked)",
        description="Lets the fixture run its own pre-programmed shows. The show type follows the song "
                    "section and the show speed is locked to the tempo. Channels 3-9 are ignored by "
                    "the fixture in this mode.",
        behaviours={1: "by_section", 3: "hand", 4: "hand", 5: "hand", 6: "hand",
                    7: "hand", 8: "hand", 9: "hand"},
        palette="cold", cycle_beats=16, pattern_pool="1-6",
        k_show=1.0, k_derby=1.0, k_laser=1.0, break_scale=0.5,
    ),
]

BY_NAME = {p.name: p for p in PRESETS}
DEFAULT = PRESETS[0]
