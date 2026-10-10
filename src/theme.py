from tkinter import ttk

from . import config

def _luminance(hex_colour: str) -> float:
    h = hex_colour.lstrip("#")
    r, g, b = (int(h[i:i + 2], 16) for i in (0, 2, 4))
    return (0.299 * r + 0.587 * g + 0.114 * b) / 255.0


def on_accent(colours: dict | None = None) -> str:
    """Text colour that stays readable on the accent colour."""
    colours = colours or config.ACTIVE_SCHEME
    return "#1e1e24" if _luminance(colours["ACCENT"]) > 0.6 else "#ffffff"


def lerp_colour(a: str, b: str, t: float) -> str:
    """Linear blend between two #rrggbb colours (t = 0 -> a, t = 1 -> b)."""
    a, b = a.lstrip("#"), b.lstrip("#")
    out = []
    for i in (0, 2, 4):
        va, vb = int(a[i:i + 2], 16), int(b[i:i + 2], 16)
        out.append(round(va + (vb - va) * t))
    return "#%02x%02x%02x" % tuple(out)


def apply_theme(widget) -> None:
    c = config.ACTIVE_SCHEME
    bg, card, fg = c["BG"], c["BG_LIGHT"], c["FG"]
    accent, accent_dark, accent2 = c["ACCENT"], c["ACCENT_DARK"], c["ACCENT2"]
    muted, line, status = c["MUTED"], c["LINE"], c["STATUS_TEXT"]
    ink = on_accent(c)

    style = ttk.Style(widget)
    style.theme_use("clam")
    style.configure(".", background=bg, foreground=fg, font=(config.FONT, 9), bordercolor=line,
                    lightcolor=bg, darkcolor=bg, focuscolor=bg, troughcolor=bg)

    # --- frames / labels
    style.configure("TFrame", background=bg)
    style.configure("Card.TFrame", background=card, bordercolor=line, relief="solid", borderwidth=1)
    style.configure("Plain.Card.TFrame", background=card, borderwidth=0, relief="flat")
    style.configure("TLabel", background=bg, foreground=fg)
    style.configure("Card.TLabel", background=card, foreground=fg)
    style.configure("Muted.TLabel", background=bg, foreground=muted)
    style.configure("CardMuted.TLabel", background=card, foreground=muted)
    style.configure("Hint.TLabel", background=card, foreground=muted)
    style.configure("CardTitle.TLabel", background=card, foreground=accent2, font=(config.FONT, 9, "bold"))
    style.configure("Group.TLabel", background=card, foreground=accent2, font=(config.FONT, 9, "bold"))
    style.configure("Title.TLabel", background=bg, foreground=fg, font=(config.FONT, 15, "bold"))
    style.configure("Subtitle.TLabel", background=bg, foreground=muted, font=(config.FONT, 9))
    style.configure("Big.TLabel", background=card, foreground=fg, font=(config.FONT, 24, "bold"))
    style.configure("Section.TLabel", background=card, foreground=fg, font=(config.FONT, 13, "bold"))
    style.configure("Value.TLabel", background=card, foreground=status, font=(config.MONO, 9))
    style.configure("Track.TLabel", background=bg, foreground=fg, font=(config.FONT, 12, "bold"))
    style.configure("Artist.TLabel", background=bg, foreground=status, font=(config.FONT, 9))
    # old names, still used by the dialogs / main window cells
    style.configure("Cell.TFrame", background=card, bordercolor=line)
    style.configure("Status.TLabel", background=card, foreground=status, font=(config.MONO, 9))
    style.configure("CellTitle.TLabel", background=card, foreground=fg, font=(config.FONT, 9, "bold"))
    style.configure("TLabelframe", background=bg, foreground=fg, bordercolor=line)
    style.configure("TLabelframe.Label", background=bg, foreground=accent)
    style.configure("TSeparator", background=line)

    # --- buttons
    style.configure("TButton", background=card, foreground=fg, bordercolor=line, focusthickness=0,
                    padding=(14, 7), relief="flat")
    style.map("TButton", background=[("active", accent_dark), ("pressed", accent)],
              foreground=[("active", fg)], bordercolor=[("active", accent)])
    style.configure("Accent.TButton", background=accent, foreground=ink, bordercolor=accent, padding=(16, 7),
                    font=(config.FONT, 9, "bold"))
    style.map("Accent.TButton", background=[("active", accent2), ("pressed", accent_dark)],
              foreground=[("active", on_accent({"ACCENT": accent2}))])
    style.configure("Blackout.TButton", background=accent_dark, foreground=fg, bordercolor=accent_dark,
                    padding=(16, 7), font=(config.FONT, 9, "bold"))
    style.map("Blackout.TButton", background=[("active", accent), ("pressed", accent2), ("selected", accent)],
              foreground=[("active", ink)])

    # --- pill toggles (Checkbutton / Radiobutton with style="Chip.Toolbutton")
    style.configure("Chip.Toolbutton", background=bg, foreground=muted, bordercolor=line, relief="flat",
                    padding=(11, 5), font=(config.FONT, 9, "bold"), focuscolor=bg)
    style.map("Chip.Toolbutton",
              background=[("selected", accent), ("active", accent_dark)],
              foreground=[("selected", ink), ("active", fg)],
              bordercolor=[("selected", accent), ("active", accent)])
    style.configure("Look.Toolbutton", background=card, foreground=fg, bordercolor=line, relief="flat",
                    padding=(10, 6), font=(config.FONT, 9, "bold"), focuscolor=card)
    style.map("Look.Toolbutton",
              background=[("selected", accent), ("active", accent_dark)],
              foreground=[("selected", ink), ("active", fg)],
              bordercolor=[("selected", accent), ("active", accent)])

    style.configure("TCheckbutton", background=bg, foreground=fg, focuscolor=bg,
                    indicatorbackground=bg, indicatorforeground=accent2)
    style.map("TCheckbutton", background=[("active", bg)], indicatorbackground=[("selected", accent)],
              foreground=[("disabled", muted)])
    style.configure("Card.TCheckbutton", background=card, foreground=fg, focuscolor=card,
                    indicatorbackground=bg, indicatorforeground=ink)
    style.map("Card.TCheckbutton", background=[("active", card)], indicatorbackground=[("selected", accent)],
              foreground=[("disabled", muted)])

    # --- inputs
    style.configure("TCombobox", fieldbackground=bg, background=card, foreground=fg, arrowcolor=accent2,
                    bordercolor=line, padding=4)
    style.map("TCombobox", fieldbackground=[("readonly", bg)], foreground=[("readonly", fg)],
              bordercolor=[("focus", accent)])
    style.configure("TEntry", fieldbackground=bg, foreground=fg, insertcolor=fg, bordercolor=line)
    widget.option_add("*TCombobox*Listbox.background", bg)
    widget.option_add("*TCombobox*Listbox.foreground", fg)
    widget.option_add("*TCombobox*Listbox.selectBackground", accent_dark)
    widget.option_add("*TCombobox*Listbox.selectForeground", fg)
    widget.option_add("*TCombobox*Listbox.font", (config.FONT, 9))

    # --- sliders: handle in the accent colour, dark groove
    for name, trough in (("Horizontal.TScale", card), ("Card.Horizontal.TScale", bg)):
        style.configure(name, background=accent, troughcolor=trough, bordercolor=line, lightcolor=accent,
                        darkcolor=accent, sliderthickness=14, sliderrelief="flat")
        style.map(name, background=[("active", accent2), ("disabled", line)],
                  lightcolor=[("active", accent2), ("disabled", line)],
                  darkcolor=[("active", accent2), ("disabled", line)])

    # --- progress bars
    style.configure("Meter.Horizontal.TProgressbar", troughcolor=bg, background=accent, bordercolor=bg,
                    lightcolor=accent, darkcolor=accent, thickness=8)
    style.configure("Build.Horizontal.TProgressbar", troughcolor=bg, background=accent2, bordercolor=bg,
                    lightcolor=accent2, darkcolor=accent2, thickness=6)
