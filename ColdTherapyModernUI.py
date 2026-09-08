from machine import Pin, PWM, ADC, SPI, reset
from time import sleep, ticks_ms, ticks_diff, ticks_add
import json
import math
import bluetooth

# Required micro-libraries saved on ESP32 flash memory
from ili9341 import Display, color565
from xpt2046 import Touch
from xglcd_font import XglcdFont

# -------------------------------------------------------------------
# 1. Non-Volatile Flash Persistence (JSON Config)
# -------------------------------------------------------------------
CONFIG_FILE = "config.json"

master_on = False
interval_on = False
interval_on_minutes = 5
interval_off_minutes = 5
temp_offset = 0.0  # Temperature calibration offset in °F
min_temp_threshold = 60.0  # Relay pauses at/below this temp, in °F

# Temperature-pause state
temp_pause_active = False
pause_start_time = 0

# View State: "MAIN", "TEMP_CALIBRATE", "BT_CONFIG", or "INTERVAL_SET"
current_view = "MAIN"

def load_config():
    """Loads saved settings from flash storage on startup."""
    global interval_on_minutes, interval_off_minutes, temp_offset, min_temp_threshold, last_bt_peer_addr
    try:
        with open(CONFIG_FILE, "r") as f:
            data = json.load(f)
            # Falls back to the old "interval_minutes" key so an existing device's
            # saved on-time survives the upgrade instead of resetting to the default.
            interval_on_minutes = data.get("interval_on_minutes", data.get("interval_minutes", 5))
            interval_off_minutes = data.get("interval_off_minutes", 5)
            temp_offset = float(data.get("temp_offset", 0.0))
            min_temp_threshold = float(data.get("min_temp_threshold", 60.0))
            last_bt_peer_addr = data.get("last_bt_peer_addr")
            print("Loaded config successfully:", data)
    except (OSError, ValueError):
        print("No valid config found. Creating default config.json...")
        save_config()

def save_config():
    """Saves current interactive states to flash storage."""
    try:
        data = {
            "interval_on_minutes": interval_on_minutes,
            "interval_off_minutes": interval_off_minutes,
            "temp_offset": temp_offset,
            "min_temp_threshold": min_temp_threshold,
            "last_bt_peer_addr": last_bt_peer_addr
        }
        with open(CONFIG_FILE, "w") as f:
            json.dump(data, f)
        print("Config saved to flash.")
    except Exception as e:
        print("Failed to save config:", e)

# -------------------------------------------------------------------
# 2. Hardware Initialization (CYD Display & Peripherals)
# -------------------------------------------------------------------
backlight = PWM(Pin(21))
backlight.freq(1000)
backlight.duty_u16(65535)  # 100% Brightness

# SPI Bus Configuration
display_spi = SPI(1, baudrate=40000000, sck=Pin(14), mosi=Pin(13))
display = Display(display_spi, dc=Pin(2), cs=Pin(15), rst=Pin(4), width=320, height=240, rotation=0)

touchscreen_spi = SPI(2, baudrate=1000000, sck=Pin(25), mosi=Pin(32), miso=Pin(39))

# Onboard LDR Light Sensor Setup (GPIO 34)
lightsensor = ADC(Pin(34))
lightsensor.atten(ADC.ATTN_0DB)

# The ili9341.py driver already packs colors as big-endian RGB565
# (color.to_bytes(2, 'big')) when writing to SPI, which is exactly what this
# panel expects - no extra byte-swap needed. (An earlier version of this file
# applied one anyway; that's harmless for pure 0/255 colors, whose swapped
# bit pattern still lands on a saturated color, but scrambles intermediate/
# subtle colors unpredictably since RGB565's 5-6-5 fields don't align to
# byte boundaries - that's what produced the wrong hues/low contrast.)
def rgb(r, g, b):
    return color565(r, g, b)

# -------------------------------------------------------------------
# Modern UI Palette (dark dashboard, iOS-style accents)
# -------------------------------------------------------------------
BLACK = rgb(0, 0, 0)
WHITE = rgb(255, 255, 255)
BG = rgb(15, 18, 26)             # app background
CARD = rgb(27, 32, 44)           # neutral card fill
CARD_BORDER = rgb(43, 50, 66)    # subtle card border
TEXT = rgb(235, 238, 242)        # primary text
TEXT_MUTED = rgb(139, 148, 165)  # secondary/muted text
GREEN = rgb(48, 209, 88)         # active / on / good
RED = rgb(255, 69, 58)           # off / alert
AMBER = rgb(255, 159, 10)        # paused / warning
BLUE = rgb(10, 132, 255)         # info / bluetooth accent
GREEN_DIM = rgb(24, 74, 42)      # dark-mode green fill (card backgrounds)
RED_DIM = rgb(74, 30, 28)        # dark-mode red fill
AMBER_DIM = rgb(74, 52, 16)      # dark-mode amber fill
BLUE_DIM = rgb(16, 44, 74)       # dark-mode blue fill

emphasis_font = XglcdFont("Unispace12x24.c", 12, 24)

# -------------------------------------------------------------------
# Card/panel drawing helper - flat rectangles only.
#
# Rounded corners were tried (rect body + 4 fill_circle() corners) but
# fill_circle() on this driver draws via a midpoint-circle sweep of many
# draw_vline() calls, each its own SPI command round-trip - fine for a
# handful of decorative dots, but far too slow once several cards with
# rounded corners get repainted on every touch (measured ~1s per tap).
# A flat fill_rectangle() is a single bulk SPI block write, so this stays
# snappy; the corrected color palette and type hierarchy carry the "modern"
# look instead of rounding.
# -------------------------------------------------------------------
def fill_round_rect(x, y, w, h, r, color):
    display.fill_rectangle(x, y, w, h, color)

def card(x, y, w, h, fill_color, r=0):
    display.fill_rectangle(x, y, w, h, fill_color)

def text_big(x, y, s, color, background=BLACK):
    display.draw_text(x, y, s, emphasis_font, color, background=background)

def text_big_centered(cx, y, s, color, background=BLACK):
    w = emphasis_font.measure_text(s)
    display.draw_text(cx - w // 2, y, s, emphasis_font, color, background=background)

def text_small(x, y, s, color, background=BLACK):
    display.draw_text8x8(x, y, s, color, background, 0)

def text_small_centered(cx, y, s, color, background=BLACK):
    w = len(s) * 8
    display.draw_text8x8(cx - w // 2, y, s, color, background, 0)

# Both fonts are fixed-size bitmaps (8x8 / 12x24), so a big +/- button doesn't
# get a bigger symbol just by drawing it in a bigger box - the glyph stays the
# same few pixels tall. These draw a bold +/- as plain filled bars instead,
# sized to whatever button they're centered in.
def draw_plus_glyph(cx, cy, size, thickness, color):
    half = size // 2
    display.fill_rectangle(cx - half, cy - thickness // 2, size, thickness, color)
    display.fill_rectangle(cx - thickness // 2, cy - half, thickness, size, color)

def draw_minus_glyph(cx, cy, size, thickness, color):
    half = size // 2
    display.fill_rectangle(cx - half, cy - thickness // 2, size, thickness, color)

# Hardware IO Setup
relay = Pin(22, Pin.OUT)             # Relay Control Output (GPIO 22)
relay.value(0)

temp_adc = ADC(Pin(35))             # Onboard NTC / Temp Sensor (GPIO 35)
temp_adc.atten(ADC.ATTN_11DB)

# Application State Variables
timer_state = "ACTIVE"              # "ACTIVE" (Relay Engaged) or "REST" (Relay Off)
timer_start = ticks_ms()
last_timer_bg = None
last_time_str = None
last_touch_time = 0

# Press-and-hold auto-repeat state for the Interval Set screen's +/- buttons.
# The XPT2046 touch IRQ only fires once on touch-down (see xpt2046.py's
# int_press) - it never reports a live x/y while a finger stays down. So a
# "hold" is detected in the main loop by polling touchscreen.int_pin.value()
# (a plain GPIO read, not an SPI transaction, so it can't collide with the
# touch/display bus) rather than by any repeated touch event.
hold_action = None       # One of "on_plus"/"on_minus"/"off_plus"/"off_minus", or None
hold_start_ms = 0
hold_last_repeat_ms = 0
hold_repeated = False    # True once auto-repeat has fired at least once for this press
HOLD_DELAY_MS = 2000     # How long a button must be held before auto-repeat kicks in
HOLD_REPEAT_MS = 300     # Auto-repeat interval once active (~3 steps/sec)

# -------------------------------------------------------------------
# 2b. Bluetooth (BLE) Setup
# -------------------------------------------------------------------
_IRQ_CENTRAL_CONNECT = 1
_IRQ_CENTRAL_DISCONNECT = 2
_IRQ_GATTS_WRITE = 3

BLE_NAME = "ESP32-ColdTherapy"

bt_connected = False
bt_advertising = False
bt_conn_handle = None
bt_peer_addr = None
last_bt_peer_addr = None    # Remembered across reboots for auto-reconnect
_bt_end_pairing_requested = False
_bt_retry_count = 0          # Consecutive failed retry_ble_init() attempts
ble_available = False       # False if the radio never came up; rest of app runs without BLE
_status_handle = None
_control_handle = None
_pending_reboot = False      # Set from the touch IRQ; actual reset() runs from the main loop
_pending_save_config = False # Set from the BLE IRQ; actual flash write runs from the main loop
_pending_ui_refresh = False  # Set from the BLE IRQ; actual SPI display redraw runs from the main loop

ble = bluetooth.BLE()

# A GATT characteristic's value buffer defaults to only 20 bytes in
# MicroPython's bluetooth module - our status string (M=...;I=...;ON_MIN=...;
# ...;EL=...) is comfortably longer than that, so without resizing it via
# gatts_set_buffer() (see _register_ctrl_service() below), every
# gatts_write()/notify silently truncates to the first 20 bytes (e.g.
# "M=1;I=1;ON_MIN=16;OF" - everything from OFF_MIN onward never reaches the
# client, on every single read and notify).
_STATUS_BUFFER_LEN = 160

def _ble_try_activate():
    try:
        ble.active(False)   # Clear any stale state left over from a prior soft-reset
        sleep(0.1)
        ble.active(True)
        ble.config(gap_name=BLE_NAME)
        # No characteristic needs encryption - disable bonding so the stack never
        # initiates an SMP Security Request (was hanging Android's system "wants to pair" dialog)
        ble.config(bond=False, mitm=False, le_secure=False, io=3)
        # gatts_set_buffer() (see _register_ctrl_service()) makes the status
        # characteristic's *value* big enough to hold the full status string,
        # but a live notify is separately capped by whatever ATT MTU actually
        # gets negotiated - some central-side BLE stacks won't raise it above
        # the ~20-byte default unless the peripheral asks first. Requesting a
        # larger MTU here means it's not left up to the central's default.
        ble.config(mtu=_STATUS_BUFFER_LEN + 20)
        return True
    except OSError as e:
        print("[BLE] Activation failed:", e)
        return False

ble_available = _ble_try_activate()
if not ble_available:
    sleep(0.5)
    ble_available = _ble_try_activate()
if not ble_available:
    print("[BLE] Bluetooth unavailable this session (radio stuck). "
          "Power-cycle the board (not just soft-reset) to retry. Continuing without BLE.")

# GATT Service: STATUS (read/notify all state) + CONTROL (write to change any setting)
_CTRL_SERVICE_UUID = bluetooth.UUID("6c9f0001-cdef-4e0a-9a1a-000000000000")
_STATUS_CHAR = (bluetooth.UUID("6c9f0002-cdef-4e0a-9a1a-000000000000"), bluetooth.FLAG_READ | bluetooth.FLAG_NOTIFY)
_CONTROL_CHAR = (bluetooth.UUID("6c9f0003-cdef-4e0a-9a1a-000000000000"), bluetooth.FLAG_WRITE)
_CTRL_SERVICE = (_CTRL_SERVICE_UUID, (_STATUS_CHAR, _CONTROL_CHAR))

def _register_ctrl_service():
    """Registers the GATT service and sizes the status characteristic's buffer
    to fit the full status string. Used both at startup and by retry_ble_init()."""
    (status_handle, control_handle), = ble.gatts_register_services((_CTRL_SERVICE,))
    ble.gatts_set_buffer(status_handle, _STATUS_BUFFER_LEN, False)
    return status_handle, control_handle

if ble_available:
    try:
        _status_handle, _control_handle = _register_ctrl_service()
    except Exception as e:
        print("[BLE] GATT service registration failed:", e)
        ble_available = False

def _bt_adv_payload(service_uuid):
    """Primary advertising packet: flags + 128-bit service UUID (name goes in the scan response to stay under 31 bytes)."""
    uuid_bytes = bytes(service_uuid)  # already little-endian per BLE spec
    payload = bytearray(b"\x02\x01\x06")
    # Complete list of 128-bit service UUIDs (type 0x07) - required for Web Bluetooth's services filter to match
    payload += bytearray((len(uuid_bytes) + 1, 0x07)) + uuid_bytes
    return payload

def _bt_scan_resp_payload(name):
    name_bytes = name.encode()
    return bytearray((len(name_bytes) + 1, 0x09)) + name_bytes

_BT_ADV_PAYLOAD = _bt_adv_payload(_CTRL_SERVICE_UUID)
_BT_SCAN_RESP_PAYLOAD = _bt_scan_resp_payload(BLE_NAME)

def start_pairing():
    """Begins BLE advertising so a central device can discover & pair."""
    global bt_advertising
    if not ble_available:
        print("[BLE] Bluetooth unavailable - cannot start pairing.")
        return
    if not ble.active():
        ble.active(True)
    try:
        ble.gap_advertise(None)  # Stop any prior advertising before restarting
        ble.gap_advertise(100000, adv_data=_BT_ADV_PAYLOAD, resp_data=_BT_SCAN_RESP_PAYLOAD, connectable=True)
        bt_advertising = True
        print("[BLE] Advertising started as:", BLE_NAME, "| adv:", bytes(_BT_ADV_PAYLOAD), "| resp:", bytes(_BT_SCAN_RESP_PAYLOAD))
    except Exception as e:
        bt_advertising = False
        print("[BLE] Failed to start advertising:", e)

def end_pairing():
    """Disconnects any active central and stops advertising."""
    global bt_connected, bt_conn_handle, bt_peer_addr, bt_advertising, _bt_end_pairing_requested
    if not ble_available:
        return
    _bt_end_pairing_requested = True
    if bt_conn_handle is not None:
        try:
            ble.gap_disconnect(bt_conn_handle)
        except Exception as e:
            print("[BLE] Disconnect error:", e)
    ble.gap_advertise(None)
    bt_advertising = False
    bt_connected = False
    bt_conn_handle = None
    bt_peer_addr = None
    print("[BLE] Pairing ended.")

def broadcast_status():
    """Publishes all device state to the STATUS characteristic (and notifies if connected)."""
    if not ble_available:
        return
    temp_f = get_temperature_f()
    # Seconds elapsed in the current phase (since timer_start) - same value
    # draw_timer_box() uses to compute "MINUTES REMAINING"/"ELAPSED TIME".
    # Sent raw rather than as a precomputed remaining/elapsed string so a BLE
    # client can derive exactly what the device's own screen shows using the
    # ON_MIN/OFF_MIN/STATE/PAUSE fields already in this same status string.
    elapsed_sec = ticks_diff(ticks_ms(), timer_start) // 1000
    status = "M={};I={};ON_MIN={};OFF_MIN={};OFF={:+.1f};THR={:.1f};TEMP={};STATE={};PAUSE={};EL={}".format(
        1 if master_on else 0,
        1 if interval_on else 0,
        interval_on_minutes,
        interval_off_minutes,
        temp_offset,
        min_temp_threshold,
        "{:.1f}".format(temp_f) if temp_f is not None else "ERR",
        timer_state,
        1 if temp_pause_active else 0,
        elapsed_sec
    )
    data = status.encode()
    ble.gatts_write(_status_handle, data)
    if bt_connected and bt_conn_handle is not None:
        try:
            ble.gatts_notify(bt_conn_handle, _status_handle, data)
        except Exception as e:
            print("[BLE] Notify error:", e)

def _handle_ble_write():
    """Parses a 'KEY=VALUE' command written to the CONTROL characteristic and applies it.
    Runs inside the BLE IRQ - must never touch the display (shares the SPI bus with the
    touchscreen IRQ) or the flash filesystem; only plain state changes happen here, and
    save_config()/redraws are deferred to the main loop via pending flags.
    """
    global master_on, interval_on, interval_on_minutes, interval_off_minutes, temp_offset, min_temp_threshold, timer_state, timer_start
    global _pending_save_config, _pending_ui_refresh
    try:
        raw = ble.gatts_read(_control_handle).decode().strip()
        print("[BLE] Control write:", raw)
        key, val = raw.split("=", 1)
        key = key.strip().upper()
        val = val.strip()

        if key == "MASTER":
            master_on = val in ("1", "ON", "TRUE")
            timer_state = "ACTIVE"
            timer_start = ticks_ms()
            _pending_save_config = True
            _pending_ui_refresh = True
        elif key == "INTERVAL":
            interval_on = val in ("1", "ON", "TRUE")
            timer_state = "ACTIVE"
            timer_start = ticks_ms()
            _pending_save_config = True
            _pending_ui_refresh = True
        elif key == "MIN_ON":
            minutes = int(float(val))
            if 1 <= minutes <= 99:
                interval_on_minutes = minutes
                _pending_save_config = True
                _pending_ui_refresh = True
        elif key == "MIN_OFF":
            minutes = int(float(val))
            if 1 <= minutes <= 99:
                interval_off_minutes = minutes
                _pending_save_config = True
                _pending_ui_refresh = True
        elif key == "OFFSET":
            temp_offset = float(val)
            _pending_save_config = True
            _pending_ui_refresh = True
        elif key == "THRESH":
            min_temp_threshold = float(val)
            _pending_save_config = True
            _pending_ui_refresh = True
        else:
            print("[BLE] Unknown control key:", key)
    except Exception as e:
        print("[BLE] Control parse error:", e)
    broadcast_status()

def _bt_irq(event, data):
    global bt_connected, bt_conn_handle, bt_peer_addr, bt_advertising, last_bt_peer_addr, _bt_end_pairing_requested
    global _pending_save_config, _pending_ui_refresh
    if event == _IRQ_CENTRAL_CONNECT:
        bt_conn_handle, _addr_type, addr = data
        bt_connected = True
        bt_advertising = False
        bt_peer_addr = ":".join("{:02X}".format(b) for b in addr)
        print("[BLE] Central connected:", bt_peer_addr)
        if bt_peer_addr != last_bt_peer_addr:
            last_bt_peer_addr = bt_peer_addr
            _pending_save_config = True
        broadcast_status()
    elif event == _IRQ_CENTRAL_DISCONNECT:
        bt_conn_handle, _addr_type, addr = data
        bt_connected = False
        bt_conn_handle = None
        bt_peer_addr = None
        print("[BLE] Central disconnected.")
        if _bt_end_pairing_requested:
            _bt_end_pairing_requested = False
        elif last_bt_peer_addr:
            start_pairing()  # Unexpected drop -> keep advertising to auto-reconnect
    elif event == _IRQ_GATTS_WRITE:
        _conn_handle, value_handle = data
        if value_handle == _control_handle:
            _handle_ble_write()

    # Never touch the display here - it shares an SPI bus with the touchscreen IRQ;
    # let the main loop perform the actual redraw.
    _pending_ui_refresh = True

if ble_available:
    ble.irq(_bt_irq)

def retry_ble_init():
    """Re-attempts BLE activation + GATT registration; used to recover from the UI without a reboot."""
    global ble_available, _status_handle, _control_handle, _bt_retry_count
    if ble_available:
        return True
    if not _ble_try_activate():
        _bt_retry_count += 1
        return False
    try:
        _status_handle, _control_handle = _register_ctrl_service()
        ble.irq(_bt_irq)
        ble_available = True
        _bt_retry_count = 0
        print("[BLE] Reinitialized successfully.")
    except Exception as e:
        print("[BLE] Reinit GATT registration failed:", e)
        ble_available = False
        _bt_retry_count += 1
    return ble_available

# Helper: Measure temperature
def read_temperature_f():
    """A single ESP32 ADC sample is noisy enough on its own to visibly jitter
    the readout, so this averages several back-to-back raw samples before the
    (nonlinear, noise-amplifying) Steinhart-Hart conversion. get_temperature_f()
    below layers a slower exponential moving average on top of that."""
    total = 0
    valid = 0
    for _ in range(16):
        raw = temp_adc.read_u16()
        if 500 < raw < 65000:
            total += raw
            valid += 1
    if valid == 0:
        return None
    raw = total / valid

    volts = (raw / 65535.0) * 3.3
    r_fixed = 10000.0
    try:
        r_ntc = r_fixed * (volts / (3.3 - volts))
    except ZeroDivisionError:
        r_ntc = r_fixed

    beta = 3950.0
    r0 = 10000.0
    t0_kelvin = 298.15

    steinhart = math.log(r_ntc / r0) / beta
    steinhart += 1.0 / t0_kelvin
    temp_k = 1.0 / steinhart

    temp_c = temp_k - 273.15
    raw_temp_f = (temp_c * 9.0 / 5.0) + 32.0
    return raw_temp_f + temp_offset

# Even an oversampled reading still wobbles more than a slow-moving physical
# temperature should, and every caller previously took its own independent
# noisy snapshot (display, BLE status, AND the min-temp safety cutoff all
# sampled separately). This adds a second, slower smoothing stage - an
# exponential moving average refreshed on its own cadence in the main loop -
# so every consumer reads the same stable value instead of racing each other.
_smoothed_temp_f = None
_last_temp_sample_ms = 0
TEMP_SAMPLE_INTERVAL_MS = 500   # How often the EMA is refreshed from a new raw reading
TEMP_EMA_ALPHA = 0.2            # Lower = smoother/slower to react; higher = more responsive

def sample_temperature(now):
    """Call every main-loop tick; internally rate-limited to TEMP_SAMPLE_INTERVAL_MS."""
    global _smoothed_temp_f, _last_temp_sample_ms
    if ticks_diff(now, _last_temp_sample_ms) < TEMP_SAMPLE_INTERVAL_MS:
        return
    _last_temp_sample_ms = now
    raw = read_temperature_f()
    if raw is None:
        return
    if _smoothed_temp_f is None:
        _smoothed_temp_f = raw
    else:
        _smoothed_temp_f = TEMP_EMA_ALPHA * raw + (1 - TEMP_EMA_ALPHA) * _smoothed_temp_f

def get_temperature_f():
    """Returns the smoothed temperature (None until the first sample completes)."""
    return _smoothed_temp_f

# -------------------------------------------------------------------
# 3. Dedicated Touch Mappings (Primary & Secondary Screens)
# Hit-test regions are pixel-identical to the original, proven layout.
# -------------------------------------------------------------------
def handle_main_touch(x, y):
    """PRIMARY DASHBOARD SCREEN TOUCH MAP"""
    global master_on, interval_on, timer_state, timer_start, current_view

    # 1. MASTER SLIDER TOGGLE
    if 15 <= x <= 180 and 240 <= y <= 315:
        master_on = not master_on
        timer_state = "ACTIVE"
        timer_start = ticks_ms()
        draw_master_slider()
        draw_timer_box(force_bg_check=True)
        save_config()
        broadcast_status()
        print("[MAIN] Master state toggled to:", "ON" if master_on else "OFF")

    # 2. INTERVAL SWITCH TOGGLE
    elif 195 <= x <= 270 and 240 <= y <= 315:
        interval_on = not interval_on
        timer_state = "ACTIVE"
        timer_start = ticks_ms()
        draw_interval_switch()
        draw_timer_box(force_bg_check=True)
        save_config()
        broadcast_status()
        print("[MAIN] Interval state toggled to:", "ON" if interval_on else "OFF")

    # 3. INTERVAL CARD TAP -> OPEN INTERVAL SET SCREEN
    # (touch_x/touch_y are the exact swap of the card's own display rect -
    # the touch panel's native frame is a 90-degree rotation of the display's,
    # so touch_x always matches a card's display-Y span and touch_y its
    # display-X span. All three stacked cards share the same touch_y band
    # (170-239; capped below the true 245 swap value so it can't overlap the
    # master/interval-switch zones, which start at y=240); touch_x splits
    # them by their own display-Y span, with the gaps left as dead zones.)
    elif 164 <= x <= 235 and 170 <= y <= 239:
        current_view = "INTERVAL_SET"
        draw_interval_set_screen()
        print("[MAIN] Opened Interval Set Screen.")

    # 4. TEMPERATURE BOX TAP -> OPEN CALIBRATION VIEW
    elif 82 <= x <= 158 and 170 <= y <= 239:
        current_view = "TEMP_CALIBRATE"
        draw_temp_calibrate_screen()
        print("[MAIN] Opened Temperature Calibration Screen.")

    # 5. BLUETOOTH CONFIG -> OPEN PAIRING VIEW
    elif 15 <= x <= 76 and 170 <= y <= 239:
        current_view = "BT_CONFIG"
        draw_bt_config_screen()
        print("[MAIN] Opened Bluetooth Configuration Screen.")

def handle_bt_config_touch(x, y):
    """THIRD SCREEN (BLUETOOTH CONFIG) TOUCH MAP.
    Touch axes are rotated 90° vs the display, so hit-tests swap X/Y.
    """
    global current_view, _pending_reboot

    # 1. ACTION BUTTON (PAIR / STOP PAIRING / END PAIRING / RETRY / REBOOT) -> display (40, 140, 220, 40)
    if 140 <= x <= 180 and 40 <= y <= 260:
        if not ble_available:
            if _bt_retry_count >= 2:
                # Do NOT call machine.reset() here: this runs inside the touch IRQ
                # callback, and resetting mid-interrupt can corrupt the SPI/display
                # bus. Flag it instead and let the main loop perform the reset.
                print("[BLE] Repeated failures - reboot scheduled to fully reset the radio...")
                save_config()
                _pending_reboot = True
                return
            print("[BT_CONFIG] Attempting to reinitialize Bluetooth...")
            if retry_ble_init():
                start_pairing()
        elif bt_connected:
            end_pairing()
        elif bt_advertising:
            end_pairing()
        else:
            start_pairing()
        update_bt_config_display()
        draw_bt_box()

    # 2. OK BUTTON -> display (90, 190, 140, 35)
    elif 190 <= x <= 225 and 90 <= y <= 230:
        current_view = "MAIN"
        draw_ui()
        print("[BT_CONFIG] Exited Bluetooth screen back to main UI.")

def handle_calibrate_touch(x, y):
    """SECOND SCREEN (CALIBRATION) TOUCH MAP.
    Touch axes are rotated 90° vs the display, so hit-tests swap X/Y:
    touch_x matches the button's display-Y span, touch_y its display-X span.
    """
    global temp_offset, min_temp_threshold, current_view

    offset_row_y, threshold_row_y = _CAL_ROW_Y

    # 1. OFFSET PLUS (+0.5°F) BUTTON -> display (247, 68, 65, 68)
    if offset_row_y <= x <= offset_row_y + _CAL_ROW_H and _STEPPER_PLUS_X <= y <= _STEPPER_PLUS_X + _STEPPER_PLUS_W:
        temp_offset += 0.5
        update_calibrate_temp_display()
        save_config()
        broadcast_status()
        print("[CALIBRATE] Temp Offset increased to:", temp_offset)

    # 2. OFFSET MINUS (-0.5°F) BUTTON -> display (185, 68, 58, 68)
    elif offset_row_y <= x <= offset_row_y + _CAL_ROW_H and _STEPPER_MINUS_X <= y <= _STEPPER_MINUS_X + _STEPPER_MINUS_W:
        temp_offset -= 0.5
        update_calibrate_temp_display()
        save_config()
        broadcast_status()
        print("[CALIBRATE] Temp Offset decreased to:", temp_offset)

    # 3. MIN THRESHOLD PLUS (+1.0°F) BUTTON -> display (247, 140, 65, 68)
    elif threshold_row_y <= x <= threshold_row_y + _CAL_ROW_H and _STEPPER_PLUS_X <= y <= _STEPPER_PLUS_X + _STEPPER_PLUS_W:
        min_temp_threshold += 1.0
        update_calibrate_temp_display()
        save_config()
        broadcast_status()
        print("[CALIBRATE] Min Temp Threshold increased to:", min_temp_threshold)

    # 4. MIN THRESHOLD MINUS (-1.0°F) BUTTON -> display (185, 140, 58, 68)
    elif threshold_row_y <= x <= threshold_row_y + _CAL_ROW_H and _STEPPER_MINUS_X <= y <= _STEPPER_MINUS_X + _STEPPER_MINUS_W:
        min_temp_threshold -= 1.0
        update_calibrate_temp_display()
        save_config()
        broadcast_status()
        print("[CALIBRATE] Min Temp Threshold decreased to:", min_temp_threshold)

    # 5. EXIT / DONE BUTTON -> display (12, 212, 300, 24)
    elif _CAL_DONE_Y <= x <= _CAL_DONE_Y + _CAL_DONE_H and 12 <= y <= 312:
        current_view = "MAIN"
        draw_ui()
        print("[CALIBRATE] Exited calibration view back to main UI.")

def handle_interval_set_touch(x, y):
    """FOURTH SCREEN (INTERVAL SET) TOUCH MAP. Same rotated-axis layout as the
    calibration screen: touch_x matches the button's display-Y span, touch_y
    its display-X span. Each +/- tap also arms the hold-repeat state; the main
    loop advances it further if the finger stays down past HOLD_DELAY_MS.
    """
    global interval_on_minutes, interval_off_minutes, current_view
    global hold_action, hold_start_ms, hold_last_repeat_ms, hold_repeated

    now = ticks_ms()
    on_row_y, off_row_y = _IVSET_ROW_Y

    # 1. ON-TIME PLUS -> display (247, 36, 65, 78)
    if on_row_y <= x <= on_row_y + _IVSET_ROW_H and _STEPPER_PLUS_X <= y <= _STEPPER_PLUS_X + _STEPPER_PLUS_W:
        interval_on_minutes = min(99, interval_on_minutes + 1)
        hold_action, hold_start_ms, hold_last_repeat_ms, hold_repeated = "on_plus", now, now, False
        update_interval_set_display()
        save_config()
        broadcast_status()
        print("[INTERVAL_SET] On time increased to:", interval_on_minutes)

    # 2. ON-TIME MINUS -> display (185, 36, 58, 78)
    elif on_row_y <= x <= on_row_y + _IVSET_ROW_H and _STEPPER_MINUS_X <= y <= _STEPPER_MINUS_X + _STEPPER_MINUS_W:
        interval_on_minutes = max(1, interval_on_minutes - 1)
        hold_action, hold_start_ms, hold_last_repeat_ms, hold_repeated = "on_minus", now, now, False
        update_interval_set_display()
        save_config()
        broadcast_status()
        print("[INTERVAL_SET] On time decreased to:", interval_on_minutes)

    # 3. OFF-TIME PLUS -> display (247, 120, 65, 78)
    elif off_row_y <= x <= off_row_y + _IVSET_ROW_H and _STEPPER_PLUS_X <= y <= _STEPPER_PLUS_X + _STEPPER_PLUS_W:
        interval_off_minutes = min(99, interval_off_minutes + 1)
        hold_action, hold_start_ms, hold_last_repeat_ms, hold_repeated = "off_plus", now, now, False
        update_interval_set_display()
        save_config()
        broadcast_status()
        print("[INTERVAL_SET] Off time increased to:", interval_off_minutes)

    # 4. OFF-TIME MINUS -> display (185, 120, 58, 78)
    elif off_row_y <= x <= off_row_y + _IVSET_ROW_H and _STEPPER_MINUS_X <= y <= _STEPPER_MINUS_X + _STEPPER_MINUS_W:
        interval_off_minutes = max(1, interval_off_minutes - 1)
        hold_action, hold_start_ms, hold_last_repeat_ms, hold_repeated = "off_minus", now, now, False
        update_interval_set_display()
        save_config()
        broadcast_status()
        print("[INTERVAL_SET] Off time decreased to:", interval_off_minutes)

    # 5. EXIT / DONE BUTTON -> display (12, 204, 300, 28)
    elif _IVSET_DONE_Y <= x <= _IVSET_DONE_Y + _IVSET_DONE_H and 12 <= y <= 312:
        hold_action = None
        current_view = "MAIN"
        draw_ui()
        print("[INTERVAL_SET] Exited interval set view back to main UI.")

def handle_touch(x, y):
    """Interrupt entry point routing touch to the active view."""
    global last_touch_time, current_view

    now = ticks_ms()
    # 350ms Debounce filter
    if ticks_diff(now, last_touch_time) < 350:
        return
    last_touch_time = now

    print("Raw Touch Captured -> X:", x, "Y:", y, "| Current View:", current_view)

    if current_view == "MAIN":
        handle_main_touch(x, y)
    elif current_view == "TEMP_CALIBRATE":
        handle_calibrate_touch(x, y)
    elif current_view == "BT_CONFIG":
        handle_bt_config_touch(x, y)
    elif current_view == "INTERVAL_SET":
        handle_interval_set_touch(x, y)

touchscreen = Touch(touchscreen_spi, cs=Pin(33), int_pin=Pin(36), int_handler=handle_touch)

# -------------------------------------------------------------------
# 4. Interface Rendering Functions (modernized: rounded cards,
#    dark dashboard palette, larger type for key numbers)
# -------------------------------------------------------------------
def draw_ui():
    """Draws full main application interface."""
    global last_timer_bg
    last_timer_bg = None  # Force timer background to redraw completely on screen switch
    display.clear(BG)

    # Left Column: Countdown Card
    draw_timer_box(force_bg_check=True)

    # Middle Column: Bluetooth / Temp / Interval cards are given equal 76px
    # heights (previously 65/57/112 - Interval dwarfed the other two) with a
    # matching hairline divider at each boundary, so the column reads as three
    # evenly-weighted tiles instead of one dominant block and two small ones.
    draw_bt_box()
    display.fill_rectangle(170, 78, 75, 2, CARD_BORDER)

    # Middle Column: Temperature Display Card
    card(170, 82, 75, 76, CARD)
    text_small(178, 91, "CURRENT", TEXT_MUTED, CARD)
    text_small(188, 105, "TEMP", TEXT_MUTED, CARD)
    update_temp_display()

    display.fill_rectangle(170, 160, 75, 2, CARD_BORDER)

    # Middle Column: Interval Control Card - tinted BLUE_DIM (instead of the
    # plain CARD gray the Temp tile uses) so it visibly reads as its own
    # tappable control. The whole card is the tap target for the Interval Set
    # screen; the configured on/off minutes show in the TIMER card once
    # interval mode is on.
    card(170, 164, 75, 76, BLUE_DIM)
    text_small(178, 172, "INTERVAL", TEXT, BLUE_DIM)
    text_small_centered(207, 188, "SET >", TEXT_MUTED, BLUE_DIM)

    # Right Column: Master Toggle Slider & Interval Mode Switch
    draw_master_slider()
    draw_interval_switch()

# Calibration screen layout - shares the same big MINUS/PLUS column geometry
# (_STEPPER_*) as the Interval Set screen, filling the panel the same way
# instead of the small 35x30 pill buttons this screen used before.
_CAL_ROW_Y = (68, 140)      # (Offset row top, Min Temp row top)
_CAL_ROW_H = 68
_CAL_READOUT_Y, _CAL_READOUT_H = 36, 28
_CAL_DONE_Y, _CAL_DONE_H = 212, 24

def draw_temp_calibrate_screen():
    """Draws full-screen temperature calibration interface ONCE."""
    display.clear(BG)
    card(4, 4, 312, 232, CARD, r=14)

    text_big_centered(160, 8, "CALIBRATION", TEXT, CARD)

    for row_y in _CAL_ROW_Y:
        row_cy = row_y + _CAL_ROW_H // 2

        fill_round_rect(_STEPPER_MINUS_X, row_y, _STEPPER_MINUS_W, _CAL_ROW_H, 10, CARD_BORDER)
        draw_minus_glyph(_STEPPER_MINUS_X + _STEPPER_MINUS_W // 2, row_cy, 32, 8, TEXT)

        fill_round_rect(_STEPPER_PLUS_X, row_y, _STEPPER_PLUS_W, _CAL_ROW_H, 10, BLUE)
        draw_plus_glyph(_STEPPER_PLUS_X + _STEPPER_PLUS_W // 2, row_cy, 32, 8, WHITE)

    # DONE / EXIT button
    fill_round_rect(12, _CAL_DONE_Y, 300, _CAL_DONE_H, 10, GREEN)
    text_small_centered(162, _CAL_DONE_Y + 8, "DONE", BLACK, GREEN)

    update_calibrate_temp_display()

def update_calibrate_temp_display():
    """Updates only the dynamic temperature, offset & threshold text (No flicker)."""
    if current_view != "TEMP_CALIBRATE":
        return

    temp_f = get_temperature_f()
    temp_str = "{:.1f} F".format(temp_f) if temp_f is not None else "ERR"

    # Refresh live temp readout bar
    fill_round_rect(14, _CAL_READOUT_Y, 298, _CAL_READOUT_H, 6, BLUE_DIM)
    text_big_centered(163, _CAL_READOUT_Y + 2, temp_str, TEXT, BLUE_DIM)

    offset_row_y, threshold_row_y = _CAL_ROW_Y

    # Refresh Offset label + big value
    display.fill_rectangle(14, offset_row_y, 165, _CAL_ROW_H, CARD)
    text_small(14, offset_row_y + 8, "OFFSET", TEXT_MUTED, CARD)
    text_big(14, offset_row_y + 28, "{:+0.1f} F".format(temp_offset), TEXT, CARD)

    # Refresh Min Temp Threshold label + big value
    display.fill_rectangle(14, threshold_row_y, 165, _CAL_ROW_H, CARD)
    text_small(14, threshold_row_y + 8, "MIN TEMP", TEXT_MUTED, CARD)
    text_big(14, threshold_row_y + 28, "{:.1f} F".format(min_temp_threshold), TEXT, CARD)

# Interval Set screen layout - fills nearly the whole 320x240 panel instead
# of the small corner cluster the calibration-screen template used. Two big
# rows (ON / OFF), each a full-height MINUS/PLUS pair; touch zones below are
# the exact swap of these display rects (touch_x = display_y, touch_y =
# display_x), the same convention proven on the other screens.
_IVSET_ROW_Y = (36, 120)          # (ON row top, OFF row top)
_IVSET_ROW_H = 78
_STEPPER_MINUS_X, _STEPPER_MINUS_W = 185, 58
_STEPPER_PLUS_X, _STEPPER_PLUS_W = 247, 65
_IVSET_DONE_Y, _IVSET_DONE_H = 204, 28

def draw_interval_set_screen():
    """Draws full-screen Interval Set interface ONCE (on/off minutes, independently adjustable)."""
    display.clear(BG)
    card(4, 4, 312, 232, CARD, r=14)

    text_big_centered(160, 8, "INTERVAL", TEXT, CARD)

    for row_y in _IVSET_ROW_Y:
        row_cy = row_y + _IVSET_ROW_H // 2

        fill_round_rect(_STEPPER_MINUS_X, row_y, _STEPPER_MINUS_W, _IVSET_ROW_H, 10, CARD_BORDER)
        draw_minus_glyph(_STEPPER_MINUS_X + _STEPPER_MINUS_W // 2, row_cy, 32, 8, TEXT)

        fill_round_rect(_STEPPER_PLUS_X, row_y, _STEPPER_PLUS_W, _IVSET_ROW_H, 10, BLUE)
        draw_plus_glyph(_STEPPER_PLUS_X + _STEPPER_PLUS_W // 2, row_cy, 32, 8, WHITE)

    # DONE / EXIT button
    fill_round_rect(12, _IVSET_DONE_Y, 300, _IVSET_DONE_H, 10, GREEN)
    text_small_centered(162, _IVSET_DONE_Y + 10, "DONE", BLACK, GREEN)

    update_interval_set_display()

def update_interval_set_display():
    """Updates only the dynamic on-time/off-time labels & values (No flicker)."""
    if current_view != "INTERVAL_SET":
        return

    on_row_y, off_row_y = _IVSET_ROW_Y

    # Refresh On-Time label + big value
    display.fill_rectangle(14, on_row_y, 165, _IVSET_ROW_H, CARD)
    text_small(14, on_row_y + 8, "ON TIME", TEXT_MUTED, CARD)
    text_big(14, on_row_y + 28, "{:2d} MIN".format(interval_on_minutes), TEXT, CARD)

    # Refresh Off-Time label + big value
    display.fill_rectangle(14, off_row_y, 165, _IVSET_ROW_H, CARD)
    text_small(14, off_row_y + 8, "OFF TIME", TEXT_MUTED, CARD)
    text_big(14, off_row_y + 28, "{:2d} MIN".format(interval_off_minutes), TEXT, CARD)

def draw_bt_box():
    """Main dashboard's small Bluetooth status card."""
    if current_view != "MAIN":
        return
    if not ble_available:
        bg = RED_DIM
        text1, text2 = "BLUETOOTH", "TAP RETRY"
    elif bt_connected:
        bg = GREEN_DIM
        text1, text2 = "PAIRED", "ACTIVE"
    elif bt_advertising:
        bg = BLUE_DIM
        text1, text2 = "PAIRING", "WAITING"
    else:
        bg = CARD
        text1, text2 = "BLUETOOTH", "CONFIG"
    card(170, 0, 75, 76, bg)
    text_small(175, 22, text1, TEXT, bg)
    text_small(185, 40, text2, TEXT_MUTED, bg)

def draw_bt_config_screen():
    """Draws full-screen Bluetooth pairing interface ONCE."""
    display.clear(BG)
    card(10, 10, 300, 220, CARD, r=14)

    text_big_centered(160, 24, "BLUETOOTH", TEXT, CARD)

    # Device name (static, doesn't change)
    text_small(35, 55, "Device: {}".format(BLE_NAME), TEXT_MUTED, CARD)

    # OK pill button
    fill_round_rect(90, 190, 140, 35, 10, GREEN)
    text_small_centered(160, 202, "OK", BLACK, GREEN)

    update_bt_config_display()

def update_bt_config_display():
    """Updates only the dynamic status, peer info & action button (No flicker)."""
    if current_view != "BT_CONFIG":
        return

    # Refresh Status Text
    display.fill_rectangle(30, 70, 260, 20, CARD)
    if not ble_available:
        status_str = "Status: Bluetooth Unavailable"
    elif bt_connected:
        status_str = "Status: Connected"
    elif bt_advertising:
        status_str = "Status: Pairing (Advertising)"
    else:
        status_str = "Status: Not Connected"
    text_small(35, 75, status_str, TEXT_MUTED, CARD)

    # Refresh Peer Info Text (only populated once paired)
    display.fill_rectangle(30, 95, 260, 20, CARD)
    if not ble_available:
        msg = "Tap to reboot device" if _bt_retry_count >= 2 else "Tap button below to retry"
        text_small(35, 100, msg, TEXT_MUTED, CARD)
    elif bt_connected and bt_peer_addr:
        text_small(35, 100, "Peer Addr: {}".format(bt_peer_addr), TEXT_MUTED, CARD)

    # Refresh Action pill Button (PAIR / STOP PAIRING / END PAIRING / RETRY / REBOOT)
    display.fill_rectangle(30, 135, 260, 50, CARD)
    if not ble_available:
        action_label = "REBOOT" if _bt_retry_count >= 2 else "RETRY"
        action_color = AMBER
    elif bt_connected:
        action_label = "END PAIRING"
        action_color = RED
    elif bt_advertising:
        action_label = "STOP PAIRING"
        action_color = RED
    else:
        action_label = "PAIR"
        action_color = BLUE
    fill_round_rect(40, 140, 220, 40, 12, action_color)
    text_small_centered(150, 156, action_label, WHITE, action_color)

def update_temp_display():
    """Updates the main screen small temp readout."""
    if current_view != "MAIN":
        return
    temp_f = get_temperature_f()
    display.fill_rectangle(172, 126, 71, 20, CARD)
    msg = "{:.1f}F".format(temp_f) if temp_f is not None else "ERR"
    text_small(178, 132, msg, TEXT, CARD)

def draw_master_slider():
    bg_color = GREEN_DIM if master_on else RED_DIM
    dot_color = GREEN if master_on else RED
    card(245, 0, 75, 180, bg_color)
    text_small(272, 12, "OFF", TEXT_MUTED, bg_color)
    text_small(275, 155, "ON", TEXT_MUTED, bg_color)
    # Track
    fill_round_rect(268, 35, 24, 110, 12, CARD)
    circle_y = 118 if master_on else 62
    display.fill_circle(280, circle_y, 15, dot_color)

def draw_interval_switch():
    bg_color = GREEN_DIM if interval_on else CARD
    card(245, 180, 75, 60, bg_color)
    text_small(250, 186, "INTERVAL", TEXT_MUTED, bg_color)
    fill_round_rect(250, 210, 65, 20, 10, CARD_BORDER if not interval_on else GREEN)
    handle_x = 252 if interval_on else 293
    display.fill_circle(handle_x + 8, 220, 8, WHITE)

def draw_timer_box(force_bg_check=False):
    """Redraws the timer card. The countdown uses the slow 12x24 font, so it's
    only repainted when the displayed string actually changes (once a second)
    rather than on every 100ms loop tick - same for the static status label,
    which only needs repainting when the card's background state changes.
    """
    global last_timer_bg, last_time_str
    if current_view != "MAIN":
        return

    if not master_on:
        bg = RED_DIM
        accent = RED
    elif temp_pause_active:
        bg = AMBER_DIM
        accent = AMBER
    elif not interval_on:
        bg = GREEN_DIM
        accent = GREEN
    else:
        bg = GREEN_DIM if timer_state == "ACTIVE" else RED_DIM
        accent = GREEN if timer_state == "ACTIVE" else RED

    bg_changed = force_bg_check or bg != last_timer_bg
    if bg_changed:
        card(0, 0, 170, 240, bg, r=14)
        # Status dot: quick at-a-glance state indicator
        display.fill_circle(20, 24, 6, accent)
        text_small(35, 20, "TIMER", TEXT_MUTED, bg)
        last_timer_bg = bg

    now = ticks_ms()
    elapsed_sec = ticks_diff(now, timer_start) // 1000

    if master_on and temp_pause_active:
        label_lines = ("MIN TEMP REACHED", "Pump Paused")
        time_str = "PAUSED"
    elif master_on and not interval_on:
        m, s = divmod(elapsed_sec, 60)
        time_str = "{:02d}:{:02d}".format(m, s)
        label_lines = ("ELAPSED TIME",)
    elif master_on and interval_on:
        # ACTIVE and REST phases run for their own independently-set duration.
        tot_sec = (interval_on_minutes if timer_state == "ACTIVE" else interval_off_minutes) * 60
        rem_sec = max(0, tot_sec - elapsed_sec)
        m, s = divmod(rem_sec, 60)
        time_str = "{:02d}:{:02d}".format(m, s)
        label_lines = ("MINUTES REMAINING",)
    else:
        time_str = "--:--"
        label_lines = ("SYSTEM INACTIVE",)

    if bg_changed:
        if len(label_lines) == 2:
            text_small_centered(85, 90, label_lines[0], TEXT, bg)
            text_small_centered(85, 105, label_lines[1], TEXT_MUTED, bg)
        else:
            text_small_centered(85, 95, label_lines[0], TEXT_MUTED, bg)

    if bg_changed or time_str != last_time_str:
        text_big_centered(85, 115, time_str, TEXT, bg)
        last_time_str = time_str

    # Interval on/off summary - only shown while interval mode is switched on
    if bg_changed and interval_on:
        summary = "ON {}m / OFF {}m".format(interval_on_minutes, interval_off_minutes)
        text_small_centered(85, 150, summary, TEXT_MUTED, bg)

# -------------------------------------------------------------------
# 5. Startup & Main Loop Execution
# -------------------------------------------------------------------
load_config()            # Restore persistent state from flash
sample_temperature(ticks_ms())  # Prime the smoothed reading so the first draw doesn't show "ERR"
draw_ui()                # Initial GUI Draw
last_temp_read = ticks_ms()
last_ldr_read = ticks_ms()

if last_bt_peer_addr:
    print("[BLE] Remembered peer", last_bt_peer_addr, "- advertising to auto-reconnect.")
    start_pairing()
    draw_bt_box()

try:
    while True:
        now = ticks_ms()

        # Refresh the smoothed temperature reading (self-throttled to
        # TEMP_SAMPLE_INTERVAL_MS internally) - decoupled from the slower
        # display/BLE refresh below so the min-temp safety check always sees
        # a reasonably fresh, but still stable, value.
        sample_temperature(now)

        # 0. Deferred reboot requested from the BT_CONFIG screen (safe context, not an IRQ)
        if _pending_reboot:
            relay.value(0)
            sleep(0.2)
            reset()

        # 0.5 Deferred flash save / display redraw requested from the BLE IRQ
        # (kept out of the IRQ itself since it shares the SPI bus with the touchscreen)
        if _pending_save_config:
            _pending_save_config = False
            save_config()
        if _pending_ui_refresh:
            _pending_ui_refresh = False
            if current_view == "MAIN":
                draw_master_slider()
                draw_interval_switch()
                draw_timer_box(force_bg_check=True)
                draw_bt_box()
            elif current_view == "TEMP_CALIBRATE":
                update_calibrate_temp_display()
            elif current_view == "BT_CONFIG":
                update_bt_config_display()
                draw_bt_box()
            elif current_view == "INTERVAL_SET":
                update_interval_set_display()

        # 0.75 Press-and-hold auto-repeat for the Interval Set screen's +/- buttons.
        # Armed by handle_interval_set_touch() on touch-down; advanced here by
        # polling the touch IRQ pin's level rather than by further touch events,
        # since the driver only reports one x/y per touch-down (see xpt2046.py).
        if hold_action is not None:
            if touchscreen.int_pin.value():  # pin goes high again on release
                if hold_repeated:
                    save_config()
                    broadcast_status()
                hold_action = None
                hold_repeated = False
            else:
                held_ms = ticks_diff(now, hold_start_ms)
                if held_ms >= HOLD_DELAY_MS and ticks_diff(now, hold_last_repeat_ms) >= HOLD_REPEAT_MS:
                    if hold_action == "on_plus":
                        interval_on_minutes = min(99, interval_on_minutes + 1)
                    elif hold_action == "on_minus":
                        interval_on_minutes = max(1, interval_on_minutes - 1)
                    elif hold_action == "off_plus":
                        interval_off_minutes = min(99, interval_off_minutes + 1)
                    elif hold_action == "off_minus":
                        interval_off_minutes = max(1, interval_off_minutes - 1)
                    hold_last_repeat_ms = now
                    hold_repeated = True
                    update_interval_set_display()

        # 1. Non-Blocking Light Sensor Auto-Dimming (Every 500ms)
        if ticks_diff(now, last_ldr_read) > 500:
            ldr_val = lightsensor.read_u16()
            if ldr_val > 18000:
                backlight.duty_u16(100)      # Min Brightness
            elif 1000 <= ldr_val <= 18000:
                backlight.duty_u16(32768)    # Medium Brightness (50%)
            else:
                backlight.duty_u16(65000)    # Max Brightness (100%)
            last_ldr_read = now

        # 2. Refresh Temperature Reading every 2 seconds
        if ticks_diff(now, last_temp_read) > 2000:
            if current_view == "MAIN":
                update_temp_display()
            elif current_view == "TEMP_CALIBRATE":
                update_calibrate_temp_display()
            broadcast_status()
            last_temp_read = now

        # 3. Minimum Temperature Safety Check (overrides interval timer)
        if master_on:
            current_temp = get_temperature_f()
            if current_temp is not None:
                if not temp_pause_active and current_temp <= min_temp_threshold:
                    temp_pause_active = True
                    pause_start_time = now
                    if current_view == "MAIN":
                        draw_timer_box(force_bg_check=True)
                    broadcast_status()
                elif temp_pause_active and current_temp >= (min_temp_threshold + 1.0):
                    # Shift timer_start forward by the paused duration so the
                    # interval countdown resumes where it left off.
                    timer_start = ticks_add(timer_start, ticks_diff(now, pause_start_time))
                    temp_pause_active = False
                    if current_view == "MAIN":
                        draw_timer_box(force_bg_check=True)
                    broadcast_status()
        elif temp_pause_active:
            temp_pause_active = False

        # 4. Hardware Relay Logic State Machine
        if master_on and temp_pause_active:
            relay.value(0)          # Min temp reached -> Relay OFF until temp recovers
        elif master_on:
            if interval_on:
                elapsed_sec = ticks_diff(now, timer_start) // 1000

                if timer_state == "ACTIVE":
                    relay.value(1)  # Relay ON
                    if elapsed_sec >= interval_on_minutes * 60:
                        timer_state = "REST"
                        timer_start = ticks_ms()
                        if current_view == "MAIN":
                            draw_timer_box(force_bg_check=True)
                        broadcast_status()
                elif timer_state == "REST":
                    relay.value(0)  # Relay OFF
                    if elapsed_sec >= interval_off_minutes * 60:
                        timer_state = "ACTIVE"
                        timer_start = ticks_ms()
                        if current_view == "MAIN":
                            draw_timer_box(force_bg_check=True)
                        broadcast_status()
            else:
                relay.value(1)      # Master ON, Interval OFF -> Keep relay ON
        else:
            relay.value(0)          # Master OFF -> Relay OFF

        # 5. Update Timer Display Text (if on main view)
        if current_view == "MAIN":
            draw_timer_box()

        sleep(0.1)

except KeyboardInterrupt:
    relay.value(0)
    print("Application Terminated Safely")
