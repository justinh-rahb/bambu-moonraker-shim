import unittest
import threading
from unittest.mock import Mock, patch

from bambu_moonraker_shim import moonraker_api as api


class CanonicalFileNameTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.names = ['cube.gcode', 'cube.gcode.3mf', 'Project.3MF', 'UPPER.GCODE.3MF']
        self.entries = [dict(name=name, is_dir=False, size=123, modified=456) for name in self.names]
        self.cache = Mock()
        self.cache.get_cached_files.return_value = self.entries
        self.ftp = Mock()
        self.ftp.list_files.return_value = self.entries
        self.ftp.get_storage_info.return_value = dict(total=1000, used=100, free=900)
        for target, value in [('Config.BAMBU_SERIAL', 'TEST'), ('ftps_client', self.ftp), ('get_sqlite_manager', Mock(return_value=self.cache))]:
            patcher = patch('bambu_moonraker_shim.moonraker_api.' + target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_http_and_rpc_lists_keep_real_names_without_collisions(self):
        self.assertEqual([f['path'] for f in api._build_file_list('gcodes')], self.names)
        http = (await api.get_directory('gcodes'))['result']['files']
        rpc = (await api.handle_jsonrpc({'jsonrpc': '2.0', 'id': 1, 'method': 'server.files.get_directory', 'params': {'path': 'gcodes'}}, 1))['result']['files']
        for files in (http, rpc):
            self.assertEqual([f['filename'] for f in files], self.names)
            self.assertEqual([f['path'] for f in files], self.names)

    def test_print_paths_prefer_exact_file_and_preserve_archive_extension(self):
        self.assertEqual(api._normalize_filename('gcodes/cube.gcode'), 'cube.gcode')
        self.assertEqual(api._normalize_filename('gcodes/cube.gcode.3mf'), 'cube.gcode.3mf')
        self.assertEqual(api._normalize_filename('gcodes/UPPER.GCODE.3MF'), 'UPPER.GCODE.3MF')

    def test_old_alias_still_works_when_no_exact_file_exists(self):
        self.cache.get_cached_files.return_value = [self.entries[1]]
        self.assertEqual(api._normalize_filename('cube.gcode'), 'cube.gcode.3mf')

    async def test_disk_usage_does_not_block_the_event_loop(self):
        loop_thread = threading.get_ident()
        threads = []
        def storage(root):
            threads.append(threading.get_ident())
            return dict(total=1000, used=100, free=900)
        with patch.object(api, '_get_disk_usage', side_effect=storage):
            await api.get_directory('gcodes')
            await api.handle_jsonrpc({'jsonrpc': '2.0', 'id': 1, 'method': 'server.files.get_directory', 'params': {'path': 'gcodes'}}, 1)
        self.assertEqual(len(threads), 2)
        self.assertTrue(all(t != loop_thread for t in threads))
