import asyncio
import json
import unittest
from unittest.mock import AsyncMock, patch

from bambu_moonraker_shim.bambu_client import BambuClient
from bambu_moonraker_shim.config import Config
from bambu_moonraker_shim.state_manager import state_manager


class BambuClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_malformed_telemetry_does_not_disconnect(self):
        from types import SimpleNamespace
        client = BambuClient()
        client._parse_telemetry = AsyncMock()
        for payload in (b"[]", b"null", b'{"print": []}', b"\xff", b"{broken"):
            await client._handle_message(SimpleNamespace(payload=payload))
        client._parse_telemetry.assert_not_awaited()
        await client._handle_message(SimpleNamespace(payload=b'{"print":{"bed_temper":25}}'))
        client._parse_telemetry.assert_awaited_once_with({"bed_temper": 25})

    async def test_prepare_and_failed_telemetry(self):
        client = BambuClient()
        with patch.object(state_manager, "update_state", AsyncMock()) as update:
            await client._parse_telemetry({"gcode_state": "PREPARE", "subtask_name": "test"})
            self.assertEqual(update.await_args.args[0]["print_stats"]["state"], "printing")
            await client._parse_telemetry({"gcode_state": "FAILED"})
            self.assertEqual(update.await_args.args[0]["print_stats"]["state"], "error")

    async def test_mock_lifecycle_stops_background_task(self):
        client = BambuClient()
        client.serial = ""
        await client.start()
        task = client._loop_task
        await client.start()
        self.assertIs(client._loop_task, task)
        await client.stop()
        self.assertTrue(task.cancelled())
        self.assertFalse(client.connected)
        self.assertIsNone(client._loop_task)

    async def test_publish_command_uses_qos_1_and_sequence_id(self):
        client = BambuClient()
        client.connected = True
        client.serial = "SERIAL123"
        client._mqtt_client = AsyncMock()

        cmd = {"print": {"command": "pause"}}
        await client.publish_command(cmd)
        await asyncio.sleep(0)

        client._mqtt_client.publish.assert_awaited_once()
        args, kwargs = client._mqtt_client.publish.await_args
        self.assertEqual(args[0], "device/SERIAL123/request")
        self.assertEqual(kwargs["qos"], 1)

        payload = json.loads(args[1])
        self.assertEqual(payload["print"]["command"], "pause")
        self.assertEqual(payload["print"]["sequence_id"], "1")
        self.assertEqual(payload["user_id"], client.user_id)

    async def test_publish_command_retries_qos_0_after_qos_1_error(self):
        client = BambuClient()
        client.connected = True
        client.serial = "SERIAL123"
        client._mqtt_client = AsyncMock()
        client._mqtt_client.publish.side_effect = [
            Exception("Operation timed out"),
            None,
        ]

        cmd = {"print": {"command": "pause"}}
        await client.publish_command(cmd)
        await asyncio.sleep(0)

        self.assertEqual(client._mqtt_client.publish.await_count, 2)
        first_call = client._mqtt_client.publish.await_args_list[0]
        second_call = client._mqtt_client.publish.await_args_list[1]
        self.assertEqual(first_call.kwargs["qos"], 1)
        self.assertEqual(second_call.kwargs["qos"], 0)
        self.assertTrue(client._prefer_qos0_for_print)

    async def test_publish_command_uses_qos_0_after_compatibility_downgrade(self):
        client = BambuClient()
        client.connected = True
        client.serial = "SERIAL123"
        client._mqtt_client = AsyncMock()
        client._prefer_qos0_for_print = True

        cmd = {"print": {"command": "resume"}}
        await client.publish_command(cmd)
        await asyncio.sleep(0)

        client._mqtt_client.publish.assert_awaited_once()
        args, kwargs = client._mqtt_client.publish.await_args
        self.assertEqual(args[0], "device/SERIAL123/request")
        self.assertEqual(kwargs["qos"], 0)

    async def test_send_temperature_command_uses_supported_heater_gcodes(self):
        client = BambuClient()
        client.connected = True
        client._mqtt_client = AsyncMock()
        client.send_gcode_line = AsyncMock()
        client._track_local_target = AsyncMock()

        result = await client.set_nozzle_temp(220)
        self.assertEqual(result, {"result": "ok"})
        client.send_gcode_line.assert_awaited_with("M109 S220\n")

        result = await client.set_bed_temp(60)
        self.assertEqual(result, {"result": "ok"})
        client.send_gcode_line.assert_awaited_with("M190 S60\n")

        result = await client.set_chamber_temp(45)
        self.assertEqual(result, {"result": "ok"})
        client.send_gcode_line.assert_awaited_with("M191 S45\n")

    async def test_extruder_zero_target_telemetry_logs_without_alternate_gcode(self):
        client = BambuClient()
        client.connected = True
        client.send_gcode_line = AsyncMock()
        client._track_local_target = AsyncMock()

        updates_mock = AsyncMock()
        with patch.object(state_manager, "update_state", updates_mock), patch("builtins.print") as print_mock:
            result = await client.set_nozzle_temp(220)
            self.assertEqual(result, {"result": "ok"})
            await client._parse_telemetry({"nozzle_target_temper": 0})

        self.assertEqual(client.send_gcode_line.await_args_list[0].args[0], "M109 S220\n")
        self.assertEqual(client.send_gcode_line.await_count, 1)
        self.assertTrue(
            any(
                "Telemetry reported extruder target as 0" in str(call.args[0])
                for call in print_mock.call_args_list
                if call.args
            )
        )

    async def test_non_ams_load_unload_commands(self):
        client = BambuClient()
        client.connected = True
        client._reported_print_state = "IDLE"
        client._mqtt_client = AsyncMock()
        with patch.object(state_manager, "update_state", AsyncMock()):
            self.assertEqual(await client.load_filament(), {"result": "ok"})
            self.assertEqual(await client.unload_filament(260), {"result": "ok"})
        messages = [json.loads(call.args[1])["print"] for call in client._mqtt_client.publish.await_args_list]
        self.assertEqual(messages[0]["command"], "ams_change_filament")
        self.assertEqual(messages[0]["ams_id"], 255)
        self.assertEqual(messages[0]["slot_id"], 254)
        self.assertEqual(messages[0]["target"], 254)
        self.assertEqual(messages[1]["slot_id"], 255)
        self.assertEqual(messages[1]["target"], 255)
        self.assertEqual(messages[1]["tar_temp"], 260)

    async def test_start_print_h2d_converts_calibration_fields_to_ints(self):
        client = BambuClient()
        client.connected = True
        client._mqtt_client = AsyncMock()
        client._reported_print_state = "IDLE"
        client._publish_print_start = AsyncMock(return_value={"result": "ok"})

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
        payload = client._publish_print_start.await_args.args[0]["print"]
        self.assertEqual(payload["url"], "ftp:///folder/test.3mf")
        self.assertEqual(payload["file"], "/folder/test.3mf")
        self.assertEqual(payload["subtask_name"], "folder/test")
        self.assertEqual(payload["param"], "Metadata/plate_2.gcode")
        self.assertEqual(payload["auto_bed_leveling"], 1)
        self.assertEqual(payload["vibration_cali"], 1)
        self.assertEqual(payload["layer_inspect"], 1)
        self.assertEqual(payload["extrude_cali_flag"], 0)
        self.assertEqual(payload["use_ams"], True)
        self.assertEqual(payload["ams_mapping"], [0, 1])
        self.assertEqual(payload["ams_mapping2"], [{"ams_id": 1, "slot_id": 2}])

    async def test_start_print_p2s_forces_vibration_cali_false(self):
        client = BambuClient()
        client.connected = True
        client._mqtt_client = AsyncMock()
        client._reported_print_state = "IDLE"
        client._publish_print_start = AsyncMock(return_value={"result": "ok"})

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
        payload = client._publish_print_start.await_args.args[0]["print"]
        self.assertEqual(payload["vibration_cali"], False)

    async def test_print_start_uses_nonzero_distinct_job_ids_and_omits_mapping(self):
        client = BambuClient()
        client.connected = True
        client._reported_print_state = 'FINISH'
        client._publish_print_start = AsyncMock(return_value={'result': 'ok'})
        with patch('bambu_moonraker_shim.bambu_client.time.time', return_value=100):
            await client.start_print('benchy.3mf')
            await client.start_print('benchy.3mf')
            await client.start_print('benchy.3mf')
        a, b, c = [call.args[0]['print'] for call in client._publish_print_start.await_args_list]
        self.assertEqual(len({a['task_id'], b['task_id'], c['task_id']}), 3)
        self.assertTrue(0 < int(a['task_id']) <= 2147483647)
        self.assertEqual(a['task_id'], a['project_id'])
        self.assertEqual(a['task_id'], a['subtask_id'])
        self.assertTrue(a['bed_leveling'])
        self.assertIs(type(a['auto_bed_leveling']), int)
        self.assertNotIn('bed_levelling', a)
        self.assertNotIn('cfg', a)
        self.assertNotIn('ams_mapping', a)

    async def test_print_start_blocks_active_unknown_or_pending_submission(self):
        client = BambuClient()
        client.connected = True
        client._publish_print_start = AsyncMock()
        for state in (None, 'RUNNING', 'PAUSE', 'PREPARE', 'SLICING'):
            client._reported_print_state = state
            self.assertIn('error', await client.start_print('test.3mf'))
        client._reported_print_state = 'IDLE'
        client._pending_print_start = ('1', None)
        self.assertIn('error', await client.start_print('test.3mf'))
        client._publish_print_start.assert_not_awaited()

    async def test_p1_and_a1_start_references_absolute_sd_card_file(self):
        client = BambuClient()
        client.connected = True
        client._reported_print_state = 'FINISH'
        client._publish_print_start = AsyncMock(return_value={'result': 'ok'})
        for model in ('P1S', 'P1P', 'A1', 'A1 MINI'):
            with patch.object(Config, 'BAMBU_MODEL', model):
                await client.start_print('/benchy.gcode.3mf')
            body = client._publish_print_start.await_args.args[0]['print']
            self.assertEqual(body['url'], 'file:///sdcard/benchy.gcode.3mf')
            self.assertEqual(body['file'], '/benchy.gcode.3mf')

    async def test_print_start_requires_matching_firmware_ack_even_without_puback(self):
        from types import SimpleNamespace
        client = BambuClient()
        client._mqtt_client = AsyncMock()
        client._parse_telemetry = AsyncMock()
        async def publish(topic, payload, **kwargs):
            body = json.loads(payload)
            fields = body['print']
            self.assertEqual(body.get('user_id'), client.user_id)
            self.assertEqual(kwargs['qos'], 1)
            self.assertTrue(20000 <= int(fields['sequence_id']) < 30000)
            # An unrelated acknowledgement must not finish the request.
            for seq in ('unrelated', fields['sequence_id']):
                await client._handle_message(SimpleNamespace(payload=json.dumps({'print': {
                    'command': 'project_file', 'sequence_id': seq, 'result': 'success',
                }}).encode()))
            raise TimeoutError('No broker PUBACK')
        client._mqtt_client.publish.side_effect = publish
        self.assertEqual(await client._publish_print_start({'print': {'command': 'project_file'}}), {'result': 'ok'})
        client._mqtt_client.publish.assert_awaited_once()
        self.assertIsNone(client._pending_print_start)

    async def test_print_start_timeout_and_rejection_never_replay(self):
        from types import SimpleNamespace
        for reject in (False, True):
            client = BambuClient()
            client._mqtt_client = AsyncMock()
            client._parse_telemetry = AsyncMock()
            client._print_start_timeout = .02
            async def publish(topic, payload, **kwargs):
                if reject:
                    seq = json.loads(payload)['print']['sequence_id']
                    await client._handle_message(SimpleNamespace(payload=json.dumps({'print': {
                        'command': 'project_file', 'sequence_id': seq, 'result': 'fail', 'reason': 'busy',
                    }}).encode()))
            client._mqtt_client.publish.side_effect = publish
            result = await client._publish_print_start({'print': {'command': 'project_file'}})
            self.assertIn('busy' if reject else 'not acknowledge', result['error'])
            client._mqtt_client.publish.assert_awaited_once()
            self.assertIsNone(client._pending_print_start)

    async def test_command_ack_does_not_replace_live_job_or_error(self):
        from types import SimpleNamespace
        client = BambuClient()
        client._parse_telemetry = AsyncMock()
        await client._handle_message(SimpleNamespace(payload=json.dumps({'print': {
            'command': 'project_file', 'result': 'success', 'subtask_name': 'request echo',
            'print_error': 123,
        }}).encode()))
        client._parse_telemetry.assert_not_awaited()

    async def test_new_job_delta_resets_previous_progress_but_resume_preserves_it(self):
        client = BambuClient()
        client._latest_layer_num = client._latest_total_layer_num = 674
        client._latest_remaining_time = 200
        with patch.object(state_manager, 'update_state', AsyncMock()) as update:
            await client._parse_telemetry({'gcode_state': 'PREPARE', 'subtask_name': 'benchy'})
            values = update.await_args.args[0]
            self.assertEqual(values['print_stats']['info'], {'current_layer': 0, 'total_layer': 0})
            self.assertEqual(values['display_status']['progress'], 0)
            self.assertEqual(values['virtual_sdcard']['file_position'], 0)
            await client._parse_telemetry({'gcode_state': 'RUNNING', 'subtask_name': 'benchy',
                                          'layer_num': 1, 'total_layer_num': 192, 'mc_percent': 1})
            values = update.await_args.args[0]
            self.assertEqual(values['print_stats']['info']['total_layer'], 192)
            self.assertEqual(values['display_status']['progress'], .01)
            await client._parse_telemetry({'gcode_state': 'PAUSE'})
            await client._parse_telemetry({'gcode_state': 'RUNNING', 'subtask_name': 'benchy'})
            values = update.await_args.args[0]
            self.assertNotIn('progress', values.get('display_status', {}))
            self.assertEqual(client._latest_layer_num, 1)

    async def test_print_start_timeout_reports_native_printer_error(self):
        client = BambuClient()
        client._mqtt_client = AsyncMock()
        client._print_start_timeout = .01
        with patch.object(state_manager, 'update_state', AsyncMock()):
            await client._parse_telemetry({'print_error': 84033543})
        result = await client._publish_print_start({'print': {'command': 'project_file'}})
        self.assertIn(f'{84033543:08X}', result['error'])

    async def test_only_correlated_successful_stop_maps_failed_to_cancelled(self):
        from types import SimpleNamespace
        client = BambuClient()
        client._cancel_sequence = '25'
        with patch.object(state_manager, 'update_state', AsyncMock()) as update:
            for seq, result in (('other', 'success'), ('25', 'fail'), ('25', 'success')):
                await client._handle_message(SimpleNamespace(payload=json.dumps({'print': {
                    'command': 'stop', 'sequence_id': seq, 'result': result,
                }}).encode()))
                await client._parse_telemetry({'gcode_state': 'FAILED'})
                expected = 'cancelled' if seq == '25' and result == 'success' else 'error'
                self.assertEqual(update.await_args.args[0]['print_stats']['state'], expected)
            await client._parse_telemetry({'gcode_state': 'PREPARE', 'subtask_name': 'next job'})
            self.assertFalse(client._cancel_confirmed)
            await client._parse_telemetry({'gcode_state': 'FAILED'})
            self.assertEqual(update.await_args.args[0]['print_stats']['state'], 'error')

    async def test_acknowledged_stop_does_not_replay_on_broker_timeout(self):
        client = BambuClient()
        client._mqtt_client = AsyncMock()
        client._cancel_sequence = '25'
        async def publish(*args, **kwargs):
            client._cancel_confirmed = True
            raise TimeoutError('Missing PUBACK')
        client._mqtt_client.publish.side_effect = publish
        await client._publish_background('topic', json.dumps({'print': {
            'command': 'stop', 'sequence_id': '25',
        }}), 1)
        client._mqtt_client.publish.assert_awaited_once()

    def test_ams_mapping_uses_wire_arrays_and_accepts_legacy_csv(self):
        for source in ([0, 1], '0,1', '[0,1]'):
            self.assertEqual(BambuClient._normalize_ams_mapping(source), [0, 1])
        self.assertIsNone(BambuClient._normalize_ams_mapping(''))
        for source in ('bad', {'tray': 0}, [True], [1.5]):
            with self.assertRaises((ValueError, TypeError)):
                BambuClient._normalize_ams_mapping(source)

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

    async def test_parse_telemetry_exposes_layer_and_duration_fields(self):
        client = BambuClient()
        client._job_started_at = 100.0
        client._active_job_filename = "open back"
        client._active_gcode_file = "open back.3mf"
        updates_mock = AsyncMock()

        with patch.object(state_manager, "update_state", updates_mock), patch(
            "bambu_moonraker_shim.bambu_client.time.time",
            return_value=160.0,
        ):
            await client._parse_telemetry(
                {
                    "gcode_state": "RUNNING",
                    "subtask_name": "open back",
                    "gcode_file": "open back.3mf",
                    "mc_percent": 7,
                    "mc_remaining_time": 188,
                    "layer_num": 1,
                    "total_layer_num": 256,
                }
            )

        updates = updates_mock.await_args.args[0]
        self.assertEqual(updates["print_stats"]["state"], "printing")
        self.assertEqual(updates["print_stats"]["filename"], "open back")
        self.assertEqual(updates["print_stats"]["info"]["current_layer"], 1)
        self.assertEqual(updates["print_stats"]["info"]["total_layer"], 256)
        self.assertAlmostEqual(updates["print_stats"]["print_duration"], 849.032, places=2)
        self.assertAlmostEqual(updates["print_stats"]["total_duration"], 849.032, places=2)
        self.assertNotIn("filament_used", updates["print_stats"])
        self.assertEqual(updates["display_status"]["progress"], 0.07)
        self.assertEqual(updates["virtual_sdcard"]["file_position"], 0)



if __name__ == "__main__":
    unittest.main()
