#!/usr/bin/env python3
"""
Quick test to verify SQLite manager and file caching works.
"""

import time
from sqlite_manager import get_sqlite_manager

def test_file_cache():
    print("Testing SQLite file cache...")
    
    sm = get_sqlite_manager()
    
    # Test data
    test_files = [
        {"name": "test1.gcode.3mf", "size": 1234, "modified": time.time(), "is_dir": False, "path": "test1.gcode.3mf"},
        {"name": "test2.gcode.3mf", "size": 5678, "modified": time.time(), "is_dir": False, "path": "test2.gcode.3mf"},
        {"name": "subfolder", "size": 0, "modified": time.time(), "is_dir": True, "path": "subfolder"},
    ]
    
    # Cache files
    print(f"  Caching {len(test_files)} files...")
    sm.cache_files(test_files)
    
    # Retrieve cache
    print("  Retrieving from cache...")
    cached = sm.get_cached_files(max_age=300)
    
    if cached:
        print(f"  ✓ Retrieved {len(cached)} cached files")
        for f in cached:
            print(f"    - {f['name']} ({'dir' if f['is_dir'] else 'file'}, {f['size']} bytes)")
    else:
        print("  ✗ Cache retrieval failed")
        return False
    
    # Test cache expiration
    print("  Testing cache with zero max_age (should return None)...")
    expired = sm.get_cached_files(max_age=0)
    if expired is None:
        print("  ✓ Cache expiration works")
    else:
        print("  ✗ Cache should have expired")
        return False
    
    # Clear cache
    print("  Clearing cache...")
    sm.clear_file_cache()
    cleared = sm.get_cached_files(max_age=300)
    if cleared is None:
        print("  ✓ Cache cleared successfully")
    else:
        print("  ✗ Cache should be empty")
        return False
    
    return True

def test_job_history():
    print("\nTesting job history...")
    
    sm = get_sqlite_manager()
    
    # Add test job
    job_data = {
        "job_id": "test123",
        "filename": "test_print.gcode.3mf",
        "start_time": time.time() - 3600,
        "end_time": time.time(),
        "total_duration": 3600,
        "status": "completed",
        "filament_used": 12.5,
        "metadata": {"test": "data"}
    }
    
    print(f"  Adding job: {job_data['job_id']}")
    sm.add_job(job_data)
    
    # Retrieve history
    print("  Retrieving job history...")
    history = sm.get_job_history(limit=10)
    
    if history and history['count'] > 0:
        print(f"  ✓ Found {history['count']} jobs in history")
        for job in history['jobs']:
            print(f"    - {job['job_id']}: {job['filename']} ({job['status']}, {job['total_duration']:.0f}s)")
    else:
        print("  ✗ No jobs found in history")
        return False
    
    return True

def test_metadata_cache():
    print("\nTesting file metadata cache...")
    
    sm = get_sqlite_manager()
    
    # Cache metadata
    metadata = {
        "slicer": "BambuStudio",
        "layer_height": 0.2,
        "first_layer_height": 0.25,
        "estimated_time": 3600,
        "filament_total": 15.5,
        "thumbnails": [{"size": "32x32", "data": "base64..."}]
    }
    
    print("  Caching metadata for 'test.gcode.3mf'...")
    sm.cache_file_metadata("test.gcode.3mf", metadata)
    
    # Retrieve metadata
    print("  Retrieving metadata...")
    retrieved = sm.get_file_metadata("test.gcode.3mf", max_age=3600)
    
    if retrieved:
        print(f"  ✓ Retrieved metadata: slicer={retrieved['slicer']}, layer_height={retrieved['layer_height']}")
    else:
        print("  ✗ Failed to retrieve metadata")
        return False
    
    return True

if __name__ == "__main__":
    print("=" * 60)
    print("SQLite Manager Test Suite")
    print("=" * 60)
    
    results = []
    
    results.append(("File Cache", test_file_cache()))
    results.append(("Job History", test_job_history()))
    results.append(("Metadata Cache", test_metadata_cache()))
    
    print("\n" + "=" * 60)
    print("Test Results:")
    print("=" * 60)
    
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"  {status}: {name}")
    
    all_passed = all(r[1] for r in results)
    
    print("\n" + ("🎉 All tests passed!" if all_passed else "❌ Some tests failed"))
    
    exit(0 if all_passed else 1)
