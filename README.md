# DMX Derby Controller

A small UI to control a DMX derby/laser fixture over a USB-DMX adapter — one slider per channel, live plain-text readout of what each value actually does, Blackout and a Music Mode.

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
├── main.py # entry point — run this
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
* See [requirements.txt](requirements.txt) for Python packages — installation differs slightly by platform, see below


**Windows**:
```bash
pip install -r requirements.txt
```


**Linux** (groundwork/experimental — see [Limitations](#limitations)):
```bash
pip install -r requirements.txt
```

`requirements.txt` uses platform markers, so on Linux this instead installs plain `PyAudio` (needs PortAudio; on Debian/Ubuntu: `sudo apt install portaudio19-dev` first) and `jeepney` for MPRIS-based title/artist/cover. Loopback capture uses your PulseAudio/PipeWire "Monitor of ..." source, which must exist and be running.

## 💻 Usage

```bash
python main.py
```

1. Select the COM port your USB-DMX adapter is connected to.
2. Click **Connect**.
3. Move the sliders — changes are sent continuously while connected.
4. **BLACKOUT** sets all channels to 0 immediately.
5. **🎵 Music Mode** opens a dedicated window for audio-reactive lighting (see below). The main window hides itself while Music Mode is open but keeps sending in the background; closing Music Mode brings it back.
6. **Disconnect** stops sending and closes the port.

### Music Mode

Click **🎵 Music Mode** to open it. It analyzes whatever is currently playing through your system's audio output and turns that into DMX values.

- **Spectrum ring** and **Waveform**: a live view of the audio. The four bands (Sub / Bass / Mids / Highs) flash on a hit.
- **Live**: BPM, tempo lock, beat/bar position, a level meter per band and a small preview of what LED, Derby and Laser are doing.
- **Look**: one-click starting points (Kick Flash, Colour Pulse, Sub Swing, Ambient).
- **When this happens -> do this**: the rules of the current look. Source (a band or the beat) + event (hit / rises above / falls below / every N beats) -> target (LED, Derby colour/position, Laser colour/rotation ...) + action (set / toggle / cycle), optionally with a hold time and a minimum gap. Editing a rule switches the look to "Custom".
- **Sensitivity**: how strongly quiet parts count.
- **Now Playing**: shows the title/artist/album of the current track, with cover art where available (see [Title, artist, album & cover art on Windows](#title-artist-album--cover-art-on-windows)). Without cover art, the disc shows a small rotating pixel-art animation instead.

Mappings can be changed while Music Mode is running.

### Presets

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
- **Linux support is groundwork, not verified**: the code paths exist (PulseAudio/PipeWire loopback capture, MPRIS-based title/artist/cover via `jeepney`) but haven't been tested against a real PulseAudio/PipeWire/D-Bus setup. If audio capture or Now Playing don't pick anything up, check `pactl list sources short` for your monitor source name, and `busctl --user list | grep mpris` for an active MPRIS player — `src/musicmode/audio_source.py`'s `_discover` and `src/musicmode/nowplaying.py`'s `_fetch_mpris_metadata` are the places to adjust if the exact names/shapes differ on your system.

## 📜 License

Distributed under the MIT License. See [LICENSE](LICENSE) for more information.
