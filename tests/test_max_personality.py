import datetime as dt
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from max_personality import (ATTENTION_MAX_SECONDS, ATTENTION_MIN_SECONDS, CHOICES,
                             CROWD_MAX_SECONDS, CROWD_MIN_SECONDS, EVENT_FLOOR_SECONDS,
                             MaxEngine, QUIET_WATCH_SECONDS)


class MaxPersonalityTests(unittest.TestCase):
    def engine(self):
        return MaxEngine(now=100.0, rng=random.Random(7))

    def test_all_six_guest_choices_return_bounded_events(self):
        engine = self.engine()
        for choice in CHOICES:
            event = engine.choice(choice, 110.0)
            self.assertIn(event["kind"], ("response", "bar"))
            self.assertLessEqual(event["duration"], 8.0)

    def test_quiet_watch_replaces_normal_events_and_randomizes_attention(self):
        engine = self.engine()
        engine.active = True
        event = engine.tick(100.0 + QUIET_WATCH_SECONDS)
        self.assertEqual(event["kind"], "attention")
        delay = engine.next_attention - (100.0 + QUIET_WATCH_SECONDS)
        self.assertGreaterEqual(delay, ATTENTION_MIN_SECONDS)
        self.assertLessEqual(delay, ATTENTION_MAX_SECONDS)

    def test_activity_wakes_quiet_watch_and_resets_schedule(self):
        engine = self.engine()
        engine.active = True
        engine.tick(100.0 + QUIET_WATCH_SECONDS)
        event = engine.activity(1100.0, 1)
        self.assertEqual(event["kind"], "wake")
        self.assertFalse(engine.quiet)
        self.assertGreater(engine.next_event, 1100.0)

    def test_phase_is_interaction_driven_with_late_watch_override(self):
        engine = self.engine()
        early = dt.datetime(2026, 10, 17, 20, 35)
        self.assertEqual(engine.phase(early), "ONLINE")
        engine.score = 26
        self.assertEqual(engine.phase(early), "PLAYFUL")
        late = dt.datetime(2026, 10, 18, 0, 15)
        self.assertEqual(engine.phase(late), "LATE WATCH")

    def test_secret_has_cooldown_but_always_responds(self):
        engine = self.engine()
        first = engine.secret(100.0)
        second = engine.secret(101.0)
        self.assertEqual(first["kind"], "secret")
        self.assertEqual(second["kind"], "response")

    def test_second_completed_discovery_reveals_accent_confession(self):
        engine = self.engine()
        first = engine.secret(100.0)
        second = engine.secret(engine.secret_ready_at + 1)
        self.assertEqual(first["kind"], "secret")
        self.assertIn("BRITISH ACCENT", second["text"])
        self.assertEqual(second["voice"], "accent_confession")

    def test_crowd_pulse_shortens_schedule_but_keeps_one_minute_floor(self):
        engine = self.engine()
        engine.active = True
        engine.activity(110.0)
        engine.activity(120.0)
        engine.activity(130.0)
        delay = engine.next_event - 130.0
        self.assertGreaterEqual(delay, CROWD_MIN_SECONDS)
        self.assertLessEqual(delay, CROWD_MAX_SECONDS)
        self.assertIsNone(engine.tick(engine.last_event + EVENT_FLOOR_SECONDS - .1))

    def test_abort_line_is_bounded_and_gazebo_specific(self):
        event = self.engine().abort_complete(120.0)
        self.assertIn("GAZEBO", event["text"])
        self.assertLessEqual(event["duration"], 8.0)


if __name__ == "__main__":
    unittest.main()
