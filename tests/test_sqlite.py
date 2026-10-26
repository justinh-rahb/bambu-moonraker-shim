import os
import tempfile
import time
import unittest
from bambu_moonraker_shim.sqlite_manager import SQLiteManager


class SQLiteTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.db = SQLiteManager(os.path.join(directory.name, 'test.db'))

    def test_file_cache(self):
        files = [{'name': 'test.3mf', 'size': 1234, 'modified': time.time(), 'is_dir': False}]
        self.db.cache_files(files)
        self.assertEqual(self.db.get_cached_files()[0]['name'], 'test.3mf')
        self.assertIsNone(self.db.get_cached_files(max_age=0))
        self.db.clear_file_cache()
        self.assertIsNone(self.db.get_cached_files())

    def test_job_history(self):
        self.db.add_job({'job_id': 'test', 'filename': 'test.3mf', 'start_time': time.time()-3600,
                         'end_time': time.time(), 'total_duration': 3600, 'status': 'completed', 'filament_used': 12.5})
        history = self.db.get_job_history()
        self.assertEqual(history['count'], 1)
        self.assertEqual(history['jobs'][0]['status'], 'completed')
        self.assertEqual(self.db.get_job_totals()['total_time'], 3600)

    def test_metadata_cache(self):
        metadata = {'slicer': 'BambuStudio', 'layer_height': 0.2, 'estimated_time': 3600, 'filament_total': 15.5,
                    'thumbnails': [{'size': 32, 'data': 'base64'}]}
        self.db.cache_file_metadata('test.3mf', metadata)
        result = self.db.get_file_metadata('test.3mf')
        for key, value in metadata.items():
            self.assertEqual(result[key], value)
