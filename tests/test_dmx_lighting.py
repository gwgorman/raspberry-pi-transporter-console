import unittest

from dmx_lighting import BLACKOUT, ParScene, Universe, WARM_AMBER


class ParSceneTests(unittest.TestCase):
    def test_static_scene_forces_strobe_and_program_off(self):
        self.assertEqual(ParScene(999, 12, -3, 44).channels(), (255, 12, 0, 44, 0, 0))

    def test_known_scenes_do_not_enable_effect_channels(self):
        self.assertEqual(BLACKOUT.channels()[4:], (0, 0))
        self.assertEqual(WARM_AMBER.channels()[4:], (0, 0))


class UniverseTests(unittest.TestCase):
    def test_par_mapping_at_both_installation_addresses(self):
        universe = Universe()
        scene = ParScene(40, 255, 80, 10)
        universe.set_par(1, scene)
        universe.set_par(30, scene)
        frame = universe.snapshot()
        self.assertEqual(frame[0], 0)
        self.assertEqual(frame[1:7], bytes(scene.channels()))
        self.assertEqual(frame[30:36], bytes(scene.channels()))

    def test_blackout_clears_every_channel(self):
        universe = Universe()
        universe.set_par(1, WARM_AMBER)
        universe.set_par(30, WARM_AMBER)
        universe.blackout()
        self.assertEqual(universe.snapshot(), bytes(513))

    def test_rejects_out_of_range_fixture(self):
        universe = Universe()
        with self.assertRaises(ValueError):
            universe.set_par(0, WARM_AMBER)
        with self.assertRaises(ValueError):
            universe.set_par(508, WARM_AMBER)


if __name__ == "__main__":
    unittest.main()
