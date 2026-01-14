import os
import json
import time
from bambu_moonraker_shim.database_manager import DatabaseManager

DB_FILE = "test_persistence.json"

def test_persistence():
    print("Testing persistence...")
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)

    # 1. Write data
    db = DatabaseManager(DB_FILE)
    print("Writing item...")
    db.post_item("test_ns", "key", "persisted_value")
    
    # 2. Verify file content manually
    with open(DB_FILE, "r") as f:
        data = json.load(f)
        print(f"File content: {data}")
        assert data["test_ns"]["key"] == "persisted_value"

    # 3. Simulate Restart (New Instance)
    print("Simulating restart (new instance)...")
    db2 = DatabaseManager(DB_FILE)
    val = db2.get_item("test_ns", "key")
    print(f"Read value: {val}")
    assert val == "persisted_value"
    
    # Clean up
    if os.path.exists(DB_FILE):
        os.remove(DB_FILE)

    print("✅ Persistence Test Passed")

if __name__ == "__main__":
    test_persistence()
