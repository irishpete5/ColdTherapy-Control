# ColdTherapy-Control

MicroPython firmware for an ESP32 + ILI9341 touchscreen ("Cheap Yellow
Display" style board) that drives a relay-controlled cold-therapy pump, with
an on-device touch UI, a min-temperature safety cutoff, and Bluetooth LE
monitoring/control.

> **Not a medical device.** This is a hobbyist project built to automate a
> relay for a personal cold-therapy setup. It has no certification, no
> redundant safety systems, and no warranty. Test it thoroughly before relying
> on it, keep an eye on it in use, and don't use it for anything where a
> software bug or hardware failure could cause harm.

## Features

- **Master on/off** slider - engages/disengages the relay.
- **Interval mode** with independently configurable **on** and **off**
  durations (1-99 minutes each), set from a dedicated Interval Set screen.
  Each +/- button supports a quick tap (±1 minute) and press-and-hold
  (auto-repeats after a 2s delay, ~3 steps/sec).
- **Temperature calibration screen**: a signed offset to correct the sensor
  reading against a reference thermometer, and a minimum-temperature safety
  threshold.
- **Min-temperature safety cutoff**: if the sensed temperature drops to/below
  the threshold, the relay is forced off until it recovers 1.0°F above the
  threshold (hysteresis, so it doesn't chatter at the boundary).
- **Smoothed temperature reading**: each reading averages 16 raw ADC samples,
  and an exponential moving average is layered on top and refreshed every
  500ms - see [Temperature smoothing](#temperature-smoothing).
- **Bluetooth LE**: broadcasts full device status and accepts remote control
  commands - see [BLE protocol](#ble-protocol).
- **Auto-dimming backlight** based on the onboard light sensor.
- Config (interval times, calibration, last paired BLE peer) persists to
  flash as `config.json` and survives reboots.

## Hardware

Built and tested on an ESP32 dev board wired like the common ILI9341/XPT2046
"Cheap Yellow Display" (CYD) boards, plus a relay and an NTC thermistor added
for the cold-therapy pump itself.

| Signal                         | GPIO |
|---------------------------------|------|
| TFT backlight (PWM)             | 21   |
| TFT SPI SCK                     | 14   |
| TFT SPI MOSI                    | 13   |
| TFT DC                          | 2    |
| TFT CS                          | 15   |
| TFT RST                         | 4    |
| Touch SPI SCK                   | 25   |
| Touch SPI MOSI                  | 32   |
| Touch SPI MISO                  | 39   |
| Touch CS                        | 33   |
| Touch IRQ                       | 36   |
| Onboard light sensor (LDR, ADC) | 34   |
| Relay control output            | 22   |
| Temperature sensor (NTC, ADC)   | 35   |

The temperature sensor is a 10k NTC thermistor (β≈3950) in a voltage-divider
with a 10k fixed resistor, read on GPIO35. If your divider uses different
resistor values, adjust `r_fixed`/`beta`/`r0` in `read_temperature_f()` in
`ColdTherapyModernUI.py` to match.

## Repo layout

```
ColdTherapyModernUI.py   Main application (this is what you run on the device)
boot.py                  Stock MicroPython boot hook (runs before main.py)
ili9341.py               ILI9341 display driver
xpt2046.py               XPT2046 touch controller driver
xglcd_font.py            Loader for the large bitmap font used for key numbers
Unispace12x24.c          Font data file loaded by xglcd_font.py at runtime
config.json              Example/current persisted settings (device rewrites this itself)
firmware/                Prebuilt MicroPython .bin images - see below
```

## Flashing the MicroPython firmware

The `firmware/` folder has two prebuilt images, both MicroPython v1.28.0 for
a classic ESP32 (not S2/S3/C3):

- `ESP32_GENERIC-20260406-v1.28.0.app-.bin` - standard build, **no external
  PSRAM**.
- `ESP32_GENERIC-SPIRAM-20260406-v1.28.0.bin` - build with PSRAM support, for
  boards that have an external PSRAM chip.

**Check which one your board needs before flashing.** Flashing the SPIRAM
build onto a board *without* PSRAM can fail to boot (it tries to initialize
memory that isn't there). If you're not sure, start with the standard
(non-SPIRAM) build - it's the safer default, and you can always reflash the
other one if you hit memory errors running the app.

Steps (using [esptool](https://github.com/espressif/esptool)):

1. Install esptool if you don't have it: `pip install esptool`
2. Plug in the board and find its serial port:
   - Windows: Device Manager -> Ports (COM & LPT) -> note the `COMx` number.
   - Linux/macOS: usually `/dev/ttyUSB0` or `/dev/ttyACM0`.
3. Erase the flash first (recommended - avoids old filesystem data conflicting
   with the new firmware):
   ```
   esptool.py --chip esp32 --port COM3 erase_flash
   ```
4. Write the firmware (classic ESP32 images load at offset `0x1000`):
   ```
   esptool.py --chip esp32 --port COM3 --baud 460800 write_flash -z 0x1000 firmware/ESP32_GENERIC-20260406-v1.28.0.app-.bin
   ```
   (swap in the SPIRAM `.bin` if that's the one your board needs).
5. Reset the board. It should come up to a bare MicroPython REPL - you're
   ready to deploy the app files below.

## Deploy the app to the device

Copy these files to the device's flash filesystem root, using
[Thonny](https://thonny.org/), `mpremote`, or `ampy`:

```
ColdTherapyModernUI.py
boot.py
ili9341.py
xpt2046.py
xglcd_font.py
Unispace12x24.c
```

Then either:

- **Rename `ColdTherapyModernUI.py` to `main.py` on the device** (simplest -
  MicroPython auto-runs `main.py` after `boot.py` on every power-up), or
- Upload it under its original name and run it manually each session (e.g.
  from Thonny's Run button, or `import ColdTherapyModernUI` at the REPL).

Example with `mpremote`:

```
mpremote connect COM3 fs cp ColdTherapyModernUI.py :main.py
mpremote connect COM3 fs cp boot.py ili9341.py xpt2046.py xglcd_font.py Unispace12x24.c :
mpremote connect COM3 reset
```

`config.json` doesn't need to be uploaded manually - the app creates it with
defaults on first boot and rewrites it whenever you change a setting. The
copy in this repo is just a snapshot of one working configuration for
reference.

## Using the app

- **Main screen**: TIMER status card (left), Bluetooth / Current Temp /
  Interval cards (middle column), Master on/off slider and Interval mode
  switch (right column).
  - Tap the **Bluetooth** card to open pairing.
  - Tap the **Current Temp** card to open calibration.
  - Tap the **Interval** card ("SET >") to open the Interval Set screen.
- **Interval Set screen**: independent On Time / Off Time steppers. Tap
  +/- for ±1 minute, hold either button down to auto-repeat after ~2s.
  DONE returns to the main screen (and saves).
- **Calibration screen**: Offset (±0.5°F per tap) corrects the sensor against
  a reference thermometer; Min Temp (±1.0°F per tap) sets the safety cutoff
  threshold. DONE returns to the main screen (and saves).
- **Bluetooth screen**: shows connection status and a PAIR/STOP
  PAIRING/END PAIRING/RETRY/REBOOT action button depending on current BLE
  state.

## Temperature smoothing

A single ESP32 ADC sample is noisy enough to visibly jitter the readout, so
the firmware smooths it in two stages:

1. `read_temperature_f()` averages 16 back-to-back raw ADC samples before the
   (nonlinear) Steinhart-Hart temperature conversion.
2. `sample_temperature()` runs every main-loop tick but is internally
   rate-limited to once every `TEMP_SAMPLE_INTERVAL_MS` (500ms by default); each
   time it fires, it folds the new oversampled reading into a slower
   exponential moving average: `smoothed = α·new + (1-α)·smoothed`, with
   `TEMP_EMA_ALPHA = 0.2` by default.

Every consumer of the temperature - the main screen readout, the calibration
screen, the BLE status broadcast, and the min-temperature safety check - reads
this single smoothed value via `get_temperature_f()`, instead of each taking
its own independent noisy sample. If it still feels too jumpy or too sluggish
to react, tune `TEMP_EMA_ALPHA` (lower = smoother/slower,
higher = more responsive) and `TEMP_SAMPLE_INTERVAL_MS` near the top of
`ColdTherapyModernUI.py`.

## BLE protocol

Device advertises as **`ESP32-ColdTherapy`** with one custom GATT service:

| | UUID |
|---|---|
| Service | `6c9f0001-cdef-4e0a-9a1a-000000000000` |
| Status characteristic (read/notify) | `6c9f0002-cdef-4e0a-9a1a-000000000000` |
| Control characteristic (write) | `6c9f0003-cdef-4e0a-9a1a-000000000000` |

### Status (read or subscribe to notifications)

A single semicolon-delimited ASCII string, e.g.:

```
M=1;I=1;ON_MIN=16;OFF_MIN=22;OFF=-8.0;THR=65.0;TEMP=75.4;STATE=ACTIVE;PAUSE=0
```

| Field | Meaning |
|---|---|
| `M` | Master on/off (`1`/`0`) |
| `I` | Interval mode on/off (`1`/`0`) |
| `ON_MIN` | Interval on-time, minutes |
| `OFF_MIN` | Interval off-time, minutes |
| `OFF` | Temperature calibration offset, °F (signed) |
| `THR` | Minimum-temperature safety threshold, °F |
| `TEMP` | Current smoothed temperature, °F (or `ERR` if the sensor read failed) |
| `STATE` | `ACTIVE` (relay on phase) or `REST` (relay off phase) within an interval cycle |
| `PAUSE` | `1` if the min-temperature safety cutoff is currently active, else `0` |

### Control (write a `KEY=VALUE` string)

| Key | Value | Effect |
|---|---|---|
| `MASTER` | `1`/`0`/`ON`/`OFF`/`TRUE` | Sets master on/off |
| `INTERVAL` | `1`/`0`/`ON`/`OFF`/`TRUE` | Sets interval mode on/off |
| `MIN_ON` | integer 1-99 | Sets interval on-time minutes |
| `MIN_OFF` | integer 1-99 | Sets interval off-time minutes |
| `OFFSET` | float | Sets the temperature calibration offset (°F) |
| `THRESH` | float | Sets the minimum-temperature safety threshold (°F) |

Any write also triggers an immediate status broadcast/notify.

## config.json

Written to flash by the device itself; you generally don't need to edit it
by hand.

```json
{
  "interval_on_minutes": 16,
  "interval_off_minutes": 22,
  "temp_offset": -8.0,
  "min_temp_threshold": 65.0,
  "last_bt_peer_addr": "63:0F:FE:EC:23:D9"
}
```

`last_bt_peer_addr` is the MAC address of the last BLE central that paired,
remembered so the device re-advertises automatically on boot to reconnect to
it. An older config written before the on/off split will still load fine -
`interval_on_minutes` falls back to a legacy `interval_minutes` key if
present.

## Development notes

- The touch controller's native coordinate frame is a 90° rotation of the
  display's (240x320 portrait vs. 320x240 landscape), so every touch
  hit-test in the code maps `touch_x` to a widget's display-Y span and
  `touch_y` to its display-X span.
- The XPT2046 driver's touch IRQ fires once on touch-down and once on
  release - it does not report a live position while held. Press-and-hold
  (used on the Interval Set screen's +/- buttons) is implemented by polling
  the IRQ pin's level directly in the main loop rather than waiting on more
  touch events.
- `fill_round_rect()` and `card()` currently draw flat (non-rounded)
  rectangles - true rounded corners were tried and reverted because
  `fill_circle()`'s per-line SPI writes made repainting a card on every touch
  visibly slow (~1s/tap) on this display driver.
