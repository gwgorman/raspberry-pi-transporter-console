#!/usr/bin/env python3
"""Touch-first Star Trek transporter kiosk for Raspberry Pi."""

import math
import io
import os
import re
import subprocess
import sys
import threading
import time
from collections import deque

import pygame

try:
    from office_telemetry import TelemetryService
except ImportError:
    TelemetryService = None

try:
    from yolink_telemetry import YoLinkService
except ImportError:
    YoLinkService = None

try:
    from flight_telemetry import FlightTelemetryService
except ImportError:
    FlightTelemetryService = None

try:
    from satellite_telemetry import SatelliteTelemetryService
except ImportError:
    SatelliteTelemetryService = None

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

MODE = "both"
TEST_MODE = "--test" in sys.argv
WINDOWED = "--windowed" in sys.argv
OFFICE_IDLE_SECONDS = 120.0
PARTY_RETURN_SECONDS = 20.0
VENUE_LINK_DEBOUNCE_SECONDS = 10.0
for arg in sys.argv:
    if arg.startswith("--mode="):
        MODE = arg.split("=", 1)[1].lower()
    elif arg.startswith("--office-timeout="):
        try:
            OFFICE_IDLE_SECONDS = max(5.0, float(arg.split("=", 1)[1]))
        except ValueError:
            raise SystemExit("--office-timeout must be a number of seconds")
if MODE not in ("transporter", "selfdestruct", "both"):
    raise SystemExit("Use --mode=transporter, --mode=selfdestruct, or --mode=both")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(os.path.expanduser("~"), ".config", "startrek-console.json")
GREEN_PIN, RED_PIN = 17, 27
TRANSPORT_DURATION = 10.0
running = True
state_lock = threading.RLock()
any_sequence_active = self_destruct_active = abort_triggered = False
abort_count = 0
ui_state, status_detail = "READY", "PATTERN BUFFER STANDING BY"
countdown_value = None
transport_progress = flash_until = arm_until = 0.0
shutdown_hold_started = shutdown_confirm_until = party_return_hold_started = 0.0
shutdown_pending = False
power_action_pending = None
afg_dragging = False
afg_value = 72
afg_pending = None
afg_event = threading.Event()
last_activity = time.monotonic()

def load_console_preferences():
    try:
        import json
        with open(CONFIG_PATH, encoding="utf-8") as config_file:
            config = json.load(config_file)
            mode = config.get("display_mode", "AUTO")
            page = config.get("status_page", "NETWORK")
            venue = config.get("venue_override", "AUTO")
            return (mode if mode in ("TRANSPORTER", "SHIP STATUS", "AIR TRAFFIC",
                                     "SPACE TRAFFIC", "AUTO") else "AUTO",
                    page if page in ("NETWORK", "ENVIRONMENT", "HOUSE SYSTEMS", "POWER CELLS") else "NETWORK",
                    venue if venue in ("AUTO", "PARTY", "OFFICE") else "AUTO")
    except (OSError, ValueError, TypeError):
        return "AUTO", "NETWORK", "AUTO"

display_mode, status_page, venue_override = load_console_preferences()
ethernet_link_connected = ethernet_raw_link = False
ethernet_raw_since = venue_link_poll = time.monotonic()
telemetry = TelemetryService() if TelemetryService else None
yolink = YoLinkService() if YoLinkService else None
flight_telemetry = FlightTelemetryService() if FlightTelemetryService else None
satellite_telemetry = SatelliteTelemetryService() if SatelliteTelemetryService else None
environment_sensor_page = 0
house_system_page = 0
battery_status_page = 0
radar_range_nm = 80
radar_weather_enabled = False
selected_aircraft = None
weather_tile_cache = {}
radar_target_hitboxes = {}
selected_satellite = None
satellite_strip_page = 0
satellite_target_hitboxes = {}
pressure_trace = deque(maxlen=120)
last_pressure_trace_sample = 0.0

BLACK, NAVY = (4, 7, 12), (8, 18, 30)
PANEL, PANEL_2 = (14, 31, 45), (18, 42, 58)
CYAN, BLUE, GREEN = (80, 235, 255), (47, 127, 211), (88, 255, 151)
AMBER, ORANGE, RED = (255, 190, 61), (255, 119, 46), (255, 55, 62)
WHITE, MUTED = (223, 243, 247), (108, 151, 164)
INSTRUMENT, BEZEL, CREAM = (3, 10, 12), (104, 111, 105), (235, 226, 190)

pygame.init()
try:
    pygame.mixer.init(frequency=22050, size=-16, channels=1, buffer=512)
except pygame.error as exc:
    print(f"Audio unavailable: {exc}")
info = pygame.display.Info()
native_size = (info.current_w or 1920, info.current_h or 1080)
flags = pygame.DOUBLEBUF
screen = pygame.display.set_mode((1280, 720), flags | pygame.RESIZABLE) if WINDOWED else pygame.display.set_mode(native_size, flags | pygame.FULLSCREEN)
pygame.display.set_caption("USS ENTERPRISE TRANSPORTER CONTROL")
pygame.mouse.set_visible(TEST_MODE or WINDOWED)
clock = pygame.time.Clock()

def read_system_volume(default=72):
    """Read the real PipeWire default-sink volume, clamped to 0–100%."""
    try:
        result = subprocess.run(
            ["wpctl", "get-volume", "@DEFAULT_AUDIO_SINK@"],
            check=True, capture_output=True, text=True, timeout=2)
        match = re.search(r"Volume:\s*([0-9.]+)", result.stdout)
        if match:
            return max(0, min(100, round(float(match.group(1)) * 100)))
    except (FileNotFoundError, subprocess.SubprocessError, ValueError) as exc:
        print(f"AFG read unavailable: {exc}")
    return default

afg_value = read_system_volume()

def system_volume_worker():
    """Coalesce slider motion and apply it to the real PipeWire output."""
    global afg_pending
    while running:
        afg_event.wait()
        afg_event.clear()
        with state_lock:
            value, afg_pending = afg_pending, None
        if value is None:
            continue
        try:
            subprocess.run(
                ["wpctl", "set-volume", "--limit", "1.0",
                 "@DEFAULT_AUDIO_SINK@", f"{value}%"],
                check=True, timeout=2)
            subprocess.run(
                ["wpctl", "set-mute", "@DEFAULT_AUDIO_SINK@",
                 "1" if value == 0 else "0"],
                check=True, timeout=2)
        except (FileNotFoundError, subprocess.SubprocessError) as exc:
            print(f"AFG write failed: {exc}")

def set_system_volume(value):
    """Update the AFG readout immediately and queue one system-volume write."""
    global afg_value, afg_pending
    value = max(0, min(100, int(round(value))))
    with state_lock:
        afg_value = afg_pending = value
    afg_event.set()

def mark_activity():
    global last_activity
    last_activity = time.monotonic()

def save_display_mode():
    try:
        import json
        os.makedirs(os.path.dirname(CONFIG_PATH), exist_ok=True)
        temporary = CONFIG_PATH + ".tmp"
        with open(temporary, "w", encoding="utf-8") as config_file:
            json.dump({"display_mode": display_mode, "status_page": status_page,
                       "venue_override": venue_override}, config_file)
        os.replace(temporary, CONFIG_PATH)
    except OSError as exc:
        print(f"Display mode was not persisted: {exc}")

def select_display_mode(mode):
    global display_mode
    if mode not in ("TRANSPORTER", "SHIP STATUS", "AIR TRAFFIC", "SPACE TRAFFIC", "AUTO"):
        return
    display_mode = mode
    mark_activity()
    save_display_mode()

def party_return_home(now):
    """Restore a predictable guest-ready console without interrupting effects."""
    global display_mode, status_page, environment_sensor_page, house_system_page
    global battery_status_page, radar_range_nm, radar_weather_enabled, selected_aircraft
    global selected_satellite, satellite_strip_page, arm_until
    if (any_sequence_active or ui_state != "READY" or shutdown_confirm_until > now or
            shutdown_pending or countdown_value is not None):
        return False
    display_mode, status_page = "TRANSPORTER", "NETWORK"
    environment_sensor_page = house_system_page = battery_status_page = 0
    radar_range_nm, radar_weather_enabled = 80, False
    selected_aircraft = selected_satellite = None
    satellite_strip_page = 0
    arm_until = 0
    if flight_telemetry:
        flight_telemetry.set_weather_enabled(False)
    save_display_mode()
    return True

def wired_carrier_present():
    """Return true when a conventional wired interface has physical carrier."""
    try:
        interfaces = os.listdir("/sys/class/net")
    except OSError:
        return False
    for interface in interfaces:
        if not interface.startswith(("eth", "en")):
            continue
        try:
            with open(f"/sys/class/net/{interface}/carrier", encoding="ascii") as carrier:
                if carrier.read().strip() == "1":
                    return True
        except OSError:
            continue
    return False

def update_venue_link(now):
    """Debounce cable changes so a brief network flap cannot change kiosk mode."""
    global ethernet_link_connected, ethernet_raw_link, ethernet_raw_since, venue_link_poll
    if now - venue_link_poll < 2.0:
        return
    venue_link_poll = now
    raw = wired_carrier_present()
    if raw != ethernet_raw_link:
        ethernet_raw_link = raw
        ethernet_raw_since = now
    elif raw != ethernet_link_connected and now - ethernet_raw_since >= VENUE_LINK_DEBOUNCE_SECONDS:
        ethernet_link_connected = raw

def party_return_armed():
    if venue_override == "PARTY":
        return True
    if venue_override == "OFFICE":
        return False
    return not ethernet_link_connected

def ship_status_active(now):
    if (any_sequence_active or ui_state != "READY" or shutdown_confirm_until > now or
            shutdown_pending or countdown_value is not None):
        return False
    if display_mode == "SHIP STATUS":
        return True
    return display_mode == "AUTO" and now - last_activity >= OFFICE_IDLE_SECONDS

def special_display_active(now):
    return (display_mode in ("AIR TRAFFIC", "SPACE TRAFFIC") and
            not any_sequence_active and ui_state == "READY" and
            shutdown_confirm_until <= now and not shutdown_pending and
            countdown_value is None)

VOICE_CHANNEL = pygame.mixer.Channel(0) if pygame.mixer.get_init() else None
SIREN_CHANNEL = pygame.mixer.Channel(1) if pygame.mixer.get_init() else None
CHIME_CHANNEL = pygame.mixer.Channel(2) if pygame.mixer.get_init() else None
BOOM_CHANNEL = pygame.mixer.Channel(3) if pygame.mixer.get_init() else None
if VOICE_CHANNEL:
    VOICE_CHANNEL.set_volume(1.0)
if SIREN_CHANNEL:
    SIREN_CHANNEL.set_volume(0.4)
if CHIME_CHANNEL:
    CHIME_CHANNEL.set_volume(1.0)
if BOOM_CHANNEL:
    BOOM_CHANNEL.set_volume(1.0)

def make_sad_mac_chime():
    """Synthesize an original short two-tone retro computer error bonk."""
    if not pygame.mixer.get_init():
        return None
    sample_rate, duration = 22050, .62
    samples = bytearray()
    for index in range(int(sample_rate * duration)):
        t = index / sample_rate
        frequency = 392.0 if t < .23 else 293.66
        local_t = t if t < .23 else t - .23
        decay = math.exp(-local_t * (8.5 if t < .23 else 6.5))
        # A quiet fundamental plus its second harmonic gives a small speaker-like thunk.
        wave = math.sin(2 * math.pi * frequency * t) * .78
        wave += math.sin(2 * math.pi * frequency * 2 * t) * .22
        value = int(max(-1.0, min(1.0, wave * decay)) * 15000)
        samples.extend(value.to_bytes(2, "little", signed=True))
    return pygame.mixer.Sound(buffer=bytes(samples))

def make_core_breach_thump():
    """Synthesize a short low impact to sit underneath the spoken kaboom."""
    if not pygame.mixer.get_init():
        return None
    sample_rate, duration = 22050, 1.15
    samples = bytearray()
    for index in range(int(sample_rate * duration)):
        t = index / sample_rate
        frequency = 72.0 - 34.0 * (t / duration)
        envelope = min(1.0, t * 35.0) * math.exp(-t * 3.8)
        rumble = math.sin(2 * math.pi * frequency * t)
        rumble += .38 * math.sin(2 * math.pi * frequency * .47 * t)
        value = int(max(-1.0, min(1.0, rumble * envelope)) * 19000)
        samples.extend(value.to_bytes(2, "little", signed=True))
    return pygame.mixer.Sound(buffer=bytes(samples))

def asset(name):
    return os.path.join(BASE_DIR, name)

def load_sound(name):
    path = asset(name)
    if not pygame.mixer.get_init() or not os.path.exists(path):
        print(f"Warning: {name} missing or mixer unavailable")
        return None
    try:
        return pygame.mixer.Sound(path)
    except pygame.error as exc:
        print(f"Failed to load {name}: {exc}")
        return None

transporter_sound = load_sound("transporter.wav")
siren_sound = load_sound("siren.wav") if MODE in ("selfdestruct", "both") else None
sad_mac_chime = make_sad_mac_chime()
core_breach_thump = make_core_breach_thump()
voice_sounds = {}
for name in os.listdir(BASE_DIR):
    if name.startswith("speak_") and name.endswith(".wav"):
        sound = load_sound(name)
        if sound:
            voice_sounds[name[6:-4]] = sound
siren_stop_event = threading.Event()

def play_voice_wait(key):
    sound = voice_sounds.get(key)
    if not sound or not VOICE_CHANNEL:
        print(f"Missing: speak_{key}.wav")
        return 0.0
    VOICE_CHANNEL.play(sound)
    while VOICE_CHANNEL.get_busy():
        time.sleep(0.05)
    return sound.get_length()

def play_siren_loop():
    if siren_sound and SIREN_CHANNEL:
        SIREN_CHANNEL.play(siren_sound, loops=-1)
        siren_stop_event.wait()
        SIREN_CHANNEL.fadeout(500)

def stop_siren():
    siren_stop_event.set()

def set_ui(state, detail, countdown=None, progress=None):
    global ui_state, status_detail, countdown_value, transport_progress
    with state_lock:
        ui_state, status_detail, countdown_value = state, detail, countdown
        if progress is not None:
            transport_progress = progress

def transport_phase(progress):
    """Return one coordinated phase name for every transport indication."""
    if progress < .12:
        return "TARGET ACQUISITION"
    if progress < .28:
        return "CONFINEMENT BEAM LOCKED"
    if progress < .52:
        return "MOLECULAR DEMATERIALIZATION"
    if progress < .78:
        return "PATTERN TRANSFER IN PROGRESS"
    if progress < .94:
        return "MOLECULAR REMATERIALIZATION"
    return "PATTERN COHERENCE VERIFIED"

def play_transporter_task():
    global any_sequence_active
    with state_lock:
        if MODE not in ("transporter", "both") or any_sequence_active:
            return
        any_sequence_active = True
    set_ui("ENERGIZING", "MOLECULAR DECOMPOSITION IN PROGRESS", progress=0)
    transport_channel = None
    if transporter_sound:
        transport_channel = transporter_sound.play()
    sequence_started = time.monotonic()
    while True:
        elapsed = time.monotonic() - sequence_started
        progress = min(1.0, elapsed / TRANSPORT_DURATION)
        set_ui("ENERGIZING", transport_phase(progress), progress=progress)
        if progress >= 1.0:
            break
        time.sleep(1 / 30)
    # Normal 10.8-second assets contain their own fade. Protect against a much
    # longer replacement WAV continuing after the visual sequence has ended.
    if (transport_channel and transport_channel.get_busy() and
            transporter_sound.get_length() > TRANSPORT_DURATION + 1.0):
        transport_channel.fadeout(800)
    time.sleep(0.6)
    set_ui("COMPLETE", "TRANSPORT COMPLETE — BUFFER PURGED", progress=1)
    time.sleep(2.2)
    with state_lock:
        any_sequence_active = False
    set_ui("READY", "PATTERN BUFFER STANDING BY", progress=0)

def self_destruct_task():
    global any_sequence_active, self_destruct_active, abort_count, abort_triggered, flash_until
    with state_lock:
        if MODE not in ("selfdestruct", "both") or any_sequence_active:
            return
        any_sequence_active = self_destruct_active = True
        abort_count, abort_triggered = 0, False
        siren_stop_event.clear()
    set_ui("DESTRUCT", "SELF DESTRUCT ACTIVE — PRESS ABORT 5 TIMES")
    if siren_sound:
        threading.Thread(target=play_siren_loop, daemon=True).start()
    play_voice_wait("started")
    for n in range(10, 0, -1):
        with state_lock:
            if abort_triggered:
                break
        set_ui("DESTRUCT", "SELF DESTRUCT SEQUENCE ACTIVE", countdown=n)
        voice_duration = play_voice_wait(str(n))
        time.sleep(max(0, 1.0 - voice_duration))
    with state_lock:
        was_aborted = abort_triggered
    if was_aborted:
        play_voice_wait("aborted")
        stop_siren()
        set_ui("ABORTED", "SELF DESTRUCT SEQUENCE ABORTED")
        time.sleep(2.0)
    else:
        if core_breach_thump and BOOM_CHANNEL:
            BOOM_CHANNEL.play(core_breach_thump)
        play_voice_wait("kaboom")
        stop_siren()
        flash_until = time.monotonic() + 2.0
        set_ui("EXPLOSION", "CATASTROPHIC CORE BREACH", countdown=None)
        time.sleep(2.0)
        if sad_mac_chime and CHIME_CHANNEL:
            CHIME_CHANNEL.play(sad_mac_chime)
        set_ui("SAD_MAC", "SYSTEM ERROR", countdown=None)
        time.sleep(5.0)
    with state_lock:
        self_destruct_active = any_sequence_active = False
        abort_count = 0
    set_ui("READY", "PATTERN BUFFER STANDING BY", progress=0)

def trigger_transporter():
    mark_activity()
    threading.Thread(target=play_transporter_task, daemon=True).start()

def trigger_self_destruct():
    global abort_count, abort_triggered
    mark_activity()
    with state_lock:
        if self_destruct_active:
            abort_count += 1
            print(f"Abort count: {min(5, abort_count)}/5")
            if abort_count >= 5:
                abort_triggered = True
            return
        if any_sequence_active:
            return
    threading.Thread(target=self_destruct_task, daemon=True).start()

def font(size, bold=False):
    return pygame.font.SysFont("DejaVu Sans", max(12, int(size)), bold=bold)

_display_font_cache = {}

def display_font(size):
    """Load the bundled OFL display face used only for the transporter masthead."""
    pixel_size = max(12, int(size))
    if pixel_size not in _display_font_cache:
        path = os.path.join(BASE_DIR, "assets", "fonts", "Michroma-Regular.ttf")
        try:
            _display_font_cache[pixel_size] = pygame.font.Font(path, pixel_size)
        except (OSError, pygame.error):
            _display_font_cache[pixel_size] = pygame.font.SysFont(
                "DejaVu Sans", pixel_size, bold=True)
    return _display_font_cache[pixel_size]

def txt(surface, value, size, color, pos, anchor="topleft", bold=False):
    image = font(size, bold).render(str(value), True, color)
    rect = image.get_rect()
    setattr(rect, anchor, pos)
    surface.blit(image, rect)
    return rect

def display_txt(surface, value, size, color, pos, anchor="topleft"):
    image = display_font(size).render(str(value), True, color)
    rect = image.get_rect()
    setattr(rect, anchor, pos)
    surface.blit(image, rect)
    return rect

def draw_starfleet_delta(surface, rect):
    """Draw a compact original vector homage to the classic command delta."""
    x, y, w, h = rect
    outer = ((x + w * .50, y), (x + w * .91, y + h * .95),
             (x + w * .53, y + h * .73), (x + w * .10, y + h * .95))
    pygame.draw.polygon(surface, (212, 176, 72), outer)
    inner = ((x + w * .50, y + h * .13), (x + w * .77, y + h * .78),
             (x + w * .52, y + h * .63), (x + w * .25, y + h * .79))
    pygame.draw.polygon(surface, (7, 14, 20), inner)
    pygame.draw.lines(surface, (255, 226, 129), True, outer, max(2, int(w * .04)))
    star = (x + w * .49, y + h * .41)
    flare = ((star[0], star[1] - h * .09), (star[0] + w * .035, star[1] - h * .025),
             (star[0] + w * .11, star[1]), (star[0] + w * .035, star[1] + h * .025),
             (star[0], star[1] + h * .10), (star[0] - w * .035, star[1] + h * .025),
             (star[0] - w * .11, star[1]), (star[0] - w * .035, star[1] - h * .025))
    pygame.draw.polygon(surface, CREAM, flare)

def panel(surface, rect, color=PANEL, border=BLUE, radius=18):
    pygame.draw.rect(surface, color, rect, border_radius=radius)
    pygame.draw.rect(surface, border, rect, width=2, border_radius=radius)

def invalid_data_flag(surface, rect, label="INVALID"):
    """Draw a red/white mechanical failure shutter across an instrument face."""
    bar = pygame.Rect(rect.x + int(rect.w * .08), rect.centery - max(11, int(rect.h * .09)),
                      int(rect.w * .84), max(22, int(rect.h * .18)))
    previous_clip = surface.get_clip()
    surface.set_clip(bar)
    pygame.draw.rect(surface, RED, bar)
    stripe = max(12, bar.h)
    for x in range(bar.left - bar.h, bar.right + bar.h, stripe * 2):
        pygame.draw.polygon(surface, WHITE,
                            [(x, bar.bottom), (x + stripe, bar.bottom),
                             (x + stripe + bar.h, bar.top), (x + bar.h, bar.top)])
    surface.set_clip(previous_clip)
    pygame.draw.rect(surface, (18, 18, 16), bar, 3)
    if bar.w >= 80:
        plate = pygame.Rect(0, 0, min(int(bar.w * .42), 130), int(bar.h * .62))
        plate.center = bar.center
        pygame.draw.rect(surface, (12, 12, 11), plate, border_radius=2)
        txt(surface, label, plate.h * .55, WHITE, plate.center, "center", True)

def bar(surface, rect, value, color=CYAN, segments=20):
    gap = max(2, rect.w // 140)
    sw = (rect.w - gap * (segments - 1)) / segments
    active = round(value * segments)
    for i in range(segments):
        r = pygame.Rect(round(rect.x + i * (sw + gap)), rect.y, max(2, round(sw)), rect.h)
        pygame.draw.rect(surface, color if i < active else (28, 58, 68), r, border_radius=3)

def edge_meter(surface, rect, value, label, color, valid=True, readout_text=None):
    """Retro edgewise panel meter with a moving pointer over a fixed scale."""
    value = max(0.0, min(1.0, value))
    pygame.draw.rect(surface, (72, 77, 73), rect, border_radius=4)
    pygame.draw.rect(surface, (145, 148, 137), rect, 2, border_radius=4)
    inner = rect.inflate(-8, -8)
    pygame.draw.rect(surface, INSTRUMENT, inner, border_radius=2)
    plate = pygame.Rect(inner.x + 5, inner.y + 4, int(inner.w * .43), 17)
    pygame.draw.rect(surface, (201, 198, 176), plate, border_radius=2)
    txt(surface, label, rect.h * .16, (22, 24, 22), (plate.x + 5, plate.centery), "midleft", True)
    readout = pygame.Rect(inner.right - 46, inner.y + 4, 40, 17)
    pygame.draw.rect(surface, (0, 0, 0), readout)
    pygame.draw.rect(surface, BEZEL, readout, 1)
    txt(surface, readout_text if readout_text is not None else f"{int(value * 100):02d}",
        rect.h * .17, CREAM, readout.center, "center", True)
    track = pygame.Rect(inner.x + 8, inner.bottom - 23, inner.w - 16, 17)
    pygame.draw.rect(surface, (2, 6, 7), track)
    for i in range(21):
        x = track.x + i * track.w / 20
        major = i % 5 == 0
        pygame.draw.line(surface, CREAM, (x, track.bottom - (12 if major else 7)), (x, track.bottom - 2), 2 if major else 1)
    pointer_x = int(track.x + value * track.w)
    pygame.draw.line(surface, color, (pointer_x, track.y - 2), (pointer_x, track.bottom), 3)
    pygame.draw.polygon(surface, color, [(pointer_x, track.y - 2), (pointer_x - 6, track.y - 9), (pointer_x + 6, track.y - 9)])
    jewel = (inner.right - 60, inner.y + 12)
    pygame.draw.circle(surface, BEZEL, jewel, 7)
    pygame.draw.circle(surface, color, jewel, 4)
    if not valid:
        invalid_data_flag(surface, inner)

def pressure_pen_tape(surface, rect, pressure_mb, valid=True):
    """Scrolling ruled-paper barograph with a mechanical pen trace."""
    global last_pressure_trace_sample
    sample_time = time.monotonic()
    if valid and pressure_mb is not None and sample_time - last_pressure_trace_sample >= 15:
        pressure_trace.append(float(pressure_mb))
        last_pressure_trace_sample = sample_time

    pygame.draw.rect(surface, (72, 77, 73), rect, border_radius=4)
    pygame.draw.rect(surface, (145, 148, 137), rect, 2, border_radius=4)
    inner = rect.inflate(-8, -8)
    paper = pygame.Rect(inner.x + 5, inner.y + 22, inner.w - 10, inner.h - 27)
    pygame.draw.rect(surface, (221, 213, 174), paper)

    plate = pygame.Rect(inner.x + 5, inner.y + 3, int(inner.w * .47), 17)
    pygame.draw.rect(surface, (201, 198, 176), plate, border_radius=2)
    txt(surface, "BAROMETRIC PEN RECORDER", rect.h * .15, (22, 24, 22),
        (plate.x + 5, plate.centery), "midleft", True)
    readout = pygame.Rect(inner.right - 79, inner.y + 3, 74, 17)
    pygame.draw.rect(surface, BLACK, readout)
    pygame.draw.rect(surface, BEZEL, readout, 1)
    value_text = f"{pressure_mb:.1f} MB" if pressure_mb is not None else "— MB"
    txt(surface, value_text, rect.h * .15, CREAM, readout.center, "center", True)

    # Ruled chart paper: heavier divisions emulate the clock-driven drums used
    # in old meteorological and aerospace recorders.
    for index in range(25):
        x = paper.x + index * paper.w / 24
        pygame.draw.line(surface, (161, 178, 153), (x, paper.y), (x, paper.bottom),
                         2 if index % 6 == 0 else 1)
    for index in range(7):
        y = paper.y + index * paper.h / 6
        pygame.draw.line(surface, (161, 178, 153), (paper.x, y), (paper.right, y),
                         2 if index % 3 == 0 else 1)

    values = list(pressure_trace)
    if values:
        center_value = sum(values) / len(values)
        half_span = max(2.0, max(abs(value - center_value) for value in values) + .35)
        lower, upper = center_value - half_span, center_value + half_span
        spacing = paper.w / max(1, pressure_trace.maxlen - 1)
        points = []
        for index, value in enumerate(values):
            x = paper.right - (len(values) - 1 - index) * spacing
            y = paper.bottom - (value - lower) / (upper - lower) * paper.h
            points.append((round(x), round(y)))
        if len(points) > 1:
            pygame.draw.lines(surface, (102, 45, 32), False, points, 3)
        pygame.draw.circle(surface, (112, 34, 27), points[-1], 4)
        comparison = values[max(0, len(values) - 21)]
        change = values[-1] - comparison
        trend = "RISING" if change > .08 else "FALLING" if change < -.08 else "STEADY"
        txt(surface, trend, rect.h * .12, (59, 55, 43),
            (paper.right - 4, paper.y + 2), "topright", True)

    if not valid:
        invalid_data_flag(surface, inner)

def lightning_warning_cover(surface, rect, distance_miles, age_seconds):
    """Hinged-looking amber cover for a dangerously close recent strike."""
    cover = rect.inflate(-4, -4)
    pygame.draw.rect(surface, (231, 174, 38), cover, border_radius=3)
    pygame.draw.rect(surface, (61, 49, 18), cover, 4, border_radius=3)
    for x in range(cover.x + 9, cover.right - 8, 34):
        pygame.draw.line(surface, (91, 70, 20), (x, cover.y + 4),
                         (x + 18, cover.y + 4), 3)
        pygame.draw.line(surface, (91, 70, 20), (x, cover.bottom - 5),
                         (x + 18, cover.bottom - 5), 3)
    for corner in ((cover.x + 11, cover.y + 11), (cover.right - 11, cover.y + 11),
                   (cover.x + 11, cover.bottom - 11), (cover.right - 11, cover.bottom - 11)):
        pygame.draw.circle(surface, (72, 67, 51), corner, 5)
        pygame.draw.line(surface, (28, 28, 25), (corner[0] - 3, corner[1]),
                         (corner[0] + 3, corner[1]), 1)
    age_seconds = max(0, int(age_seconds))
    age_text = (f"{age_seconds // 60:02d}M {age_seconds % 60:02d}S AGO"
                if age_seconds < 3600 else f"{age_seconds // 3600:02d}H AGO")
    txt(surface, "CLOSE STRIKE WARNING", cover.h * .20, (35, 30, 15),
        (cover.centerx, cover.y + 9), "midtop", True)
    txt(surface, f"{distance_miles:.2f} MI", cover.h * .34, (35, 30, 15),
        (cover.centerx, cover.centery + 3), "center", True)
    txt(surface, f"LAST CLOSE STRIKE • {age_text}", cover.h * .13, (35, 30, 15),
        (cover.centerx, cover.bottom - 8), "midbottom", True)

def tape_meter(surface, rect, value, vertical=False, accent=AMBER, background=INSTRUMENT):
    """Apollo-style moving-tape instrument with a fixed datum pointer."""
    pygame.draw.rect(surface, BEZEL, rect, border_radius=3)
    window = rect.inflate(-6, -6)
    pygame.draw.rect(surface, background, window)
    # Faint center band suggests the illuminated glass of a mechanical tape window.
    band = pygame.Rect(window.x, window.centery - max(2, window.h // 14), window.w, max(4, window.h // 7))
    glow = tuple(min(255, channel + 9) for channel in background)
    pygame.draw.rect(surface, glow, band)
    value = max(0.0, min(1.0, value))
    steps = 21
    if vertical:
        center = window.centery
        spacing = max(10, window.h // 10)
        phase = (value * 100) % 10 / 10
        for i in range(-11, 12):
            y = round(center + (i + phase) * spacing)
            if window.top + 2 <= y <= window.bottom - 2:
                major = i % 5 == 0
                length = int(window.w * (.58 if major else .34))
                pygame.draw.line(surface, CREAM, (window.right - length, y), (window.right - 3, y), 2 if major else 1)
                if major and window.w >= 34:
                    txt(surface, f"{int(value * 100) - i * 2:02d}", window.w * .20, CREAM, (window.left + 3, y), "midleft", True)
        pygame.draw.polygon(surface, accent, [(rect.right + 1, center), (rect.right + 10, center - 7), (rect.right + 10, center + 7)])
        pygame.draw.line(surface, accent, (window.left, center), (window.right, center), 2)
    else:
        center = window.centerx
        spacing = max(12, window.w // 16)
        phase = (value * 100) % 10 / 10
        for i in range(-18, 19):
            x = round(center + (i + phase) * spacing)
            if window.left + 2 <= x <= window.right - 2:
                major = i % 5 == 0
                length = int(window.h * (.62 if major else .38))
                pygame.draw.line(surface, CREAM, (x, window.bottom - length), (x, window.bottom - 3), 2 if major else 1)
                if major:
                    txt(surface, f"{int(value * 100) + i * 2:02d}", window.h * .24, CREAM, (x, window.top + 2), "midtop", True)
        pygame.draw.polygon(surface, accent, [(center, rect.bottom + 1), (center - 8, rect.bottom + 10), (center + 8, rect.bottom + 10)])
        pygame.draw.line(surface, accent, (center, window.top), (center, window.bottom), 2)

def gauge(surface, center, radius, value, label, color, readout_text=None, valid=True):
    start, span = math.radians(140), math.radians(260)
    box = pygame.Rect(center[0] - radius, center[1] - radius, radius * 2, radius * 2)
    face = pygame.Rect(center[0] - radius - 19, center[1] - radius - 17, radius * 2 + 38, radius * 2 + 49)
    pygame.draw.rect(surface, (72, 77, 73), face, border_radius=7)
    pygame.draw.rect(surface, (132, 137, 128), face, 3, border_radius=7)
    inner = face.inflate(-16, -16)
    pygame.draw.rect(surface, INSTRUMENT, inner, border_radius=3)
    # Four visible fasteners make the meter feel mounted rather than drawn on.
    for screw in ((face.left + 9, face.top + 9), (face.right - 9, face.top + 9),
                  (face.left + 9, face.bottom - 9), (face.right - 9, face.bottom - 9)):
        pygame.draw.circle(surface, (35, 38, 36), screw, 4)
        pygame.draw.line(surface, (170, 170, 156), (screw[0] - 3, screw[1]), (screw[0] + 3, screw[1]), 1)
    pygame.draw.arc(surface, CREAM, box, start, start + span, 2)
    # A small red overload sector replaces the modern colored progress ring.
    pygame.draw.arc(surface, RED, box, start + span * .82, start + span, 5)
    for i in range(21):
        a = start + span * i / 20
        major = i % 2 == 0
        p1 = (center[0] + math.cos(a) * radius * (.70 if major else .78), center[1] + math.sin(a) * radius * (.70 if major else .78))
        p2 = (center[0] + math.cos(a) * radius * .92, center[1] + math.sin(a) * radius * .92)
        pygame.draw.line(surface, CREAM, p1, p2, 2 if major else 1)
        if major:
            number_pos = (center[0] + math.cos(a) * radius * .58, center[1] + math.sin(a) * radius * .58)
            txt(surface, i * 5, radius * .105, CREAM, number_pos, "center", True)
    needle = start + span * value
    tail = (center[0] - math.cos(needle) * radius * .12, center[1] - math.sin(needle) * radius * .12)
    end = (center[0] + math.cos(needle) * radius * .68, center[1] + math.sin(needle) * radius * .68)
    pygame.draw.line(surface, (238, 225, 180), tail, end, 3)
    pygame.draw.circle(surface, (42, 44, 40), center, 9)
    pygame.draw.circle(surface, (174, 177, 163), center, 9, 2)
    # Small mechanical-style readout and jewel lamp.
    readout = pygame.Rect(center[0] - int(radius * .25), center[1] + int(radius * .24), int(radius * .50), int(radius * .22))
    pygame.draw.rect(surface, (0, 0, 0), readout)
    pygame.draw.rect(surface, BEZEL, readout, 2)
    txt(surface, readout_text if readout_text is not None else f"{int(value * 100):02d}",
        radius * .17, CREAM, readout.center, "center", True)
    pygame.draw.circle(surface, color, (inner.right - 13, inner.top + 13), 5)
    label_plate = pygame.Rect(center[0] - int(radius * .60), center[1] + int(radius * .54), int(radius * 1.20), int(radius * .18))
    pygame.draw.rect(surface, (198, 194, 170), label_plate, border_radius=2)
    txt(surface, label, radius * .105, (22, 24, 22), label_plate.center, "center", True)
    # Restrained glass reflection along the upper-left edge.
    pygame.draw.arc(surface, (72, 86, 85), box.inflate(-18, -18), math.radians(188), math.radians(260), 2)
    if not valid:
        invalid_data_flag(surface, inner)

def button(surface, rect, title, subtitle, color, enabled=True, armed=False):
    c = color if enabled else (48, 62, 67)
    fill = tuple(max(8, int(x * (.55 if armed else .36))) for x in c)
    pygame.draw.rect(surface, fill, rect, border_radius=18)
    pygame.draw.rect(surface, c, rect, 4, border_radius=18)
    txt(surface, title, rect.h * .25, WHITE if enabled else MUTED, (rect.centerx, rect.centery - rect.h * .08), "center", True)
    txt(surface, subtitle, rect.h * .11, c, (rect.centerx, rect.centery + rect.h * .23), "center", True)

def status_lamp(surface, rect, label, state, color, on=True):
    """Panel-mounted jewel lamp with bezel and engraved identification plate."""
    pygame.draw.rect(surface, (75, 80, 76), rect, border_radius=4)
    pygame.draw.rect(surface, (145, 148, 137), rect, 2, border_radius=4)
    inset = rect.inflate(-8, -8)
    pygame.draw.rect(surface, (7, 12, 13), inset, border_radius=2)
    lamp_center = (inset.x + 19, inset.centery)
    pygame.draw.circle(surface, (168, 171, 158), lamp_center, 12)
    pygame.draw.circle(surface, (36, 39, 36), lamp_center, 9)
    lens = color if on else tuple(max(8, channel // 5) for channel in color)
    pygame.draw.circle(surface, lens, lamp_center, 7)
    pygame.draw.circle(surface, tuple(min(255, channel + 70) for channel in lens), (lamp_center[0] - 2, lamp_center[1] - 2), 2)
    plate = pygame.Rect(inset.x + 39, inset.y + 4, inset.w - 45, inset.h - 8)
    pygame.draw.rect(surface, (201, 198, 176), plate, border_radius=2)
    pygame.draw.rect(surface, (57, 60, 55), plate, 1, border_radius=2)
    txt(surface, label, rect.h * .21, (22, 24, 22), (plate.x + 7, plate.centery - rect.h * .09), "midleft", True)
    txt(surface, state, rect.h * .15, (72, 66, 48), (plate.x + 7, plate.centery + rect.h * .15), "midleft", True)

def layout(size):
    w, h = size
    margin, gap = int(w * .018), int(w * .012)
    header_h, footer_h = int(h * .115), int(h * .205)
    body_y, body_h = margin + header_h + gap, h - (margin + header_h + gap) - footer_h - margin * 2
    selector_w = max(132, int(w * .088))
    content_x = margin + selector_w + gap
    content_w = w - margin - content_x
    left_w, right_w = int(content_w * .28), int(content_w * .25)
    center_w = content_w - left_w - right_w - gap * 2
    result = {
        "header": pygame.Rect(margin, margin, w - margin * 2, header_h),
        "left": pygame.Rect(content_x, body_y, left_w, body_h),
        "center": pygame.Rect(content_x + left_w + gap, body_y, center_w, body_h),
        "right": pygame.Rect(w - margin - right_w, body_y, right_w, body_h),
        "energize": pygame.Rect(margin, h - margin - footer_h, int(w * .64), footer_h),
        "destruct": pygame.Rect(margin + int(w * .64) + gap, h - margin - footer_h, w - margin * 2 - int(w * .64) - gap, footer_h),
        "mode_selector": pygame.Rect(margin, body_y, selector_w, min(body_h, int(h * .48))),
    }
    result["maker_plate"] = pygame.Rect(result["right"].x + 62, result["right"].bottom - 31, result["right"].w - 124, 18)
    result["afg"] = pygame.Rect(result["center"].x + 62,
                                result["center"].bottom - int(h * .115),
                                result["center"].w - 124, int(h * .075))
    return result

def draw_mode_selector(surface, rect):
    """Panel-mounted five-position rotary display selector."""
    panel(surface, rect, (38, 42, 39), BEZEL, 5)
    inner = rect.inflate(-10, -10)
    pygame.draw.rect(surface, (10, 14, 13), inner, border_radius=3)
    txt(surface, "DISPLAY", rect.w * .105, CREAM, (rect.centerx, rect.y + 18), "midtop", True)
    txt(surface, "SELECTOR", rect.w * .085, MUTED, (rect.centerx, rect.y + 35), "midtop", True)

    options = ("TRANSPORTER", "SHIP STATUS", "AIR TRAFFIC", "SPACE TRAFFIC", "AUTO")
    knob_center = (rect.centerx, rect.y + int(rect.h * .31))
    radius = max(25, int(rect.w * .29))
    pygame.draw.circle(surface, (155, 157, 145), knob_center, radius + 9)
    pygame.draw.circle(surface, (28, 31, 29), knob_center, radius + 5)
    pygame.draw.circle(surface, (76, 80, 74), knob_center, radius)
    angles = (-150, -120, -90, -60, -30)
    selected_index = options.index(display_mode)
    for index, angle in enumerate(angles):
        radians = math.radians(angle)
        outer = (knob_center[0] + math.cos(radians) * (radius + 16),
                 knob_center[1] + math.sin(radians) * (radius + 16))
        inner_tick = (knob_center[0] + math.cos(radians) * (radius + 8),
                      knob_center[1] + math.sin(radians) * (radius + 8))
        pygame.draw.line(surface, CREAM, inner_tick, outer, 2)
    pointer_angle = math.radians(angles[selected_index])
    pointer = (knob_center[0] + math.cos(pointer_angle) * radius * .76,
               knob_center[1] + math.sin(pointer_angle) * radius * .76)
    pygame.draw.line(surface, AMBER, knob_center, pointer, 7)
    pygame.draw.circle(surface, (24, 26, 24), knob_center, 10)
    pygame.draw.circle(surface, (177, 178, 163), knob_center, 10, 2)

    row_top = rect.y + int(rect.h * .51)
    row_h = max(30, int((rect.bottom - row_top - 10) / len(options)))
    for index, option in enumerate(options):
        segment = pygame.Rect(rect.x + 8, row_top + index * row_h,
                              rect.w - 16, row_h - 5)
        selected = display_mode == option
        color = GREEN if option == "AUTO" else BLUE if "TRAFFIC" in option else AMBER
        pygame.draw.rect(surface, (190, 188, 166) if selected else (91, 94, 87), segment, border_radius=2)
        pygame.draw.rect(surface, color if selected else (145, 146, 134), segment, 2, border_radius=2)
        lamp = (segment.x + 10, segment.centery)
        pygame.draw.circle(surface, (41, 44, 41), lamp, 6)
        pygame.draw.circle(surface, color if selected else (18, 22, 20), lamp, 4)
        txt(surface, option, rect.w * .071,
            (22, 24, 22) if selected else CREAM,
            (segment.centerx + 5, segment.centery), "center", True)
    draw_party_return(surface, rect)

def party_return_rect(selector_rect):
    return pygame.Rect(selector_rect.x, selector_rect.bottom + 14,
                       selector_rect.w, int(selector_rect.h * .205))

def draw_party_return(surface, selector_rect):
    """Draw the guarded automatic/manual venue control."""
    rect = party_return_rect(selector_rect)
    panel(surface, rect, (38, 42, 39), BEZEL, 5)
    inner = rect.inflate(-10, -10)
    pygame.draw.rect(surface, (10, 14, 13), inner, border_radius=3)
    txt(surface, "VENUE CONTROL", rect.w * .076, CREAM,
        (rect.centerx, rect.y + 13), "midtop", True)
    lamp = (rect.x + 22, rect.y + 48)
    armed = party_return_armed()
    color = GREEN if armed else CYAN
    pygame.draw.circle(surface, (110, 114, 104), lamp, 10)
    pygame.draw.circle(surface, (15, 19, 17), lamp, 7)
    pygame.draw.circle(surface, color, lamp, 5)
    state = "PARTY" if armed else "OFFICE"
    txt(surface, state, rect.w * .082, color,
        (rect.x + 40, rect.y + 48), "midleft", True)
    if venue_override == "AUTO":
        detail = "WIRED • AUTO" if ethernet_link_connected else "WI-FI • AUTO"
    else:
        detail = f"MANUAL • {venue_override}"
    txt(surface, detail, rect.w * .065, MUTED,
        (rect.centerx, rect.y + 72), "center", True)
    txt(surface, "CREW ONLY • HOLD 5 SEC", rect.w * .043, AMBER,
        (rect.centerx, rect.bottom - 15), "center", True)
    if party_return_hold_started:
        progress = min(1.0, (time.monotonic() - party_return_hold_started) / 5.0)
        pygame.draw.rect(surface, AMBER,
                         (inner.x, inner.bottom - 4, int(inner.w * progress), 3))

def mode_at_position(pos, size):
    rect = layout(size)["mode_selector"]
    if not rect.collidepoint(pos):
        return None
    options = ("TRANSPORTER", "SHIP STATUS", "AIR TRAFFIC", "SPACE TRAFFIC", "AUTO")
    row_top = rect.y + int(rect.h * .51)
    if pos[1] < row_top:
        return options[(options.index(display_mode) + 1) % len(options)]
    row_h = max(30, int((rect.bottom - row_top - 10) / len(options)))
    index = min(len(options) - 1, max(0, int((pos[1] - row_top) / row_h)))
    return options[index]

def party_return_at_position(pos, size):
    return party_return_rect(layout(size)["mode_selector"]).collidepoint(pos)

def status_nav_layout(size):
    r = layout(size)
    gap = int(size[0] * .012)
    footer = pygame.Rect(r["mode_selector"].right + gap, size[1] - int(size[1] * .075),
                         size[0] - r["mode_selector"].right - gap - int(size[0] * .018),
                         int(size[1] * .052))
    fourth = (footer.w - gap * 3) // 4
    return {
        "NETWORK": pygame.Rect(footer.x, footer.y, fourth, footer.h),
        "ENVIRONMENT": pygame.Rect(footer.x + fourth + gap, footer.y, fourth, footer.h),
        "HOUSE SYSTEMS": pygame.Rect(footer.x + (fourth + gap) * 2, footer.y, fourth, footer.h),
        "POWER CELLS": pygame.Rect(footer.x + (fourth + gap) * 3, footer.y,
                                   footer.w - fourth * 3 - gap * 3, footer.h),
    }

def status_page_at_position(pos, size):
    for page, rect in status_nav_layout(size).items():
        if rect.collidepoint(pos):
            return page
    return None

def draw_status_nav(surface):
    labels = {"NETWORK": "CORE & WEATHER", "ENVIRONMENT": "ROOM SENSORS",
              "HOUSE SYSTEMS": "HOUSE SYSTEMS", "POWER CELLS": "POWER CELLS"}
    for page, rect in status_nav_layout(surface.get_size()).items():
        selected = status_page == page
        color = (CYAN if page == "NETWORK" else AMBER if page == "ENVIRONMENT"
                 else GREEN if page == "HOUSE SYSTEMS" else ORANGE)
        pygame.draw.rect(surface, tuple(channel // (4 if selected else 8) for channel in color),
                         rect, border_radius=5)
        pygame.draw.rect(surface, color if selected else BEZEL, rect, 3, border_radius=5)
        txt(surface, labels[page], rect.h * .30, WHITE if selected else MUTED,
            rect.center, "center", True)

def environment_pager_layout(size):
    r = layout(size)
    w, h = size
    gap, margin = int(w * .012), int(w * .018)
    content_x = r["mode_selector"].right + gap
    content_w = w - margin - content_x
    grid_w = content_w - int(content_w * .29) - gap
    y = status_nav_layout(size)["NETWORK"].y - int(h * .047)
    return {
        "PREVIOUS": pygame.Rect(content_x + 18, y, int(w * .085), int(h * .035)),
        "NEXT": pygame.Rect(content_x + grid_w - 18 - int(w * .085), y,
                            int(w * .085), int(h * .035)),
    }

def house_pager_layout(size):
    r = layout(size)
    w, h = size
    gap, margin = int(w * .012), int(w * .018)
    content_x = r["mode_selector"].right + gap
    content_w = w - margin - content_x
    grid_w = int(content_w * .70)
    y = status_nav_layout(size)["NETWORK"].y - int(h * .047)
    return {
        "PREVIOUS": pygame.Rect(content_x + 18, y, int(w * .085), int(h * .035)),
        "NEXT": pygame.Rect(content_x + grid_w - 18 - int(w * .085), y,
                            int(w * .085), int(h * .035)),
    }

def battery_pager_layout(size):
    r = layout(size)
    w, h = size
    gap, margin = int(w * .012), int(w * .018)
    content_x = r["mode_selector"].right + gap
    content_w = w - margin - content_x
    y = status_nav_layout(size)["NETWORK"].y - int(h * .047)
    return {
        "PREVIOUS": pygame.Rect(content_x + 18, y, int(w * .085), int(h * .035)),
        "NEXT": pygame.Rect(content_x + content_w - 18 - int(w * .085), y,
                            int(w * .085), int(h * .035)),
    }

def radar_controls_layout(size):
    r = layout(size)
    w, h = size
    gap, margin = int(w * .012), int(w * .018)
    content_x = r["mode_selector"].right + gap
    content_w = w - margin - content_x
    left_w = int(content_w * .65)
    y = r["header"].bottom + gap + 10
    button_w, button_h = int(w * .052), int(h * .035)
    controls = {}
    for index, value in enumerate((20, 40, 80, 160)):
        controls[value] = pygame.Rect(content_x + 18 + index * (button_w + 7), y,
                                      button_w, button_h)
    controls["WX"] = pygame.Rect(content_x + left_w - int(w * .115), y,
                                 int(w * .10), button_h)
    return controls

def shutdown_layout(size):
    w, h = size
    return {
        "exit": pygame.Rect(int(w * .10), int(h * .57), int(w * .245), int(h * .16)),
        "restart": pygame.Rect(int(w * .3775), int(h * .57), int(w * .245), int(h * .16)),
        "shutdown": pygame.Rect(int(w * .655), int(h * .57), int(w * .245), int(h * .16)),
        "cancel": pygame.Rect(int(w * .35), int(h * .76), int(w * .30), int(h * .105)),
    }

def request_power_action(action):
    global shutdown_pending, power_action_pending
    if action not in ("reboot", "poweroff"):
        return
    shutdown_pending = True
    power_action_pending = action
    print(f"SAFE {action.upper()} REQUESTED")
    time.sleep(0.8)
    result = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", action], check=False)
    if result.returncode:
        shutdown_pending = False
        power_action_pending = None
        print(f"{action.title()} failed with status {result.returncode}")

def draw_shutdown_confirmation(surface, now):
    w, h = surface.get_size()
    overlay = pygame.Surface((w, h), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, 225))
    surface.blit(overlay, (0, 0))
    border = pygame.Rect(int(w * .08), int(h * .17), int(w * .84), int(h * .68))
    panel(surface, border, (22, 25, 24), BEZEL, 10)
    txt(surface, "GROUND OPERATIONS", h * .028, AMBER, (w // 2, int(h * .22)), "center", True)
    txt(surface, "MAX  •  EMERGENCY COMMAND HOLOGRAM  •  TRANSPORTER SYSTEMS DIVISION",
        h * .014, CYAN, (w // 2, int(h * .275)), "center", True)
    pending_title = "RESTARTING RASPBERRY PI" if power_action_pending == "reboot" else "SHUTTING DOWN"
    title = pending_title if shutdown_pending else "CONSOLE OPERATIONS"
    txt(surface, title, h * .068, CREAM, (w // 2, int(h * .36)), "center", True)
    if shutdown_pending and power_action_pending == "reboot":
        detail = "THE CONSOLE WILL RETURN AUTOMATICALLY"
    elif shutdown_pending:
        detail = "WAIT FOR THE DISPLAY TO GO DARK BEFORE REMOVING POWER"
    else:
        detail = "SELECT A PROTECTED GROUND OPERATION"
    txt(surface, detail, h * .022, MUTED, (w // 2, int(h * .47)), "center", True)
    if not shutdown_pending:
        txt(surface, "PLEASE STATE THE NATURE OF THE PARTY EMERGENCY", h * .016,
            CREAM, (w // 2, int(h * .515)), "center", True)
        controls = shutdown_layout((w, h))
        button(surface, controls["exit"], "EXIT KIOSK", "RETURN TO DESKTOP", CYAN, True)
        button(surface, controls["restart"], "RESTART", "REBOOT RASPBERRY PI", AMBER, True)
        button(surface, controls["shutdown"], "SHUT DOWN", "SAFE POWER-OFF", RED, True, True)
        button(surface, controls["cancel"], "CANCEL", "RETURN TO CONSOLE", GREEN, True)
        remaining = max(0, int(shutdown_confirm_until - now) + 1)
        txt(surface, f"CANCELS AUTOMATICALLY IN {remaining}", h * .014, MUTED,
            (w // 2, int(h * .89)), "center", True)

def draw_console(surface, now):
    r, w, h = layout(surface.get_size()), *surface.get_size()
    surface.fill(RED if now < flash_until else BLACK)
    panel(surface, r["header"], NAVY, CYAN)
    display_txt(surface, "STARFLEET TRANSPORT COMMAND  •  NCC-1701", h * .017,
                MUTED, (r["header"].x + 24, r["header"].y + 16))
    display_txt(surface, "TRANSPORTER CONTROL", h * .043, WHITE,
                (r["header"].x + 24, r["header"].bottom - 15), "bottomleft")
    emblem = pygame.Rect(r["header"].right - int(w * .285), r["header"].y + 11,
                         int(h * .066), int(h * .090))
    draw_starfleet_delta(surface, emblem)
    lamp = GREEN if ui_state in ("READY", "COMPLETE") else RED if ui_state in ("DESTRUCT", "DESTROYED") else AMBER
    pygame.draw.circle(surface, lamp, (r["header"].right - 38, r["header"].centery), 14)
    txt(surface, ui_state, h * .03, lamp, (r["header"].right - 66, r["header"].centery), "midright", True)
    draw_mode_selector(surface, r["mode_selector"])

    panel(surface, r["left"])
    # Idle instruments make broad, slow mechanical sweeps; operating indications
    # settle into tighter bands with quick regulator corrections.
    if ui_state == "ENERGIZING":
        # Both systems build toward the caution sector as transport proceeds.
        integrity = .68 + transport_progress * .25 + math.sin(now * .85) * .025
        confinement = .58 + transport_progress * .30 + math.sin(now * .72 + 1.2) * .035
    elif ui_state == "COMPLETE":
        integrity, confinement = .94, .86
    else:
        # Independent overlapping cycles create visible mechanical wander without twitching.
        integrity = .62 + math.sin(now * .28) * .12 + math.sin(now * .11 + .7) * .035
        confinement = .55 + math.sin(now * .23 + 1.2) * .14 + math.sin(now * .09) * .03
    integrity = max(.08, min(.98, integrity))
    confinement = max(.08, min(.98, confinement))
    gauge(surface, (r["left"].centerx, r["left"].y + int(r["left"].h * .26)), int(r["left"].w * .23), integrity, "PATTERN INTEGRITY", GREEN)
    gauge(surface, (r["left"].centerx, r["left"].y + int(r["left"].h * .75)), int(r["left"].w * .23), confinement, "CONFINEMENT BEAM", CYAN)

    panel(surface, r["center"])
    txt(surface, "PATTERN BUFFER 01", h * .026, CREAM, (r["center"].x + 22, r["center"].y + 14), bold=True)
    operating = ui_state == "ENERGIZING"
    if operating:
        fast_trim = math.sin(now * 8.2) * .018 + math.sin(now * 13.7 + .8) * .009
        active_value = .70 + transport_progress * .18 + fast_trim
    else:
        active_value = .50 + math.sin(now * .24) * .25 + math.sin(now * .09 + .5) * .06
    if ui_state in ("DESTRUCT", "DESTROYED"):
        tape_background, tape_accent = (55, 5, 8), RED
    elif ui_state in ("ENERGIZING", "ABORTED") or now < arm_until:
        tape_background, tape_accent = (48, 34, 4), AMBER
    elif ui_state in ("READY", "COMPLETE"):
        tape_background, tape_accent = (4, 35, 21), GREEN
    else:
        tape_background, tape_accent = INSTRUMENT, CREAM
    top_strip = pygame.Rect(r["center"].x + 22, r["center"].y + 47, r["center"].w - 44, 38)
    tape_meter(surface, top_strip, active_value, accent=tape_accent, background=tape_background)
    chamber = pygame.Rect(r["center"].x + 62, r["center"].y + 101, r["center"].w - 124, int(r["center"].h * .45))
    pygame.draw.rect(surface, (5, 20, 30), chamber, border_radius=12)
    if operating:
        left_value = .76 + transport_progress * .12 + math.sin(now * 9.1 + .4) * .025
        right_value = .71 + transport_progress * .15 + math.sin(now * 10.4 + 2) * .022
    else:
        left_value = .54 + math.sin(now * .21 + .4) * .28 + math.sin(now * .08) * .05
        right_value = .48 + math.sin(now * .18 + 2) * .27 + math.sin(now * .07 + 1.1) * .05
    tape_meter(surface, pygame.Rect(chamber.x - 48, chamber.y, 34, chamber.h), left_value, True, tape_accent, tape_background)
    tape_meter(surface, pygame.Rect(chamber.right + 14, chamber.y, 34, chamber.h), right_value, True, tape_accent, tape_background)
    idle_levels = (.42, .67, .31, .78, .53, .63, .46)
    for i in range(7):
        x = chamber.x + (i + 1) * chamber.w // 8
        if operating:
            level = .73 + transport_progress * .12 + math.sin(now * (8.0 + i * .31) + i) * .035
        elif ui_state == "COMPLETE":
            level = .9
        else:
            level = idle_levels[i] + math.sin(now * (.18 + i * .012) + i) * .19 + math.sin(now * .065 + i * .8) * .045
        level = max(.08, min(.96, level))
        top = chamber.bottom - int(chamber.h * level)
        pygame.draw.line(surface, CYAN if i % 2 else AMBER, (x, chamber.bottom - 14), (x, top), 8)
        pygame.draw.circle(surface, WHITE, (x, top), 6)
    if ui_state == "ENERGIZING":
        radius = int(min(chamber.w, chamber.h) * (.08 + .7 * abs(math.sin(transport_progress * math.pi))))
        pygame.draw.circle(surface, CYAN, chamber.center, radius, max(3, w // 400))
    read_y = chamber.bottom + 18
    txt(surface, "TARGET COORDINATES", h * .019, MUTED, (chamber.x, read_y), bold=True)
    if operating or ui_state == "COMPLETE":
        coordinate_values = (47.221, 118.093, 6.714)
    else:
        # Slow sensor-search drift stops the instant target acquisition begins.
        coordinate_values = (47.221 + math.sin(now * .17) * .024,
                             118.093 + math.sin(now * .13 + 1.4) * .031,
                             6.714 + math.sin(now * .19 + 2.2) * .018)
    for i, (axis, coordinate) in enumerate(zip("XYZ", coordinate_values)):
        val = f"{coordinate:07.3f}"
        x = chamber.x + i * chamber.w // 3
        txt(surface, axis, h * .019, AMBER, (x, read_y + 28), bold=True)
        txt(surface, val, h * .029, WHITE, (x + 25, read_y + 24), bold=True)

    afg = r["afg"]
    pygame.draw.rect(surface, (70, 76, 72), afg, border_radius=5)
    pygame.draw.rect(surface, (153, 157, 143), afg, 2, border_radius=5)
    inner = afg.inflate(-10, -10)
    pygame.draw.rect(surface, (4, 12, 15), inner, border_radius=3)
    label_color = RED if afg_value == 0 else CREAM
    txt(surface, "ACOUSTIC FIELD GAIN", h * .015, label_color,
        (inner.x + 8, inner.y + 5), bold=True)
    readout = "AURAL FIELD MUTED" if afg_value == 0 else f"AFG {afg_value:03d}"
    txt(surface, readout, h * .017, label_color,
        (inner.right - 8, inner.y + 5), "topright", True)
    track = pygame.Rect(inner.x + 9, inner.bottom - int(inner.h * .36),
                        inner.w - 18, max(8, int(inner.h * .16)))
    pygame.draw.rect(surface, (25, 37, 39), track, border_radius=track.h // 2)
    fill_w = int(track.w * afg_value / 100)
    if fill_w:
        pygame.draw.rect(surface, CYAN, (track.x, track.y, fill_w, track.h),
                         border_radius=track.h // 2)
    for tick in range(0, 101, 10):
        tick_x = track.x + int(track.w * tick / 100)
        pygame.draw.line(surface, CREAM, (tick_x, track.bottom + 2),
                         (tick_x, track.bottom + 6), 1)
    knob_x = track.x + int(track.w * afg_value / 100)
    pygame.draw.circle(surface, (38, 42, 40), (knob_x, track.centery), track.h + 5)
    pygame.draw.circle(surface, label_color, (knob_x, track.centery), track.h + 5, 3)
    txt(surface, status_detail, h * .019, lamp, (r["center"].centerx, r["center"].bottom - 20), "midbottom", True)

    panel(surface, r["right"])
    txt(surface, "SYSTEM STATUS", h * .026, WHITE, (r["right"].x + 20, r["right"].y + 18), bold=True)
    if operating:
        phase = transport_progress
        systems = (("HEISENBERG COMP.", "COMPENSATING", GREEN, phase >= .12),
                   ("BIOFILTER", "SCREENING", GREEN, phase >= .28),
                   ("PHASE COILS", "ENERGIZED", CYAN, phase >= .12),
                   ("TARGET LOCK", "HARD LOCK", AMBER, True))
    elif ui_state == "COMPLETE":
        systems = (("HEISENBERG COMP.", "NOMINAL", GREEN, True),
                   ("BIOFILTER", "CLEAR", GREEN, True),
                   ("PHASE COILS", "STANDBY", CYAN, True),
                   ("TARGET LOCK", "RELEASED", AMBER, False))
    elif ui_state in ("DESTRUCT", "DESTROYED"):
        systems = (("HEISENBERG COMP.", "OFFLINE", RED, False),
                   ("BIOFILTER", "BYPASSED", RED, False),
                   ("PHASE COILS", "INHIBITED", RED, False),
                   ("TARGET LOCK", "RELEASED", RED, False))
    else:
        systems = (("HEISENBERG COMP.", "NOMINAL", GREEN, True),
                   ("BIOFILTER", "ACTIVE", GREEN, True),
                   ("PHASE COILS", "SYNCHRONIZED", CYAN, True),
                   ("TARGET LOCK", "ACQUIRED", AMBER, True))
    y = r["right"].y + 60
    row_h = int(h * .052)
    for label, state, color, lamp_on in systems:
        status_lamp(surface, pygame.Rect(r["right"].x + 15, y, r["right"].w - 30, row_h - 4), label, state, color, lamp_on)
        y += row_h + 4
    y += 5
    if operating:
        edge_values = (("MATTER STREAM", .79 + transport_progress * .10 + math.sin(now * 9.3) * .025, CYAN),
                       ("PHASE GAIN", .74 + transport_progress * .08 + math.sin(now * 11.1 + 1.7) * .022, AMBER),
                       ("ENERGY MATRIX", .82 + transport_progress * .09 + math.sin(now * 8.7 + 3.1) * .020, GREEN))
    else:
        edge_values = (("MATTER STREAM", .55 + math.sin(now * .22) * .25 + math.sin(now * .07) * .05, CYAN),
                       ("PHASE GAIN", .50 + math.sin(now * .19 + 1.7) * .27 + math.sin(now * .08) * .04, AMBER),
                       ("ENERGY MATRIX", .58 + math.sin(now * .17 + 3.1) * .24 + math.sin(now * .06 + .3) * .05, GREEN))
    meter_h = int(h * .061)
    for label, value, color in edge_values:
        edge_meter(surface, pygame.Rect(r["right"].x + 15, y, r["right"].w - 30, meter_h), value, label, color)
        y += meter_h + 8
    if countdown_value is None and not self_destruct_active:
        maker_plate = r["maker_plate"]
        pygame.draw.rect(surface, (72, 77, 73), maker_plate, border_radius=2)
        pygame.draw.rect(surface, (18, 21, 20), maker_plate.inflate(-4, -4), border_radius=1)
        txt(surface, "GREG // MAX  •  ECH TRANSPORTER SYSTEMS  •  2026",
            h * .0095, CREAM, maker_plate.center, "center", True)
        if shutdown_hold_started:
            hold_progress = min(1.0, (now - shutdown_hold_started) / 5.0)
            pygame.draw.rect(surface, AMBER, (maker_plate.x, maker_plate.bottom + 3, int(maker_plate.w * hold_progress), 3))
    # The transporter page draws its large instrument panels after the selector;
    # repaint the guard last so its lower enclosure cannot be obscured.
    draw_party_return(surface, r["mode_selector"])
    if countdown_value is None and self_destruct_active:
        txt(surface, f"ABORT {abort_count}/5", h * .038, RED, (r["right"].centerx, r["right"].bottom - 42), "center", True)

    button(surface, r["energize"], "ENERGIZE", "TOUCH TO INITIATE TRANSPORT", CYAN, MODE in ("transporter", "both") and not any_sequence_active, ui_state == "ENERGIZING")
    if self_destruct_active:
        button(surface, r["destruct"], "ABORT", "PRESS 5 TIMES", RED, True, True)
    elif now < arm_until:
        button(surface, r["destruct"], "CONFIRM", "SELF DESTRUCT", RED, not any_sequence_active, True)
    else:
        button(surface, r["destruct"], "SELF DESTRUCT", "TOUCH TO ARM", ORANGE, MODE in ("selfdestruct", "both") and not any_sequence_active)
    if TEST_MODE:
        txt(surface, "TEST  G: ENERGIZE   R: DESTRUCT/ABORT   Q/ESC: QUIT", h * .016, MUTED, (w // 2, h - 3), "midbottom")

def format_uptime(seconds):
    seconds = max(0, int(seconds or 0))
    days, remainder = divmod(seconds, 86400)
    hours, remainder = divmod(remainder, 3600)
    minutes = remainder // 60
    return f"{days:03d}D {hours:02d}:{minutes:02d}"

def format_rate(value):
    value = max(0.0, float(value or 0))
    if value >= 1024 * 1024:
        return f"{value / (1024 * 1024):4.1f} MB/S"
    if value >= 1024:
        return f"{value / 1024:4.1f} KB/S"
    return f"{value:4.0f} B/S"

def format_bitrate(bytes_per_second):
    bits = max(0.0, float(bytes_per_second or 0)) * 8
    if bits >= 1_000_000:
        return f"{bits / 1_000_000:.1f}M"
    if bits >= 1_000:
        return f"{bits / 1_000:.1f}K"
    return f"{bits:.0f}"

def data_age(timestamp):
    if not timestamp:
        return "NO DATA", RED
    age = max(0, time.time() - timestamp)
    if age < 90:
        return f"LIVE {int(age):02d}S", GREEN
    if age < 3600:
        return f"STALE {int(age // 60):02d}M", AMBER
    return f"STALE {int(age // 3600):02d}H", RED

def telemetry_card(surface, rect, label, value, detail="", color=GREEN, state=True, valid=True):
    pygame.draw.rect(surface, (73, 78, 73), rect, border_radius=4)
    pygame.draw.rect(surface, (158, 159, 145), rect, 2, border_radius=4)
    inner = rect.inflate(-8, -8)
    pygame.draw.rect(surface, (5, 12, 13), inner, border_radius=2)
    lamp_center = (inner.x + 17, inner.centery)
    pygame.draw.circle(surface, BEZEL, lamp_center, 10)
    pygame.draw.circle(surface, color if state else (25, 28, 25), lamp_center, 6)
    txt(surface, label, rect.h * .18, CREAM, (inner.x + 36, inner.y + 5), bold=True)
    txt(surface, value, rect.h * .28, WHITE, (inner.x + 36, inner.centery + 4), "midleft", True)
    if detail:
        txt(surface, detail, rect.h * .14, color, (inner.right - 7, inner.bottom - 5), "bottomright", True)
    if not valid:
        invalid_data_flag(surface, inner)

def mini_dial(surface, center, radius, value, label, readout, color, valid=True):
    """Compact panel-mounted analog indicator for dense status cards."""
    value = max(0.0, min(1.0, float(value or 0)))
    pygame.draw.circle(surface, BEZEL, center, radius + 4)
    pygame.draw.circle(surface, INSTRUMENT, center, radius)
    start, span = math.radians(140), math.radians(260)
    for index in range(9):
        angle = start + span * index / 8
        outer = (center[0] + math.cos(angle) * radius * .86,
                 center[1] + math.sin(angle) * radius * .86)
        inner = (center[0] + math.cos(angle) * radius * (.66 if index % 2 == 0 else .73),
                 center[1] + math.sin(angle) * radius * (.66 if index % 2 == 0 else .73))
        pygame.draw.line(surface, CREAM, inner, outer, 2 if index % 2 == 0 else 1)
    needle = start + span * value
    endpoint = (center[0] + math.cos(needle) * radius * .62,
                center[1] + math.sin(needle) * radius * .62)
    pygame.draw.line(surface, color, center, endpoint, 2)
    pygame.draw.circle(surface, BEZEL, center, 4)
    readout_rect = pygame.Rect(center[0] - int(radius * .48), center[1] + int(radius * .16),
                               int(radius * .96), max(12, int(radius * .30)))
    pygame.draw.rect(surface, BLACK, readout_rect)
    pygame.draw.rect(surface, BEZEL, readout_rect, 1)
    txt(surface, readout, radius * .23, WHITE, readout_rect.center, "center", True)
    txt(surface, label, radius * .25, CREAM,
        (center[0], center[1] + radius + 2), "midtop", True)
    if not valid:
        invalid_data_flag(surface, pygame.Rect(center[0] - radius, center[1] - radius,
                                               radius * 2, radius * 2))

def network_instrument_card(surface, rect, label, info):
    """Network link card with independent logarithmic RX and TX bit-rate dials."""
    pygame.draw.rect(surface, (73, 78, 73), rect, border_radius=4)
    pygame.draw.rect(surface, (158, 159, 145), rect, 2, border_radius=4)
    inner = rect.inflate(-8, -8)
    pygame.draw.rect(surface, (5, 12, 13), inner, border_radius=2)
    link_up = bool(info.get("up"))
    lamp = (inner.x + 17, inner.y + 18)
    pygame.draw.circle(surface, BEZEL, lamp, 10)
    pygame.draw.circle(surface, GREEN if link_up else RED, lamp, 6)
    txt(surface, label, rect.h * .16, CREAM, (inner.x + 34, inner.y + 5), bold=True)
    txt(surface, info.get("ipv4", "LINK DOWN") if link_up else "LINK DOWN",
        rect.h * .24, WHITE, (inner.x + 12, inner.centery + 10), "midleft", True)
    radius = max(22, int(rect.h * .25))
    centers = ((inner.x + int(inner.w * .68), inner.centery - 1),
               (inner.x + int(inner.w * .87), inner.centery - 1))
    for center, key, dial_label, color in ((centers[0], "rx_rate", "RX b/s", GREEN),
                                           (centers[1], "tx_rate", "TX b/s", CYAN)):
        byte_rate = float(info.get(key) or 0)
        bit_rate = byte_rate * 8
        normalized = min(1.0, math.log10(1 + bit_rate) / 8.0)
        mini_dial(surface, center, radius, normalized, dial_label,
                  format_bitrate(byte_rate), color, link_up)

def dual_door_card(surface, rect, left_state, right_state, valid=True):
    """Two independent, bezel-mounted shuttle-bay door indicators."""
    pygame.draw.rect(surface, (73, 78, 73), rect, border_radius=4)
    pygame.draw.rect(surface, (158, 159, 145), rect, 2, border_radius=4)
    inner = rect.inflate(-8, -8)
    pygame.draw.rect(surface, (5, 12, 13), inner, border_radius=2)
    txt(surface, "SHUTTLE BAY DOORS", rect.h * .18, CREAM,
        (inner.x + 8, inner.y + 4), bold=True)
    for position, label, state in ((.25, "LEFT", left_state), (.72, "RIGHT", right_state)):
        state_text = str(state or "UNKNOWN").upper()
        color = GREEN if state_text == "CLOSED" else AMBER if state_text == "OPEN" else RED
        center = (int(inner.x + inner.w * position), int(inner.y + inner.h * .66))
        pygame.draw.circle(surface, BEZEL, center, 10)
        pygame.draw.circle(surface, (28, 31, 28), center, 7)
        pygame.draw.circle(surface, color, center, 6)
        pygame.draw.circle(surface, tuple(min(255, channel + 65) for channel in color),
                           (center[0] - 2, center[1] - 2), 2)
        txt(surface, f"{label} {state_text}", rect.h * .22, WHITE,
            (center[0] + 16, center[1]), "midleft", True)
    if not valid:
        invalid_data_flag(surface, inner)

def led_segment_readout(surface, rect, value, color=AMBER):
    """Four-digit, seven-segment display with a fixed decimal point."""
    pygame.draw.rect(surface, (55, 58, 52), rect, border_radius=3)
    window = rect.inflate(-5, -5)
    pygame.draw.rect(surface, (7, 5, 3), window, border_radius=2)
    text_value = "--.--" if value is None else f"{min(99.99, max(0.0, value)):05.2f}"
    digit_map = {
        "0": "abcedf", "1": "bc", "2": "abdeg", "3": "abcdg",
        "4": "bcfg", "5": "acdfg", "6": "acdefg", "7": "abc",
        "8": "abcdefg", "9": "abcdfg", "-": "g",
    }
    digit_count = sum(character != "." for character in text_value)
    dot_count = len(text_value) - digit_count
    gap = max(2, int(window.h * .06))
    digit_w = int((window.w - gap * (digit_count + dot_count - 1)) /
                  (digit_count + dot_count * .28))
    dot_w = max(4, int(digit_w * .28))
    thickness = max(3, int(window.h * .12))
    digit_h = window.h - 4
    off_color = tuple(max(6, int(channel * .12)) for channel in color)
    x = window.x + max(2, (window.w - (digit_count * digit_w + dot_count * dot_w +
                                      gap * (digit_count + dot_count - 1))) // 2)
    for character in text_value:
        if character == ".":
            pygame.draw.circle(surface, color,
                               (x + dot_w // 2, window.bottom - thickness),
                               max(2, thickness // 2))
            x += dot_w + gap
            continue
        segments = digit_map.get(character, "")
        horizontal_w = digit_w - thickness * 2
        half = digit_h // 2
        shapes = {
            "a": pygame.Rect(x + thickness, window.y + 2, horizontal_w, thickness),
            "g": pygame.Rect(x + thickness, window.y + half - thickness // 2,
                             horizontal_w, thickness),
            "d": pygame.Rect(x + thickness, window.y + digit_h - thickness,
                             horizontal_w, thickness),
            "f": pygame.Rect(x + 2, window.y + thickness, thickness, half - thickness),
            "b": pygame.Rect(x + digit_w - thickness - 2, window.y + thickness,
                             thickness, half - thickness),
            "e": pygame.Rect(x + 2, window.y + half, thickness, half - thickness),
            "c": pygame.Rect(x + digit_w - thickness - 2, window.y + half,
                             thickness, half - thickness),
        }
        for name, segment_rect in shapes.items():
            pygame.draw.rect(surface, color if name in segments else off_color,
                             segment_rect, border_radius=max(1, thickness // 2))
        x += digit_w + gap

def precipitation_panel(surface, rect, rate_inh, precip_type, total_in, source,
                        rate_valid=True, total_valid=True):
    """Apollo-style rain rate tape, precipitation lamps, and local-day counter."""
    gap = 8
    rate_rect = pygame.Rect(rect.x, rect.y, int(rect.w * .54), rect.h)
    total_rect = pygame.Rect(rate_rect.right + gap, rect.y, rect.right - rate_rect.right - gap, rect.h)
    rate_scale = min(1.0, math.sqrt(max(0.0, rate_inh) / 2.0))
    edge_meter(surface, rate_rect, rate_scale, "RAIN RATE IN/HR", CYAN,
               rate_valid, f"{rate_inh:.2f}")

    pygame.draw.rect(surface, (73, 78, 73), total_rect, border_radius=4)
    pygame.draw.rect(surface, (158, 159, 145), total_rect, 2, border_radius=4)
    inner = total_rect.inflate(-8, -8)
    pygame.draw.rect(surface, (5, 12, 13), inner, border_radius=2)
    txt(surface, f"LOCAL DAY ACCUM • {source}", rect.h * .13, CREAM,
        (inner.x + 7, inner.y + 3), bold=True)
    led_rect = pygame.Rect(inner.x + 7, inner.y + int(inner.h * .27),
                           int(inner.w * .53), int(inner.h * .56))
    led_segment_readout(surface, led_rect, total_in)
    unit_label = "TRACE • INCHES" if total_in is not None and 0 < total_in < .005 else "INCHES"
    txt(surface, unit_label, rect.h * .085, MUTED,
        (led_rect.centerx, inner.bottom - 2), "midbottom", True)
    types = ((0, "DRY"), (1, "RAIN"), (2, "HAIL"), (3, "MIX"))
    for index, (code, label) in enumerate(types):
        column, row = index % 2, index // 2
        x = int(inner.x + inner.w * (.65 + column * .21))
        y = int(inner.y + inner.h * (.43 + row * .34))
        color = GREEN if code == 0 else CYAN if code == 1 else AMBER if code == 2 else RED
        pygame.draw.circle(surface, BEZEL, (x, y), 6)
        pygame.draw.circle(surface, color if precip_type == code else (24, 28, 25),
                           (x, y), 4)
        txt(surface, label, rect.h * .095, MUTED, (x + 7, y), "midleft", True)
    if not total_valid:
        invalid_data_flag(surface, inner)

def draw_ship_status(surface, now, data):
    """Apollo/steampunk telemetry panel; all data is read-only."""
    r, w, h = layout(surface.get_size()), *surface.get_size()
    surface.fill(BLACK)
    panel(surface, r["header"], NAVY, CYAN)
    txt(surface, "USS ENTERPRISE • NCC-1701", h * .027, MUTED,
        (r["header"].x + 24, r["header"].y + 15), bold=True)
    txt(surface, "COMPUTATION / WEATHER", h * .047, WHITE,
        (r["header"].x + 24, r["header"].bottom - 16), "bottomleft", True)
    draw_mode_selector(surface, r["mode_selector"])
    fresh = time.time() - data.get("updated", 0) < 4
    status_color = GREEN if fresh else AMBER
    pygame.draw.circle(surface, status_color, (r["header"].right - 38, r["header"].centery), 14)
    txt(surface, "NOMINAL" if fresh else "DEGRADED", h * .026, status_color,
        (r["header"].right - 66, r["header"].centery), "midright", True)

    margin, gap = int(w * .018), int(w * .012)
    body_y = r["header"].bottom + gap
    body_h = status_nav_layout(surface.get_size())["NETWORK"].y - body_y - gap
    content_x = r["mode_selector"].right + gap
    content_w = w - margin - content_x
    left_w, center_w = int(content_w * .29), int(content_w * .36)
    left = pygame.Rect(content_x, body_y, left_w, body_h)
    center = pygame.Rect(left.right + gap, body_y, center_w, body_h)
    right = pygame.Rect(center.right + gap, body_y, w - margin - center.right - gap, body_h)

    system = data.get("system", {})
    panel(surface, left, PANEL, BLUE, 12)
    txt(surface, "COMPUTATION CORE", h * .025, CREAM, (left.x + 18, left.y + 14), bold=True)
    gauge_y = left.y + int(left.h * .27)
    radius = int(min(left.w * .19, left.h * .17))
    gauge(surface, (left.x + int(left.w * .28), gauge_y), radius,
          min(1, system.get("cpu", 0) / 100), "CORE LOAD", GREEN,
          valid=fresh and system.get("cpu") is not None)
    gauge(surface, (left.x + int(left.w * .72), gauge_y), radius,
          min(1, system.get("memory", 0) / 100), "LOGIC STORAGE", CYAN,
          valid=fresh and system.get("memory") is not None)
    card_h = int(h * .080)
    temperature_c = system.get("temperature_c")
    temperature_f = temperature_c * 9 / 5 + 32 if temperature_c is not None else None
    telemetry_card(surface, pygame.Rect(left.x + 18, left.y + int(left.h * .50), left.w - 36, card_h),
                   "CORE THERMAL", (f"{temperature_f:05.1f} °F / {temperature_c:04.1f} °C"
                                    if temperature_f is not None else "N/A"),
                   "NORMAL" if temperature_c is not None and temperature_c < 75 else "CAUTION",
                   GREEN if temperature_c is not None and temperature_c < 75 else AMBER,
                   temperature_c is not None, fresh and temperature_c is not None)
    telemetry_card(surface, pygame.Rect(left.x + 18, left.y + int(left.h * .62), left.w - 36, card_h),
                   "MISSION ELAPSED TIME", format_uptime(system.get("uptime")), "PI UPTIME", AMBER,
                   valid=fresh and system.get("uptime") is not None)
    sys_age, sys_age_color = data_age(data.get("updated"))
    telemetry_card(surface, pygame.Rect(left.x + 18, left.y + int(left.h * .74), left.w - 36, card_h),
                   "TELEMETRY CLOCK", time.strftime("%H:%M:%S"), sys_age, sys_age_color, fresh, fresh)

    panel(surface, center, PANEL, BLUE, 12)
    txt(surface, "EXTERNAL ENVIRONMENT", h * .025, CREAM, (center.x + 18, center.y + 14), bold=True)
    weather = data.get("weather", {})
    forecast = data.get("forecast", {})
    local_weather = bool(weather.get("temperature_c") is not None)
    air_f = (weather.get("temperature_c") * 9 / 5 + 32) if local_weather else forecast.get("temperature_f")
    weather_age = data.get("weatherflow", {}).get("updated") if local_weather else forecast.get("updated")
    weather_valid = bool(weather_age and time.time() - weather_age < 180 and air_f is not None)
    big_radius = int(min(center.w * .15, center.h * .14))
    dial_y = center.y + int(center.h * .25)
    gauge(surface, (center.x + int(center.w * .28), dial_y), big_radius,
          max(0, min(1, (float(air_f or 0) + 10) / 130)), "AIR TEMP °F", AMBER,
          f"{air_f:.0f}" if air_f is not None else "—", weather_valid)
    wind_mph = float(weather.get("wind_mps") or 0) * 2.23694
    direction = weather.get("wind_direction")
    gauge(surface, (center.x + int(center.w * .72), dial_y), big_radius,
          min(1, wind_mph / 50), f"WIND {int(direction):03d}°" if direction is not None else "WIND",
          CYAN, f"{wind_mph:.0f}", weather_valid and direction is not None)
    meter_y = center.y + int(center.h * .45)
    meter_h = int(h * .065)
    pressure = float(weather.get("pressure_mb") or 0)
    humidity = float(weather.get("humidity") or 0)
    rain_rate_inh = float(weather.get("rain_rate_mmh") or 0) / 25.4
    local_day_mm = weather.get("local_day_rain_mm")
    local_day_in = float(local_day_mm) / 25.4 if local_day_mm is not None else None
    precip_type = int(weather.get("precip_type") or 0)
    lightning_5m = int(weather.get("lightning_5m") or 0)
    lightning_km = weather.get("last_lightning_km")
    if lightning_km is None:
        lightning_km = weather.get("lightning_5m_km")
    lightning_miles = float(lightning_km) * .621371 if lightning_km is not None else None
    pressure_pen_tape(surface, pygame.Rect(center.x + 20, meter_y, center.w - 40, meter_h),
                      pressure if weather.get("pressure_mb") is not None else None,
                      weather_valid and weather.get("pressure_mb") is not None)
    edge_meter(surface, pygame.Rect(center.x + 20, meter_y + meter_h + 10, center.w - 40, meter_h),
               humidity / 100, "ATMOSPHERIC HUMIDITY", CYAN,
               weather_valid and weather.get("humidity") is not None)
    lightning_y = meter_y + (meter_h + 10) * 2 + 10
    lightning_rect = pygame.Rect(center.x + 20, lightning_y, center.w - 40, int(h * .092))
    lightning_color = RED if lightning_5m else CYAN
    panel(surface, lightning_rect, (5, 12, 13), lightning_color, 5)
    pygame.draw.circle(surface, BEZEL, (lightning_rect.x + 20, lightning_rect.y + 22), 10)
    pygame.draw.circle(surface, lightning_color, (lightning_rect.x + 20, lightning_rect.y + 22), 6)
    txt(surface, "LIGHTNING PROXIMITY", lightning_rect.h * .16, CREAM,
        (lightning_rect.x + 38, lightning_rect.y + 9), bold=True)
    divider_x = lightning_rect.centerx
    pygame.draw.line(surface, BEZEL, (divider_x, lightning_rect.y + 12),
                     (divider_x, lightning_rect.bottom - 12), 2)
    txt(surface, f"{lightning_5m:02d}", lightning_rect.h * .42, lightning_color,
        (lightning_rect.x + lightning_rect.w * .27, lightning_rect.centery + 12), "center", True)
    txt(surface, "STRIKES / 5 MIN", lightning_rect.h * .13, MUTED,
        (lightning_rect.x + lightning_rect.w * .27, lightning_rect.bottom - 7), "midbottom", True)
    distance_text = f"{lightning_miles:.1f} MI" if lightning_miles is not None else "— MI"
    txt(surface, distance_text, lightning_rect.h * .34, WHITE,
        (lightning_rect.x + lightning_rect.w * .73, lightning_rect.centery + 10), "center", True)
    txt(surface, "LAST / AVG RANGE", lightning_rect.h * .13, MUTED,
        (lightning_rect.x + lightning_rect.w * .73, lightning_rect.bottom - 7), "midbottom", True)
    lightning_valid = weather_valid and "lightning_5m" in weather
    if not lightning_valid:
        invalid_data_flag(surface, lightning_rect)
    close_timestamp = weather.get("last_close_lightning")
    close_km = weather.get("last_close_lightning_km")
    close_age = time.time() - float(close_timestamp) if close_timestamp is not None else None
    if (lightning_valid and close_km is not None and close_age is not None and
            0 <= close_age <= 1800 and float(close_km) <= 0.804672):
        lightning_warning_cover(surface, lightning_rect, float(close_km) * .621371, close_age)
    detail_y = lightning_rect.bottom + 8
    cloud_age = data.get("tempest_cloud", {}).get("updated")
    cloud_valid = bool(cloud_age and time.time() - cloud_age < 180 and local_day_in is not None)
    precip_rect = pygame.Rect(center.x + 20, detail_y, center.w - 40, int(h * .105))
    precipitation_panel(surface, precip_rect, rain_rate_inh, precip_type, local_day_in,
                        weather.get("rain_source", "TEMPEST"),
                        weather_valid and weather.get("rain_rate_mmh") is not None,
                        cloud_valid)

    panel(surface, right, PANEL, BLUE, 12)
    txt(surface, "COMMUNICATIONS", h * .025, CREAM, (right.x + 18, right.y + 14), bold=True)
    network = data.get("network", {})
    network_y = right.y + 54
    network_h = int(h * .105)
    for index, (name, label) in enumerate((("wlan0", "WIRELESS TELEMETRY"), ("eth0", "HARDLINE TELEMETRY"))):
        info = network.get(name, {})
        card = pygame.Rect(right.x + 16, network_y + index * (network_h + 10), right.w - 32, network_h)
        network_instrument_card(surface, card, label, info)
    aux_y = network_y + 2 * (network_h + 10) + 12
    txt(surface, "AUXILIARY SENSOR BUS", h * .020, AMBER, (right.x + 18, aux_y), bold=True)
    aux_y += 34
    house = data.get("house", {})
    mqtt_info = data.get("mqtt", {})
    small_h = int(h * .068)
    wine = house.get("wine_cellar", {})
    keg = house.get("keg", {})
    telemetry_card(surface, pygame.Rect(right.x + 16, aux_y, right.w - 32, small_h),
                   "WINE CELLAR CLIMATE", f"{wine.get('temperature_f', '—')} °F  {wine.get('humidity', '—')}%",
                   data_age(wine.get("updated"))[0], CYAN, bool(wine.get("updated")),
                   bool(wine.get("updated") and time.time() - wine["updated"] < 3600))
    aux_y += small_h + 7
    telemetry_card(surface, pygame.Rect(right.x + 16, aux_y, right.w - 32, small_h),
                   "KEG THERMAL", f"{keg.get('temperature_f', '—')} °F  {keg.get('humidity', '—')}%",
                   data_age(keg.get("updated"))[0], AMBER, bool(keg.get("updated")),
                   bool(keg.get("updated") and time.time() - keg["updated"] < 3600))
    aux_y += small_h + 7
    left_door_data = house.get("garage_left", {})
    right_door_data = house.get("garage_right", {})
    left_door = left_door_data.get("state", "—")
    right_door = right_door_data.get("state", "—")
    doors_valid = all(item.get("updated") and time.time() - item["updated"] < 86400
                      for item in (left_door_data, right_door_data))
    dual_door_card(surface, pygame.Rect(right.x + 16, aux_y, right.w - 32, small_h),
                   left_door, right_door, doors_valid)
    aux_y += small_h + 7
    leaks = house.get("leaks", {})
    current_items = [item for item in leaks.values()
                     if item.get("updated") and time.time() - item["updated"] < 86400]
    wet = sum(1 for item in current_items if item.get("state") == "wet")
    current = len(current_items)
    leak_safe = wet == 0
    telemetry_card(surface, pygame.Rect(right.x + 16, aux_y, right.w - 32, small_h),
                   "WATER RECLAMATION", "DRY" if leak_safe else f"{wet} LEAK ALERT",
                   f"{current}/{len(leaks)} CURRENT", GREEN if leak_safe else RED, leak_safe,
                   bool(current))
    mqtt_age, mqtt_color = data_age(mqtt_info.get("updated"))
    txt(surface, f"SENSOR BUS {'ONLINE' if mqtt_info.get('connected') else 'OFFLINE'}  •  {mqtt_age}",
        h * .014, mqtt_color, (right.centerx, right.bottom - 13), "midbottom", True)
    draw_status_nav(surface)

def compact_age(timestamp):
    if not timestamp:
        return "NO REPORT"
    age = max(0, time.time() - timestamp)
    if age < 60:
        return f"{int(age)} SEC AGO"
    if age < 3600:
        return f"{int(age // 60)} MIN AGO"
    if age < 86400:
        return f"{int(age // 3600)} HR AGO"
    return f"{int(age // 86400)} DAY AGO"

def draw_environment_status(surface, now, data):
    """YoLink environmental page; renders truthfully even before configuration."""
    r, w, h = layout(surface.get_size()), *surface.get_size()
    surface.fill(BLACK)
    panel(surface, r["header"], NAVY, CYAN)
    txt(surface, "USS ENTERPRISE • NCC-1701", h * .027, MUTED,
        (r["header"].x + 24, r["header"].y + 15), bold=True)
    txt(surface, "HABITATION SENSOR GRID", h * .047, WHITE,
        (r["header"].x + 24, r["header"].bottom - 16), "bottomleft", True)
    draw_mode_selector(surface, r["mode_selector"])
    source = data.get("source", {})
    source_state = source.get("state", "NOT CONFIGURED")
    state_color = GREEN if source_state == "CONNECTED" else AMBER if source_state in ("RECONNECTING", "NOT CONFIGURED") else RED
    pygame.draw.circle(surface, state_color, (r["header"].right - 38, r["header"].centery), 14)
    txt(surface, source_state, h * .026, state_color,
        (r["header"].right - 66, r["header"].centery), "midright", True)

    gap = int(w * .012)
    body_y = r["header"].bottom + gap
    nav_y = status_nav_layout(surface.get_size())["NETWORK"].y
    body_h = nav_y - body_y - gap
    content_x = r["mode_selector"].right + gap
    content_w = w - int(w * .018) - content_x
    temps = list(data.get("temperature_sensors", []))
    door = data.get("shed", {})

    door_w = int(content_w * .29)
    grid = pygame.Rect(content_x, body_y, content_w - door_w - gap, body_h)
    door_panel = pygame.Rect(grid.right + gap, body_y, door_w, body_h)
    panel(surface, grid, PANEL, BLUE, 10)
    txt(surface, "HABITATION CLIMATE", h * .025, CREAM, (grid.x + 18, grid.y + 14), bold=True)

    if not temps:
        message = "YOLINK CREDENTIALS REQUIRED" if source_state == "NOT CONFIGURED" else "NO SUPPORTED TEMPERATURE SENSORS"
        txt(surface, message, h * .031, AMBER, grid.center, "center", True)
        txt(surface, "CONFIGURE PRIVATELY ON THE RASPBERRY PI", h * .017, MUTED,
            (grid.centerx, grid.centery + 44), "center", True)
    else:
        page_size = 6
        page_count = max(1, math.ceil(len(temps) / page_size))
        active_page = min(environment_sensor_page, page_count - 1)
        shown = temps[active_page * page_size:(active_page + 1) * page_size]
        card_gap = 12
        columns, rows = 2, 3
        top = grid.y + 58
        card_w = (grid.w - 36 - card_gap) // columns
        card_h = (grid.bottom - top - 18 - card_gap * (rows - 1)) // rows
        for index, sensor in enumerate(shown):
            col, row = index % columns, index // columns
            card = pygame.Rect(grid.x + 18 + col * (card_w + card_gap),
                               top + row * (card_h + card_gap), card_w, card_h)
            online = sensor.get("online") is True
            warning = sensor.get("alarm") or not online
            color = RED if sensor.get("error") else AMBER if warning else GREEN
            panel(surface, card, (8, 16, 17), BEZEL, 5)
            txt(surface, str(sensor.get("name", "UNNAMED"))[:28].upper(), card.h * .13, CREAM,
                (card.x + 13, card.y + 10), bold=True)
            temperature = sensor.get("temperature_f")
            txt(surface, f"{temperature:05.1f} °F" if temperature is not None else "— °F",
                card.h * .27, WHITE, (card.x + 13, card.centery - 3), "midleft", True)
            humidity = sensor.get("humidity")
            txt(surface, f"HUM {humidity:.0f}%" if humidity is not None else "HUM —",
                card.h * .12, CYAN, (card.x + 15, card.bottom - 17), "bottomleft", True)
            battery = sensor.get("battery")
            txt(surface, f"BAT {battery}/4" if battery is not None else "BAT —/4",
                card.h * .11, color, (card.right - 13, card.bottom - 18), "bottomright", True)
            txt(surface, compact_age(sensor.get("reported_at")), card.h * .09, MUTED,
                (card.right - 13, card.y + 12), "topright", True)
            pygame.draw.circle(surface, BEZEL, (card.right - 18, card.centery), 9)
            pygame.draw.circle(surface, color, (card.right - 18, card.centery), 5)
        if page_count > 1:
            pager = environment_pager_layout(surface.get_size())
            for name, rect in pager.items():
                enabled = (name == "PREVIOUS" and active_page > 0) or (name == "NEXT" and active_page + 1 < page_count)
                pygame.draw.rect(surface, (45, 48, 44), rect, border_radius=3)
                pygame.draw.rect(surface, AMBER if enabled else BEZEL, rect, 2, border_radius=3)
                txt(surface, "◀ PREV" if name == "PREVIOUS" else "NEXT ▶", rect.h * .30,
                    CREAM if enabled else MUTED, rect.center, "center", True)
            txt(surface, f"SENSOR PAGE {active_page + 1} / {page_count}", h * .012, CREAM,
                (grid.centerx, pager["NEXT"].centery), "center", True)

    panel(surface, door_panel, PANEL, BLUE, 10)
    txt(surface, "SHUTTLE BAY — SHED", h * .023, CREAM,
        (door_panel.x + 18, door_panel.y + 14), bold=True)
    door_state = str(door.get("state", "UNKNOWN")).upper()
    qualified = source_state == "CONNECTED" and door.get("online") is True
    if not qualified and door_state in ("OPEN", "CLOSED"):
        display_state = f"LAST KNOWN: {door_state}"
        door_color = AMBER
    else:
        display_state = door_state
        door_color = GREEN if door_state == "CLOSED" and qualified else AMBER if door_state == "OPEN" else RED
    lamp_center = (door_panel.centerx, door_panel.y + int(door_panel.h * .30))
    pygame.draw.circle(surface, BEZEL, lamp_center, int(door_panel.w * .16))
    pygame.draw.circle(surface, (18, 22, 20), lamp_center, int(door_panel.w * .135))
    pygame.draw.circle(surface, door_color, lamp_center, int(door_panel.w * .095))
    txt(surface, display_state, h * (.031 if len(display_state) < 14 else .021), door_color,
        (door_panel.centerx, door_panel.y + int(door_panel.h * .52)), "center", True)
    telemetry_card(surface, pygame.Rect(door_panel.x + 18, door_panel.y + int(door_panel.h * .61),
                                        door_panel.w - 36, int(h * .075)),
                   "LAST STATE CHANGE", compact_age(door.get("changed_at")),
                   "DEVICE ONLINE" if door.get("online") else "DEVICE STATUS UNKNOWN",
                   GREEN if door.get("online") else AMBER, bool(door.get("online")))
    telemetry_card(surface, pygame.Rect(door_panel.x + 18, door_panel.y + int(door_panel.h * .74),
                                        door_panel.w - 36, int(h * .075)),
                   "SENSOR BATTERY", f"LEVEL {door.get('battery', '—')} / 4",
                   compact_age(door.get("reported_at")), door_color, door.get("battery") is not None)
    source_age = compact_age(source.get("last_activity"))
    txt(surface, f"YOLINK {source_state}  •  SOURCE {source_age}",
        h * .012, state_color, (door_panel.centerx, door_panel.bottom - 16), "midbottom", True)
    draw_status_nav(surface)

HOUSE_GROUP_ORDER = ("BAR", "BREAKFAST NOOK", "COUCH", "DINING ROOM",
                     "FAMILY ROOM", "GARAGE REFRIGERATOR", "HALLWAY", "FENCE 1",
                     "PATIO 1", "PATIO 2", "PATIO 3", "PATIO AUDIO",
                     "BASEMENT SONOS", "GARAGE SONOS", "KITCHEN SONOS",
                     "LIVING ROOM SONOS", "MAIN BEDROOM SONOS", "OFFICE SONOS",
                     "EXERCISE ROOM BOSE")

def grouped_house_systems(data):
    grouped = {name: [] for name in HOUSE_GROUP_ORDER}
    for item in data.get("house", {}).get("systems", {}).values():
        if item.get("group") in grouped:
            grouped[item["group"]].append(item)
    return [(name, grouped[name]) for name in HOUSE_GROUP_ORDER]

def house_group_lines(devices):
    switches = [item.get("switch") for item in devices if item.get("switch") in ("on", "off")]
    playing = [item for item in devices if str(item.get("playbackStatus", "")).lower() == "playing"]
    water = [item.get("water") for item in devices if item.get("water")]
    temperatures = [item.get("temperature") for item in devices if item.get("temperature") is not None]
    online = [item.get("DeviceWatch-DeviceStatus") for item in devices
              if item.get("DeviceWatch-DeviceStatus")]
    lines = []
    if playing:
        audio = playing[0]
        track = audio.get("audioTrackData")
        if isinstance(track, dict) and track.get("title"):
            source = str(track.get("mediaSource") or "SONOS").upper()
            lines.append(f"NOW PLAYING  •  {source}")
            lines.append(str(track["title"]).upper()[:30])
            if track.get("artist"):
                lines.append(str(track["artist"]).upper()[:30])
        else:
            volume = audio.get("volume", audio.get("groupVolume"))
            lines.append(f"SONOS PLAYING  •  VOL {volume if volume is not None else '—'}")
    elif switches:
        lines.append(f"{sum(value == 'on' for value in switches)} ON  /  {len(switches)} CONTROLS")
    if water:
        lines.append("WATER " + " / ".join(str(value).upper() for value in water))
    if temperatures:
        lines.append(f"THERMAL {float(temperatures[0]):.1f} °F")
    if online and len(lines) < 3:
        lines.append(f"LINK {sum(value == 'online' for value in online)}/{len(online)} ONLINE")
    if not lines:
        lines.append("NO USABLE CAPABILITY DATA")
    updated = max((item.get("updated", 0) for item in devices), default=0)
    return lines[:3], updated

def draw_house_card(surface, rect, group, devices):
    lines, updated = house_group_lines(devices)
    stale = not updated or time.time() - updated > 7 * 86400
    fault = not devices or lines[0] == "NO USABLE CAPABILITY DATA"
    color = RED if fault else AMBER if stale else GREEN
    panel(surface, rect, (8, 16, 17), BEZEL, 5)
    pygame.draw.circle(surface, BEZEL, (rect.x + 18, rect.y + 20), 9)
    pygame.draw.circle(surface, color, (rect.x + 18, rect.y + 20), 5)
    title_scale = .080 if len(group) > 16 else .095 if len(group) > 12 else .125
    txt(surface, group, rect.h * title_scale, CREAM, (rect.x + 35, rect.y + 10), bold=True)
    txt(surface, compact_age(updated), rect.h * .065, color,
        (rect.right - 10, rect.bottom - 8), "bottomright", True)
    line_y = rect.y + int(rect.h * .42)
    for index, line in enumerate(lines):
        first_scale = .065 if len(line) > 22 else .085 if len(line) > 18 else .105
        txt(surface, line, rect.h * (first_scale if index == 0 else .080), WHITE if index == 0 else MUTED,
            (rect.x + 13, line_y + index * int(rect.h * .19)), "midleft", index == 0)

def draw_reservoir_scale(surface, rect, lake):
    panel(surface, rect, (6, 13, 15), BEZEL, 5)
    txt(surface, "LEWISVILLE RESERVOIR", rect.h * .055, CREAM, (rect.x + 16, rect.y + 12), bold=True)
    txt(surface, f"USGS PROV • {compact_age(lake.get('updated'))}", rect.h * .030, MUTED,
        (rect.x + 16, rect.y + int(rect.h * .11)), "topleft", True)
    elevation = lake.get("elevation_ft")
    fault = lake.get("error") or elevation is None
    txt(surface, f"{elevation:06.2f} FT" if elevation is not None else "DATA LINK FAULT",
        rect.h * .090, RED if fault else WHITE, (rect.centerx, rect.y + int(rect.h * .22)), "center", True)
    track = pygame.Rect(rect.x + 35, rect.y + int(rect.h * .42), rect.w - 70, int(rect.h * .12))
    pygame.draw.rect(surface, (3, 8, 9), track)
    pygame.draw.rect(surface, BEZEL, track, 3)
    minimum, maximum = 481.0, 552.0
    if elevation is not None:
        fraction = max(0, min(1, (float(elevation) - minimum) / (maximum - minimum)))
        fill_color = RED if elevation >= 552 else ORANGE if elevation >= 532 else CYAN
        pygame.draw.rect(surface, tuple(channel // 4 for channel in fill_color),
                         pygame.Rect(track.x + 3, track.y + 3, int((track.w - 6) * fraction), track.h - 6))
        x = track.x + int(track.w * fraction)
        pygame.draw.polygon(surface, fill_color,
                            ((x, track.y - 11), (x - 8, track.y - 1), (x + 8, track.y - 1)))
    markers = ((481, "DEAD", RED, 0), (522, "NORMAL", GREEN, 0),
               (532, "SPILL", ORANGE, 1), (552, "EMERG", RED, 0))
    for value, label, color, row in markers:
        x = track.x + int(track.w * (value - minimum) / (maximum - minimum))
        pygame.draw.line(surface, color, (x, track.y), (x, track.bottom + 10), 3)
        anchor = "topleft" if value == 481 else "topright" if value == 552 else "midtop"
        label_x = track.x if value == 481 else track.right if value == 552 else x
        txt(surface, f"{value} {label}", rect.h * .028, color,
            (label_x, track.bottom + 12 + row * int(rect.h * .055)), anchor, True)
    if elevation is not None:
        delta = float(elevation) - 522.0
        detail = f"{abs(delta):.2f} FT {'ABOVE' if delta >= 0 else 'BELOW'} NORMAL"
        to_spillway = 532.0 - float(elevation)
        if to_spillway >= 0:
            detail += f"  •  {to_spillway:.2f} FT TO SPILLWAY"
        if lake.get("error"):
            detail = f"LAST VALID {compact_age(lake.get('updated'))}  •  {lake['error']}"
        txt(surface, detail, rect.h * .038, RED if lake.get("error") else AMBER if elevation >= 532 else CREAM,
            (rect.centerx, rect.bottom - 18), "midbottom", True)
    else:
        txt(surface, lake.get("error") or "NO RESERVOIR REPORT", rect.h * .038, RED,
            (rect.centerx, rect.bottom - 18), "midbottom", True)

def draw_house_status(surface, now, data):
    r, w, h = layout(surface.get_size()), *surface.get_size()
    surface.fill(BLACK)
    panel(surface, r["header"], NAVY, CYAN)
    txt(surface, "USS ENTERPRISE • NCC-1701", h * .027, MUTED,
        (r["header"].x + 24, r["header"].y + 15), bold=True)
    txt(surface, "HOUSE SYSTEMS MONITOR", h * .047, WHITE,
        (r["header"].x + 24, r["header"].bottom - 16), "bottomleft", True)
    draw_mode_selector(surface, r["mode_selector"])
    mqtt_info = data.get("mqtt", {})
    connected = bool(mqtt_info.get("connected"))
    link_color = GREEN if connected else RED
    pygame.draw.circle(surface, link_color, (r["header"].right - 38, r["header"].centery), 14)
    txt(surface, "LOCAL BUS ONLINE" if connected else "LOCAL BUS OFFLINE", h * .024, link_color,
        (r["header"].right - 66, r["header"].centery), "midright", True)

    gap, margin = int(w * .012), int(w * .018)
    body_y = r["header"].bottom + gap
    body_h = status_nav_layout(surface.get_size())["NETWORK"].y - body_y - gap
    content_x = r["mode_selector"].right + gap
    content_w = w - margin - content_x
    grid_w = int(content_w * .70)
    grid = pygame.Rect(content_x, body_y, grid_w, body_h)
    water_panel = pygame.Rect(grid.right + gap, body_y, content_w - grid_w - gap, body_h)
    panel(surface, grid, PANEL, BLUE, 10)
    txt(surface, "SELECTED HABITATION SYSTEMS", h * .024, CREAM,
        (grid.x + 18, grid.y + 14), bold=True)
    groups = grouped_house_systems(data)
    page_size, page_count = 12, max(1, math.ceil(len(groups) / 12))
    active_page = min(house_system_page, page_count - 1)
    shown = groups[active_page * page_size:(active_page + 1) * page_size]
    card_gap, top = 12, grid.y + 58
    columns, rows = 4, 3
    card_w = (grid.w - 36 - card_gap * (columns - 1)) // columns
    pager = house_pager_layout(surface.get_size())
    cards_bottom = pager["PREVIOUS"].y - 8 if page_count > 1 else grid.bottom - 18
    card_h = (cards_bottom - top - card_gap * (rows - 1)) // rows
    for index, (group, devices) in enumerate(shown):
        col, row = index % columns, index // columns
        draw_house_card(surface, pygame.Rect(grid.x + 18 + col * (card_w + card_gap),
                                             top + row * (card_h + card_gap), card_w, card_h),
                        group, devices)
    if page_count > 1:
        for name, rect in pager.items():
            enabled = (name == "PREVIOUS" and active_page > 0) or (name == "NEXT" and active_page + 1 < page_count)
            pygame.draw.rect(surface, (45, 48, 44), rect, border_radius=3)
            pygame.draw.rect(surface, GREEN if enabled else BEZEL, rect, 2, border_radius=3)
            txt(surface, "◀ PREV" if name == "PREVIOUS" else "NEXT ▶", rect.h * .30,
                CREAM if enabled else MUTED, rect.center, "center", True)
        txt(surface, f"SYSTEM PAGE {active_page + 1} / {page_count}", h * .012, CREAM,
            (grid.centerx, pager["NEXT"].centery), "center", True)

    panel(surface, water_panel, PANEL, BLUE, 10)
    txt(surface, "WATER RESOURCES", h * .024, CREAM,
        (water_panel.x + 18, water_panel.y + 14), bold=True)
    water = data.get("water", {})
    lake_rect = pygame.Rect(water_panel.x + 14, water_panel.y + 50,
                            water_panel.w - 28, int(water_panel.h * .49))
    draw_reservoir_scale(surface, lake_rect, water.get("lake", {}))
    trinity = water.get("trinity", {})
    river_rect = pygame.Rect(water_panel.x + 14, lake_rect.bottom + 12,
                             water_panel.w - 28, water_panel.bottom - lake_rect.bottom - 26)
    panel(surface, river_rect, (6, 13, 15), BEZEL, 5)
    txt(surface, "TRINITY OUTFLOW", river_rect.h * .11, CREAM,
        (river_rect.x + 15, river_rect.y + 11), bold=True)
    txt(surface, f"USGS PROV • {compact_age(trinity.get('updated'))}", river_rect.h * .050, MUTED,
        (river_rect.x + 15, river_rect.y + int(river_rect.h * .15)), "topleft", True)
    flow = trinity.get("flow_cfs")
    gage = trinity.get("gage_ft")
    if flow is not None:
        value = f"{flow:,.0f} CFS"
        detail = f"GAGE {gage:.2f} FT" if gage is not None else "GAGE NOT REPORTED"
        if trinity.get("error"):
            detail = f"LAST VALID {compact_age(trinity.get('updated'))}  •  {trinity['error']}"
        color = RED if trinity.get("error") else CYAN
    else:
        value, detail, color = "DATA LINK FAULT", trinity.get("error") or "NO OUTFLOW REPORT", RED
    txt(surface, value, river_rect.h * .18, color, (river_rect.centerx, river_rect.centery - 3), "center", True)
    txt(surface, detail, river_rect.h * .09, MUTED, (river_rect.centerx, river_rect.bottom - 17), "midbottom", True)
    draw_status_nav(surface)

def battery_inventory(telemetry_data, yolink_data):
    """Combine battery-capable sources without pretending old readings are live."""
    items = [dict(item) for item in telemetry_data.get("house", {}).get("batteries", {}).values()]
    for sensor in yolink_data.get("temperature_sensors", []):
        level = sensor.get("battery")
        if level is not None:
            items.append({
                "name": sensor.get("name", "YOLINK SENSOR"), "source": "YOLINK",
                "level_pct": max(0, min(100, float(level) * 25)),
                "value_text": f"{level}/4", "reported_at": sensor.get("reported_at"),
                "activity_at": sensor.get("reported_at"), "online": sensor.get("online"),
                "stale_after": 86400,
            })
    shed = yolink_data.get("shed", {})
    if shed.get("battery") is not None:
        level = shed["battery"]
        items.append({
            "name": "SHED DOOR", "source": "YOLINK",
            "level_pct": max(0, min(100, float(level) * 25)),
            "value_text": f"{level}/4", "reported_at": shed.get("reported_at"),
            "activity_at": shed.get("reported_at"), "online": shed.get("online"),
            "stale_after": 86400,
        })
    weather = telemetry_data.get("weather", {})
    voltage = weather.get("battery_v")
    if voltage is not None:
        weatherflow = telemetry_data.get("weatherflow", {})
        items.append({
            "name": "TEMPEST STATION", "source": "WEATHERFLOW",
            "level_pct": max(0, min(100, (float(voltage) - 2.2) / .6 * 100)),
            "value_text": f"{float(voltage):.2f} V", "reported_at": weatherflow.get("updated"),
            "activity_at": weatherflow.get("updated"), "online": weatherflow.get("connected"),
            "stale_after": 600,
        })

    now = time.time()
    for item in items:
        activity = item.get("activity_at") or item.get("reported_at") or 0
        stale_after = item.get("stale_after", 7 * 86400)
        item["stale"] = not activity or now - activity > stale_after
        item["offline"] = item.get("online") in (False, "offline")
        level = float(item.get("level_pct") or 0)
        item["condition"] = ("CRITICAL" if level <= 15 else "LOW" if level <= 30 else
                             "OFFLINE" if item["offline"] else "STALE" if item["stale"] else "GOOD")
    order = {"CRITICAL": 0, "LOW": 1, "OFFLINE": 2, "STALE": 3, "GOOD": 4}
    return sorted(items, key=lambda item: (order[item["condition"]],
                                           float(item.get("level_pct") or 0),
                                           str(item.get("name", ""))))

def draw_battery_card(surface, rect, item):
    condition = item["condition"]
    color = RED if condition in ("CRITICAL", "OFFLINE") else AMBER if condition in ("LOW", "STALE") else GREEN
    panel(surface, rect, (8, 16, 17), BEZEL, 5)
    pygame.draw.circle(surface, BEZEL, (rect.x + 18, rect.y + 20), 9)
    pygame.draw.circle(surface, color, (rect.x + 18, rect.y + 20), 5)
    display_name = str(item.get("name", "UNNAMED"))[:36].upper()
    name_size = rect.h * (.085 if len(display_name) > 27 else .105)
    txt(surface, display_name, name_size, CREAM,
        (rect.x + 34, rect.y + 10), bold=True)
    txt(surface, str(item.get("source", "UNKNOWN")), rect.h * .075, MUTED,
        (rect.right - 12, rect.y + 12), "topright", True)
    txt(surface, item.get("value_text", "—"), rect.h * .25, color,
        (rect.x + 16, rect.centery), "midleft", True)
    bar_rect = pygame.Rect(rect.x + int(rect.w * .38), rect.centery - 12,
                           int(rect.w * .57), 24)
    pygame.draw.rect(surface, (3, 7, 8), bar_rect)
    segments = 10
    active = round(max(0, min(100, float(item.get("level_pct") or 0))) / 10)
    gap = 3
    segment_w = (bar_rect.w - gap * (segments + 1)) / segments
    for index in range(segments):
        segment = pygame.Rect(round(bar_rect.x + gap + index * (segment_w + gap)),
                              bar_rect.y + 4, max(2, round(segment_w)), bar_rect.h - 8)
        segment_color = (RED if index < 2 else AMBER if index < 4 else GREEN)
        pygame.draw.rect(surface, segment_color if index < active else (24, 34, 31), segment)
    txt(surface, condition, rect.h * .085, color,
        (rect.x + 15, rect.bottom - 13), "bottomleft", True)
    txt(surface, f"BATTERY {compact_age(item.get('reported_at'))}  •  DEVICE {compact_age(item.get('activity_at'))}",
        rect.h * .065, MUTED, (rect.right - 12, rect.bottom - 13), "bottomright", True)

def draw_power_status(surface, now, telemetry_data, yolink_data):
    r, w, h = layout(surface.get_size()), *surface.get_size()
    surface.fill(BLACK)
    panel(surface, r["header"], NAVY, CYAN)
    txt(surface, "USS ENTERPRISE • NCC-1701", h * .027, MUTED,
        (r["header"].x + 24, r["header"].y + 15), bold=True)
    txt(surface, "REMOTE POWER CELL STATUS", h * .047, WHITE,
        (r["header"].x + 24, r["header"].bottom - 16), "bottomleft", True)
    draw_mode_selector(surface, r["mode_selector"])
    items = battery_inventory(telemetry_data, yolink_data)
    fault_count = sum(item["condition"] != "GOOD" for item in items)
    status_color = AMBER if fault_count else GREEN
    pygame.draw.circle(surface, status_color, (r["header"].right - 38, r["header"].centery), 14)
    txt(surface, f"{fault_count:02d} SERVICE ITEMS" if fault_count else "ALL CELLS NOMINAL",
        h * .024, status_color, (r["header"].right - 66, r["header"].centery), "midright", True)

    gap, margin = int(w * .012), int(w * .018)
    body_y = r["header"].bottom + gap
    nav_y = status_nav_layout(surface.get_size())["NETWORK"].y
    content_x = r["mode_selector"].right + gap
    content_w = w - margin - content_x
    page_size = 12
    page_count = max(1, math.ceil(len(items) / page_size))
    active_page = min(battery_status_page, page_count - 1)
    shown = items[active_page * page_size:(active_page + 1) * page_size]
    pager = battery_pager_layout(surface.get_size())
    grid_bottom = pager["PREVIOUS"].y - 8 if page_count > 1 else nav_y - gap
    columns, rows, card_gap = 3, 4, 10
    card_w = (content_w - card_gap * (columns - 1)) // columns
    card_h = (grid_bottom - body_y - card_gap * (rows - 1)) // rows
    if not shown:
        txt(surface, "NO BATTERY TELEMETRY DETECTED", h * .035, AMBER,
            (content_x + content_w // 2, (body_y + grid_bottom) // 2), "center", True)
    for index, item in enumerate(shown):
        col, row = index % columns, index // columns
        draw_battery_card(surface, pygame.Rect(content_x + col * (card_w + card_gap),
                                                body_y + row * (card_h + card_gap),
                                                card_w, card_h), item)
    if page_count > 1:
        for name, rect in pager.items():
            enabled = (name == "PREVIOUS" and active_page > 0) or (name == "NEXT" and active_page + 1 < page_count)
            pygame.draw.rect(surface, (45, 48, 44), rect, border_radius=3)
            pygame.draw.rect(surface, ORANGE if enabled else BEZEL, rect, 2, border_radius=3)
            txt(surface, "◀ PREV" if name == "PREVIOUS" else "NEXT ▶", rect.h * .30,
                CREAM if enabled else MUTED, rect.center, "center", True)
        txt(surface, f"CELL PAGE {active_page + 1} / {page_count}  •  {len(items)} SOURCES",
            h * .012, CREAM, ((content_x * 2 + content_w) // 2, pager["NEXT"].centery), "center", True)
    draw_status_nav(surface)

def flight_offset_nm(item, receiver):
    if item.get("lat") is None or item.get("lon") is None:
        return None
    latitude = float(receiver.get("lat", 0))
    dx = (float(item["lon"]) - float(receiver.get("lon", 0))) * 60 * math.cos(math.radians(latitude))
    dy = (float(item["lat"]) - latitude) * 60
    return dx, dy, math.hypot(dx, dy)

def _world_pixel(lat, lon, zoom):
    scale = 256 * 2 ** zoom
    latitude = math.radians(max(-85.0511, min(85.0511, lat)))
    return ((lon + 180) / 360 * scale,
            (1 - math.asinh(math.tan(latitude)) / math.pi) / 2 * scale)

def draw_nexrad_layer(surface, center, radius, range_nm, receiver, weather):
    if not weather.get("tiles"):
        return
    zoom = int(weather.get("zoom", 7))
    station_x, station_y = _world_pixel(float(receiver["lat"]), float(receiver["lon"]), zoom)
    world_pixels_per_nm = (256 * 2 ** zoom) / (360 * 60 * math.cos(math.radians(float(receiver["lat"]))))
    image_scale = (radius / range_nm) / world_pixels_per_nm
    diameter = radius * 2
    layer = pygame.Surface((diameter, diameter), pygame.SRCALPHA)
    for (tile_x, tile_y), payload in weather.get("tiles", {}).items():
        cache_key = (zoom, tile_x, tile_y, hash(payload))
        tile = weather_tile_cache.get(cache_key)
        if tile is None:
            try:
                tile = pygame.image.load(io.BytesIO(payload)).convert_alpha()
                weather_tile_cache[cache_key] = tile
            except pygame.error:
                continue
        tile_size = max(1, round(256 * image_scale))
        scaled = pygame.transform.smoothscale(tile, (tile_size, tile_size))
        scaled.set_alpha(150)
        x = radius + ((tile_x * 256) - station_x) * image_scale
        y = radius + ((tile_y * 256) - station_y) * image_scale
        layer.blit(scaled, (round(x), round(y)))
    mask = pygame.Surface((diameter, diameter), pygame.SRCALPHA)
    pygame.draw.circle(mask, (255, 255, 255, 255), (radius, radius), radius)
    layer.blit(mask, (0, 0), special_flags=pygame.BLEND_RGBA_MULT)
    surface.blit(layer, (center[0] - radius, center[1] - radius))

def draw_aircraft_symbol(surface, point, heading, color, selected=False):
    angle = math.radians(float(heading or 0) - 90)
    size = 11 if selected else 8
    shape = []
    for dx, dy in ((size, 0), (-size * .65, -size * .55), (-size * .35, 0),
                   (-size * .65, size * .55)):
        shape.append((point[0] + dx * math.cos(angle) - dy * math.sin(angle),
                      point[1] + dx * math.sin(angle) + dy * math.cos(angle)))
    pygame.draw.polygon(surface, color, shape)
    if selected:
        pygame.draw.circle(surface, WHITE, point, size + 6, 2)

def draw_flight_strip(surface, rect, item, selected=False):
    emergency = item.get("emergency") not in (None, "none") or item.get("squawk") in ("7500", "7600", "7700")
    border = RED if emergency else WHITE if selected else BEZEL
    panel(surface, rect, (6, 13, 15), border, 4)
    callsign = str(item.get("flight") or item.get("registration") or item.get("hex", "UNKNOWN")).strip().upper()
    aircraft_type = item.get("type") or item.get("category") or "—"
    altitude = item.get("alt_baro")
    vertical = float(item.get("baro_rate") or item.get("geom_rate") or 0)
    trend = "↑" if vertical > 256 else "↓" if vertical < -256 else "→"
    txt(surface, callsign, rect.h * .20, RED if emergency else CREAM,
        (rect.x + 12, rect.y + 8), bold=True)
    txt(surface, f"{aircraft_type}  •  {str(item.get('hex', '')).upper()}", rect.h * .105, MUTED,
        (rect.right - 10, rect.y + 10), "topright", True)
    alt_text = "GROUND" if altitude == "ground" else f"{int(altitude):,} FT" if altitude is not None else "ALT —"
    txt(surface, f"{alt_text} {trend}", rect.h * .17, WHITE,
        (rect.x + 12, rect.centery + 6), "midleft", True)
    txt(surface, f"GS {float(item.get('gs') or 0):.0f} KT  •  HDG {float(item.get('track') or item.get('mag_heading') or 0):03.0f}°",
        rect.h * .11, CYAN, (rect.right - 10, rect.centery + 7), "midright", True)
    txt(surface, f"RNG {item.get('range_nm', 0):.1f} NM  •  {compact_age(time.time() - float(item.get('seen') or 0))}",
        rect.h * .09, MUTED, (rect.x + 12, rect.bottom - 7), "bottomleft", True)

def draw_air_traffic_status(surface, now, data):
    global radar_target_hitboxes
    r, w, h = layout(surface.get_size()), *surface.get_size()
    surface.fill(BLACK)
    panel(surface, r["header"], NAVY, CYAN)
    txt(surface, "USS ENTERPRISE • NCC-1701", h * .027, MUTED,
        (r["header"].x + 24, r["header"].y + 15), bold=True)
    txt(surface, "LOCAL AIRSPACE SURVEILLANCE", h * .047, WHITE,
        (r["header"].x + 24, r["header"].bottom - 16), "bottomleft", True)
    draw_mode_selector(surface, r["mode_selector"])
    connected = bool(data.get("connected")) and time.time() - data.get("updated", 0) < 15
    link_color = GREEN if connected else RED
    pygame.draw.circle(surface, link_color, (r["header"].right - 38, r["header"].centery), 14)
    txt(surface, "PIAWARE LINK NOMINAL" if connected else "PIAWARE LINK FAULT", h * .023, link_color,
        (r["header"].right - 66, r["header"].centery), "midright", True)

    gap, margin = int(w * .012), int(w * .018)
    body_y = r["header"].bottom + gap
    nav_y = h - margin
    content_x = r["mode_selector"].right + gap
    content_w = w - margin - content_x
    left_w = int(content_w * .65)
    scope_panel = pygame.Rect(content_x, body_y, left_w, nav_y - body_y - gap)
    strip_panel = pygame.Rect(scope_panel.right + gap, body_y,
                              content_x + content_w - scope_panel.right - gap, scope_panel.h)
    panel(surface, scope_panel, PANEL, BLUE, 10)
    panel(surface, strip_panel, PANEL, BLUE, 10)
    txt(surface, "ADS-B / MLAT PLAN POSITION INDICATOR", h * .018, CREAM,
        (scope_panel.centerx, scope_panel.y + 13), "midtop", True)
    txt(surface, "ACTIVE FLIGHT STRIPS", h * .022, CREAM,
        (strip_panel.x + 16, strip_panel.y + 12), bold=True)

    controls = radar_controls_layout(surface.get_size())
    for value in (20, 40, 80, 160):
        rect = controls[value]
        selected = radar_range_nm == value
        pygame.draw.rect(surface, (61, 65, 58), rect, border_radius=3)
        pygame.draw.rect(surface, AMBER if selected else BEZEL, rect, 2, border_radius=3)
        txt(surface, f"{value} NM", rect.h * .30, CREAM if selected else MUTED,
            rect.center, "center", True)
    wx = controls["WX"]
    weather = data.get("weather", {})
    wx_color = GREEN if radar_weather_enabled and weather.get("tiles") else AMBER if radar_weather_enabled else BEZEL
    pygame.draw.rect(surface, (47, 51, 46), wx, border_radius=3)
    pygame.draw.rect(surface, wx_color, wx, 2, border_radius=3)
    txt(surface, "WX OVERLAY ON" if radar_weather_enabled else "WX OVERLAY OFF", wx.h * .29,
        CREAM if radar_weather_enabled else MUTED, wx.center, "center", True)

    receiver = data.get("receiver", {})
    center = (scope_panel.x + int(scope_panel.w * .49), scope_panel.y + int(scope_panel.h * .58))
    radius = int(min(scope_panel.w * .35, scope_panel.h * .40))
    pygame.draw.circle(surface, (3, 12, 12), center, radius)
    if radar_weather_enabled and receiver.get("lat") is not None:
        draw_nexrad_layer(surface, center, radius, radar_range_nm, receiver, weather)
    for ring in range(1, 5):
        ring_radius = radius * ring // 4
        pygame.draw.circle(surface, (31, 92, 74), center, ring_radius, 1 if ring < 4 else 2)
        txt(surface, f"{radar_range_nm * ring // 4}", h * .011, MUTED,
            (center[0] + 5, center[1] - ring_radius + 3), "topleft", True)
    pygame.draw.line(surface, (31, 92, 74), (center[0] - radius, center[1]),
                     (center[0] + radius, center[1]), 1)
    pygame.draw.line(surface, (31, 92, 74), (center[0], center[1] - radius),
                     (center[0], center[1] + radius), 1)
    for degrees, label in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
        angle = math.radians(degrees - 90)
        point = (center[0] + math.cos(angle) * (radius + 14),
                 center[1] + math.sin(angle) * (radius + 14))
        txt(surface, label, h * .014, CREAM, point, "center", True)
    sweep_angle = (now * .38) % (math.pi * 2) - math.pi / 2
    sweep_end = (center[0] + math.cos(sweep_angle) * radius,
                 center[1] + math.sin(sweep_angle) * radius)
    pygame.draw.line(surface, (49, 157, 91), center, sweep_end, 2)

    plotted = []
    radar_target_hitboxes = {}
    if receiver.get("lat") is not None and receiver.get("lon") is not None:
        for item in data.get("aircraft", []):
            offset = flight_offset_nm(item, receiver)
            if offset is None or float(item.get("seen_pos") or item.get("seen") or 999) > 60:
                continue
            dx, dy, distance = offset
            enriched = dict(item, range_nm=distance)
            plotted.append(enriched)
            if distance > radar_range_nm:
                continue
            point = (round(center[0] + dx / radar_range_nm * radius),
                     round(center[1] - dy / radar_range_nm * radius))
            altitude = item.get("alt_baro")
            altitude_num = float(altitude) if isinstance(altitude, (int, float)) else 0
            color = GREEN if altitude_num < 5000 else AMBER if altitude_num < 18000 else CYAN
            selected = item.get("hex") == selected_aircraft
            trail_points = []
            for lat, lon, _stamp in item.get("trail", []):
                tdx = (lon - float(receiver["lon"])) * 60 * math.cos(math.radians(float(receiver["lat"])))
                tdy = (lat - float(receiver["lat"])) * 60
                trail_points.append((round(center[0] + tdx / radar_range_nm * radius),
                                     round(center[1] - tdy / radar_range_nm * radius)))
            if len(trail_points) > 1:
                pygame.draw.lines(surface, tuple(channel // 2 for channel in color), False, trail_points, 1)
            draw_aircraft_symbol(surface, point, item.get("track") or item.get("mag_heading"), color, selected)
            radar_target_hitboxes[item.get("hex")] = pygame.Rect(point[0] - 14, point[1] - 14, 28, 28)
            callsign = str(item.get("flight") or item.get("registration") or "").strip()
            if callsign and (selected or distance < radar_range_nm * .22):
                txt(surface, callsign, h * .010, color, (point[0] + 10, point[1] - 10), bold=True)
    pygame.draw.circle(surface, WHITE, center, 5)
    txt(surface, "RX", h * .010, WHITE, (center[0] + 8, center[1] + 5), bold=True)

    plotted.sort(key=lambda item: (item.get("hex") != selected_aircraft, item.get("range_nm", 9999)))
    strips = plotted[:8]
    strip_top = strip_panel.y + 49
    strip_gap = 7
    strip_h = (strip_panel.bottom - strip_top - 16 - strip_gap * 7) // 8
    for index, item in enumerate(strips):
        rect = pygame.Rect(strip_panel.x + 12, strip_top + index * (strip_h + strip_gap),
                           strip_panel.w - 24, strip_h)
        draw_flight_strip(surface, rect, item, item.get("hex") == selected_aircraft)
        radar_target_hitboxes[f"strip:{item.get('hex')}"] = rect
    if not strips:
        txt(surface, "NO POSITIONED AIRCRAFT", h * .025, AMBER, strip_panel.center, "center", True)

    status = data.get("status", {})
    footer_text = (f"TRACKS {len(plotted):03d}  •  RADIO {str((status.get('radio') or {}).get('status', '—')).upper()}"
                   f"  •  MLAT {str((status.get('mlat') or {}).get('status', '—')).upper()}"
                   f"  •  FEED {compact_age(data.get('updated'))}")
    txt(surface, footer_text, h * .012, GREEN if connected else RED,
        (scope_panel.centerx, scope_panel.bottom - 10), "midbottom", True)
    if radar_weather_enabled and weather.get("error"):
        txt(surface, "WX LINK FAULT", h * .012, AMBER,
            (scope_panel.right - 16, scope_panel.bottom - 10), "bottomright", True)
    emergencies = [item for item in plotted if item.get("emergency") not in (None, "none") or
                   item.get("squawk") in ("7500", "7600", "7700")]
    if emergencies:
        warning = pygame.Rect(scope_panel.x + 22, scope_panel.bottom - 78, scope_panel.w - 44, 48)
        pygame.draw.rect(surface, RED, warning, border_radius=4)
        pygame.draw.rect(surface, CREAM, warning, 3, border_radius=4)
        target = emergencies[0]
        txt(surface, f"AIRSPACE EMERGENCY • {str(target.get('flight') or target.get('hex')).strip()} • SQUAWK {target.get('squawk', '—')}",
            warning.h * .31, WHITE, warning.center, "center", True)

def _satellite_point(center, radius, azimuth, elevation):
    radial = radius * (90.0 - max(0.0, min(90.0, elevation))) / 90.0
    angle = math.radians(azimuth - 90.0)
    return (round(center[0] + math.cos(angle) * radial),
            round(center[1] + math.sin(angle) * radial))

def _pass_clock(timestamp):
    return time.strftime("%H:%M", time.localtime(timestamp)) if timestamp else "—"

def draw_country_flag(surface, rect, country):
    """Draw a compact flag plate without depending on color-emoji fonts."""
    country = str(country or "UNKNOWN").upper()
    pygame.draw.rect(surface, (218, 216, 194), rect)
    if country == "USA":
        stripe_h = max(1, rect.h // 7)
        for index in range(7):
            pygame.draw.rect(surface, RED if index % 2 == 0 else WHITE,
                             (rect.x, rect.y + index * stripe_h, rect.w, stripe_h + 1))
        pygame.draw.rect(surface, (31, 62, 116), (rect.x, rect.y, rect.w * .43, rect.h * .55))
    elif country in ("RUSSIA", "NETHERLANDS"):
        colors = (WHITE, BLUE, RED) if country == "RUSSIA" else (RED, WHITE, BLUE)
        for index, color in enumerate(colors):
            pygame.draw.rect(surface, color, (rect.x, rect.y + index * rect.h // 3,
                                               rect.w, rect.h // 3 + 1))
    elif country == "CHINA":
        pygame.draw.rect(surface, (202, 28, 35), rect)
        pygame.draw.circle(surface, (255, 218, 45), (rect.x + 7, rect.y + 6), 3)
    elif country == "JAPAN":
        pygame.draw.circle(surface, (188, 0, 45), rect.center, max(3, rect.h // 3))
    elif country == "INDIA":
        for index, color in enumerate(((255, 153, 51), WHITE, (19, 136, 8))):
            pygame.draw.rect(surface, color, (rect.x, rect.y + index * rect.h // 3,
                                               rect.w, rect.h // 3 + 1))
        pygame.draw.circle(surface, (0, 0, 128), rect.center, 2)
    elif country in ("FRANCE", "FRENCH GUIANA"):
        for index, color in enumerate(((20, 53, 132), WHITE, (239, 65, 53))):
            pygame.draw.rect(surface, color, (rect.x + index * rect.w // 3, rect.y,
                                               rect.w // 3 + 1, rect.h))
    elif country == "BRAZIL":
        pygame.draw.rect(surface, (0, 146, 70), rect)
        pygame.draw.polygon(surface, (255, 223, 0),
                            ((rect.centerx, rect.y + 2), (rect.right - 3, rect.centery),
                             (rect.centerx, rect.bottom - 2), (rect.x + 3, rect.centery)))
        pygame.draw.circle(surface, (0, 39, 118), rect.center, 3)
    elif country == "NORWAY":
        pygame.draw.rect(surface, (186, 12, 47), rect)
        pygame.draw.rect(surface, WHITE, (rect.x + 7, rect.y, 5, rect.h))
        pygame.draw.rect(surface, WHITE, (rect.x, rect.y + rect.h // 2 - 2, rect.w, 5))
        pygame.draw.rect(surface, (0, 32, 91), (rect.x + 8, rect.y, 2, rect.h))
        pygame.draw.rect(surface, (0, 32, 91), (rect.x, rect.y + rect.h // 2 - 1, rect.w, 2))
    elif country == "KAZAKHSTAN":
        pygame.draw.rect(surface, (0, 175, 202), rect)
        pygame.draw.circle(surface, (255, 203, 0), rect.center, 3)
    elif country == "ISRAEL":
        pygame.draw.rect(surface, WHITE, rect)
        pygame.draw.line(surface, BLUE, (rect.x, rect.y + 3), (rect.right, rect.y + 3), 2)
        pygame.draw.line(surface, BLUE, (rect.x, rect.bottom - 4), (rect.right, rect.bottom - 4), 2)
        pygame.draw.circle(surface, BLUE, rect.center, 3, 1)
    else:
        pygame.draw.rect(surface, (66, 72, 69), rect)
        txt(surface, country[:3], rect.h * .48, CREAM, rect.center, "center", True)
    pygame.draw.rect(surface, BEZEL, rect, 1)

def draw_satellite_status(surface, now, data):
    """Apollo-era all-sky orbital plot with current and predicted passes."""
    global satellite_target_hitboxes
    r, w, h = layout(surface.get_size()), *surface.get_size()
    surface.fill(BLACK)
    panel(surface, r["header"], NAVY, CYAN)
    txt(surface, "USS ENTERPRISE • NCC-1701", h * .027, MUTED,
        (r["header"].x + 24, r["header"].y + 15), bold=True)
    txt(surface, "ORBITAL TRACKING • SPACE TRAFFIC", h * .047, WHITE,
        (r["header"].x + 24, r["header"].bottom - 16), "bottomleft", True)
    draw_mode_selector(surface, r["mode_selector"])

    catalog_age = time.time() - float(data.get("catalog_updated") or 0)
    connected = bool(data.get("connected")) and catalog_age < 14400
    link_color = GREEN if connected else RED
    pygame.draw.circle(surface, link_color, (r["header"].right - 38, r["header"].centery), 14)
    txt(surface, "ORBITAL SOLUTION NOMINAL" if connected else "ORBITAL DATA FAULT", h * .023,
        link_color, (r["header"].right - 66, r["header"].centery), "midright", True)

    gap, margin = int(w * .012), int(w * .018)
    body_y = r["header"].bottom + gap
    body_h = h - margin - body_y
    content_x = r["mode_selector"].right + gap
    content_w = w - margin - content_x
    sky_w = int(content_w * .61)
    sky_panel = pygame.Rect(content_x, body_y, sky_w, body_h)
    list_panel = pygame.Rect(sky_panel.right + gap, body_y,
                             content_x + content_w - sky_panel.right - gap, body_h)
    panel(surface, sky_panel, PANEL, BLUE, 10)
    panel(surface, list_panel, PANEL, BLUE, 10)
    txt(surface, "LOCAL CELESTIAL HEMISPHERE", h * .020, CREAM,
        (sky_panel.centerx, sky_panel.y + 14), "midtop", True)

    center = (sky_panel.centerx, sky_panel.y + int(sky_panel.h * .53))
    radius = int(min(sky_panel.w * .39, sky_panel.h * .42))
    pygame.draw.circle(surface, (3, 10, 13), center, radius)
    for elevation in (0, 30, 60):
        ring = int(radius * (90 - elevation) / 90)
        pygame.draw.circle(surface, (32, 84, 91), center, ring, 2 if elevation == 0 else 1)
        txt(surface, f"{elevation}°", h * .010, MUTED,
            (center[0] + 5, center[1] - ring + 3), bold=True)
    pygame.draw.line(surface, (32, 84, 91), (center[0] - radius, center[1]),
                     (center[0] + radius, center[1]), 1)
    pygame.draw.line(surface, (32, 84, 91), (center[0], center[1] - radius),
                     (center[0], center[1] + radius), 1)
    for azimuth, label in ((0, "N"), (90, "E"), (180, "S"), (270, "W")):
        point = _satellite_point(center, radius + 18, azimuth, 0)
        txt(surface, label, h * .015, CREAM, point, "center", True)

    overhead = data.get("overhead", [])
    satellite_target_hitboxes = {}
    for index, satellite in enumerate(overhead):
        point = _satellite_point(center, radius, satellite.get("azimuth", 0),
                                 satellite.get("elevation", 0))
        is_station = any(token in satellite.get("name", "").upper()
                         for token in ("ISS", "TIANGONG", "CSS"))
        color = AMBER if is_station else GREEN if satellite.get("elevation", 0) >= 30 else CYAN
        selected = satellite.get("catalog_id") == selected_satellite
        if selected:
            pygame.draw.circle(surface, WHITE, point, 14, 2)
            pygame.draw.line(surface, WHITE, (point[0] - 19, point[1]),
                             (point[0] + 19, point[1]), 1)
            pygame.draw.line(surface, WHITE, (point[0], point[1] - 19),
                             (point[0], point[1] + 19), 1)
        pygame.draw.circle(surface, BEZEL, point, 7 if is_station or selected else 5)
        pygame.draw.circle(surface, color, point, 4 if is_station else 3)
        satellite_target_hitboxes[f"sat:{satellite.get('catalog_id')}"] = pygame.Rect(
            point[0] - 24, point[1] - 24, 48, 48)
        if index < 8 or is_station or selected:
            txt(surface, satellite.get("name", "")[:18], h * .010, color,
                (point[0] + 8, point[1] - 7), bold=is_station or selected)
    for planet in data.get("planets", []):
        point = _satellite_point(center, radius, planet.get("azimuth", 0),
                                 planet.get("elevation", 0))
        pygame.draw.polygon(surface, AMBER,
                            ((point[0], point[1] - 7), (point[0] + 7, point[1]),
                             (point[0], point[1] + 7), (point[0] - 7, point[1])), 2)
        txt(surface, planet.get("name", ""), h * .010, AMBER,
            (point[0] + 9, point[1] + 2), "midleft", True)
    for star in data.get("stars", []):
        point = _satellite_point(center, radius, star.get("azimuth", 0),
                                 star.get("elevation", 0))
        pygame.draw.line(surface, WHITE, (point[0] - 5, point[1]),
                         (point[0] + 5, point[1]), 2)
        pygame.draw.line(surface, WHITE, (point[0], point[1] - 5),
                         (point[0], point[1] + 5), 2)
        txt(surface, star.get("name", ""), h * .009, WHITE,
            (point[0] + 8, point[1] - 5), bold=True)
    pygame.draw.circle(surface, WHITE, center, 4)
    txt(surface, "ZENITH", h * .010, WHITE, (center[0] + 8, center[1] + 5), bold=True)
    txt(surface, f"SAT {len(overhead):02d}  •  PLANETS {len(data.get('planets', [])):02d}  •  CATALOG {int(data.get('catalog_count') or 0):03d}",
        h * .014, GREEN if connected else RED,
        (sky_panel.centerx, sky_panel.bottom - 14), "midbottom", True)
    txt(surface, "● SATELLITE    ◇ PLANET    + BRIGHT STAR", h * .011, MUTED,
        (sky_panel.x + 17, sky_panel.bottom - 14), "bottomleft", True)
    if not connected:
        invalid_data_flag(surface, pygame.Rect(center[0] - radius, center[1] - radius,
                                               radius * 2, radius * 2), "ORBIT DATA")

    txt(surface, "SATELLITES OVERHEAD", h * .021, CREAM,
        (list_panel.x + 15, list_panel.y + 13), bold=True)
    ordered_overhead = list(overhead)
    if selected_satellite:
        ordered_overhead.sort(key=lambda item: item.get("catalog_id") != selected_satellite)
    page_size = 4
    page_count = max(1, math.ceil(len(ordered_overhead) / page_size))
    active_page = min(satellite_strip_page, page_count - 1)
    shown_overhead = ordered_overhead[active_page * page_size:(active_page + 1) * page_size]
    pager_y = list_panel.y + 11
    prev_rect = pygame.Rect(list_panel.right - 164, pager_y, 42, 28)
    next_rect = pygame.Rect(list_panel.right - 54, pager_y, 42, 28)
    for label, rect, enabled in (("◀", prev_rect, active_page > 0),
                                 ("▶", next_rect, active_page + 1 < page_count)):
        pygame.draw.rect(surface, (45, 48, 44), rect, border_radius=3)
        pygame.draw.rect(surface, AMBER if enabled else BEZEL, rect, 2, border_radius=3)
        txt(surface, label, rect.h * .42, CREAM if enabled else MUTED,
            rect.center, "center", True)
    txt(surface, f"{active_page + 1}/{page_count}", h * .012, MUTED,
        (list_panel.right - 88, pager_y + 14), "center", True)
    if active_page > 0:
        satellite_target_hitboxes["page:prev"] = prev_rect.inflate(18, 12)
    if active_page + 1 < page_count:
        satellite_target_hitboxes["page:next"] = next_rect.inflate(18, 12)
    row_x = list_panel.x + 12
    current_top = list_panel.y + 48
    current_h = int(h * .069)
    for index, satellite in enumerate(shown_overhead):
        rect = pygame.Rect(row_x, current_top + index * (current_h + 6),
                           list_panel.w - 24, current_h)
        selected = satellite.get("catalog_id") == selected_satellite
        panel(surface, rect, (10, 22, 24) if selected else (6, 13, 15),
              WHITE if selected else BEZEL, 3)
        txt(surface, satellite.get("name", "UNKNOWN")[:24], rect.h * .18, CREAM,
            (rect.x + 10, rect.y + 7), bold=True)
        txt(surface, f"NORAD {satellite.get('catalog_id', '—')}", rect.h * .13, MUTED,
            (rect.right - 9, rect.y + 8), "topright", True)
        flag = pygame.Rect(rect.x + 10, rect.centery - 7, 25, 14)
        draw_country_flag(surface, flag, satellite.get("launch_country"))
        txt(surface, f"{satellite.get('launch_country', 'UNKNOWN')}  •  LAUNCH {satellite.get('launch_date') or '—'}",
            rect.h * .115, AMBER, (flag.right + 6, rect.centery + 1), "midleft", True)
        txt(surface, f"AZ {satellite.get('azimuth', 0):05.1f}°  EL {satellite.get('elevation', 0):04.1f}°",
            rect.h * .14, CYAN, (rect.x + 10, rect.bottom - 7), "bottomleft", True)
        txt(surface, f"{satellite.get('range_km', 0) * .621371:,.0f} MI",
            rect.h * .15, WHITE, (rect.right - 9, rect.bottom - 8), "bottomright", True)
        satellite_target_hitboxes[f"strip:{satellite.get('catalog_id')}"] = rect
    if not overhead:
        txt(surface, "NO CATALOGED OBJECTS ABOVE HORIZON", h * .017, AMBER,
            (list_panel.centerx, current_top + current_h), "center", True)

    passes_y = current_top + 4 * (current_h + 6) + 15
    txt(surface, "NEXT 10° PASSES • LOCAL TIME", h * .019, AMBER,
        (list_panel.x + 15, passes_y), bold=True)
    pass_top = passes_y + 34
    pass_h = int(h * .060)
    for index, orbit_pass in enumerate(data.get("upcoming", [])[:6]):
        rect = pygame.Rect(row_x, pass_top + index * (pass_h + 5), list_panel.w - 24, pass_h)
        pygame.draw.rect(surface, (7, 15, 16), rect, border_radius=3)
        pygame.draw.rect(surface, BEZEL, rect, 1, border_radius=3)
        txt(surface, orbit_pass.get("name", "UNKNOWN")[:20], rect.h * .17, CREAM,
            (rect.x + 9, rect.y + 6), bold=True)
        flag = pygame.Rect(rect.x + 9, rect.centery - 6, 22, 12)
        draw_country_flag(surface, flag, orbit_pass.get("launch_country"))
        txt(surface, f"{orbit_pass.get('launch_country', 'UNKNOWN')}  •  {orbit_pass.get('launch_date') or '—'}",
            rect.h * .10, AMBER, (flag.right + 5, rect.centery + 1), "midleft", True)
        txt(surface, f"RISE {_pass_clock(orbit_pass.get('rise'))}  PEAK {_pass_clock(orbit_pass.get('culminate'))}",
            rect.h * .105, MUTED, (rect.x + 9, rect.bottom - 5), "bottomleft", True)
        elevation = orbit_pass.get("max_elevation")
        txt(surface, f"{elevation:.0f}°" if elevation is not None else "—",
            rect.h * .23, AMBER, (rect.right - 9, rect.centery), "midright", True)
    txt(surface, f"CELESTRAK VISUAL + STATIONS  •  ELEMENTS {compact_age(data.get('catalog_updated'))}",
        h * .011, MUTED, (list_panel.centerx, list_panel.bottom - 9), "midbottom", True)

def draw_sad_mac(surface):
    """Full-screen monochrome homage to the original compact-Mac crash icon."""
    w, h = surface.get_size()
    paper, ink = (190, 190, 184), (17, 17, 16)
    surface.fill(paper)
    scale = max(3, int(min(w, h) * .011))
    mac_w, mac_h = 28 * scale, 31 * scale
    x, y = (w - mac_w) // 2, int(h * .20)

    # Chunky pixel-built compact Macintosh case and screen.
    pygame.draw.rect(surface, ink, (x, y, mac_w, mac_h))
    pygame.draw.rect(surface, paper, (x + 2 * scale, y + 2 * scale,
                                      mac_w - 4 * scale, mac_h - 5 * scale))
    pygame.draw.rect(surface, ink, (x + 5 * scale, y + 5 * scale,
                                    18 * scale, 14 * scale))
    pygame.draw.rect(surface, paper, (x + 7 * scale, y + 7 * scale,
                                      14 * scale, 10 * scale))

    # Pixel eyes and unmistakable downturned mouth.
    for eye_x in (x + 10 * scale, x + 18 * scale):
        pygame.draw.line(surface, ink, (eye_x - scale, y + 9 * scale),
                         (eye_x + scale, y + 11 * scale), scale)
        pygame.draw.line(surface, ink, (eye_x + scale, y + 9 * scale),
                         (eye_x - scale, y + 11 * scale), scale)
    mouth = [(x + 10 * scale, y + 15 * scale),
             (x + 12 * scale, y + 13 * scale),
             (x + 16 * scale, y + 13 * scale),
             (x + 18 * scale, y + 15 * scale)]
    pygame.draw.lines(surface, ink, False, mouth, scale)
    pygame.draw.rect(surface, ink, (x + 4 * scale, y + 24 * scale,
                                    20 * scale, 2 * scale))
    pygame.draw.rect(surface, ink, (x + 20 * scale, y + 27 * scale,
                                    3 * scale, scale))

    txt(surface, "0000000F", scale * 2.0, ink,
        (w // 2, y + mac_h + 4 * scale), "midtop", True)
    txt(surface, "SYSTEM FAILURE", scale * 1.45, ink,
        (w // 2, y + mac_h + 8 * scale), "midtop", True)

def draw_mushroom_cloud(surface, now):
    """Animated mid-century mushroom-cloud silhouette for the core breach."""
    w, h = surface.get_size()
    pulse = .94 + math.sin(now * 14.0) * .06
    surface.fill((65, 4, 0))

    # Concentric blast glow keeps the transition theatrical at kiosk distance.
    center = (w // 2, int(h * .54))
    for radius, color in ((int(h * .52 * pulse), (122, 12, 0)),
                          (int(h * .39 * pulse), (212, 48, 0)),
                          (int(h * .27 * pulse), (255, 143, 0)),
                          (int(h * .16 * pulse), (255, 229, 126))):
        pygame.draw.circle(surface, color, center, radius)

    ink = (24, 12, 8)
    stem_w = int(w * .105 * pulse)
    stem = pygame.Rect(w // 2 - stem_w // 2, int(h * .40), stem_w, int(h * .43))
    pygame.draw.rect(surface, ink, stem)
    pygame.draw.polygon(surface, ink, ((stem.left, stem.bottom),
                                       (int(w * .39), int(h * .94)),
                                       (int(w * .61), int(h * .94)),
                                       (stem.right, stem.bottom)))

    # Overlapping lobes form a recognizable boiling cloud without an asset file.
    cloud_y = int(h * .35)
    lobes = ((-.17, .01, .105), (-.10, -.06, .125), (0, -.10, .15),
             (.10, -.06, .125), (.17, .01, .105), (-.08, .07, .13),
             (.08, .07, .13))
    for dx, dy, radius in lobes:
        pygame.draw.circle(surface, ink,
                           (w // 2 + int(w * dx), cloud_y + int(h * dy)),
                           int(h * radius * pulse))
    pygame.draw.ellipse(surface, ink, (int(w * .27), int(h * .29),
                                       int(w * .46), int(h * .22)))
    txt(surface, "CATASTROPHIC CORE BREACH", h * .037, (255, 231, 172),
        (w // 2, int(h * .08)), "center", True)

def draw_destruct_countdown(surface, now):
    """Turn the entire console into a high-visibility red countdown display."""
    w, h = surface.get_size()
    pulse = (math.sin(now * 7.0) + 1.0) / 2.0
    veil = pygame.Surface((w, h), pygame.SRCALPHA)
    veil.fill((110 + int(45 * pulse), 0, 0, 145 + int(35 * pulse)))
    surface.blit(veil, (0, 0))
    border = max(12, int(min(w, h) * .022))
    pygame.draw.rect(surface, (255, 35, 35), (border // 2, border // 2,
                                             w - border, h - border), border)
    txt(surface, "SELF DESTRUCT", h * .075, WHITE,
        (w // 2, int(h * .09)), "center", True)
    txt(surface, countdown_value, h * .58, (255, 225, 210),
        (w // 2, int(h * .45)), "center", True)

    # Draw this in precisely the same rectangle used by handle_touch().
    # The visual control and its live touchscreen target therefore cannot drift apart.
    abort_rect = layout((w, h))["destruct"].inflate(-10, -10)
    pygame.draw.rect(surface, (64 + int(30 * pulse), 0, 0), abort_rect, border_radius=18)
    pygame.draw.rect(surface, (255, 215, 205), abort_rect,
                     7 + int(4 * pulse), border_radius=18)
    txt(surface, "ABORT", abort_rect.h * .30, WHITE,
        (abort_rect.centerx, abort_rect.centery - abort_rect.h * .13), "center", True)
    txt(surface, f"PRESS 5 TIMES  •  {abort_count}/5", abort_rect.h * .13,
        (255, 205, 190), (abort_rect.centerx, abort_rect.centery + abort_rect.h * .22),
        "center", True)

    instruction_x = layout((w, h))["energize"].centerx
    txt(surface, "SELF DESTRUCT ACTIVE", h * .038, WHITE,
        (instruction_x, int(h * .88)), "center", True)
    txt(surface, "TOUCH THE ILLUMINATED ABORT CONTROL", h * .022,
        (255, 205, 190), (instruction_x, int(h * .935)), "center", True)

def handle_touch(pos, now):
    global running
    global arm_until, shutdown_confirm_until, status_page, environment_sensor_page, house_system_page, battery_status_page
    global radar_range_nm, radar_weather_enabled, selected_aircraft
    global selected_satellite, satellite_strip_page
    if shutdown_confirm_until > now:
        controls = shutdown_layout(screen.get_size())
        if controls["exit"].collidepoint(pos) and not shutdown_pending:
            print("EXIT KIOSK REQUESTED")
            running = False
        elif controls["restart"].collidepoint(pos) and not shutdown_pending:
            threading.Thread(target=request_power_action, args=("reboot",), daemon=True).start()
        elif controls["shutdown"].collidepoint(pos) and not shutdown_pending:
            threading.Thread(target=request_power_action, args=("poweroff",), daemon=True).start()
        elif controls["cancel"].collidepoint(pos):
            shutdown_confirm_until = 0
        return
    r = layout(screen.get_size())
    selected_mode = mode_at_position(pos, screen.get_size())
    if selected_mode:
        select_display_mode(selected_mode)
        return
    if ship_status_active(now) or special_display_active(now):
        if display_mode == "SHIP STATUS" and status_page == "ENVIRONMENT":
            sensor_count = len((yolink.snapshot() if yolink else {}).get("temperature_sensors", []))
            page_count = max(1, math.ceil(sensor_count / 6))
            pager = environment_pager_layout(screen.get_size())
            if pager["PREVIOUS"].collidepoint(pos):
                environment_sensor_page = max(0, environment_sensor_page - 1)
                mark_activity()
                return
            if pager["NEXT"].collidepoint(pos):
                environment_sensor_page = min(page_count - 1, environment_sensor_page + 1)
                mark_activity()
                return
        if display_mode == "SHIP STATUS" and status_page == "HOUSE SYSTEMS":
            page_count = max(1, math.ceil(len(grouped_house_systems(
                telemetry.snapshot() if telemetry else {})) / 6))
            pager = house_pager_layout(screen.get_size())
            if pager["PREVIOUS"].collidepoint(pos):
                house_system_page = max(0, house_system_page - 1)
                mark_activity()
                return
            if pager["NEXT"].collidepoint(pos):
                house_system_page = min(page_count - 1, house_system_page + 1)
                mark_activity()
                return
        if display_mode == "SHIP STATUS" and status_page == "POWER CELLS":
            telemetry_data = telemetry.snapshot() if telemetry else {}
            yolink_data = yolink.snapshot() if yolink else {}
            page_count = max(1, math.ceil(len(battery_inventory(telemetry_data, yolink_data)) / 12))
            pager = battery_pager_layout(screen.get_size())
            if pager["PREVIOUS"].collidepoint(pos):
                battery_status_page = max(0, battery_status_page - 1)
                mark_activity()
                return
            if pager["NEXT"].collidepoint(pos):
                battery_status_page = min(page_count - 1, battery_status_page + 1)
                mark_activity()
                return
        if display_mode == "AIR TRAFFIC":
            controls = radar_controls_layout(screen.get_size())
            for value in (20, 40, 80, 160):
                if controls[value].collidepoint(pos):
                    radar_range_nm = value
                    mark_activity()
                    return
            if controls["WX"].collidepoint(pos):
                radar_weather_enabled = not radar_weather_enabled
                if flight_telemetry:
                    flight_telemetry.set_weather_enabled(radar_weather_enabled)
                mark_activity()
                return
            for key, rect in radar_target_hitboxes.items():
                if rect.collidepoint(pos):
                    selected_aircraft = key.split(":", 1)[-1]
                    mark_activity()
                    return
        if display_mode == "SPACE TRAFFIC":
            for key, rect in satellite_target_hitboxes.items():
                if not rect.collidepoint(pos):
                    continue
                if key == "page:prev":
                    satellite_strip_page = max(0, satellite_strip_page - 1)
                elif key == "page:next":
                    satellite_strip_page += 1
                else:
                    catalog_id = key.split(":", 1)[-1]
                    selected_satellite = None if catalog_id == selected_satellite else catalog_id
                    satellite_strip_page = 0
                mark_activity()
                return
        selected_page = status_page_at_position(pos, screen.get_size())
        if display_mode == "SHIP STATUS" and selected_page:
            status_page = selected_page
            mark_activity()
            save_display_mode()
            return
        # AUTO wake taps are deliberately consumed so the hidden console control
        # underneath cannot fire. Forced SHIP STATUS remains until its selector moves.
        mark_activity()
        return
    mark_activity()
    if r["afg"].collidepoint(pos) and countdown_value is None:
        track = r["afg"].inflate(-28, -10)
        set_system_volume((pos[0] - track.x) * 100 / max(1, track.w))
    elif r["energize"].collidepoint(pos):
        trigger_transporter()
    elif r["destruct"].collidepoint(pos):
        if self_destruct_active:
            trigger_self_destruct()
        elif MODE in ("selfdestruct", "both") and not any_sequence_active:
            if now < arm_until:
                arm_until = 0
                trigger_self_destruct()
            else:
                arm_until = now + 4.0

def handle_press(pos, now):
    global shutdown_hold_started, party_return_hold_started, afg_dragging
    r = layout(screen.get_size())
    if (party_return_at_position(pos, screen.get_size()) and not any_sequence_active and
            shutdown_confirm_until <= now and not shutdown_pending):
        party_return_hold_started = now
        mark_activity()
        return
    if (mode_at_position(pos, screen.get_size()) or ship_status_active(now) or
            special_display_active(now)):
        handle_touch(pos, now)
        return
    if (r["afg"].collidepoint(pos) and countdown_value is None and
            shutdown_confirm_until <= now):
        afg_dragging = True
        handle_touch(pos, now)
        return
    # The visible plate stays subtle; its invisible hold target is more forgiving.
    if not any_sequence_active and r["maker_plate"].inflate(70, 46).collidepoint(pos):
        shutdown_hold_started = now
    else:
        handle_touch(pos, now)

def handle_release():
    global shutdown_hold_started, party_return_hold_started, afg_dragging
    shutdown_hold_started = 0
    party_return_hold_started = 0
    afg_dragging = False

if not TEST_MODE and GPIO:
    GPIO.setmode(GPIO.BCM)
    if MODE in ("transporter", "both"):
        GPIO.setup(GREEN_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.add_event_detect(GREEN_PIN, GPIO.FALLING, callback=lambda _: trigger_transporter(), bouncetime=300)
    if MODE in ("selfdestruct", "both"):
        GPIO.setup(RED_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
        GPIO.add_event_detect(RED_PIN, GPIO.FALLING, callback=lambda _: trigger_self_destruct(), bouncetime=300)
elif not TEST_MODE:
    print("RPi.GPIO unavailable; touchscreen controls remain active")

threading.Thread(target=system_volume_worker, daemon=True).start()
if telemetry:
    telemetry.start()
if yolink:
    yolink.start()
if flight_telemetry:
    flight_telemetry.set_weather_enabled(radar_weather_enabled)
    flight_telemetry.start()
if satellite_telemetry:
    satellite_telemetry.start()

print(f"MODE: {MODE.upper()} | TEST: {TEST_MODE} | DISPLAY: {screen.get_size()}")
try:
    while running:
        now = time.monotonic()
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                mark_activity()
                if event.key == pygame.K_g:
                    trigger_transporter()
                elif event.key == pygame.K_r:
                    trigger_self_destruct()
                elif event.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1 and not getattr(event, "touch", False):
                handle_press(event.pos, now)
            elif event.type == pygame.MOUSEBUTTONUP and event.button == 1 and not getattr(event, "touch", False):
                handle_release()
            elif event.type == pygame.MOUSEMOTION and afg_dragging:
                handle_touch(event.pos, now)
            elif event.type == pygame.FINGERDOWN:
                handle_press((int(event.x * screen.get_width()), int(event.y * screen.get_height())), now)
            elif event.type == pygame.FINGERMOTION and afg_dragging:
                handle_touch((int(event.x * screen.get_width()), int(event.y * screen.get_height())), now)
            elif event.type == pygame.FINGERUP:
                handle_release()
        if shutdown_hold_started and now - shutdown_hold_started >= 5.0:
            shutdown_hold_started = 0
            shutdown_confirm_until = now + 10.0
        if party_return_hold_started and now - party_return_hold_started >= 5.0:
            party_return_hold_started = 0
            venue_modes = ("AUTO", "PARTY", "OFFICE")
            venue_override = venue_modes[(venue_modes.index(venue_override) + 1) % len(venue_modes)]
            mark_activity()
            save_display_mode()
        update_venue_link(now)
        if (party_return_armed() and display_mode != "TRANSPORTER" and
                now - last_activity >= PARTY_RETURN_SECONDS):
            party_return_home(now)
        if arm_until and now >= arm_until:
            arm_until = 0
        if shutdown_confirm_until and now >= shutdown_confirm_until and not shutdown_pending:
            shutdown_confirm_until = 0
        if ui_state == "EXPLOSION":
            draw_mushroom_cloud(screen, now)
        elif ui_state == "SAD_MAC":
            draw_sad_mac(screen)
        elif display_mode == "AIR TRAFFIC" and special_display_active(now):
            draw_air_traffic_status(screen, now,
                                    flight_telemetry.snapshot() if flight_telemetry else {})
        elif display_mode == "SPACE TRAFFIC" and special_display_active(now):
            draw_satellite_status(screen, now,
                                  satellite_telemetry.snapshot() if satellite_telemetry else {})
        elif ship_status_active(now):
            if status_page == "ENVIRONMENT":
                draw_environment_status(screen, now, yolink.snapshot() if yolink else {})
            elif status_page == "HOUSE SYSTEMS":
                draw_house_status(screen, now, telemetry.snapshot() if telemetry else {})
            elif status_page == "POWER CELLS":
                draw_power_status(screen, now,
                                  telemetry.snapshot() if telemetry else {},
                                  yolink.snapshot() if yolink else {})
            else:
                draw_ship_status(screen, now, telemetry.snapshot() if telemetry else {})
        else:
            draw_console(screen, now)
            if countdown_value is not None:
                draw_destruct_countdown(screen, now)
        if shutdown_confirm_until > now or shutdown_pending:
            draw_shutdown_confirmation(screen, now)
        pygame.display.flip()
        clock.tick(60)
finally:
    running = False
    afg_event.set()
    if telemetry:
        telemetry.stop()
    if yolink:
        yolink.stop()
    if flight_telemetry:
        flight_telemetry.stop()
    if satellite_telemetry:
        satellite_telemetry.stop()
    stop_siren()
    if pygame.mixer.get_init():
        pygame.mixer.stop()
        pygame.mixer.quit()
    pygame.quit()
    if not TEST_MODE and GPIO:
        GPIO.cleanup()
    print("KIOSK OFFLINE")
