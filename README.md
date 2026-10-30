# DMX Derby Controller

This was built because I didn't want to buy a massive lighting controller just to run one DMX derby/laser fixture.
This is a lightweight UI that runs over a USB-DMX adapter so you can bypass confusing remote control auto-modes and actually control the lights yourself. I also added experimental live music analysis -- though obviously it's not the same experience as a real human preparing before a show starts.

## Features

* One slider per DMX channel (9-channel mode), each showing a description of the current value
* Three selectable color themes via `COLOR_SCHEME` in `src/config.py`
* **Music Mode**: analyzes your system's audio in real time and drives the fixture from it
  - Live spectrum ring (Sub / Bass / Mids / Highs) + waveform display
  - Hit detection per band, tempo tracking (BPM, beat clock, bars)
  - Rules: "when this happens -> do this" (band hit / level / every N beats -> LED, Derby, Laser)
  - Ready-made looks as starting points, adjustable sensitivity
  - Shows the currently playing track's title, artist, and cover art

## Requirements

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


## Usage

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
source .venv/bin/activate && python3 main.py
```

On Linux the adapter shows up as `/dev/ttyUSB0` (or `/dev/ttyACM0`). Ignore the `/dev/ttyS*` entries, those are unused built-in ports.

## Limitations

DMX512 is a unidirectional protocol: The controller has no way of verifying whether a device is actually receiving data; it can only check whether the USB-DMX adapter itself is accessible via the serial interface. And anyway, I don't think you can use this script for other devices because of the different channels; that might be the biggest limitation :/

## Acknowledgments
Thanks to [Claude](https://claude.ai/) for helping build the UI and for taking on the tedious parts: data formatting, saving, and the many small fixes/debbunging in between.


## License

Distributed under the MIT License. See [LICENSE](LICENSE) for more information.
