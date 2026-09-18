# DMX Derby Controller

A small UI to control a DMX derby/laser fixture over a USB-DMX adapter — one slider per channel, live plain-text readout of what each value actually does, Blackout and Music Mode.

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![License](https://img.shields.io/badge/license-MIT-green)

## Features

- One slider per DMX channel (9-channel mode), each showing a description of the current value (e.g. `114 | Derby + Laser`) instead of a raw number.
- Non-blocking connect, detects connection loss (e.g. adapter unplugged)
- One-click blackout
- Three selectable color themes (purple / blue / black-white) via a config toggle at the top of the file.

## Todo
- Music Mode

## Requirements

- python 3.10+
- pyserial 3.5+
- PyAudioWPatch 0.2.12+
- numpy 1.26+
- pywin32 306+ (should be optional for Music title and cover)

Installation is done via the [requirements.txt](requirements.txt) file. You will need a USB-DMX adapter that is recognized as a serial (COM) port.

## Installation

```bash
git clone https://github.com/jesajaw/DMX-RDC
cd DMX-RDC # or the folder you selected
pip install -r requirements.txt
```

## Usage

```bash
python main.py
```

1. Select the COM port your USB-DMX adapter is connected to.
2. Click **Connect**.
3. Move the sliders — changes are sent continuously (~33 Hz) while connected
4. **BLACKOUT** sets all channels to 0 immediately.
5. **Music Mode** takes you to a mode optimized for music, where you can set up lighting effects that sync with your music.
6. **Disconnect** stops sending and closes the port.

## Presets

Channel setups can be saved and reloaded as presets, stored as individual JSON files in the `presets/` folder (created automatically on first run).

- **Save As...** — stores the current slider values under a name you choose
- **Load** — applies the selected preset's values to all sliders (and live DMX output, if connected)
- **Delete** — removes the selected preset

Each preset is a plain JSON file (`presets/<name>.json`), so they can be copied, renamed, or shared individually:

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
- Tested on Windows with a generic USB-DMX (FTDI-based) adapter.

## License

MIT — see [LICENSE](LICENSE).
