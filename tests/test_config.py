import unittest

from bambu_moonraker_shim.config import (
    Config,
    model_supports_chamber_temperature,
)
from bambu_moonraker_shim.state_manager import StateManager


class ConfigModelTests(unittest.TestCase):
    def test_model_supports_chamber_temperature(self):
        self.assertTrue(model_supports_chamber_temperature(""))
        self.assertTrue(model_supports_chamber_temperature("X1C"))
        self.assertTrue(model_supports_chamber_temperature("H2D"))
        self.assertFalse(model_supports_chamber_temperature("P1S"))
        self.assertFalse(model_supports_chamber_temperature("A1"))
        self.assertFalse(model_supports_chamber_temperature("A1 Mini"))

    def test_state_manager_hides_chamber_for_p1_a1(self):
        previous_model = Config.BAMBU_MODEL
        try:
            Config.BAMBU_MODEL = "P1S"
            state = StateManager().get_state()

            self.assertNotIn("heater_chamber", state)
            self.assertIn("heaters", state)
            self.assertNotIn("heater_chamber", state["heaters"]["available_heaters"])
            self.assertNotIn("heater_chamber", state["heaters"]["available_sensors"])
            self.assertNotIn("heater_chamber", state["configfile"]["settings"])
            self.assertNotIn("heater_chamber", state["configfile"]["config"])
        finally:
            Config.BAMBU_MODEL = previous_model


if __name__ == "__main__":
    unittest.main()
