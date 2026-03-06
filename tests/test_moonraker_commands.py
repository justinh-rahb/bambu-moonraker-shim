import unittest
from unittest.mock import AsyncMock, patch

from bambu_moonraker_shim import moonraker_api
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


class MoonrakerHeaterDedupTests(unittest.IsolatedAsyncioTestCase):
    async def test_printer_gcode_script_suppresses_duplicate_heater_targets(self):
        request = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "printer.gcode.script",
            "params": {
                "script": "SET_HEATER_TEMPERATURE HEATER=extruder TARGET=50\nM104 S50",
            },
        }

        set_nozzle_mock = AsyncMock(return_value={"result": "ok"})
        update_state_mock = AsyncMock()
        send_gcode_mock = AsyncMock()
        with patch.object(moonraker_api.bambu_client, "set_nozzle_temp", set_nozzle_mock), patch.object(
            moonraker_api.state_manager, "update_state", update_state_mock
        ), patch.object(moonraker_api.bambu_client, "send_gcode_line", send_gcode_mock):
            response = await moonraker_api.handle_jsonrpc(request, connection_id=1)

        self.assertEqual(response["result"], "ok")
        set_nozzle_mock.assert_awaited_once_with(50.0)
        send_gcode_mock.assert_not_awaited()
        self.assertEqual(update_state_mock.await_count, 1)


if __name__ == "__main__":
    unittest.main()
