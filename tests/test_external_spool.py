import json
import os
import re
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from bambu_moonraker_shim import moonraker_api as api
from bambu_moonraker_shim.bambu_client import BambuClient
from bambu_moonraker_shim.database_manager import DatabaseManager
from bambu_moonraker_shim.external_spool import EXTERNAL_MACROS, MATERIAL_PRESETS, MATERIALS, filament_settings
from bambu_moonraker_shim.state_manager import StateManager


class ExternalSettingsTests(unittest.TestCase):
    def test_abs_type_uses_abs_profile_and_range(self):
        result = filament_settings({}, {'MATERIAL': 'ABS', 'COLOR': '#aabbcc'})
        self.assertEqual(result, {'material': 'ABS', 'profile_id': 'GFB99', 'color': 'AABBCCFF', 'nozzle_temp_min': 240, 'nozzle_temp_max': 280})
        # Partial printer telemetry must not inherit a PLA profile.
        result = filament_settings({'material': 'ABS'}, {'COLOR': '00000000'})
        self.assertEqual(result['profile_id'], 'GFB99')
        self.assertEqual(result['color'], '00000000')

    def test_material_changes_reset_profile_and_range_and_preserve_color(self):
        current = filament_settings({}, {'MATERIAL': 'ABS', 'COLOR': 'FF0000'})
        result = filament_settings(current, {'MATERIAL': 'PLA'})
        self.assertEqual(result['profile_id'], 'GFL99')
        self.assertEqual(result['nozzle_temp_min'], 190)
        self.assertEqual(result['color'], 'FF0000FF')
        self.assertEqual(filament_settings(result, {'TEMP_MAX': '230'})['nozzle_temp_max'], 230)

    def test_invalid_settings(self):
        for params in ({}, {'MATERIAL': 'mystery'}, {'COLOR': 'red'}, {'COLOR': 'GGGGGG'}, {'TEMP_MIN': 'nan'}, {'TEMP_MIN': '260', 'TEMP_MAX': '200'}, {'TEMP_MAX': '999'}, {'MATERIAL': 'ABS\nG28'}, {'PROFILE_ID': 'bad id'}, {'LANE': '0'}):
            with self.subTest(params=params), self.assertRaises(ValueError):
                filament_settings({}, params)

    def test_macro_discovery_and_parameter_templates(self):
        state = StateManager().get_state()
        for name in EXTERNAL_MACROS:
            self.assertIn('gcode_macro '+name, state)
            self.assertIn(name, state['gcode']['commands'])
            definition = state['configfile']['settings']['gcode_macro '+name.lower()]
            self.assertIn('[gcode_macro '+name+']', api.CONFIG_FILES['printer.cfg'])
            self.assertIn(name, api._GCODE_HELP)
            if name == 'SET_EXTERNAL_FILAMENT':
                self.assertEqual(set(re.findall(r'params\.([A-Z_]+)', definition['gcode'])), {'MATERIAL', 'COLOR', 'TEMP_MIN', 'TEMP_MAX', 'PROFILE_ID'})

    def test_macro_parser_preserves_quoted_values_and_hash_colors(self):
        name, params = api._parse_macro('SET_EXTERNAL_FILAMENT MATERIAL="ABS" COLOR=#ff0000')
        self.assertEqual(params, {'MATERIAL': 'ABS', 'COLOR': '#ff0000'})


class ExternalClientTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = DatabaseManager(os.path.join(directory.name, 'legacy.json'), os.path.join(directory.name, 'db.sqlite'))
        self.state = StateManager()
        self.client = BambuClient()
        self.client.connected = True
        self.client._reported_print_state = 'IDLE'
        self.client._mqtt_client = AsyncMock()
        for path, value in (
            ('bambu_moonraker_shim.database_manager.database_manager', self.db),
            ('bambu_moonraker_shim.bambu_client.state_manager', self.state),
            ('bambu_moonraker_shim.bambu_client.Config.BAMBU_MODEL', 'P1S'),
        ):
            patcher = patch(path, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_one_click_presets_select_defaults_and_preserve_colour(self):
        with patch.object(api, 'bambu_client', self.client):
            for material, (profile, minimum, maximum) in MATERIALS.items():
                with self.subTest(material=material):
                    self.client._external_reported = {
                        'material': material, 'color': '76D9F4FF', 'profile_id': 'CUSTOM',
                        'nozzle_temp_min': 250, 'nozzle_temp_max': 270,
                    }
                    result = await api.handle_jsonrpc({'jsonrpc': '2.0', 'id': 1,
                        'method': 'printer.gcode.script', 'params': {'script': 'SET_'+material}}, 1)
                    self.assertEqual(result['result'], 'ok', result)
                    wire = json.loads(self.client._mqtt_client.publish.await_args.args[1])['print']
                    self.assertEqual(wire['command'], 'ams_filament_setting')
                    self.assertEqual(wire['tray_type'], material)
                    self.assertEqual(wire['tray_color'], '76D9F4FF')
                    self.assertEqual(wire['tray_info_idx'], profile)
                    self.assertEqual((wire['nozzle_temp_min'], wire['nozzle_temp_max']), (minimum, maximum))
                    self.assertEqual(self.state.get_object('extruder')['target'], 0)
        self.assertEqual(self.client._mqtt_client.publish.await_count, len(MATERIALS))

    async def test_presets_registered_in_ui_and_reject_custom_arguments(self):
        from scripts.configuration.install_external_spool_panel import MACROS
        state = self.state.get_state()
        for name in MATERIAL_PRESETS:
            self.assertIn(name, MACROS)
            self.assertIn('gcode_macro '+name, state)
            definition = state['configfile']['settings']['gcode_macro '+name.lower()]
            self.assertNotIn('params.', definition['gcode'])
            self.assertIn(name, api._GCODE_HELP)
        with patch.object(api, 'bambu_client', self.client):
            result = await api.handle_jsonrpc({'jsonrpc': '2.0', 'id': 1,
                'method': 'printer.gcode.script', 'params': {'script': 'SET_PLA COLOR=FFFFFF'}}, 1)
        self.assertIn('error', result)
        self.client._mqtt_client.publish.assert_not_awaited()

    async def test_setting_payload_persistence_and_telemetry_confirmation(self):
        result = await self.client.set_external_filament({'MATERIAL': 'ABS', 'COLOR': 'aabbcc'})
        self.assertEqual(result, {'result': 'ok'})
        call = self.client._mqtt_client.publish.await_args
        wire = json.loads(call.args[1])['print']
        self.assertEqual(wire['ams_id'], 255)
        self.assertEqual(wire['tray_id'], 254)
        self.assertEqual(wire['slot_id'], 0)
        self.assertEqual(wire['tray_type'], 'ABS')
        self.assertEqual(wire['tray_color'], 'AABBCCFF')
        self.assertEqual(wire['tray_info_idx'], 'GFB99')
        self.assertEqual(call.kwargs['qos'], 0)
        variables = self.state.get_object('gcode_macro SET_EXTERNAL_FILAMENT')
        self.assertEqual(variables['sync_state'], 'pending')
        self.assertIsNone(variables['loaded'])
        await self.client._parse_external_filament({'vt_tray': {'tray_type': 'ABS', 'tray_color': 'AABBCCFF', 'tray_info_idx': 'GFB99', 'nozzle_temp_min': '240', 'nozzle_temp_max': '280'}})
        self.assertEqual(variables['sync_state'], 'confirmed')
        self.assertEqual(self.db.get_item('bambu_shim', 'external_filament')['material'], 'ABS')
        second = BambuClient()
        await second.restore_external_filament()
        self.assertEqual(second._external_settings['material'], 'ABS')
        self.assertEqual(variables['sync_state'], 'saved')

    async def test_load_unload_do_not_optimistically_claim_loaded(self):
        self.assertEqual(await self.client.load_filament(), {'result': 'ok'})
        variables = self.state.get_object('gcode_macro LOAD_EXTERNAL_FILAMENT')
        self.assertIsNone(variables['loaded'])
        self.assertEqual(variables['last_action'], 'load_requested')
        await self.client._parse_external_filament({'ams': {'tray_now': '254'}})
        self.assertTrue(variables['loaded'])
        self.assertEqual(await self.client.unload_filament(260), {'result': 'ok'})
        self.assertTrue(variables['loaded'])
        await self.client._parse_external_filament({'ams': {'tray_now': '255'}})
        self.assertFalse(variables['loaded'])

    async def test_native_extrusion_question_choices_and_telemetry_clear(self):
        self.client._external_tray_now = 254
        with patch.object(api, 'bambu_client', self.client):
            for code in (0x07FF8007, 0x07FFC00A):
                await self.client._parse_external_filament({'print_error': code})
                await self.client._parse_external_filament({'nozzle_temper': 250})
                self.assertTrue(self.state.get_object('gcode_macro LOAD_EXTERNAL_FILAMENT')['awaiting_extrusion_confirmation'])
                for macro, action in (('EXTERNAL_FILAMENT_EXTRUDED', 'done'), ('EXTERNAL_FILAMENT_RETRY', 'resume')):
                    result = await api.handle_jsonrpc({'jsonrpc': '2.0', 'id': 1,
                        'method': 'printer.gcode.script', 'params': {'script': macro}}, 1)
                    self.assertEqual(result['result'], 'ok', result)
                    wire = json.loads(self.client._mqtt_client.publish.await_args.args[1])['print']
                    self.assertEqual((wire['command'], wire['param']), ('ams_control', action))
                    self.assertTrue(self.client._external_extrusion_prompt)
            await self.client._parse_external_filament({'print_error': 0})
            self.assertFalse(self.client._external_extrusion_prompt)
            self.assertIn('error', await self.client.answer_external_extrusion('done'))

    async def test_extrusion_choices_refuse_prints_ams_unknown_and_unrelated_errors(self):
        for code, tray, state in ((0, 254, 'IDLE'), (123, 254, 'IDLE'),
                                 (0x07FF8007, 0, 'IDLE'), (0x07FF8007, None, 'IDLE'),
                                 (0x07FF8007, 254, 'RUNNING'), (0x07FF8007, 254, 'PAUSE')):
            self.client._external_tray_now = tray
            self.client._reported_print_state = state
            await self.client._parse_external_filament({'print_error': code})
            self.assertIn('error', await self.client.answer_external_extrusion('done'))
        self.client._mqtt_client.publish.assert_not_awaited()

    async def test_command_echo_does_not_create_extrusion_question(self):
        await self.client._parse_external_filament({'command': 'ams_control', 'print_error': 0x07FF8007})
        self.assertFalse(self.client._external_extrusion_prompt)

    async def test_extrusion_delivery_failure_preserves_question_and_returns_error(self):
        self.client._external_tray_now = 254
        await self.client._parse_external_filament({'print_error': 0x07FF8007})
        self.client._mqtt_client.publish.side_effect = OSError('disconnected')
        self.assertIn('error', await self.client.answer_external_extrusion('done'))
        self.assertTrue(self.client._external_extrusion_prompt)

    async def test_unload_uses_abs_range_without_altering_heater_commands(self):
        self.client._external_reported = {'material': 'ABS', 'nozzle_temp_min': 240, 'nozzle_temp_max': 280}
        self.assertEqual(await self.client.unload_filament(), {'result': 'ok'})
        wire = json.loads(self.client._mqtt_client.publish.await_args.args[1])['print']
        self.assertEqual(wire['curr_temp'], 260)
        self.assertEqual(wire['tar_temp'], 260)
        self.assertEqual(wire['target'], 255)
        self.assertEqual(self.client._mqtt_client.publish.await_count, 1)

    async def test_unknown_material_requires_explicit_unload_temperature(self):
        self.assertIn('error', await self.client.unload_filament())
        self.client._mqtt_client.publish.assert_not_awaited()

    async def test_blocked_states_and_ams_source_do_not_publish(self):
        for state in ('RUNNING', 'PREPARE', 'PAUSE', 'SLICING', None, 'UNKNOWN'):
            with self.subTest(state=state):
                self.client._reported_print_state = state
                self.assertIn('error', await self.client.load_filament())
                self.assertIn('error', await self.client.unload_filament(260))
                self.assertIn('error', await self.client.set_external_filament({'MATERIAL': 'ABS'}))
        self.client._reported_print_state = 'IDLE'
        self.client._external_tray_now = 0
        self.assertIn('error', await self.client.load_filament())
        self.client._mqtt_client.publish.assert_not_awaited()

    async def test_transport_failure_is_reported_and_not_retried(self):
        self.client._mqtt_client.publish.side_effect = OSError('socket closed')
        self.assertIn('error', await self.client.load_filament())
        self.assertEqual(self.client._mqtt_client.publish.await_count, 1)
        self.assertIsNone(self.state.get_object('gcode_macro LOAD_EXTERNAL_FILAMENT')['loaded'])

    async def test_invalid_edit_does_not_save_or_publish(self):
        result = await self.client.set_external_filament({'MATERIAL': 'ABS', 'COLOR': 'not hex'})
        self.assertIn('error', result)
        self.client._mqtt_client.publish.assert_not_awaited()
        self.assertIsNone(self.db.get_item('bambu_shim', 'external_filament'))

    async def test_disconnected_and_dual_spool_are_rejected(self):
        self.client.connected = False
        self.assertIn('error', await self.client.load_filament())
        self.client.connected = True
        self.client._multiple_external_spools = True
        self.assertIn('error', await self.client.load_filament())
        self.client._mqtt_client.publish.assert_not_awaited()

    async def test_macro_rpc_dispatch_only_sends_translated_filament_command(self):
        with patch.object(api, 'bambu_client', self.client):
            for command in ('LOAD_EXTERNAL_FILAMENT', 'UNLOAD_EXTERNAL_FILAMENT TEMP=260', 'SET_EXTERNAL_FILAMENT MATERIAL=ABS COLOR=112233'):
                result = await api.handle_jsonrpc({'jsonrpc': '2.0', 'id': 1, 'method': 'printer.gcode.script', 'params': {'script': command}}, 1)
                self.assertEqual(result['result'], 'ok', result)
        wires = [json.loads(call.args[1])['print']['command'] for call in self.client._mqtt_client.publish.await_args_list]
        self.assertEqual(wires, ['ams_change_filament', 'ams_change_filament', 'ams_filament_setting'])

    async def test_matching_rejection_is_reported_but_unrelated_reply_is_ignored(self):
        from types import SimpleNamespace
        await self.client.set_external_filament({'MATERIAL': 'ABS'})
        sequence = json.loads(self.client._mqtt_client.publish.await_args.args[1])['print']['sequence_id']
        variables = self.state.get_object('gcode_macro SET_EXTERNAL_FILAMENT')
        async def report(seq):
            await self.client._handle_message(SimpleNamespace(payload=json.dumps({'print': {
                'command': 'ams_filament_setting', 'sequence_id': seq, 'result': 'fail', 'reason': 'test rejection'
            }}).encode()))
        await report('unrelated')
        self.assertEqual(variables['sync_state'], 'pending')
        await report(sequence)
        self.assertEqual(variables['sync_state'], 'failed')
        self.assertEqual(variables['last_error'], 'test rejection')

    async def test_legacy_aliases_route_to_external_without_ams_params(self):
        with patch.object(api, 'bambu_client', self.client):
            for command in ('LOAD_FILAMENT', 'UNLOAD_FILAMENT TEMP=260'):
                # Generic names remain aliases; TEMP is honored for external unloading.
                self.client._external_reported = {'material': 'ABS', 'nozzle_temp_min': 240, 'nozzle_temp_max': 280}
                result = await api.handle_jsonrpc({'jsonrpc': '2.0', 'id': 1, 'method': 'printer.gcode.script', 'params': {'script': command}}, 1)
                self.assertEqual(result['result'], 'ok')
        self.assertEqual(self.client._mqtt_client.publish.await_count, 2)
