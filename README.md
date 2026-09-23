# DMX Derby Controller

A small UI to control a DMX derby/laser fixture over a USB-DMX adapter — one slider per channel, live plain-text readout of what each value actually does, Blackout and a full Music Mode.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- One slider per DMX channel (9-channel mode), each showing a description of the current value (e.g. `114 | Derby + Laser`) instead of a raw number.
- Non-blocking connect, detects connection loss (e.g. adapter unplugged)
- One-click blackout
- Three selectable color themes (purple / blue / black-white) via `COLOR_SCHEME` in `src/config.py`
- **Music Mode**: analyzes your system's audio in real time and drives the fixture from it
  - Live spectrum + waveform display
  - Bass / Mid / Treble / Beat / Pitch, each mappable to any DMX channel
  - Adjustable sensitivity and smoothing
  - Shows the currently playing track's title, artist, and (where available) cover art

## Project layout

```
DMX-RDC/
├── main.py                       # entry point — run this
├── requirements.txt
├── presets/                      # created automatically, holds saved channel presets
└── src/
    ├── config.py                 # all constants live here (parameters class)
    ├── controller.py             # DMX serial link, preset persistence, platform helpers
    ├── musicmode.py               # audio analysis + Music Mode window
    ├── ui.py                     # main window, theme, dialogs
    └── NowPlayingBridge.ps1      # Windows-only helper (see Music Mode below)
```

## Requirements

- Python 3.10+
- A USB-DMX adapter that is recognized as a serial (COM) port
- See [requirements.txt](requirements.txt) for Python packages — installation differs slightly by platform, see below

**Windows** (primary, fully supported):
```bash
pip install -r requirements.txt
```
This installs `PyAudioWPatch` (WASAPI loopback audio capture) and `pywin32` (optional fallback for track title/artist).

**Linux** (groundwork/experimental — see [Limitations](#limitations)):
```bash
pip install -r requirements.txt
```
`requirements.txt` uses platform markers, so on Linux this instead installs plain `PyAudio` (needs PortAudio; on Debian/Ubuntu: `sudo apt install portaudio19-dev` first) and `jeepney` for MPRIS-based title/artist/cover. Loopback capture uses your PulseAudio/PipeWire "Monitor of ..." source, which must exist and be running.

## Installation

```bash
git clone https://github.com/jesajaw/DMX-RDC
cd DMX-RDC # or the folder you selected
pip install -r requirements.txt
```

### Cover art on Windows

Track title/artist work out of the box (Music Mode launches `src/NowPlayingBridge.ps1`, a small PowerShell script that talks to Windows' own media APIs — the same source behind the volume flyout preview). Nothing to install: PowerShell and the required Windows APIs ship with every Windows install, so this runs automatically the first time you open Music Mode.

## Usage

```bash
python main.py
```

1. Select the COM port your USB-DMX adapter is connected to.
2. Click **Connect**.
3. Move the sliders — changes are sent continuously while connected.
4. **BLACKOUT** sets all channels to 0 immediately.
5. **🎵 Music Mode** opens a dedicated window for audio-reactive lighting (see below). The main window hides itself while Music Mode is open but keeps sending in the background; closing Music Mode brings it back.
6. **Disconnect** stops sending and closes the port.

## Music Mode

Click **🎵 Music Mode** to open it. It analyzes whatever is currently playing through your system's audio output (any app — Spotify, a browser tab, a game, anything) and turns that into DMX values.

- **Spectrum** and **Waveform**: a live view of the audio, side by side.
- **Channel Mapping**: assign any of Bass / Mid / Treble / Beat / Pitch to any DMX channel. Beat is a short pulse on sudden loudness spikes (good for strobes); Pitch reflects how bright/dark the sound currently is (better suited to continuous rotation/speed than a plain band).
- **Sensitivity** / **Smoothing**: tune how strongly and how quickly the fixture reacts.
- **Now Playing**: shows the title/artist of the current track, with cover art where available (see [Cover art on Windows](#cover-art-on-windows)). Without cover art, the disc shows a small rotating pixel-art animation instead.

Mappings can be changed while Music Mode is running.

## Presets

Channel setups can be saved and reloaded as presets, stored as individual JSON files in the `presets/` folder (created automatically on first run).

- **Save As...** — stores the current slider values under a name you choose
- **Load** — applies the selected preset's values to all sliders
- **Delete** — removes the selected preset

Each preset is a plain JSON file: `presets/<name>.json`:

```json
{
  "1": 44,
  "2": 180,
  "3": 216,
  "4": 0,
  "5": 128,
  "6": 60,
  "7": 0,
  "8": 254,
  "9": 127
}
```

## Limitations

- DMX512 is a unidirectional protocol: the controller has no way to confirm that a fixture is actually receiving data, only that the USB-DMX adapter itself is reachable over serial.
- Tested on Windows with a generic USB-DMX (FTDI-based) adapter. That's the primary, fully-tested platform.
- **Linux support is groundwork, not verified**: the code paths exist (PulseAudio/PipeWire loopback capture, MPRIS-based title/artist/cover via `jeepney`) but haven't been tested against a real PulseAudio/PipeWire/D-Bus setup. If audio capture or Now Playing don't pick anything up, check `pactl list sources short` for your monitor source name, and `busctl --user list | grep mpris` for an active MPRIS player — `src/musicmode.py`'s `_resolve_loopback_device` and `_fetch_mpris_metadata` are the places to adjust if the exact names/shapes differ on your system.
- macOS is untested and currently unsupported for Music Mode (no loopback backend implemented); the rest of the app should still work.
- Cover art on Windows depends on the app you're playing from registering with Windows' media session API (most modern players do) and on PowerShell being available on your system (it is, on every normal Windows install) — if `NowPlayingBridge.ps1` can't run for some reason, you still get title/artist via a window-title fallback, just for a smaller, hardcoded list of known player processes (`KNOWN_PLAYER_PROCESSES` in `src/config.py`).
- `NowPlayingBridge.ps1` itself is based on a known public PowerShell/WinRT interop pattern for calling `Windows.Media.Control` without a compiled binding, but wasn't run against a live Windows media session while writing it. If it doesn't pick up your player, try running it directly (`powershell -File src/NowPlayingBridge.ps1 .`) to see any errors instead of them disappearing into the background process.

## License

MIT — see [LICENSE](LICENSE).
