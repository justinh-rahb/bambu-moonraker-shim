import unittest

from bambu_moonraker_shim.fan_control import build_fan_command, normalize_fan_speed


class FanControlTests(unittest.TestCase):
    def test_aux_fan_gcode(self):
        command = build_fan_command("aux", 128)
        self.assertEqual(command.gcode, "M106 P2 S128\n")

    def test_chamber_fan_gcode(self):
        command = build_fan_command("chamber", 0)
        self.assertEqual(command.gcode, "M106 P3 S0\n")

    def test_speed_boundaries(self):
        self.assertEqual(normalize_fan_speed(-5), 0)
        self.assertEqual(normalize_fan_speed(255), 255)
        self.assertEqual(normalize_fan_speed(300), 255)

    def test_percent_conversion(self):
        command = build_fan_command("aux", "50%")
        self.assertEqual(command.gcode, "M106 P2 S128\n")
        self.assertEqual(normalize_fan_speed(0.5), 128)

    def test_newline_presence(self):
        command = build_fan_command("part", 200)
        self.assertTrue(command.gcode.endswith("\n"))

    def test_unknown_fan_target(self):
        with self.assertRaises(ValueError):
            build_fan_command("unknown", 100)


if __name__ == "__main__":
    unittest.main()
