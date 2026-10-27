import asyncio
import json
import os
import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from starlette.requests import Request
from bambu_moonraker_shim.database_manager import DatabaseManager, DatabaseError
from bambu_moonraker_shim import moonraker_api as api


class DatabaseTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.legacy = os.path.join(self.directory.name, 'moonraker.json')
        self.sqlite = os.path.join(self.directory.name, 'settings.db')
        self.db = DatabaseManager(self.legacy, self.sqlite)

    def test_nested_and_literal_dot_keys_survive_restart(self):
        self.db.post_item('fluidd', 'general.theme', 'dark')
        self.db.post_item('fluidd', ['files', 'part.3mf'], {'count': 2})
        db = DatabaseManager(self.legacy, self.sqlite)
        self.assertEqual(db.get_item('fluidd'), {'general': {'theme': 'dark'}, 'files': {'part.3mf': {'count': 2}}})
        self.assertEqual(db.get_item('fluidd', ['files', 'part.3mf', 'count']), 2)
        self.assertFalse(os.path.exists(self.legacy))

    def test_import_is_once_and_preserves_json(self):
        path = self.sqlite + '.import'
        original = json.dumps({'mainsail': {'general.theme': 'dark', 'view.webcam': {'name': 'Camera'}, 'view.webcam.name': 'New'}, 'moonraker': {'webcams': []}})
        with open(self.legacy, 'w') as f:
            f.write(original)
        db = DatabaseManager(self.legacy, path)
        self.assertEqual(db.get_item('mainsail', 'view.webcam.name'), 'New')
        db.post_item('mainsail', 'general.theme', 'light')
        self.assertEqual(DatabaseManager(self.legacy, path).get_item('mainsail', 'general.theme'), 'light')
        with open(self.legacy) as f:
            self.assertEqual(f.read(), original)

    def test_corrupt_import_does_not_mark_complete(self):
        with open(self.legacy, 'w') as f:
            f.write('{broken')
        path = self.sqlite + '.corrupt'
        with self.assertRaises(ValueError):
            DatabaseManager(self.legacy, path)
        with open(self.legacy, 'w') as f:
            json.dump({'fluidd': {'general.theme': 'dark'}}, f)
        self.assertEqual(DatabaseManager(self.legacy, path).get_item('fluidd', 'general.theme'), 'dark')

    def test_missing_null_and_delete_semantics(self):
        self.db.post_item('client', 'null', None)
        self.assertIsNone(self.db.get_item('client', 'null', strict=True))
        with self.assertRaises(DatabaseError) as error:
            self.db.get_item('client', 'missing', strict=True)
        self.assertEqual(error.exception.code, 404)
        self.assertIsNone(self.db.delete_item('client', 'null'))
        self.assertNotIn('client', self.db.get_namespaces())
        with self.assertRaises(DatabaseError):
            self.db.delete_item('client', 'missing')

    def test_delete_returns_value_and_preserves_siblings(self):
        self.db.post_item('client', 'settings', {'theme': 'dark', 'size': 10})
        self.assertEqual(self.db.delete_item('client', 'settings.size'), 10)
        self.assertEqual(self.db.get_item('client'), {'settings': {'theme': 'dark'}})

    def test_invalid_writes_rollback(self):
        self.db.post_item('client', 'value', 42)
        for key in (None, '', [], [''], 42):
            with self.assertRaises(DatabaseError):
                self.db.post_item('client', key, 'bad')
        with self.assertRaises(DatabaseError):
            self.db.post_item('client', 'value.child', 'bad')
        with self.assertRaises(ValueError):
            self.db.post_item('client', 'value', float('nan'))
        self.assertEqual(self.db.get_item('client', 'value'), 42)
        with sqlite3.connect(self.sqlite) as conn:
            self.assertEqual(conn.execute('PRAGMA integrity_check').fetchone()[0], 'ok')

    def test_concurrent_writers_do_not_lose_siblings(self):
        def write(index):
            DatabaseManager(self.legacy, self.sqlite).post_item('client', f'settings.key{index}', index)
        with ThreadPoolExecutor(max_workers=8) as executor:
            list(executor.map(write, range(40)))
        self.assertEqual(len(self.db.get_item('client', 'settings')), 40)


class DatabaseAPITests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.db = DatabaseManager(os.path.join(self.directory.name, 'legacy.json'), os.path.join(self.directory.name, 'db.sqlite'))
        patcher = patch.object(api, 'database_manager', self.db)
        patcher.start()
        self.addCleanup(patcher.stop)

    async def rpc(self, method, **params):
        return await api.handle_jsonrpc({'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params}, 1)

    async def http(self, method, query='', body=None):
        encoded = json.dumps(body).encode() if body is not None else b''
        async def receive():
            return {'type': 'http.request', 'body': encoded}
        request = Request({'type': 'http', 'method': method, 'path': '/server/database/item', 'query_string': query.encode(), 'headers': []}, receive)
        handler = {'GET': api.database_get, 'POST': api.database_post, 'DELETE': api.database_delete}[method]
        result = await handler(request)
        if hasattr(result, 'body'):
            return result.status_code, json.loads(result.body)
        return 200, result

    async def test_http_rpc_interoperability_and_maintenance(self):
        code, value = await self.http('POST', body={'namespace': 'maintenance', 'key': ['parts', 'nozzle.04'], 'value': {'hours': 12}})
        self.assertEqual(code, 200)
        result = await self.rpc('server.database.get_item', namespace='maintenance', key=['parts', 'nozzle.04'])
        self.assertEqual(result['result']['value'], {'hours': 12})
        code, value = await self.http('GET', 'namespace=maintenance&key:json=%5B%22parts%22,%22nozzle.04%22%5D')
        self.assertEqual(value['result']['value'], {'hours': 12})
        code, value = await self.http('DELETE', body={'namespace': 'maintenance', 'key': ['parts', 'nozzle.04']})
        self.assertEqual(value['result']['value'], {'hours': 12})

    async def test_validation_missing_and_protected(self):
        for params, expected in (({'namespace': 'x', 'key': 'a'}, 400), ({'namespace': 'x', 'value': 1}, 400), ({'namespace': 'moonraker', 'key': 'a', 'value': 1}, 403)):
            result = await self.rpc('server.database.post_item', **params)
            code, value = await self.http('POST', body=params)
            self.assertEqual(result['error']['code'], expected)
            self.assertEqual(code, expected)
        result = await self.rpc('server.database.get_item', namespace='x', key='a')
        self.assertEqual(result['error']['code'], 404)
        self.assertEqual((await self.http('GET', 'namespace=x&key=a'))[0], 404)

    async def test_typed_query_and_null(self):
        self.assertEqual((await self.http('POST', 'namespace=x&key=a&value:json=null'))[0], 200)
        result = await self.rpc('server.database.get_item', namespace='x', key='a')
        self.assertIsNone(result['result']['value'])
        self.assertEqual((await self.http('POST', body=[]))[0], 400)

    async def test_bad_rpc_does_not_break_subsequent_requests(self):
        result = await api.handle_jsonrpc(42, 1)
        self.assertEqual(result['error']['code'], -32600)
        result = await self.rpc('does.not.exist')
        self.assertEqual(result['error']['code'], -32601)
        result = await self.rpc('server.database.post_item', namespace='x', key=['literal.dot'], value=False)
        self.assertFalse(result['result']['value'])
        result = await api.handle_jsonrpc({'jsonrpc': '2.0', 'method': 'server.database.post_item', 'params': {'namespace': 'x', 'key': 'a', 'value': 0}}, 1)
        self.assertIsNone(result)
        self.assertEqual(self.db.get_item('x', 'a'), 0)

    async def test_builtin_camera_settings_persist(self):
        builtin = {'uid': 'builtin', 'name': 'Bambu', 'service': 'mjpegstreamer', 'stream_url': '/webcam?action=stream', 'snapshot_url': '/webcam?action=snapshot'}
        with patch.object(api.camera_manager, 'get_builtin_webcam', return_value=builtin):
            response = await self.rpc('server.webcams.post_item', uid='builtin', name='P1S', rotation=180, flip_horizontal=True, stream_url='bad')
            self.assertEqual(response['result']['item']['stream_url'], builtin['stream_url'])
            saved = api._current_webcams()[0]
            self.assertEqual(saved['name'], 'P1S')
            self.assertEqual(saved['rotation'], 180)
            self.assertTrue(saved['flip_horizontal'])

    async def test_webcam_delete_by_name_removes_legacy_duplicates_and_notifies(self):
        cameras = [{'uid': 'keep', 'name': 'Webcam'}, {'uid': 'v4l2', 'name': 'V4L2'},
                   {'uid': 'one', 'name': 'New Webcam'}, {'uid': 'two', 'name': 'New Webcam'}]
        self.db.post_item('moonraker', 'webcams', cameras)
        with patch.object(api.manager, 'broadcast', new_callable=unittest.mock.AsyncMock) as notify:
            result = await self.rpc('server.webcams.delete_item', name='New Webcam')
            self.assertEqual(result['result']['item']['name'], 'New Webcam')
            self.assertEqual(self.db.get_item('moonraker', 'webcams'), cameras[:2])
            event = notify.await_args.args[0]
            self.assertEqual(event['method'], 'notify_webcams_changed')
            self.assertFalse(any(cam['name'] == 'New Webcam' for cam in event['params'][0]['webcams']))
        # Repeating deletion is harmless, but an empty selector must fail.
        self.assertIn('result', await self.rpc('server.webcams.delete_item', name='New Webcam'))
        self.assertEqual((await self.rpc('server.webcams.delete_item'))['error']['code'], 400)

    async def test_webcam_uid_delete_only_removes_selected_duplicate(self):
        cameras = [{'uid': 'one', 'name': 'Same'}, {'uid': 'two', 'name': 'Same'}]
        self.db.post_item('moonraker', 'webcams', cameras)
        await self.rpc('server.webcams.delete_item', uid='one', name='Same')
        self.assertEqual(self.db.get_item('moonraker', 'webcams'), cameras[1:])

    async def test_webcam_post_preserves_client_uid_and_updates_by_name(self):
        result = await self.rpc('server.webcams.post_item', uid='helix-camera', name='Camera', stream_url='/first')
        self.assertEqual(result['result']['item']['uid'], 'helix-camera')
        await self.rpc('server.webcams.post_item', uid='helix-camera', name='Camera', stream_url='/second')
        await self.rpc('server.webcams.post_item', name='Camera', stream_url='/third')
        cameras = self.db.get_item('moonraker', 'webcams')
        self.assertEqual(len(cameras), 1)
        self.assertEqual(cameras[0]['uid'], 'helix-camera')
        self.assertEqual(cameras[0]['stream_url'], '/third')

    async def test_unnamed_new_webcams_and_invalid_selectors_are_rejected(self):
        for params in ({'stream_url': '/stream'}, {'uid': 'new', 'stream_url': '/stream'},
                       {'name': ''}, {'name': None}, {'name': 'Camera', 'uid': []}):
            result = await self.rpc('server.webcams.post_item', **params)
            self.assertEqual(result['error']['code'], 400, result)
        self.assertIsNone(self.db.get_item('moonraker', 'webcams'))

    async def test_builtin_webcam_name_edit_does_not_create_duplicate_and_delete_is_explicit(self):
        builtin = {'uid': 'builtin', 'name': 'Bambu Camera', 'stream_url': '/webcam?action=stream',
                   'snapshot_url': '/webcam?action=snapshot', 'service': 'mjpegstreamer'}
        with patch.object(api.camera_manager, 'get_builtin_webcam', return_value=builtin):
            await self.rpc('server.webcams.post_item', name='Bambu Camera', rotation=180, stream_url='bad')
            self.assertEqual(len(api._current_webcams()), 1)
            self.assertEqual(api._current_webcams()[0]['stream_url'], builtin['stream_url'])
            for selector in ({'name': 'Bambu Camera'}, {'uid': 'builtin'}):
                self.assertEqual((await self.rpc('server.webcams.delete_item', **selector))['error']['code'], 403)

    async def test_standard_http_object_queries_filter_fields(self):
        request = Request({'type': 'http', 'method': 'GET', 'path': '/printer/objects/query', 'query_string': b'extruder=temperature,target&print_stats', 'headers': []})
        result = await api.objects_query(request)
        self.assertEqual(set(result['result']['status']['extruder']), {'temperature', 'target'})
        self.assertIn('state', result['result']['status']['print_stats'])
        rpc = await self.rpc('printer.objects.query', objects={'extruder': ['temperature']})
        self.assertEqual(set(rpc['result']['status']['extruder']), {'temperature'})

    async def test_advertised_webcam_component_and_gcode_help(self):
        result = await self.rpc('server.info')
        self.assertIn('webcam', result['result']['components'])
        self.assertEqual(result['result']['api_version_string'], '1.0.0')
        result = await self.rpc('printer.gcode.help')
        self.assertIn('PAUSE', result['result'])
        self.assertIn('SET_HEATER_TEMPERATURE', result['result'])
