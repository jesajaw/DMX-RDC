from pathlib import Path

UNIVERSE_SIZE = 513
SEND_SLOTS = 33
SEND_INTERVAL_S = 0.012

_BASE_SCHEMES = {
    "dark_purple": dict(BG="#1e1e24", BG_LIGHT="#2a2a33", FG="#e0dff0", ACCENT="#9b59d9", ACCENT_DARK="#6c3fa0", STATUS_TEXT="#c9a6f5"),
    "dark_blue": dict(BG="#1e1e24", BG_LIGHT="#2a2a33", FG="#e0dff0", ACCENT="#4a90d9", ACCENT_DARK="#2f5f9e", STATUS_TEXT="#a6c9f5"),
    "black_white": dict(BG="#000000", BG_LIGHT="#1a1a1a", FG="#ffffff", ACCENT="#ffffff", ACCENT_DARK="#808080", STATUS_TEXT="#d9d9d9"),
}


def _mix(a: str, b: str, t: float) -> str:
    a, b = a.lstrip("#"), b.lstrip("#")
    return "#" + "".join("%02x" % round(int(a[i:i + 2], 16) + (int(b[i:i + 2], 16) - int(a[i:i + 2], 16)) * t) for i in (0, 2, 4))

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

PRESETS_DIR = Path(__file__).resolve().parent.parent / "presets"


FONT = "Segoe UI"
MONO = "Consolas"