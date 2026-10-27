import json
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from bambu_moonraker_shim import moonraker_api as api
from bambu_moonraker_shim.state_manager import StateManager


class FileDeleteTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = directory.name
        self.state = StateManager()
        self.cache = Mock()
        self.broadcast = AsyncMock()
        for target, value in (
            ('Config.BAMBU_SERIAL', ''), ('Config.GCODES_DIR', self.root),
            ('state_manager', self.state), ('get_sqlite_manager', Mock(return_value=self.cache)),
            ('manager.broadcast', self.broadcast),
        ):
            patcher = patch.object(api, target, value) if '.' not in target else patch('bambu_moonraker_shim.moonraker_api.'+target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def rpc(self, path):
        return await api.handle_jsonrpc({'jsonrpc': '2.0', 'id': 1, 'method': 'server.files.delete_file', 'params': {'path': path}}, 1)

    def create(self, filename):
        path = os.path.join(self.root, filename)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, 'w') as f:
            f.write('; disposable test file')
        return path

    async def test_rpc_deletes_file_and_sends_moonraker_result_and_notification(self):
        path = self.create('folder/test file.gcode')
        result = await self.rpc('gcodes/folder/test file.gcode')
        self.assertFalse(os.path.exists(path))
        expected = {'item': {'path': 'folder/test file.gcode', 'root': 'gcodes', 'size': 0, 'modified': 0, 'permissions': ''}, 'action': 'delete_file'}
        self.assertEqual(result['result'], expected)
        self.cache.clear_file_cache.assert_called_once()
        self.broadcast.assert_awaited_once_with({'jsonrpc': '2.0', 'method': 'notify_filelist_changed', 'params': [expected]})

    async def test_http_uses_same_result(self):
        path = self.create('test.gcode.3mf')
        result = await api.file_delete('test.gcode.3mf')
        self.assertEqual(result['result']['action'], 'delete_file')
        self.assertFalse(os.path.exists(path))
        self.assertEqual(result['result']['item']['path'], 'test.gcode.3mf')

    async def test_missing_file_is_not_success(self):
        self.assertEqual((await self.rpc('gcodes/missing.gcode'))['error']['code'], 404)
        result = await api.file_delete('missing.gcode')
        self.assertEqual(result.status_code, 404)
        self.broadcast.assert_not_awaited()
        self.cache.clear_file_cache.assert_not_called()

    async def test_invalid_paths_and_read_only_roots(self):
        for path in (None, '', 'gcodes', 'gcodes/', '/gcodes/foo.gcode', 'gcodes/../foo', 'gcodes/a/../../foo', 'gcodes/./foo', 'gcodes/foo\nDELE /other', 'gcodes/a\\b', 'config/printer.cfg'):
            with self.subTest(path=path):
                self.assertIn('error', await self.rpc(path))
        result = await api.file_delete('printer.cfg', root='config')
        self.assertEqual(result.status_code, 403)
        self.broadcast.assert_not_awaited()

    async def test_directory_is_not_deleted(self):
        os.mkdir(os.path.join(self.root, 'folder'))
        self.assertEqual((await self.rpc('gcodes/folder'))['error']['code'], 400)
        self.assertTrue(os.path.isdir(os.path.join(self.root, 'folder')))

    async def test_symlink_cannot_escape_mock_root(self):
        with tempfile.TemporaryDirectory() as outside:
            target = os.path.join(outside, 'keep.gcode')
            with open(target, 'w') as f:
                f.write('keep')
            os.symlink(target, os.path.join(self.root, 'link.gcode'))
            self.assertEqual((await self.rpc('gcodes/link.gcode'))['error']['code'], 400)
            self.assertTrue(os.path.exists(target))

    async def test_active_or_paused_print_blocks_delete(self):
        path = self.create('keep.gcode')
        for state in ('printing', 'paused'):
            self.state.get_state()['print_stats']['state'] = state
            self.assertEqual((await self.rpc('gcodes/keep.gcode'))['error']['code'], 409)
        self.assertTrue(os.path.exists(path))
        self.broadcast.assert_not_awaited()

    async def test_real_backend_resolves_alias_with_fresh_listing(self):
        ftp = Mock()
        ftp.list_files.return_value = [{'name': 'cube3.gcode.3mf', 'is_dir': False}]
        with patch.object(api.Config, 'BAMBU_SERIAL', 'TEST'), patch.object(api.Config, 'BAMBU_FTPS_UPLOADS_DIR', '/'), patch.object(api, 'ftps_client', ftp):
            result = await self.rpc('gcodes/nested/cube3.gcode')
        self.assertIn('result', result)
        ftp.list_files.assert_called_once_with('/nested')
        ftp.delete_file.assert_called_once_with('nested/cube3.gcode.3mf')
        self.assertEqual(result['result']['item']['path'], 'nested/cube3.gcode')

    async def test_real_backend_prefers_exact_path_when_both_exist(self):
        ftp = Mock()
        ftp.list_files.return_value = [{'name': name, 'is_dir': False} for name in ('cube3.gcode', 'cube3.gcode.3mf')]
        with patch.object(api.Config, 'BAMBU_SERIAL', 'TEST'), patch.object(api, 'ftps_client', ftp):
            self.assertIn('result', await self.rpc('gcodes/cube3.gcode'))
        ftp.delete_file.assert_called_once_with('cube3.gcode')

    async def test_ftps_failure_does_not_broadcast_false_success(self):
        ftp = Mock()
        ftp.list_files.return_value = [{'name': 'cube3.gcode', 'is_dir': False}]
        ftp.delete_file.side_effect = OSError('550 deletion denied')
        with patch.object(api.Config, 'BAMBU_SERIAL', 'TEST'), patch.object(api, 'ftps_client', ftp):
            result = await self.rpc('gcodes/cube3.gcode')
        self.assertEqual(result['error']['code'], 502)
        self.assertIn('550 deletion denied', result['error']['message'])
        self.broadcast.assert_not_awaited()
        self.cache.clear_file_cache.assert_not_called()

    async def test_alias_preserves_original_extension_case(self):
        ftp = Mock()
        ftp.list_files.return_value = [{'name': 'cube3.gcode.3MF', 'is_dir': False}]
        with patch.object(api.Config, 'BAMBU_SERIAL', 'TEST'), patch.object(api, 'ftps_client', ftp):
            self.assertIn('result', await self.rpc('gcodes/cube3.gcode'))
        ftp.delete_file.assert_called_once_with('cube3.gcode.3MF')
