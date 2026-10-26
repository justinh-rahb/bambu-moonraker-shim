"""Check transactional settings persistence without touching runtime data."""
import os
import tempfile
from bambu_moonraker_shim.database_manager import DatabaseManager


def test_persistence():
    with tempfile.TemporaryDirectory() as directory:
        legacy = os.path.join(directory, 'moonraker.json')
        sqlite = os.path.join(directory, 'settings.db')
        db = DatabaseManager(legacy, sqlite)
        db.post_item('test_ns', 'settings.theme', 'dark')
        db2 = DatabaseManager(legacy, sqlite)
        assert db2.get_item('test_ns', 'settings.theme') == 'dark'
        assert not os.path.exists(legacy)
    print('Persistence check passed')


if __name__ == '__main__':
    test_persistence()
