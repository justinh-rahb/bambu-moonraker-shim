import os
import tempfile
import unittest
from unittest.mock import patch
from bambu_moonraker_shim.state_manager import StateManager
from bambu_moonraker_shim.sqlite_manager import SQLiteManager
from bambu_moonraker_shim.bambu_client import BambuClient


class HistoryTests(unittest.IsolatedAsyncioTestCase):
    async def test_resume_preserves_job_start_and_id(self):
        with tempfile.TemporaryDirectory() as directory:
            db = SQLiteManager(os.path.join(directory, 'test.db'))
            state = StateManager()
            with patch('bambu_moonraker_shim.sqlite_manager.get_sqlite_manager', return_value=db):
                await state.update_state({'print_stats': {'state': 'printing', 'filename': 'part.3mf'}})
                job_id = state._current_job_id
                started = state._current_job_start
                await state.update_state({'print_stats': {'state': 'paused'}})
                await state.update_state({'print_stats': {'state': 'printing'}})
                self.assertEqual(state._current_job_id, job_id)
                self.assertEqual(state._current_job_start, started)
                await state.update_state({'print_stats': {'state': 'complete'}})
            jobs = db.get_job_history()['jobs']
            self.assertEqual(len(jobs), 1)
            self.assertEqual(jobs[0]['job_id'], job_id)
            self.assertEqual(jobs[0]['status'], 'completed')

    def test_history_pagination(self):
        with tempfile.TemporaryDirectory() as directory:
            db = SQLiteManager(os.path.join(directory, 'test.db'))
            for i in range(5):
                db.add_job({'job_id': str(i), 'filename': 'part.3mf', 'start_time': i})
            first = db.get_job_history(limit=2)
            second = db.get_job_history(limit=2, start=2)
            self.assertEqual(first['count'], 5)
            self.assertEqual([j['job_id'] for j in first['jobs']], ['4', '3'])
            self.assertEqual([j['job_id'] for j in second['jobs']], ['2', '1'])
            self.assertEqual(db.get_job_history(since=0)['count'], 4)

    def test_completed_duration_is_frozen_and_same_filename_can_restart(self):
        client = BambuClient()
        client._update_job_timing('printing', 'part.3mf', 100)
        client._update_job_timing('paused', 'part.3mf', 120)
        client._update_job_timing('printing', 'part.3mf', 130)
        client._update_job_timing('complete', 'part.3mf', 160)
        self.assertEqual(client._current_total_duration(200), 60)
        self.assertEqual(client._current_print_duration(200), 50)
        client._update_job_timing('printing', 'part.3mf', 300)
        self.assertEqual(client._current_total_duration(310), 10)
