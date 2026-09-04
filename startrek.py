#!/usr/bin/env python3
"""Touch-first Star Trek transporter kiosk for Raspberry Pi."""

import math
import os
import sys
import threading
import time

import pygame

try:
    import RPi.GPIO as GPIO
except ImportError:
    GPIO = None

MODE = "both"
TEST_MODE = "--test" in sys.argv
WINDOWED = "--windowed" in sys.argv
for arg in sys.argv:
    if arg.startswith("--mode="):
        MODE = arg.split("=", 1)[1].lower()
if MODE not in ("transporter", "selfdestruct", "both"):
    raise SystemExit("Use --mode=transporter, --mode=selfdestruct, or --mode=both")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
GREEN_PIN, RED_PIN = 17, 27
running = True
state_lock = threading.RLock()
any_sequence_active = self_destruct_active = abort_triggered = False
abort_count = 0
ui_state, status_detail = "READY", "PATTERN BUFFER STANDING BY"
countdown_value = None
transport_progress = flash_until = arm_until = 0.0

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

VOICE_CHANNEL = pygame.mixer.Channel(0) if pygame.mixer.get_init() else None
SIREN_CHANNEL = pygame.mixer.Channel(1) if pygame.mixer.get_init() else None
if VOICE_CHANNEL:
    VOICE_CHANNEL.set_volume(1.0)
if SIREN_CHANNEL:
    SIREN_CHANNEL.set_volume(0.4)

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

def play_transporter_task():
    global any_sequence_active
    with state_lock:
        if MODE not in ("transporter", "both") or any_sequence_active:
            return
        any_sequence_active = True
    set_ui("ENERGIZING", "MOLECULAR DECOMPOSITION IN PROGRESS", progress=0)
    if transporter_sound:
        transporter_sound.play()
    for i in range(101):
        set_ui("ENERGIZING", "PATTERN STREAM LOCKED", progress=i / 100.0)
        time.sleep(0.05)
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
        play_voice_wait("kaboom")
        stop_siren()
        flash_until = time.monotonic() + 2.0
        set_ui("DESTROYED", "CATASTROPHIC CORE BREACH", countdown=0)
        time.sleep(2.0)
    with state_lock:
        self_destruct_active = any_sequence_active = False
        abort_count = 0
    set_ui("READY", "PATTERN BUFFER STANDING BY", progress=0)

def trigger_transporter():
    threading.Thread(target=play_transporter_task, daemon=True).start()

def trigger_self_destruct():
    global abort_count, abort_triggered
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
    left_w, right_w = int(w * .28), int(w * .25)
    center_w = w - margin * 2 - left_w - right_w - gap * 2
    return {
        "header": pygame.Rect(margin, margin, w - margin * 2, header_h),
        "left": pygame.Rect(margin, body_y, left_w, body_h),
        "center": pygame.Rect(margin + left_w + gap, body_y, center_w, body_h),
        "right": pygame.Rect(w - margin - right_w, body_y, right_w, body_h),
        "energize": pygame.Rect(margin, h - margin - footer_h, int(w * .64), footer_h),
        "destruct": pygame.Rect(margin + int(w * .64) + gap, h - margin - footer_h, w - margin * 2 - int(w * .64) - gap, footer_h),
    }

def draw_console(surface, now):
    r, w, h = layout(surface.get_size()), *surface.get_size()
    surface.fill(RED if now < flash_until else BLACK)
    panel(surface, r["header"], NAVY, CYAN)
    txt(surface, "USS ENTERPRISE • NCC-1701", h * .027, MUTED, (r["header"].x + 24, r["header"].y + 15), bold=True)
    txt(surface, "TRANSPORTER CONTROL", h * .047, WHITE, (r["header"].x + 24, r["header"].bottom - 16), "bottomleft", True)
    lamp = GREEN if ui_state in ("READY", "COMPLETE") else RED if ui_state in ("DESTRUCT", "DESTROYED") else AMBER
    pygame.draw.circle(surface, lamp, (r["header"].right - 38, r["header"].centery), 14)
    txt(surface, ui_state, h * .03, lamp, (r["header"].right - 66, r["header"].centery), "midright", True)

    panel(surface, r["left"])
    # Real instruments do not twitch constantly: idle needles drift almost imperceptibly.
    motion = 1.0 if ui_state == "ENERGIZING" else 0.12
    integrity = .965 + math.sin(now * (.9 if motion == 1 else .18)) * .018 * motion
    confinement = .78 + math.sin(now * (.7 if motion == 1 else .14) + 1.2) * .07 * motion
    gauge(surface, (r["left"].centerx, r["left"].y + int(r["left"].h * .26)), int(r["left"].w * .23), integrity, "PATTERN INTEGRITY", GREEN)
    gauge(surface, (r["left"].centerx, r["left"].y + int(r["left"].h * .75)), int(r["left"].w * .23), confinement, "CONFINEMENT BEAM", CYAN)

    panel(surface, r["center"])
    txt(surface, "PATTERN BUFFER 01", h * .026, CREAM, (r["center"].x + 22, r["center"].y + 14), bold=True)
    active_value = transport_progress if ui_state == "ENERGIZING" else .42 + math.sin(now * .12) * .002
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
    left_value = transport_progress if ui_state == "ENERGIZING" else .67 + math.sin(now * .10) * .002
    right_value = 1.0 - transport_progress if ui_state == "ENERGIZING" else .36 + math.sin(now * .09 + 2) * .002
    tape_meter(surface, pygame.Rect(chamber.x - 48, chamber.y, 34, chamber.h), left_value, True, tape_accent, tape_background)
    tape_meter(surface, pygame.Rect(chamber.right + 14, chamber.y, 34, chamber.h), right_value, True, tape_accent, tape_background)
    idle_levels = (.42, .67, .31, .78, .53, .63, .46)
    for i in range(7):
        x = chamber.x + (i + 1) * chamber.w // 8
        if ui_state == "ENERGIZING":
            level = .18 + .72 * abs(math.sin(((now * 1.8 + i * .7) % 1) * math.pi))
        elif ui_state == "COMPLETE":
            level = .9
        else:
            level = idle_levels[i] + math.sin(now * .16 + i) * .008
        top = chamber.bottom - int(chamber.h * level)
        pygame.draw.line(surface, CYAN if i % 2 else AMBER, (x, chamber.bottom - 14), (x, top), 8)
        pygame.draw.circle(surface, WHITE, (x, top), 6)
    if ui_state == "ENERGIZING":
        radius = int(min(chamber.w, chamber.h) * (.08 + .7 * abs(math.sin(transport_progress * math.pi))))
        pygame.draw.circle(surface, CYAN, chamber.center, radius, max(3, w // 400))
    read_y = chamber.bottom + 18
    txt(surface, "TARGET COORDINATES", h * .019, MUTED, (chamber.x, read_y), bold=True)
    for i, (axis, val) in enumerate(zip("XYZ", ("047.221", "118.093", "006.714"))):
        x = chamber.x + i * chamber.w // 3
        txt(surface, axis, h * .019, AMBER, (x, read_y + 28), bold=True)
        txt(surface, val, h * .029, WHITE, (x + 25, read_y + 24), bold=True)
    txt(surface, status_detail, h * .019, lamp, (r["center"].centerx, r["center"].bottom - 20), "midbottom", True)

    panel(surface, r["right"])
    txt(surface, "SYSTEM STATUS", h * .026, WHITE, (r["right"].x + 20, r["right"].y + 18), bold=True)
    systems = (("HEISENBERG COMP.", "NOMINAL", GREEN), ("BIOFILTER", "ACTIVE", GREEN),
               ("PHASE COILS", "SYNCHRONIZED", CYAN), ("TARGET LOCK", "ACQUIRED", AMBER))
    y = r["right"].y + 60
    row_h = int(h * .052)
    for i, (label, state, color) in enumerate(systems):
        lamp_on = i != 3 or int(now * .7) % 2 == 0
        status_lamp(surface, pygame.Rect(r["right"].x + 15, y, r["right"].w - 30, row_h - 4), label, state, color, lamp_on)
        y += row_h + 4
    y += 5
    for label, value, color in (("MATTER STREAM", .88, CYAN), ("PHASE GAIN", .73, AMBER), ("ENERGY MATRIX", .94, GREEN)):
        txt(surface, label, h * .016, MUTED, (r["right"].x + 20, y), bold=True)
        bar(surface, pygame.Rect(r["right"].x + 20, y + 24, r["right"].w - 40, 16), value + math.sin(now + y) * .025, color, 14)
        y += int(h * .075)
    if countdown_value is None and not self_destruct_active:
        maker_plate = pygame.Rect(r["right"].x + 62, r["right"].bottom - 31, r["right"].w - 124, 18)
        pygame.draw.rect(surface, (72, 77, 73), maker_plate, border_radius=2)
        pygame.draw.rect(surface, (18, 21, 20), maker_plate.inflate(-4, -4), border_radius=1)
        txt(surface, "GREG // MAX  •  TRANSPORTER LAB  •  2026", h * .0095, CREAM, maker_plate.center, "center", True)
    if countdown_value is not None:
        txt(surface, countdown_value, h * .16, RED, (r["right"].centerx, r["right"].bottom - 58), "center", True)
    elif self_destruct_active:
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

def handle_touch(pos, now):
    global arm_until
    r = layout(screen.get_size())
    if r["energize"].collidepoint(pos):
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

print(f"MODE: {MODE.upper()} | TEST: {TEST_MODE} | DISPLAY: {screen.get_size()}")
try:
    while running:
        now = time.monotonic()
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key == pygame.K_g:
                    trigger_transporter()
                elif event.key == pygame.K_r:
                    trigger_self_destruct()
                elif event.key in (pygame.K_q, pygame.K_ESCAPE):
                    running = False
            elif event.type == pygame.MOUSEBUTTONDOWN and event.button == 1 and not getattr(event, "touch", False):
                handle_touch(event.pos, now)
            elif event.type == pygame.FINGERDOWN:
                handle_touch((int(event.x * screen.get_width()), int(event.y * screen.get_height())), now)
        if arm_until and now >= arm_until:
            arm_until = 0
        draw_console(screen, now)
        pygame.display.flip()
        clock.tick(60)
finally:
    running = False
    stop_siren()
    if pygame.mixer.get_init():
        pygame.mixer.stop()
        pygame.mixer.quit()
    pygame.quit()
    if not TEST_MODE and GPIO:
        GPIO.cleanup()
    print("KIOSK OFFLINE")
