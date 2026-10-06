#!/usr/bin/env python3
"""Touch-first Star Trek transporter kiosk for Raspberry Pi."""

import math
import os
import re
import subprocess
import sys
import threading
import time

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
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

MODE = "both"
TEST_MODE = "--test" in sys.argv
WINDOWED = "--windowed" in sys.argv
OFFICE_IDLE_SECONDS = 120.0
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
shutdown_hold_started = shutdown_confirm_until = 0.0
shutdown_pending = False
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
            return (mode if mode in ("TRANSPORTER", "SHIP STATUS", "AUTO") else "AUTO",
                    page if page in ("NETWORK", "ENVIRONMENT", "HOUSE SYSTEMS") else "NETWORK")
    except (OSError, ValueError, TypeError):
        return "AUTO", "NETWORK"

display_mode, status_page = load_console_preferences()
telemetry = TelemetryService() if TelemetryService else None
yolink = YoLinkService() if YoLinkService else None
environment_sensor_page = 0
house_system_page = 0

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
            json.dump({"display_mode": display_mode, "status_page": status_page}, config_file)
        os.replace(temporary, CONFIG_PATH)
    except OSError as exc:
        print(f"Display mode was not persisted: {exc}")

def select_display_mode(mode):
    global display_mode
    if mode not in ("TRANSPORTER", "SHIP STATUS", "AUTO"):
        return
    display_mode = mode
    mark_activity()
    save_display_mode()

def ship_status_active(now):
    if (any_sequence_active or ui_state != "READY" or shutdown_confirm_until > now or
            shutdown_pending or countdown_value is not None):
        return False
    if display_mode == "SHIP STATUS":
        return True
    return display_mode == "AUTO" and now - last_activity >= OFFICE_IDLE_SECONDS

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

def txt(surface, value, size, color, pos, anchor="topleft", bold=False):
    image = font(size, bold).render(str(value), True, color)
    rect = image.get_rect()
    setattr(rect, anchor, pos)
    surface.blit(image, rect)
    return rect

def panel(surface, rect, color=PANEL, border=BLUE, radius=18):
    pygame.draw.rect(surface, color, rect, border_radius=radius)
    pygame.draw.rect(surface, border, rect, width=2, border_radius=radius)

def bar(surface, rect, value, color=CYAN, segments=20):
    gap = max(2, rect.w // 140)
    sw = (rect.w - gap * (segments - 1)) / segments
    active = round(value * segments)
    for i in range(segments):
        r = pygame.Rect(round(rect.x + i * (sw + gap)), rect.y, max(2, round(sw)), rect.h)
        pygame.draw.rect(surface, color if i < active else (28, 58, 68), r, border_radius=3)

def edge_meter(surface, rect, value, label, color):
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
    txt(surface, f"{int(value * 100):02d}", rect.h * .17, CREAM, readout.center, "center", True)
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

def gauge(surface, center, radius, value, label, color):
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
    txt(surface, f"{int(value * 100):02d}", radius * .17, CREAM, readout.center, "center", True)
    pygame.draw.circle(surface, color, (inner.right - 13, inner.top + 13), 5)
    label_plate = pygame.Rect(center[0] - int(radius * .60), center[1] + int(radius * .54), int(radius * 1.20), int(radius * .18))
    pygame.draw.rect(surface, (198, 194, 170), label_plate, border_radius=2)
    txt(surface, label, radius * .105, (22, 24, 22), label_plate.center, "center", True)
    # Restrained glass reflection along the upper-left edge.
    pygame.draw.arc(surface, (72, 86, 85), box.inflate(-18, -18), math.radians(188), math.radians(260), 2)

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
    selector_w = max(108, int(w * .068))
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
        "mode_selector": pygame.Rect(margin, body_y, selector_w, min(body_h, int(h * .37))),
    }
    result["maker_plate"] = pygame.Rect(result["right"].x + 62, result["right"].bottom - 31, result["right"].w - 124, 18)
    result["afg"] = pygame.Rect(result["center"].x + 62,
                                result["center"].bottom - int(h * .115),
                                result["center"].w - 124, int(h * .075))
    return result

def draw_mode_selector(surface, rect):
    """Panel-mounted three-position rotary display selector."""
    panel(surface, rect, (38, 42, 39), BEZEL, 5)
    inner = rect.inflate(-10, -10)
    pygame.draw.rect(surface, (10, 14, 13), inner, border_radius=3)
    txt(surface, "DISPLAY", rect.w * .105, CREAM, (rect.centerx, rect.y + 18), "midtop", True)
    txt(surface, "SELECTOR", rect.w * .085, MUTED, (rect.centerx, rect.y + 35), "midtop", True)

    options = ("TRANSPORTER", "SHIP STATUS", "AUTO")
    knob_center = (rect.centerx, rect.y + int(rect.h * .31))
    radius = max(25, int(rect.w * .29))
    pygame.draw.circle(surface, (155, 157, 145), knob_center, radius + 9)
    pygame.draw.circle(surface, (28, 31, 29), knob_center, radius + 5)
    pygame.draw.circle(surface, (76, 80, 74), knob_center, radius)
    angles = (-140, -90, -40)
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
    row_h = max(34, int((rect.bottom - row_top - 10) / 3))
    for index, option in enumerate(options):
        segment = pygame.Rect(rect.x + 8, row_top + index * row_h,
                              rect.w - 16, row_h - 5)
        selected = display_mode == option
        color = GREEN if option == "AUTO" else AMBER
        pygame.draw.rect(surface, (190, 188, 166) if selected else (91, 94, 87), segment, border_radius=2)
        pygame.draw.rect(surface, color if selected else (145, 146, 134), segment, 2, border_radius=2)
        lamp = (segment.x + 10, segment.centery)
        pygame.draw.circle(surface, (41, 44, 41), lamp, 6)
        pygame.draw.circle(surface, color if selected else (18, 22, 20), lamp, 4)
        txt(surface, option, rect.w * .071,
            (22, 24, 22) if selected else CREAM,
            (segment.centerx + 5, segment.centery), "center", True)

def mode_at_position(pos, size):
    rect = layout(size)["mode_selector"]
    if not rect.collidepoint(pos):
        return None
    options = ("TRANSPORTER", "SHIP STATUS", "AUTO")
    row_top = rect.y + int(rect.h * .51)
    if pos[1] < row_top:
        return options[(options.index(display_mode) + 1) % len(options)]
    row_h = max(34, int((rect.bottom - row_top - 10) / 3))
    index = min(2, max(0, int((pos[1] - row_top) / row_h)))
    return options[index]

def status_nav_layout(size):
    r = layout(size)
    gap = int(size[0] * .012)
    footer = pygame.Rect(r["mode_selector"].right + gap, size[1] - int(size[1] * .075),
                         size[0] - r["mode_selector"].right - gap - int(size[0] * .018),
                         int(size[1] * .052))
    third = (footer.w - gap * 2) // 3
    return {
        "NETWORK": pygame.Rect(footer.x, footer.y, third, footer.h),
        "ENVIRONMENT": pygame.Rect(footer.x + third + gap, footer.y, third, footer.h),
        "HOUSE SYSTEMS": pygame.Rect(footer.x + (third + gap) * 2, footer.y,
                                     footer.w - third * 2 - gap * 2, footer.h),
    }

def status_page_at_position(pos, size):
    for page, rect in status_nav_layout(size).items():
        if rect.collidepoint(pos):
            return page
    return None

def draw_status_nav(surface):
    for page, rect in status_nav_layout(surface.get_size()).items():
        selected = status_page == page
        color = CYAN if page == "NETWORK" else AMBER if page == "ENVIRONMENT" else GREEN
        pygame.draw.rect(surface, tuple(channel // (4 if selected else 8) for channel in color),
                         rect, border_radius=5)
        pygame.draw.rect(surface, color if selected else BEZEL, rect, 3, border_radius=5)
        txt(surface, page, rect.h * .30, WHITE if selected else MUTED,
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
    grid_w = int(content_w * .63)
    y = status_nav_layout(size)["NETWORK"].y - int(h * .047)
    return {
        "PREVIOUS": pygame.Rect(content_x + 18, y, int(w * .085), int(h * .035)),
        "NEXT": pygame.Rect(content_x + grid_w - 18 - int(w * .085), y,
                            int(w * .085), int(h * .035)),
    }

def shutdown_layout(size):
    w, h = size
    return {
        "confirm": pygame.Rect(int(w * .12), int(h * .61), int(w * .46), int(h * .20)),
        "cancel": pygame.Rect(int(w * .62), int(h * .61), int(w * .26), int(h * .20)),
    }

def request_shutdown():
    global shutdown_pending
    shutdown_pending = True
    print("SAFE SHUTDOWN REQUESTED")
    time.sleep(0.8)
    result = subprocess.run(["sudo", "-n", "/usr/bin/systemctl", "poweroff"], check=False)
    if result.returncode:
        shutdown_pending = False
        print(f"Shutdown failed with status {result.returncode}")

def draw_shutdown_confirmation(surface, now):
    w, h = surface.get_size()
    overlay = pygame.Surface((w, h), pygame.SRCALPHA)
    overlay.fill((0, 0, 0, 225))
    surface.blit(overlay, (0, 0))
    border = pygame.Rect(int(w * .08), int(h * .17), int(w * .84), int(h * .68))
    panel(surface, border, (22, 25, 24), BEZEL, 10)
    txt(surface, "GROUND OPERATIONS", h * .028, AMBER, (w // 2, int(h * .24)), "center", True)
    title = "SHUTTING DOWN" if shutdown_pending else "POWER DOWN CONSOLE?"
    txt(surface, title, h * .068, CREAM, (w // 2, int(h * .36)), "center", True)
    detail = "WAIT FOR THE DISPLAY TO GO DARK BEFORE REMOVING POWER" if shutdown_pending else "THIS SAFELY STOPS THE RASPBERRY PI"
    txt(surface, detail, h * .022, MUTED, (w // 2, int(h * .47)), "center", True)
    if not shutdown_pending:
        controls = shutdown_layout((w, h))
        button(surface, controls["confirm"], "SHUT DOWN", "CONFIRM SAFE POWER-OFF", RED, True, True)
        button(surface, controls["cancel"], "CANCEL", "RETURN TO CONSOLE", CYAN, True)
        remaining = max(0, int(shutdown_confirm_until - now) + 1)
        txt(surface, f"CANCELS AUTOMATICALLY IN {remaining}", h * .016, MUTED, (w // 2, int(h * .83)), "center", True)

def draw_console(surface, now):
    r, w, h = layout(surface.get_size()), *surface.get_size()
    surface.fill(RED if now < flash_until else BLACK)
    panel(surface, r["header"], NAVY, CYAN)
    txt(surface, "USS ENTERPRISE • NCC-1701", h * .027, MUTED, (r["header"].x + 24, r["header"].y + 15), bold=True)
    txt(surface, "TRANSPORTER CONTROL", h * .047, WHITE, (r["header"].x + 24, r["header"].bottom - 16), "bottomleft", True)
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
        txt(surface, "GREG // MAX  •  TRANSPORTER LAB  •  2026", h * .0095, CREAM, maker_plate.center, "center", True)
        if shutdown_hold_started:
            hold_progress = min(1.0, (now - shutdown_hold_started) / 5.0)
            pygame.draw.rect(surface, AMBER, (maker_plate.x, maker_plate.bottom + 3, int(maker_plate.w * hold_progress), 3))
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

def data_age(timestamp):
    if not timestamp:
        return "NO DATA", RED
    age = max(0, time.time() - timestamp)
    if age < 90:
        return f"LIVE {int(age):02d}S", GREEN
    if age < 3600:
        return f"STALE {int(age // 60):02d}M", AMBER
    return f"STALE {int(age // 3600):02d}H", RED

def telemetry_card(surface, rect, label, value, detail="", color=GREEN, state=True):
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

def draw_ship_status(surface, now, data):
    """Apollo/steampunk telemetry panel; all data is read-only."""
    r, w, h = layout(surface.get_size()), *surface.get_size()
    surface.fill(BLACK)
    panel(surface, r["header"], NAVY, CYAN)
    txt(surface, "USS ENTERPRISE • NCC-1701", h * .027, MUTED,
        (r["header"].x + 24, r["header"].y + 15), bold=True)
    txt(surface, "SHIP SYSTEMS STATUS", h * .047, WHITE,
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
          min(1, system.get("cpu", 0) / 100), "CORE LOAD", GREEN)
    gauge(surface, (left.x + int(left.w * .72), gauge_y), radius,
          min(1, system.get("memory", 0) / 100), "LOGIC STORAGE", CYAN)
    card_h = int(h * .080)
    temperature_c = system.get("temperature_c")
    temperature_f = temperature_c * 9 / 5 + 32 if temperature_c is not None else None
    telemetry_card(surface, pygame.Rect(left.x + 18, left.y + int(left.h * .50), left.w - 36, card_h),
                   "CORE THERMAL", (f"{temperature_f:05.1f} °F / {temperature_c:04.1f} °C"
                                    if temperature_f is not None else "N/A"),
                   "NORMAL" if temperature_c is not None and temperature_c < 75 else "CAUTION",
                   GREEN if temperature_c is not None and temperature_c < 75 else AMBER,
                   temperature_c is not None)
    telemetry_card(surface, pygame.Rect(left.x + 18, left.y + int(left.h * .62), left.w - 36, card_h),
                   "MISSION ELAPSED TIME", format_uptime(system.get("uptime")), "PI UPTIME", AMBER)
    sys_age, sys_age_color = data_age(data.get("updated"))
    telemetry_card(surface, pygame.Rect(left.x + 18, left.y + int(left.h * .74), left.w - 36, card_h),
                   "TELEMETRY CLOCK", time.strftime("%H:%M:%S"), sys_age, sys_age_color, fresh)

    panel(surface, center, PANEL, BLUE, 12)
    txt(surface, "EXTERNAL ENVIRONMENT", h * .025, CREAM, (center.x + 18, center.y + 14), bold=True)
    weather = data.get("weather", {})
    forecast = data.get("forecast", {})
    local_weather = bool(weather.get("temperature_c") is not None)
    air_f = (weather.get("temperature_c") * 9 / 5 + 32) if local_weather else forecast.get("temperature_f")
    weather_age = data.get("weatherflow", {}).get("updated") if local_weather else forecast.get("updated")
    weather_source = "WEATHERFLOW UDP" if local_weather else "MQTT FORECAST"
    weather_color = GREEN if weather_age and time.time() - weather_age < 180 else AMBER
    big_radius = int(min(center.w * .19, center.h * .18))
    gauge(surface, (center.x + int(center.w * .28), center.y + int(center.h * .28)), big_radius,
          max(0, min(1, (float(air_f or 0) + 10) / 130)), "AIR TEMPERATURE", AMBER)
    wind_mph = float(weather.get("wind_mps") or 0) * 2.23694
    gauge(surface, (center.x + int(center.w * .72), center.y + int(center.h * .28)), big_radius,
          min(1, wind_mph / 50), "WIND VELOCITY", CYAN)
    txt(surface, f"{air_f:05.1f} °F" if air_f is not None else "NO DATA", h * .025, WHITE,
        (center.x + int(center.w * .28), center.y + int(center.h * .47)), "center", True)
    direction = weather.get("wind_direction")
    txt(surface, f"{wind_mph:04.1f} MPH  {int(direction):03d}°" if direction is not None else "WIND LINK WAITING",
        h * .021, WHITE, (center.x + int(center.w * .72), center.y + int(center.h * .47)), "center", True)
    meter_y = center.y + int(center.h * .55)
    meter_h = int(h * .065)
    pressure = float(weather.get("pressure_mb") or 0)
    humidity = float(weather.get("humidity") or 0)
    rain = float(weather.get("daily_rain_mm") or weather.get("rain_mm") or 0) / 25.4
    edge_meter(surface, pygame.Rect(center.x + 20, meter_y, center.w - 40, meter_h),
               max(0, min(1, (pressure - 970) / 80)) if pressure else 0, "BAROMETRIC PRESSURE", AMBER)
    edge_meter(surface, pygame.Rect(center.x + 20, meter_y + meter_h + 10, center.w - 40, meter_h),
               humidity / 100, "ATMOSPHERIC HUMIDITY", CYAN)
    detail_y = meter_y + (meter_h + 10) * 2 + 10
    age_text, _ = data_age(weather_age)
    telemetry_card(surface, pygame.Rect(center.x + 20, detail_y, center.w - 40, card_h),
                   weather_source, forecast.get("summary", "LOCAL OBSERVATION") if not local_weather else f"RAIN {rain:.2f} IN",
                   age_text, weather_color, bool(weather_age))

    panel(surface, right, PANEL, BLUE, 12)
    txt(surface, "COMMUNICATIONS", h * .025, CREAM, (right.x + 18, right.y + 14), bold=True)
    network = data.get("network", {})
    network_y = right.y + 54
    network_h = int(h * .105)
    for index, (name, label) in enumerate((("wlan0", "WIRELESS TELEMETRY"), ("eth0", "HARDLINE TELEMETRY"))):
        info = network.get(name, {})
        card = pygame.Rect(right.x + 16, network_y + index * (network_h + 10), right.w - 32, network_h)
        state = bool(info.get("up"))
        detail = f"RX {format_rate(info.get('rx_rate'))}  TX {format_rate(info.get('tx_rate'))}"
        value = f"{info.get('ipv4', '—')}" if state else "LINK DOWN"
        telemetry_card(surface, card, label, value, detail, GREEN if state else RED, state)
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
                   data_age(wine.get("updated"))[0], CYAN, bool(wine.get("updated")))
    aux_y += small_h + 7
    telemetry_card(surface, pygame.Rect(right.x + 16, aux_y, right.w - 32, small_h),
                   "KEG THERMAL", f"{keg.get('temperature_f', '—')} °F  {keg.get('humidity', '—')}%",
                   data_age(keg.get("updated"))[0], AMBER, bool(keg.get("updated")))
    aux_y += small_h + 7
    left_door = house.get("garage_left", {}).get("state", "—")
    right_door = house.get("garage_right", {}).get("state", "—")
    doors_safe = left_door == right_door == "closed"
    telemetry_card(surface, pygame.Rect(right.x + 16, aux_y, right.w - 32, small_h),
                   "SHUTTLE BAY DOORS", f"L {str(left_door).upper()}   R {str(right_door).upper()}",
                   "SECURED" if doors_safe else "CHECK BAY", GREEN if doors_safe else AMBER, doors_safe)
    aux_y += small_h + 7
    leaks = house.get("leaks", {})
    current_items = [item for item in leaks.values()
                     if item.get("updated") and time.time() - item["updated"] < 86400]
    wet = sum(1 for item in current_items if item.get("state") == "wet")
    current = len(current_items)
    leak_safe = wet == 0
    telemetry_card(surface, pygame.Rect(right.x + 16, aux_y, right.w - 32, small_h),
                   "WATER RECLAMATION", "DRY" if leak_safe else f"{wet} LEAK ALERT",
                   f"{current}/{len(leaks)} CURRENT", GREEN if leak_safe else RED, leak_safe)
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
    txt(surface, "ENVIRONMENTAL CONTROL", h * .047, WHITE,
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

HOUSE_GROUP_ORDER = ("BACK YARD", "BAR", "BREAKFAST NOOK", "COUCH", "DINING ROOM",
                     "FAMILY ROOM", "FENCE", "GARAGE REFRIGERATOR", "HALLWAY", "PATIO")

def grouped_house_systems(data):
    grouped = {name: [] for name in HOUSE_GROUP_ORDER}
    for item in data.get("house", {}).get("systems", {}).values():
        if item.get("group") in grouped:
            grouped[item["group"]].append(item)
    return [(name, grouped[name]) for name in HOUSE_GROUP_ORDER]

def house_group_lines(devices):
    switches = [item.get("switch") for item in devices if item.get("switch") in ("on", "off")]
    playing = [item for item in devices if item.get("playbackStatus")]
    water = [item.get("water") for item in devices if item.get("water")]
    temperatures = [item.get("temperature") for item in devices if item.get("temperature") is not None]
    online = [item.get("DeviceWatch-DeviceStatus") for item in devices
              if item.get("DeviceWatch-DeviceStatus")]
    lines = []
    if switches:
        lines.append(f"{sum(value == 'on' for value in switches)} ON  /  {len(switches)} CONTROLS")
    if playing:
        audio = playing[0]
        volume = audio.get("volume", audio.get("groupVolume"))
        lines.append(f"AUDIO {str(audio.get('playbackStatus')).upper()}  •  VOL {volume if volume is not None else '—'}")
    if water:
        lines.append("WATER " + " / ".join(str(value).upper() for value in water))
    if temperatures:
        lines.append(f"THERMAL {float(temperatures[0]):.1f} °F")
    if online:
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
    txt(surface, group, rect.h * .15, CREAM, (rect.x + 35, rect.y + 10), bold=True)
    txt(surface, compact_age(updated), rect.h * .095, color, (rect.right - 12, rect.y + 12), "topright", True)
    line_y = rect.y + int(rect.h * .43)
    for index, line in enumerate(lines):
        txt(surface, line, rect.h * (.14 if index == 0 else .105), WHITE if index == 0 else MUTED,
            (rect.x + 15, line_y + index * int(rect.h * .20)), "midleft", index == 0)

def draw_reservoir_scale(surface, rect, lake):
    panel(surface, rect, (6, 13, 15), BEZEL, 5)
    txt(surface, "LEWISVILLE RESERVOIR", rect.h * .055, CREAM, (rect.x + 16, rect.y + 12), bold=True)
    txt(surface, f"USGS PROV • {compact_age(lake.get('updated'))}", rect.h * .030, MUTED,
        (rect.right - 14, rect.y + 14), "topright", True)
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
    grid_w = int(content_w * .63)
    grid = pygame.Rect(content_x, body_y, grid_w, body_h)
    water_panel = pygame.Rect(grid.right + gap, body_y, content_w - grid_w - gap, body_h)
    panel(surface, grid, PANEL, BLUE, 10)
    txt(surface, "SELECTED HABITATION SYSTEMS", h * .024, CREAM,
        (grid.x + 18, grid.y + 14), bold=True)
    groups = grouped_house_systems(data)
    page_size, page_count = 6, max(1, math.ceil(len(groups) / 6))
    active_page = min(house_system_page, page_count - 1)
    shown = groups[active_page * page_size:(active_page + 1) * page_size]
    card_gap, top = 12, grid.y + 58
    card_w = (grid.w - 36 - card_gap) // 2
    pager = house_pager_layout(surface.get_size())
    cards_bottom = pager["PREVIOUS"].y - 8 if page_count > 1 else grid.bottom - 18
    card_h = (cards_bottom - top - card_gap * 2) // 3
    for index, (group, devices) in enumerate(shown):
        col, row = index % 2, index // 2
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
    lake_rect = pygame.Rect(water_panel.x + 16, water_panel.y + 54,
                            water_panel.w - 32, int(water_panel.h * .53))
    draw_reservoir_scale(surface, lake_rect, water.get("lake", {}))
    trinity = water.get("trinity", {})
    river_rect = pygame.Rect(water_panel.x + 16, lake_rect.bottom + 14,
                             water_panel.w - 32, water_panel.bottom - lake_rect.bottom - 30)
    panel(surface, river_rect, (6, 13, 15), BEZEL, 5)
    txt(surface, "TRINITY OUTFLOW", river_rect.h * .11, CREAM,
        (river_rect.x + 15, river_rect.y + 11), bold=True)
    txt(surface, f"USGS PROV • {compact_age(trinity.get('updated'))}", river_rect.h * .055, MUTED,
        (river_rect.right - 14, river_rect.y + 14), "topright", True)
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
    global arm_until, shutdown_confirm_until, status_page, environment_sensor_page, house_system_page
    if shutdown_confirm_until > now:
        controls = shutdown_layout(screen.get_size())
        if controls["confirm"].collidepoint(pos) and not shutdown_pending:
            threading.Thread(target=request_shutdown, daemon=True).start()
        elif controls["cancel"].collidepoint(pos):
            shutdown_confirm_until = 0
        return
    r = layout(screen.get_size())
    selected_mode = mode_at_position(pos, screen.get_size())
    if selected_mode:
        select_display_mode(selected_mode)
        return
    if ship_status_active(now):
        if display_mode == "SHIP STATUS" and status_page == "ENVIRONMENT":
            sensor_count = len((yolink.snapshot() if yolink else {}).get("temperature_sensors", []))
            page_count = max(1, math.ceil(sensor_count / 6))
            pager = environment_pager_layout(screen.get_size())
            if pager["PREVIOUS"].collidepoint(pos):
                environment_sensor_page = max(0, environment_sensor_page - 1)
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
            if pager["NEXT"].collidepoint(pos):
                environment_sensor_page = min(page_count - 1, environment_sensor_page + 1)
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
    global shutdown_hold_started, afg_dragging
    r = layout(screen.get_size())
    if mode_at_position(pos, screen.get_size()) or ship_status_active(now):
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
    global shutdown_hold_started, afg_dragging
    shutdown_hold_started = 0
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
        if arm_until and now >= arm_until:
            arm_until = 0
        if shutdown_confirm_until and now >= shutdown_confirm_until and not shutdown_pending:
            shutdown_confirm_until = 0
        if ui_state == "EXPLOSION":
            draw_mushroom_cloud(screen, now)
        elif ui_state == "SAD_MAC":
            draw_sad_mac(screen)
        elif ship_status_active(now):
            if status_page == "ENVIRONMENT":
                draw_environment_status(screen, now, yolink.snapshot() if yolink else {})
            elif status_page == "HOUSE SYSTEMS":
                draw_house_status(screen, now, telemetry.snapshot() if telemetry else {})
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
    stop_siren()
    if pygame.mixer.get_init():
        pygame.mixer.stop()
        pygame.mixer.quit()
    pygame.quit()
    if not TEST_MODE and GPIO:
        GPIO.cleanup()
    print("KIOSK OFFLINE")
