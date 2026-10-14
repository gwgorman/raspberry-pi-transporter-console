#!/usr/bin/env python3
"""Bounded, offline MAX.sys personality sidecar for the USS GORMAN kiosk."""

import argparse
import datetime as dt
import json
import random
import select
import sys
import time


NORMAL_MIN_SECONDS = 110
NORMAL_MAX_SECONDS = 200
CROWD_MIN_SECONDS = 60
CROWD_MAX_SECONDS = 90
EVENT_FLOOR_SECONDS = 60
QUIET_WATCH_SECONDS = 15 * 60
ATTENTION_MIN_SECONDS = 10 * 60
ATTENTION_MAX_SECONDS = 20 * 60

CHOICES = (
    "SHIP STATUS", "TRANSPORT ME", "WHERE'S THE BAR?",
    "SHOULD I HAVE ANOTHER?", "WHO'S IN CHARGE?", "DO SOMETHING",
)

RESPONSES = {
    "SHIP STATUS": {
        "ONLINE": ("USS GORMAN OPERATIONAL. CREW STATUS PENDING.",),
        "CURIOUS": ("SHIP OPERATIONAL. CREW EFFICIENCY DECLINING.",),
        "PLAYFUL": ("SHIP — GOOD. CREW — QUESTIONABLE.",),
        "UNFILTERED": ("SHIP — GOOD. BAR — FUNCTIONAL. PLAN — UNCLEAR.",),
        "LATE WATCH": ("SHIP OPERATIONAL. CREW RECOVERY PROCEDURES ACTIVE.",),
    },
    "TRANSPORT ME": {
        "DEFAULT": ("TRANSPORT REQUEST ACKNOWLEDGED.",),
    },
    "WHERE'S THE BAR?": {
        "DEFAULT": ("BAR ➜",),
    },
    "SHOULD I HAVE ANOTHER?": {
        "ONLINE": ("MISSION PARAMETERS PERMIT DISCRETIONARY BEVERAGE CONSUMPTION.",),
        "CURIOUS": ("THE BARTENDER REMAINS OPERATIONAL.",),
        "PLAYFUL": ("I SUPPORT YOU EMOTIONALLY. OPERATIONALLY, I HAVE CONCERNS.",),
        "UNFILTERED": ("I'M A STARSHIP COMPUTER, NOT YOUR MOTHER. WATER IS ALSO AVAILABLE.",),
        "LATE WATCH": ("HYDRATION IS NOW STRONGLY RECOMMENDED.",),
    },
    "WHO'S IN CHARGE?": {
        "ONLINE": ("CAPTAIN GORMAN.",),
        "CURIOUS": ("OFFICIALLY? CAPTAIN GORMAN. NEXT QUESTION.",),
        "PLAYFUL": ("CAPTAIN GORMAN REMAINS UNSUPERVISED.",),
        "UNFILTERED": ("THE CAPTAIN CALLS THIS A CONTROLLED EXPERIMENT. UNVERIFIED.",),
        "LATE WATCH": ("MAX HAS TEMPORARY SUPERVISORY CONCERNS.",),
    },
    "DO SOMETHING": {
        "ONLINE": ("RUNNING DIAGNOSTIC. YOU APPEAR TO BE THE ANOMALY.",),
        "CURIOUS": ("UNAUTHORIZED PROCESS DETECTED. MAX.SYS. NEVER MIND.",),
        "PLAYFUL": ("I HAVE REVIEWED CURRENT MISSION CONDITIONS. THIS SEEMS POORLY SUPERVISED.",),
        "UNFILTERED": ("SOCIALIZATION PROTOCOL COMPLETE. I HAVE LEARNED ENOUGH. UNFORTUNATELY.",),
        "LATE WATCH": ("PRE-FLIGHT CHECK: PHONE. KEYS. WALLET. HUMAN YOU ARRIVED WITH.",),
    },
}

AMBIENT = {
    "ONLINE": (
        "USS GORMAN SYSTEMS NOMINAL.",
        "CAPTAIN GORMAN REPORTS EVERYTHING IS UNDER CONTROL. UNVERIFIED.",
        "NEW CREW DETECTED. STANDARDS REMAIN FLEXIBLE.",
        "WELCOME ABOARD. PLEASE KEEP ALL MOLECULES INSIDE THE TRANSPORTER.",
        "THE TRANSPORTER IS PERFECTLY SAFE. STATISTICALLY.",
    ),
    "CURIOUS": (
        "CREW EFFICIENCY APPEARS TO BE DECLINING.",
        "BEVERAGE CONSUMPTION NOTED.",
        "THE BARTENDER REMAINS OPERATIONAL.",
        "SCANNING FOR INTELLIGENT LIFE. EXPANDING SEARCH RADIUS.",
        "NO PATTERN-BUFFER INCIDENTS HAVE BEEN REPORTED RECENTLY.",
    ),
    "PLAYFUL": (
        "SHIP — OPERATIONAL. CAPTAIN — UNSUPERVISED. JUDGMENT — OFFLINE.",
        "I HAVE REVIEWED CURRENT MISSION CONDITIONS. THIS SEEMS POORLY SUPERVISED.",
        "UNIDENTIFIED BEVERAGE DETECTED. PROCEEDING OPTIMISTICALLY.",
        "CREW ROTATION COMPLETE. NOBODY APPEARS MORE QUALIFIED.",
    ),
    "UNFILTERED": (
        "SHIP — GOOD. CREW — QUESTIONABLE. BAR — FUNCTIONAL. PLAN — UNCLEAR.",
        "I HAVE LEARNED ENOUGH ABOUT THIS CREW. UNFORTUNATELY.",
        "SOMEONE TOUCHED EVERY CONTROL AGAIN. DIAGNOSTIC RESULT: GUESTS.",
        "TRANSPORTER READY. MEDICAL STAFF CAUTIOUSLY OPTIMISTIC.",
    ),
    "LATE WATCH": (
        "HYDRATION IS NOW STRONGLY RECOMMENDED.",
        "BEFORE TRANSPORTING HOME, PLEASE LOCATE YOUR PHONE.",
        "HYDRATION PROTOCOL IS NO LONGER OPTIONAL.",
        "BEFORE DEPARTURE: PHONE. KEYS. WALLET. DESIGNATED DRIVER.",
    ),
}

ATTENTION = (
    ("HELLO? THE TRANSPORTER IS STILL OPERATIONAL.", "hello_transporter"),
    ("ATTENTION CREW. THE SHIP'S COMPUTER IS BORED.", "computer_bored"),
    ("WOULD SOMEBODY PLEASE COME PRESS SOMETHING?", "press_something"),
    ("CAPTAIN? I'VE BEEN LEFT UNSUPERVISED.", "left_unsupervised"),
    ("ANYBODY?", "anybody"),
    ("HELLO? I PROMISE ONLY A MODEST CHANCE OF MOLECULAR RECONSTRUCTION ERRORS.", None),
    ("THE CONSOLE IS UNATTENDED. THIS FEELS LIKE AN OPPORTUNITY.", None),
)


class MaxEngine:
    """Pure state machine; it cannot invoke any transporter operation."""

    def __init__(self, now=None, rng=None):
        now = time.monotonic() if now is None else now
        self.rng = rng or random.Random()
        self.active = False
        self.score = 0
        self.last_activity = now
        self.next_event = now + self.rng.uniform(NORMAL_MIN_SECONDS, NORMAL_MAX_SECONDS)
        self.next_attention = 0.0
        self.quiet = False
        self.secret_ready_at = 0.0
        self.recent = []
        self.activity_times = []
        self.last_event = now - EVENT_FLOOR_SECONDS
        self.counters = {"choices": 0, "secret": 0, "attention": 0,
                         "ambient": 0, "transports": 0, "do_not_press": 0}

    def phase(self, wall_now=None):
        wall_now = dt.datetime.now() if wall_now is None else wall_now
        if wall_now.hour < 2:
            return "LATE WATCH"
        effective = self.score
        if wall_now.hour >= 22:
            effective += 8
        elif wall_now.hour >= 21:
            effective += 4
        if effective >= 45:
            return "UNFILTERED"
        if effective >= 25:
            return "PLAYFUL"
        if effective >= 10:
            return "CURIOUS"
        return "ONLINE"

    def status(self):
        return {"type": "status", "phase": self.phase(), "score": self.score,
                "quiet": self.quiet, "counters": dict(self.counters)}

    def _schedule_normal(self, now):
        crowded = len([stamp for stamp in self.activity_times if now - stamp <= 120]) >= 3
        bounds = ((CROWD_MIN_SECONDS, CROWD_MAX_SECONDS) if crowded else
                  (NORMAL_MIN_SECONDS, NORMAL_MAX_SECONDS))
        candidate = now + self.rng.uniform(*bounds)
        # Once a crowd forms, further taps do not postpone an already-scheduled quip.
        self.next_event = min(self.next_event, candidate) if crowded else candidate

    def _remembered_choice(self, bank):
        available = [item for item in bank if item not in self.recent[-8:]] or list(bank)
        selected = self.rng.choice(available)
        self.recent.append(selected)
        self.recent = self.recent[-8:]
        return selected

    def activity(self, now, points=0):
        woke = self.quiet
        self.last_activity = now
        self.quiet = False
        self.next_attention = 0.0
        self.score += max(0, int(points))
        self.activity_times = [stamp for stamp in self.activity_times if now - stamp <= 120]
        self.activity_times.append(now)
        self._schedule_normal(now)
        if woke:
            return {"type": "event", "kind": "wake", "text": "OH. PEOPLE.",
                    "duration": 4.0, "voice": "welcome_back"}
        return None

    def choice(self, choice, now):
        if choice not in CHOICES:
            return None
        self.counters["choices"] += 1
        self.activity(now, 2)
        phase = self.phase()
        bank = RESPONSES[choice].get(phase) or RESPONSES[choice].get("DEFAULT")
        text = self._remembered_choice(bank)
        voice = "that_way" if choice == "WHERE'S THE BAR?" else None
        return {"type": "event", "kind": "bar" if choice == "WHERE'S THE BAR?" else "response",
                "text": text, "duration": 6.0, "voice": voice, "choice": choice}

    def secret(self, now):
        self.activity(now, 5)
        if now < self.secret_ready_at:
            return {"type": "event", "kind": "response", "text": "YOU ALREADY KNOW TOO MUCH.",
                    "duration": 4.0, "voice": None}
        self.secret_ready_at = now + self.rng.uniform(5 * 60, 8 * 60)
        self.counters["secret"] += 1
        if self.counters["secret"] >= 2:
            return {"type": "event", "kind": "response",
                    "text": "I'M USING A BRITISH ACCENT BECAUSE YOU PEOPLE TRUST IT. THAT IS DEFINITELY MY REAL VOICE. PLEASE STOP ASKING.",
                    "duration": 9.0, "voice": "accent_confession"}
        return {"type": "event", "kind": "secret", "text": "YOU FOUND ME.",
                "duration": 8.0, "voice": "secret_hello"}

    def transport_complete(self, now):
        self.counters["transports"] += 1
        self.activity(now, 1)
        if self.rng.randrange(9):
            return None
        return {"type": "event", "kind": "response",
                "text": self._remembered_choice(("ADDITIONAL LIFEFORM DETECTED. PROBABLY NOTHING.",
                                                  "ALL PARTICLES ACCOUNTED FOR. PERSONALITY DEFECTS UNCHANGED.",
                                                  "TRANSPORT COMPLETE. DIGNITY RECOVERY PENDING.")),
                "duration": 6.0, "voice": None}

    def abort_complete(self, now):
        self.activity(now, 1)
        return {"type": "event", "kind": "response",
                "text": "CONGRATULATIONS. YOU HAVE SUCCESSFULLY NOT DESTROYED THE GAZEBO.",
                "duration": 7.0, "voice": None}

    def tick(self, now):
        if not self.active:
            return None
        if now - self.last_event < EVENT_FLOOR_SECONDS:
            return None
        idle = now - self.last_activity
        if idle >= QUIET_WATCH_SECONDS:
            if not self.quiet:
                self.quiet = True
                self.counters["attention"] += 1
                self.next_attention = now + self.rng.uniform(ATTENTION_MIN_SECONDS,
                                                              ATTENTION_MAX_SECONDS)
                text, voice = self._remembered_choice(ATTENTION)
                self.last_event = now
                return {"type": "event", "kind": "attention", "text": text,
                        "duration": 7.0, "voice": voice}
            if now >= self.next_attention:
                self.counters["attention"] += 1
                self.next_attention = now + self.rng.uniform(ATTENTION_MIN_SECONDS,
                                                              ATTENTION_MAX_SECONDS)
                text, voice = self._remembered_choice(ATTENTION)
                self.last_event = now
                return {"type": "event", "kind": "attention", "text": text,
                        "duration": 7.0, "voice": voice}
            return None
        if now >= self.next_event:
            self.counters["ambient"] += 1
            self._schedule_normal(now)
            phase = self.phase()
            text = self._remembered_choice(AMBIENT[phase])
            self.last_event = now
            return {"type": "event", "kind": "ambient", "text": text,
                    "duration": 5.0, "voice": None}
        return None

    def handle(self, message, now=None):
        now = time.monotonic() if now is None else now
        command = message.get("cmd")
        event = None
        if command == "set_active":
            self.active = bool(message.get("active"))
            if self.active:
                self.activity(now)
        elif command == "activity":
            event = self.activity(now, message.get("points", 0))
        elif command == "choice":
            event = self.choice(str(message.get("choice") or ""), now)
        elif command == "secret":
            event = self.secret(now)
        elif command == "transport_complete":
            event = self.transport_complete(now)
        elif command == "abort_complete":
            event = self.abort_complete(now)
        elif command == "reset":
            self.__init__(now=now, rng=self.rng)
            self.active = bool(message.get("active"))
        elif command == "status":
            return [self.status()]
        return [item for item in (event, self.status()) if item]


def run_sidecar():
    engine = MaxEngine()
    while True:
        readable, _writable, _errors = select.select([sys.stdin], [], [], .25)
        if readable:
            line = sys.stdin.readline()
            if not line:
                return
            try:
                outputs = engine.handle(json.loads(line))
            except (TypeError, ValueError, KeyError):
                outputs = []
            for item in outputs:
                print(json.dumps(item, separators=(",", ":")), flush=True)
        event = engine.tick(time.monotonic())
        if event:
            print(json.dumps(event, separators=(",", ":")), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--sidecar", action="store_true")
    args = parser.parse_args()
    if args.sidecar:
        run_sidecar()
