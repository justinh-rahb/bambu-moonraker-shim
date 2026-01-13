#!/usr/bin/env python3
"""
FTPS Probe Script - Verifies connection and explores printer filesystem
This script performs the "Gap A/B/C" discovery outlined in the user's research.
"""

import os
from ftps_client import ftps_client
from config import Config

def probe_ftps():
    """Run discovery probes on the Bambu printer FTPS server."""
    
    print("=" * 60)
    print("FTPS PROBE - Bambu Printer Filesystem Discovery")
    print("=" * 60)
    print()
    
    # 1. Test connection
    print("1. Testing FTPS Connection...")
    print(f"   Host: {Config.BAMBU_HOST}")
    print(f"   Port: {Config.BAMBU_FTPS_PORT}")
    print(f"   User: {Config.BAMBU_FTPS_USER}")
    print()
    
    try:
        ftps_client.connect()
        print("✓ Connection successful!")
    except Exception as e:
        print(f"✗ Connection failed: {e}")
        return
    
    print()
    
    # 2. Check current directory
    print("2. Checking Current Directory (PWD)...")
    try:
        pwd = ftps_client.ftp.pwd()
        print(f"   Current directory: {pwd}")
    except Exception as e:
        print(f"   Error: {e}")
    
    print()
    
    # 3. List root directory
    print("3. Listing Root Directory (/)...")
    try:
        files = ftps_client.list_files("/")
        print(f"   Found {len(files)} items:")
        for f in files:
            file_type = "DIR " if f["is_dir"] else "FILE"
            size = f"({f['size']:,} bytes)" if not f["is_dir"] else ""
            print(f"     [{file_type}] {f['name']} {size}")
    except Exception as e:
        print(f"   Error: {e}")
    
    print()
    
    # 4. Check for common directories
    print("4. Checking for Known Directories...")
    known_dirs = ["/cache", "/timelapse", "/ipcam", "/gcodes"]
    for dir_path in known_dirs:
        try:
            files = ftps_client.list_files(dir_path)
            print(f"   ✓ {dir_path} exists ({len(files)} items)")
        except:
            print(f"   ✗ {dir_path} not found or not accessible")
    
    print()
    
    # 5. Test upload to root (with cleanup)
    print("5. Testing Upload to Root (/)...")
    test_filename = "test_moonraker_probe.gcode"
    test_content = "; Bambu Moonraker Shim Test File\n; This can be safely deleted\n"
    
    try:
        # Create temp test file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', delete=False, suffix='.gcode') as tmp:
            tmp.write(test_content)
            temp_path = tmp.name
        
        # Upload
        ftps_client.upload_file(temp_path, test_filename)
        print(f"   ✓ Upload successful: {test_filename}")
        
        # Verify it appears in listing
        files = ftps_client.list_files("/")
        if any(f["name"] == test_filename for f in files):
            print(f"   ✓ File appears in root directory listing")
        else:
            print(f"   ⚠ File uploaded but not visible in listing")
        
        # Clean up remote file
        ftps_client.delete_file(test_filename)
        print(f"   ✓ Test file deleted successfully")
        
        # Clean up local temp file
        os.unlink(temp_path)
        
    except Exception as e:
        print(f"   ✗ Upload test failed: {e}")
    
    print()
    
    # 6. Test MLSD support
    print("6. Testing MLSD Support...")
    try:
        # This is already tested in list_files, but let's be explicit
        data = list(ftps_client.ftp.mlsd("/"))
        print(f"   ✓ MLSD supported ({len(data)} items)")
    except:
        print(f"   ✗ MLSD not supported (will use NLST fallback)")
    
    print()
    print("=" * 60)
    print("Probe Complete!")
    print("=" * 60)
    print()
    print("RECOMMENDATIONS:")
    print(f"  • Upload directory: {Config.BAMBU_FTPS_UPLOADS_DIR}")
    print(f"  • Supported extensions: .gcode, .gcode.3mf, .3mf")
    print()

if __name__ == "__main__":
    probe_ftps()
