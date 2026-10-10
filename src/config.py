from pathlib import Path

# --- DMX / Controller ---
UNIVERSE_SIZE = 513  # channel 0 unused, DMX should start at 1
# A frame is sent over and over. The fixture only reads channels 1..9, so a short frame is enough:
# 33 slots take ~1.5 ms on the wire instead of ~23 ms for all 513 -> roughly 60 frames/s instead of ~19 and
# ~20 ms less latency. If your fixture ever flickers or ignores it, go back to SEND_SLOTS = 513, SEND_INTERVAL_S = 0.03.
SEND_SLOTS = 33
SEND_INTERVAL_S = 0.012

# Music Mode tunables (audio, analysis, tempo, UI, now playing) live in src/musicmode/config.py

# --- Theme ---
_BASE_SCHEMES = {
    "dark_purple": dict(BG="#1e1e24", BG_LIGHT="#2a2a33", FG="#e0dff0", ACCENT="#9b59d9", ACCENT_DARK="#6c3fa0", STATUS_TEXT="#c9a6f5"),
    "dark_blue": dict(BG="#1e1e24", BG_LIGHT="#2a2a33", FG="#e0dff0", ACCENT="#4a90d9", ACCENT_DARK="#2f5f9e", STATUS_TEXT="#a6c9f5"),
    "black_white": dict(BG="#000000", BG_LIGHT="#1a1a1a", FG="#ffffff", ACCENT="#ffffff", ACCENT_DARK="#808080", STATUS_TEXT="#d9d9d9"),
}


def _mix(a: str, b: str, t: float) -> str:
    # blends two #rrggbb colours (t = 0 -> a, t = 1 -> b)
    a, b = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join("%02x" % round(int(a[i:i + 2], 16) + (int(b[i:i + 2], 16) - int(a[i:i + 2], 16)) * t)
                         for i in (0, 2, 4))


# the new layout also uses: ACCENT2 (second spectrum colour, titles, hover), MUTED (secondary text), LINE (borders)
_SCHEMES = {
    name: dict(s, ACCENT2=s["STATUS_TEXT"], MUTED=_mix(s["BG_LIGHT"], s["FG"], 0.55), LINE=s["ACCENT_DARK"])
    for name, s in _BASE_SCHEMES.items()
}
COLOR_SCHEME = "dark_purple"

ACTIVE_SCHEME = _SCHEMES[COLOR_SCHEME]
COLOR_BG = ACTIVE_SCHEME["BG"]
COLOR_BG_LIGHT = ACTIVE_SCHEME["BG_LIGHT"]
COLOR_FG = ACTIVE_SCHEME["FG"]
COLOR = ACTIVE_SCHEME["ACCENT"]
COLOR_DARK = ACTIVE_SCHEME["ACCENT_DARK"]
COLOR_STATUS_TEXT = ACTIVE_SCHEME["STATUS_TEXT"]

# Channel names, device groups and value texts live in fixture.py

# main.py lives at the project root, config.py in <root>/src -- hence two levels up
PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"
