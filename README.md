# DMX Derby Controller

This was built because I didn't want to buy a massive lighting controller just to run one DMX derby/laser fixture.
This is a lightweight UI that runs over a USB-DMX adapter so you can bypass confusing remote control auto-modes and actually control the lights yourself. I also added experimental live music analysis -- though obviously it's not the same experience as a real human preparing before a show starts.

## 🚀 Features

* One slider per DMX channel (9-channel mode), each showing a description of the current value
* Blackout
* Three selectable color themes via `COLOR_SCHEME` in `src/config.py`
* **Music Mode**: analyzes your system's audio in real time and drives the fixture from it
  - Live spectrum ring (Sub / Bass / Mids / Highs) + waveform display
  - Hit detection per band, tempo tracking (BPM, beat clock, bars)
  - Rules: "when this happens -> do this" (band hit / level / every N beats -> LED, Derby, Laser)
  - Ready-made looks as starting points, adjustable sensitivity
  - Shows the currently playing track's title, artist, and cover art

## 📁 Project layout

```
DMX-RDC/
├── main.py # entry point -- run this
├── requirements.txt
├── ...manual.pdf   # usermanual for the used DMX Derby Laser
├── presets/    # created automatically, holds saved channel presets .json
├── nowplaying_cache/    # just the cache for powershell
├── _old/    # superseded files (old Music Mode, old config) -- reference only, safe to delete
└── src/
    ├── __init__.py
    ├── app.py   # main window, dialogs
    ├── config.py   # main window / DMX / theme constants
    ├── controller.py   # DMX serial link, preset persistence, platform helpers
    ├── theme.py   # every ttk style
    └── musicmode/
        ├── __init__.py
        ├── config.py   # every Music Mode tunable
        ├── audio_source.py   # loopback capture (WASAPI / PulseAudio)
        ├── analysis.py   # band levels, hits, tempo, beat clock
        ├── devices.py   # LED / Derby / Laser: states -> DMX values
        ├── engine.py   # rules + looks -> DMX frame
        ├── music_app.py   # the Music Mode window
        ├── nowplaying.py   # title / artist / cover reader
        └── NowPlayingBridge.ps1    # Windows: title/artist/album/cover bridge
```


## 🛠️ Requirements

* Cross-platform:
  * Python 3.10+
  * pyserial 3.5+
  * numpy 1.26+
  * pillow 10.0+
* Windows only:
  * PyAudioWPatch>=0.2.12
  * pywin32>=306
* Linux only (not tested yet):
  * PyAudio>=0.2.14
  * jeepney>=0.8.0

* A USB-DMX adapter that is recognized as a serial (COM) port
* See [requirements.txt](requirements.txt) for Python packages -- installation differs slightly by platform, see below


## 💻 Usage

### **Windows**:
```bash
pip install -r requirements.txt
```
And thats basically it, just run main:

```bash
python main.py
```
and you can go on to [Instructions](#instructions).

### **Linux**:
Well, of course, we (fortunately) have far more options and possibilities available to us for solving all these problems. I recommend setting up a development environment first:

```bash
# 1. System packages
sudo apt update
sudo apt install python3-venv python3-dev python3-tk build-essential portaudio19-dev

# 2. Virtual environment + Python packages
cd <.../DMX-RDC-root>
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

#### USB-DMX adapter permission (udev rule)

By default only root may open `/dev/ttyUSB*` ("Permission denied"). Install a udev rule once. The example is for FTDI adapters (`0403:6001`) -- check your adapter's IDs with `lsusb`.

```bash
sudo tee /etc/udev/rules.d/99-dmx.rules <<'EOF'
SUBSYSTEM=="tty", ATTRS{idVendor}=="0403", ATTRS{idProduct}=="6001", MODE="0666"
EOF
sudo udevadm control --reload-rules
sudo udevadm trigger
```

Unplug and replug the adapter. `ls -l /dev/ttyUSB0` should now show `crw-rw-rw-`.

> Use the `tee` form instead of `echo '...' | sudo tee`: quotes inside `echo` easily get mangled when pasted, which leaves the port with *no* permissions (`c---------`).

#### Run

```bash
source .venv/bin/activate   # once per new terminal
python main.py
```

On Linux the adapter shows up as `/dev/ttyUSB0` (or `/dev/ttyACM0`). Ignore the `/dev/ttyS*` entries, those are unused built-in ports.

### Instructions
1. Select the serial port your USB-DMX adapter is connected to (`COM3` etc. on Windows, `/dev/ttyUSB0` or `/dev/ttyACM0` on Linux).
2. Click **Connect**.
3. Move the sliders -- changes are sent continuously while connected.
4. **BLACKOUT** sets all channels to 0 immediately.
5. **🎵 Music Mode** opens a dedicated window for audio-reactive lighting (see below). The main window hides itself while Music Mode is open but keeps sending in the background; closing Music Mode brings it back.
6. **Disconnect** stops sending and closes the port.

### Music Mode

Click **🎵 Music Mode** to open it. It analyzes whatever is currently playing through your system's audio output and turns that into DMX values. The track title and artist are shown top left (Windows: via the bundled `NowPlayingBridge.ps1`; Linux: via MPRIS over D-Bus), with cover art where available. Without cover art, the disc shows a small rotating pixel-art animation instead.

**Spectrum ring (left)**: a live frequency view, 20 Hz to 20 kHz on a logarithmic scale, with level rings from 0 dB down to -40 dB. Hover over it to read the exact frequency and level at that point (e.g. `50 Hz  -3 dB`). The four bands (Sub / Bass / Mids / Highs) light up on a hit.

**Live**
- **BPM** with tempo lock and four beat/bar indicators. **Re-check BPM** restarts the tempo detection, e.g. after a tempo change.
- A level meter and hit lamp per band (Sub / Bass / Mids / Highs).
- A small preview of what **LED**, **Derby** and **Laser** are doing right now.

**Look**: one-click starting points (*Kick Flash*, *Colour Pulse*, *Sub Swing*, *Ambient*) with a short description of what each does. Editing the mapping below switches the look to "Custom".

**When this hits -> do this**: the mapping matrix. Each row is a source (**Sub / Bass / Mids / Highs** band hit, or the **Beat**), each column is something to trigger:
- **HIT = FLASH**: LED, Derby, Laser
- **HIT = CHANGE**: LED pattern, Derby colour, Laser colour, Derby swing, Laser spin

Click a cell to cycle through its states: `–` (nothing) → `ON` (flash on hit) → `OFF` (dark on hit); for CHANGE columns the cell instead steps to the next pattern / colour / position on every hit. Right-click goes back. In the **Beat** row, the dropdown picks how often it fires (every N bars/beats).

**Extras**
- **Bouncy Bass**: the Derby motor jumps between position 10 and 110 on every kick. When kicks come faster than the motor can follow, it only jumps on every 2nd (3rd, ...) kick (`BOUNCE_TRAVEL_S`, default 0.45 s, in `src/musicmode/config.py`).
- **Learn kick & thresholds per song**: the analysis learns each track's kick frequency, band thresholds and BPM while it plays, and caches them in `src/musicmode/song_profiles.json` (keyed by "artist - title"), so a song you've played before is detected better right from the start.

**Tuning**
- **Sensitivity**: how strongly quiet parts count (1.0x = default).
- **Smoothness**: how slowly the band levels fall off after a hit. Higher values give calmer, longer lights; lower values react snappier.

Mappings and tuning can be changed while Music Mode is running. **Blackout** sets all channels to 0, **Back to Manual Control** returns to the sliders. The status line at the bottom warns if no DMX adapter is connected (go back to manual control and click Connect).

### Presets

Channel setups can be saved and reloaded as presets, stored as individual JSON files in the `presets/` folder (created automatically on first run).

- **Save As...** -- stores the current slider values under a name you choose
- **Load** -- applies the selected preset's values to all sliders
- **Delete** -- removes the selected preset

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

DMX512 is a unidirectional protocol: The controller has no way of verifying whether a device is actually receiving data; it can only check whether the USB-DMX adapter itself is accessible via the serial interface. And anyway, I don't think you can use this script for other devices because of the different channels; that might be the biggest limitation :/


## 📜 License

Distributed under the MIT License. See [LICENSE](LICENSE) for more information.
