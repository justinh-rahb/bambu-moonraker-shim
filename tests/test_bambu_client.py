import json
import unittest
from unittest.mock import AsyncMock, patch

from bambu_moonraker_shim.bambu_client import BambuClient
from bambu_moonraker_shim.config import Config
from bambu_moonraker_shim.state_manager import state_manager


class BambuClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_command_uses_qos_1_and_sequence_id(self):
        client = BambuClient()
        client.connected = True
        client.serial = "SERIAL123"
        client._mqtt_client = AsyncMock()

        cmd = {"print": {"command": "pause"}}
        await client.publish_command(cmd)

        client._mqtt_client.publish.assert_awaited_once()
        args, kwargs = client._mqtt_client.publish.await_args
        self.assertEqual(args[0], "device/SERIAL123/request")
        self.assertEqual(kwargs["qos"], 1)

        payload = json.loads(args[1])
        self.assertEqual(payload["print"]["command"], "pause")
        self.assertEqual(payload["print"]["sequence_id"], "1")

    async def test_send_temperature_command_uses_non_blocking_gcodes(self):
        client = BambuClient()
        client.connected = True
        client._mqtt_client = AsyncMock()
        client.send_gcode_line = AsyncMock()
        client._track_local_target = AsyncMock()

        result = await client.set_nozzle_temp(220)
        self.assertEqual(result, {"result": "ok"})
        client.send_gcode_line.assert_awaited_with("M104 T0 S220\n")

        result = await client.set_bed_temp(60)
        self.assertEqual(result, {"result": "ok"})
        client.send_gcode_line.assert_awaited_with("M140 S60\n")

        result = await client.set_chamber_temp(45)
        self.assertEqual(result, {"result": "ok"})
        client.send_gcode_line.assert_awaited_with("M141 S45\n")

    async def test_start_print_h2d_converts_calibration_fields_to_ints(self):
        client = BambuClient()
        client.connected = True
        client._mqtt_client = AsyncMock()
        client.publish_command = AsyncMock()

        previous_model = Config.BAMBU_MODEL
        Config.BAMBU_MODEL = "H2D"
        try:
            result = await client.start_print(
                filename="folder/test.3mf",
                plate_number=2,
                use_ams=True,
                bed_leveling=True,
                flow_calibration=False,
                timelapse=True,
                vibration_cali=True,
                layer_inspect=True,
                cfg="fast",
                extrude_cali_flag=False,
                ams_mapping=[0, 1],
                ams_mapping2=[{"ams_id": 1, "slot_id": 2}],
            )
        finally:
            Config.BAMBU_MODEL = previous_model

        self.assertEqual(result, {"result": "ok"})
        payload = client.publish_command.await_args.args[0]["print"]
        self.assertEqual(payload["url"], "ftp://folder/test.3mf")
        self.assertEqual(payload["file"], "folder/test.3mf")
        self.assertEqual(payload["subtask_name"], "folder/test")
        self.assertEqual(payload["param"], "Metadata/plate_2.gcode")
        self.assertEqual(payload["auto_bed_leveling"], 1)
        self.assertEqual(payload["vibration_cali"], 1)
        self.assertEqual(payload["layer_inspect"], 1)
        self.assertEqual(payload["extrude_cali_flag"], 0)
        self.assertEqual(payload["use_ams"], True)
        self.assertEqual(payload["ams_mapping"], "0,1")
        self.assertEqual(payload["ams_mapping2"], [{"ams_id": 1, "slot_id": 2}])

    async def test_start_print_p2s_forces_vibration_cali_false(self):
        client = BambuClient()
        client.connected = True
        client._mqtt_client = AsyncMock()
        client.publish_command = AsyncMock()

        previous_model = Config.BAMBU_MODEL
        Config.BAMBU_MODEL = "P2S"
        try:
            result = await client.start_print(
                filename="test.gcode",
                vibration_cali=True,
            )
        finally:
            Config.BAMBU_MODEL = previous_model

        self.assertEqual(result, {"result": "ok"})
        payload = client.publish_command.await_args.args[0]["print"]
        self.assertEqual(payload["vibration_cali"], False)

    async def test_parse_telemetry_normalizes_fan_steps(self):
        client = BambuClient()
        updates_mock = AsyncMock()
        with patch.object(state_manager, "update_state", updates_mock):
            await client._parse_telemetry(
                {
                    "cooling_fan_speed": 15,
                    "big_fan1_speed": 8,
                    "big_fan2_speed": 3,
                }
            )

        updates = updates_mock.await_args.args[0]
        self.assertEqual(updates["fan"]["speed"], 1.0)
        self.assertAlmostEqual(updates["fan_generic aux"]["speed"], 8.0 / 15.0)
        self.assertAlmostEqual(updates["fan_generic chamber"]["speed"], 3.0 / 15.0)


if __name__ == "__main__":
    unittest.main()
