import unittest

from bambu_moonraker_shim.moonraker_api import _extract_skip_object_ids, _m220_percent_to_mode


class MoonrakerCommandTests(unittest.TestCase):
    def test_m220_percent_maps_to_expected_modes(self):
        self.assertEqual(_m220_percent_to_mode(50), 1)
        self.assertEqual(_m220_percent_to_mode(100), 2)
        self.assertEqual(_m220_percent_to_mode(124), 3)
        self.assertEqual(_m220_percent_to_mode(166), 4)
        self.assertEqual(_m220_percent_to_mode(140), 3)

    def test_extract_skip_object_ids_from_fields(self):
        self.assertEqual(_extract_skip_object_ids({"OBJECT": "5"}), [5])
        self.assertEqual(_extract_skip_object_ids({"ID": "7"}), [7])
        self.assertEqual(_extract_skip_object_ids({"NAME": "object_12"}), [12])
        self.assertEqual(_extract_skip_object_ids({"NAME": "foo", "OBJECT": "2"}), [2])


if __name__ == "__main__":
    unittest.main()
