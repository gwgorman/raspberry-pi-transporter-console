import datetime as dt
import os
import random
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from max_personality import (ATTENTION_MAX_SECONDS, ATTENTION_MIN_SECONDS, CHOICES,
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


if __name__ == "__main__":
    unittest.main()
