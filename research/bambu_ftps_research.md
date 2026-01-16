# Bambu Lab FTPS Research - Complete Guide

## Executive Summary
Bambu Lab printers support FTPS (FTP over TLS) on port 990 with implicit encryption. This provides file upload/download capabilities for 3MF files, G-code, and accessing camera footage. Critical implementation details for Python-based integration.

---

## 1. FTPS Connection Details

### Connection Parameters
```python
Protocol: FTPS (NOT SFTP!)
Port: 990 (Implicit FTP over TLS)
Username: bblp
Password: <access_code>  # Found in printer LCD WiFi settings
Encryption: Implicit TLS (NOT explicit)
Mode: Passive (PASV)
```

### Key Differences
- **FTPS vs SFTP**: These are COMPLETELY different protocols
  - FTPS = FTP over TLS (what Bambu uses)
  - SFTP = SSH File Transfer Protocol (NOT supported)
- **Implicit vs Explicit TLS**:
  - Implicit: TLS from connection start (Port 990)
  - Explicit: Starts plain, upgrades to TLS (Port 21)
  - Bambu uses **Implicit TLS only**

### Printer Support

| Printer | FTPS Support | Notes |
|---------|-------------|--------|
| X1/X1C | ✅ Yes | Port 990, changed from plain FTP in firmware update |
| P1P/P1S | ✅ Yes | Port 990 |
| A1/A1 Mini | ✅ Yes | Port 990 |
| H2D | ✅ Yes (assumed) | Port 990 |

**Important**: FTPS works WITHOUT LAN-only mode! Works even when connected to Bambu Cloud.

---

## 2. Directory Structure

### Root Directory
```
/ (root)
├── cache/                  # Temporary print files
├── ipcam/                  # Camera footage/images  
├── timelapse/              # Timelapse videos
└── *.3mf                   # User files on SD card root
```

### Directory Details

#### `/cache/`
- **Purpose**: Temporary storage for prints sent via Bambu Studio
- **FIFO Behavior**: Auto-deletes old files (keeps last ~6 by default)
- **Bypass**: Direct FTP upload can bypass FIFO limit
- **Typical Contents**: Recent .3mf files sent from slicer

#### `/ipcam/`
- **Purpose**: Camera footage storage
- **File Types**: Images/video frames from camera
- **Access**: Read-only in most cases

#### `/timelapse/`
- **Purpose**: Generated timelapse videos
- **File Types**: Video files of completed prints
- **Access**: Read/download timelapses

#### Root `/`
- **Purpose**: SD card root - persistent storage
- **File Types**: .3mf files uploaded by user
- **Behavior**: Files stay until manually deleted
- **Note**: Can create subdirectories for organization

### File Path References in MQTT

When starting a print via MQTT `project_file` command:
```json
{
  "url": "file:///mnt/sdcard",           // SD card root
  "url": "ftp:///myfile.3mf",            // Root via FTP reference
  "url": "ftp:///cache/recentprint.3mf"  // Cache directory
}
```

**Important**: MQTT paths use `ftp://` or `file://` scheme, but actual FTP access is different!

---

## 3. Python Implementation

### Using Python's ftplib

#### Working Solution: ImplicitFTP_TLS Class

**This is the PROVEN solution from the Bambu community** - tested and working on real P1S printers:

```python
from ftplib import FTP_TLS
import ssl

class ImplicitFTP_TLS(FTP_TLS):
    """
    FTP_TLS subclass that automatically wraps sockets in SSL to support implicit FTPS.
    
    This is the key to making Python's ftplib work with Bambu Lab printers!
    Standard FTP_TLS doesn't support implicit TLS on port 990.
    
    Credit: SuiDog from Bambu Lab Community Forums
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        """Return the socket."""
        return self._sock

    @sock.setter
    def sock(self, value):
        """
        When modifying the socket, ensure that it is SSL wrapped.
        This is the magic that makes implicit TLS work!
        """
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value

# Usage example
def connect_to_bambu(host, access_code):
    """Connect to Bambu Lab printer via FTPS"""
    # Create SSL context
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE  # Bambu uses self-signed cert
    
    # Create FTP connection
    ftp = ImplicitFTP_TLS(context=context)
    ftp.set_pasv(True)  # MUST use passive mode
    
    # Connect and login
    ftp.connect(host=host, port=990, timeout=10)
    ftp.login('bblp', access_code)
    ftp.prot_p()  # Set up secure data connection
    
    return ftp

# Example usage
ftp = connect_to_bambu('192.168.1.100', 'your_access_code')

# List files
files = []
ftp.retrlines('LIST', files.append)
for line in files:
    print(line)

# Change directory
ftp.cwd('/timelapse')

# Download file
with open('local_file.mp4', 'wb') as f:
    ftp.retrbinary('RETR remote_file.mp4', f.write)

# Upload file
with open('local_model.3mf', 'rb') as f:
    ftp.storbinary('STOR remote_model.3mf', f)

# Cleanup
ftp.quit()
```

#### Better Approach: Use Dedicated Library

Python's standard ftplib doesn't easily support implicit TLS. Better options:

**Option A**: Use `ftputil` library
```python
import ftputil
import ftputil.session

# Create custom session factory for implicit TLS
def session_factory(host, port, user, password):
    from ftplib import FTP_TLS
    import ssl
    
    # Custom FTP_TLS that connects to port 990 with implicit TLS
    class ImplicitFTP_TLS(FTP_TLS):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.port = 990
            
        def connect(self, host='', port=0, timeout=-999):
            if host:
                self.host = host
            if port > 0:
                self.port = port
            else:
                self.port = 990
                
            # Create SSL socket directly
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            
            context = ssl.create_default_context()
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
            
            ssl_sock = context.wrap_socket(sock, server_hostname=self.host)
            ssl_sock.connect((self.host, self.port))
            
            self.sock = ssl_sock
            self.file = self.sock.makefile('r', encoding='utf-8')
            self.welcome = self.getresp()
            return self.welcome
    
    session = ImplicitFTP_TLS()
    session.connect(host, 990)
    session.login(user, password)
    session.set_pasv(True)
    return session

# Use it
with ftputil.FTPHost(
    printer_ip,
    'bblp',
    access_code,
    session_factory=lambda h, u, p: session_factory(h, 990, u, p)
) as ftp_host:
    # Now use like local filesystem
    ftp_host.upload('local_file.3mf', 'remote_file.3mf')
    files = ftp_host.listdir('/')
```

**Option B**: Simpler - Just use a mature library

Many people report success with:
- `bambu-lab-cloud-api` (PyPI package)
- `bambu-cli` (PyPI package)
- `bambu-connect` (PyPI package)

Example using `bambu-lab-cloud-api`:
```python
from bambulab import LocalFTPClient

client = LocalFTPClient("192.168.1.100", "access_code")
client.connect()

# Upload file
result = client.upload_file("model.3mf")
print(f"Uploaded to: {result['remote_path']}")

# List files
files = client.list_files()
for file in files:
    print(f"{file['name']} - {file['size']} bytes")

# Download file
client.download_file('remote.3mf', 'local_copy.3mf')

client.disconnect()
```

---

## 4. Common Issues & Solutions

### Issue: Connection Timeout

**Problem**: Python times out, but FileZilla works fine

**Cause**: Standard ftplib doesn't handle implicit TLS on port 990

**Solution**: 
1. Use dedicated library (recommended)
2. Manual socket wrapping (complex)
3. Use subprocess to call external FTP client

### Issue: "Access Denied" / FTP Code 9

**Problem**: `Failed to upload file to FTP` with code 9 "Server denied change to given directory"

**Causes**:
- SD card full
- SD card filesystem errors
- Trying to write to read-only directory
- Bad directory path

**Solutions**:
```python
# Check available space first
ftp.voidcmd('SIZE somefile.3mf')  # Get file size

# Format SD card in printer if needed (via printer menu)

# Stick to root or /cache directories for uploads
ftp.cwd('/')  # Root is safest
ftp.storbinary('STOR myfile.3mf', open('myfile.3mf', 'rb'))
```

### Issue: Certificate Verification Fails

**Problem**: SSL certificate error

**Cause**: Bambu uses self-signed certificates

**Solution**:
```python
context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE  # Disable cert verification
```

### Issue: Passive Mode Failures

**Problem**: Can't list files or transfer

**Cause**: Not using passive mode

**Solution**:
```python
ftp.set_pasv(True)  # MUST be set
```

---

## 5. File Operations

### Upload .3MF File
```python
def upload_3mf(ftp, local_path, remote_name=None):
    """Upload 3MF file to printer SD card"""
    if not remote_name:
        remote_name = os.path.basename(local_path)
    
    # Calculate MD5 for verification (optional but recommended)
    import hashlib
    with open(local_path, 'rb') as f:
        md5 = hashlib.md5(f.read()).hexdigest()
    
    # Upload
    with open(local_path, 'rb') as f:
        ftp.storbinary(f'STOR {remote_name}', f)
    
    return {
        'remote_path': remote_name,
        'md5': md5
    }
```

### List Files
```python
def list_files(ftp, directory='/'):
    """List files in directory"""
    ftp.cwd(directory)
    
    files = []
    ftp.retrlines('LIST', files.append)
    
    # Parse output (Unix ls format)
    parsed = []
    for line in files:
        parts = line.split()
        if len(parts) >= 9:
            parsed.append({
                'permissions': parts[0],
                'size': parts[4],
                'name': ' '.join(parts[8:]),
                'is_directory': parts[0].startswith('d')
            })
    
    return parsed
```

### Download File
```python
def download_file(ftp, remote_path, local_path):
    """Download file from printer"""
    with open(local_path, 'wb') as f:
        ftp.retrbinary(f'RETR {remote_path}', f.write)
```

### Real-World Example: Download Latest Timelapse

**From Bambu Community Forums** - This script downloads the newest completed timelapse:

```python
from datetime import datetime

def parse_ftp_listing(line):
    """Parse a line from an FTP LIST command"""
    parts = line.split(maxsplit=8)
    if len(parts) < 9:
        return None
    return {
        'permissions': parts[0],
        'links': int(parts[1]),
        'owner': parts[2],
        'group': parts[3],
        'size': int(parts[4]),
        'month': parts[5],
        'day': int(parts[6]),
        'time_or_year': parts[7],
        'name': parts[8]
    }

def parse_date(item):
    """Parse date from FTP listing"""
    try:
        date_str = f"{item['month']} {item['day']} {item['time_or_year']}"
        return datetime.strptime(date_str, "%b %d %H:%M")
    except ValueError:
        return None

def get_base_name(filename):
    """Get filename without extension"""
    return filename.rsplit('.', 1)[0]

def download_latest_timelapse(ftp, local_dir='.'):
    """
    Download the newest completed timelapse.
    Checks for matching thumbnail to ensure timelapse is finished.
    """
    tldirlist = []
    tltndirlist = []
    
    # Get timelapse video files
    ftp.cwd('/timelapse')
    ftp.retrlines('LIST', tldirlist.append)
    tldirlist = [parse_ftp_listing(line) for line in tldirlist 
                 if parse_ftp_listing(line)]
    
    # Get timelapse thumbnails
    ftp.cwd('/timelapse/thumbnail')
    ftp.retrlines('LIST', tltndirlist.append)
    tltndirlist = [parse_ftp_listing(line) for line in tltndirlist 
                   if parse_ftp_listing(line)]
    
    # Match videos with thumbnails (thumbnail = completed)
    tldirlist_dict = {get_base_name(item['name']): item 
                      for item in tldirlist}
    tltndirlist_set = {get_base_name(item['name']) 
                       for item in tltndirlist}
    
    matching_files = [tldirlist_dict[base_name] 
                      for base_name in tldirlist_dict 
                      if base_name in tltndirlist_set]
    
    # Find newest completed timelapse
    newest_file = None
    newest_time = None
    for item in matching_files:
        file_time = parse_date(item)
        if file_time and (newest_time is None or file_time > newest_time):
            newest_time = file_time
            newest_file = item
    
    if newest_file:
        print(f'Downloading: {newest_file["name"]}')
        local_path = os.path.join(local_dir, newest_file["name"])
        
        with open(local_path, 'wb') as f:
            ftp.retrbinary(f'RETR /timelapse/{newest_file["name"]}', f.write)
        
        print(f'Downloaded to: {local_path}')
        return local_path
    else:
        print('No completed timelapses found')
        return None

# Usage
ftp = connect_to_bambu('192.168.1.100', 'access_code')
download_latest_timelapse(ftp, '/path/to/downloads')
ftp.quit()
```

### Delete File
```python
def delete_file(ftp, remote_path):
    """Delete file from printer"""
    try:
        ftp.delete(remote_path)
        return True
    except Exception as e:
        print(f"Delete failed: {e}")
        return False
```

---

## 6. Integration with MQTT

### Workflow: Upload and Print

```python
import json
from ftplib import FTP_TLS
import paho.mqtt.client as mqtt

def upload_and_print(printer_ip, access_code, device_id, file_path):
    """Complete workflow: Upload via FTP, trigger print via MQTT"""
    
    # 1. Upload file via FTPS
    ftp = connect_ftps(printer_ip, access_code)
    result = upload_3mf(ftp, file_path)
    ftp.quit()
    
    # 2. Trigger print via MQTT
    mqtt_client = mqtt.Client()
    mqtt_client.tls_set(cert_reqs=ssl.CERT_NONE)
    mqtt_client.username_pw_set('bblp', access_code)
    mqtt_client.connect(printer_ip, 8883)
    
    # Send print command
    command = {
        "print": {
            "sequence_id": "0",
            "command": "project_file",
            "param": "Metadata/plate_1.gcode",  # Gcode file inside 3MF
            "project_id": "0",
            "profile_id": "0",
            "task_id": "0",
            "subtask_id": "0",
            "subtask_name": "",
            "file": result['remote_path'],
            "url": f"ftp:///{result['remote_path']}",
            "md5": result['md5'],
            "timelapse": False,
            "bed_type": "auto",
            "bed_levelling": True,
            "flow_cali": True,
            "vibration_cali": True,
            "layer_inspect": False,
            "use_ams": False
        }
    }
    
    mqtt_client.publish(
        f"device/{device_id}/request",
        json.dumps(command)
    )
    
    mqtt_client.disconnect()
```

---

## 7. File Management Best Practices

### SD Card Health
```python
def check_sd_health(ftp):
    """Check SD card status"""
    # List root directory
    files = list_files(ftp, '/')
    
    # Check for FSCK*.REC files (filesystem check results)
    fsck_files = [f for f in files if 'FSCK' in f['name']]
    if fsck_files:
        print("Warning: Filesystem errors detected!")
        print("Recommend formatting SD card via printer menu")
    
    return len(fsck_files) == 0
```

### File Organization
```python
def organize_files(ftp):
    """Create organized directory structure"""
    directories = ['models', 'calibration', 'tests']
    
    for dir_name in directories:
        try:
            ftp.mkd(dir_name)
        except:
            pass  # Directory might already exist
```

### Cache Management
```python
def clear_cache(ftp):
    """Clear cache directory (free space)"""
    try:
        ftp.cwd('/cache')
        files = []
        ftp.retrlines('LIST', files.append)
        
        for file_line in files:
            filename = file_line.split()[-1]
            if filename not in ['.', '..']:
                try:
                    ftp.delete(filename)
                except:
                    pass
    except Exception as e:
        print(f"Cache clear failed: {e}")
```

---

## 8. Python Libraries Comparison

### Standard Library (ftplib) with ImplicitFTP_TLS
**Pros:**
- Built-in ftplib base, minimal dependencies
- **Community-proven solution** (working code from forums)
- Lightweight custom class
- Full control over connection

**Cons:**
- Requires custom `ImplicitFTP_TLS` class (copy/paste)
- Slightly lower-level API

**Verdict**: **EXCELLENT choice** - Use the `ImplicitFTP_TLS` class from forum post! It's proven to work and doesn't require external dependencies.

### bambu-lab-cloud-api (Recommended)
**Install**: `pip install bambu-lab-cloud-api`

**Pros:**
- Purpose-built for Bambu Lab
- Handles implicit TLS correctly
- Clean API
- Actively maintained
- Includes MQTT support too

**Cons:**
- External dependency

**Verdict**: **Best choice for new projects**

### bambu-cli
**Install**: `pip install bambu-cli`

**Pros:**
- CLI tool included
- Python library also usable
- Good for quick scripts

**Cons:**
- Less documentation than bambu-lab-cloud-api
- Development state (per docs)

**Verdict**: Good for CLI workflows

### bambu-connect
**Install**: `pip install bambu-connect`

**Pros:**
- Simple API
- Well-documented

**Cons:**
- Less actively maintained (appears)
- Fewer features

**Verdict**: Solid alternative

---

## 9. Security Considerations

### Access Code Security
```python
# DON'T hardcode
access_code = "12345678"  # BAD

# DO use environment variables
import os
access_code = os.getenv('BAMBU_ACCESS_CODE')

# OR use config file with restricted permissions
import json
with open('config.json') as f:
    config = json.load(f)
    access_code = config['access_code']
```

### Certificate Validation
```python
# In production, consider pinning Bambu's cert
# For now, we disable verification (self-signed)

context = ssl.create_default_context()
context.check_hostname = False
context.verify_mode = ssl.CERT_NONE

# Future: If Bambu provides CA cert
# context.load_verify_locations('bambu_ca.crt')
# context.verify_mode = ssl.CERT_REQUIRED
```

### Network Security
- FTPS encrypts data in transit
- Still vulnerable to MITM if cert validation disabled
- Use on trusted networks only
- Consider VPN for remote access

---

## 10. Complete Working Example

### Full Implementation
```python
#!/usr/bin/env python3
"""
Bambu Lab FTPS Example
Uploads a 3MF file and starts printing
"""

import os
import sys
from bambulab import LocalFTPClient
import paho.mqtt.client as mqtt
import json
import ssl

class BambuPrinter:
    def __init__(self, ip, access_code, device_id):
        self.ip = ip
        self.access_code = access_code
        self.device_id = device_id
        
    def upload_file(self, local_path):
        """Upload file via FTPS"""
        print(f"Uploading {local_path}...")
        
        ftp = LocalFTPClient(self.ip, self.access_code)
        ftp.connect()
        
        result = ftp.upload_file(local_path)
        
        ftp.disconnect()
        
        print(f"Upload complete: {result['remote_path']}")
        return result
    
    def start_print(self, remote_path, md5=None):
        """Start print via MQTT"""
        print(f"Starting print: {remote_path}")
        
        # Connect MQTT
        client = mqtt.Client()
        client.tls_set(cert_reqs=ssl.CERT_NONE)
        client.username_pw_set('bblp', self.access_code)
        client.connect(self.ip, 8883, 60)
        client.loop_start()
        
        # Build command
        command = {
            "print": {
                "sequence_id": "0",
                "command": "project_file",
                "param": "Metadata/plate_1.gcode",
                "project_id": "0",
                "profile_id": "0", 
                "task_id": "0",
                "subtask_id": "0",
                "file": remote_path,
                "url": f"ftp:///{remote_path}",
                "md5": md5 or "",
                "timelapse": False,
                "bed_type": "auto",
                "bed_levelling": True,
                "flow_cali": True,
                "vibration_cali": True,
                "use_ams": False
            }
        }
        
        # Send command
        topic = f"device/{self.device_id}/request"
        client.publish(topic, json.dumps(command))
        
        # Wait a bit for message to send
        import time
        time.sleep(2)
        
        client.loop_stop()
        client.disconnect()
        
        print("Print command sent!")

def main():
    # Configuration
    PRINTER_IP = os.getenv('BAMBU_IP', '192.168.1.100')
    ACCESS_CODE = os.getenv('BAMBU_ACCESS_CODE')
    DEVICE_ID = os.getenv('BAMBU_DEVICE_ID')
    
    if not ACCESS_CODE or not DEVICE_ID:
        print("Error: Set BAMBU_ACCESS_CODE and BAMBU_DEVICE_ID environment variables")
        sys.exit(1)
    
    if len(sys.argv) < 2:
        print("Usage: python bambu_print.py <file.3mf>")
        sys.exit(1)
    
    file_path = sys.argv[1]
    
    # Upload and print
    printer = BambuPrinter(PRINTER_IP, ACCESS_CODE, DEVICE_ID)
    result = printer.upload_file(file_path)
    printer.start_print(result['remote_path'], result.get('md5'))

if __name__ == '__main__':
    main()
```

---

## 11. Testing & Debugging

### Test Connection
```python
def test_ftps_connection(ip, access_code):
    """Test if FTPS connection works"""
    try:
        from bambulab import LocalFTPClient
        
        print(f"Connecting to {ip}...")
        client = LocalFTPClient(ip, access_code)
        client.connect()
        
        print("Connected! Listing root directory...")
        files = client.list_files('/')
        
        print(f"Found {len(files)} items:")
        for f in files[:5]:  # Show first 5
            print(f"  {f['name']} ({f['size']} bytes)")
        
        client.disconnect()
        print("Test successful!")
        return True
        
    except Exception as e:
        print(f"Test failed: {e}")
        return False
```

### Debug Mode
```python
# Enable FTP debug output
import ftplib
ftplib.FTP.debugging = 2  # Max verbosity

# Or with FTP_TLS
ftp = FTP_TLS()
ftp.set_debuglevel(2)
```

---

## 12. Moonraker Bridge Integration

### File Upload Endpoint
```python
async def upload_file(request):
    """Handle file upload from Moonraker/Mainsail/Fluidd"""
    reader = await request.multipart()
    
    # Read uploaded file
    field = await reader.next()
    filename = field.filename
    
    # Save temporarily
    temp_path = f"/tmp/{filename}"
    with open(temp_path, 'wb') as f:
        while True:
            chunk = await field.read_chunk()
            if not chunk:
                break
            f.write(chunk)
    
    # Upload to printer
    printer_ip = config['printer_ip']
    access_code = config['access_code']
    
    client = LocalFTPClient(printer_ip, access_code)
    client.connect()
    result = client.upload_file(temp_path)
    client.disconnect()
    
    # Cleanup
    os.remove(temp_path)
    
    return web.json_response(result)
```

### File List Endpoint
```python
async def list_files(request):
    """List files on printer SD card"""
    printer_ip = config['printer_ip']
    access_code = config['access_code']
    
    client = LocalFTPClient(printer_ip, access_code)
    client.connect()
    
    files = client.list_files('/')
    
    client.disconnect()
    
    # Format for Moonraker
    formatted = []
    for f in files:
        formatted.append({
            'path': f['name'],
            'modified': 0,  # Would need to parse from FTP LIST
            'size': int(f['size']),
            'permissions': f['permissions']
        })
    
    return web.json_response({'result': formatted})
```

---

## Summary

### Key Takeaways

1. **Use Port 990 with Implicit TLS**
   - NOT port 21
   - NOT explicit TLS
   - **SOLVED**: Use the `ImplicitFTP_TLS` class (proven community solution)

2. **Two Good Options for Python**
   - **Option A** (Recommended): Use `ImplicitFTP_TLS` class (no external deps, proven working)
   - **Option B**: Use `bambu-lab-cloud-api` library (feature-rich, actively maintained)
   - Both work great - choose based on your needs

3. **Directory Structure Matters**
   - Root `/` for persistent files
   - `/cache` for temporary (auto-deleted)
   - `/ipcam` and `/timelapse` for media

4. **Combine with MQTT**
   - FTPS for file transfer
   - MQTT for print control
   - Together they enable full workflow

5. **Handle Errors Gracefully**
   - Check SD card health
   - Verify uploads
   - Provide user feedback

### Recommended Implementation

**For Moonraker Bridge: Use `ImplicitFTP_TLS` class**
- No external dependencies
- Proven working code from community
- Easy to integrate
- Full control over connection
- Just copy the class and use it!

### Next Steps for Bridge

1. **Install Dependencies**
   ```bash
   pip install bambu-lab-cloud-api
   ```

2. **Implement Endpoints**
   - `/server/files/upload` (POST)
   - `/server/files/list` (GET)
   - `/server/files/metadata?filename=X` (GET)
   - `/server/files/delete?filename=X` (DELETE)

3. **Test Workflow**
   - Upload via API
   - List files
   - Start print via MQTT
   - Monitor via MQTT

4. **Error Handling**
   - Connection failures
   - SD card full
   - Invalid files

---

## APPENDIX: Ready-to-Use ImplicitFTP_TLS Class

**Copy this directly into your code** - proven working solution from Bambu community:

```python
#!/usr/bin/env python3
"""
Bambu Lab FTPS Connection
Implicit TLS support for Python's ftplib

Credit: SuiDog from Bambu Lab Community Forums
"""

from ftplib import FTP_TLS
import ssl

class ImplicitFTP_TLS(FTP_TLS):
    """
    FTP_TLS subclass that automatically wraps sockets in SSL 
    to support implicit FTPS (port 990).
    
    Standard FTP_TLS only supports explicit TLS (port 21).
    Bambu Lab printers use implicit TLS on port 990.
    """
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._sock = None

    @property
    def sock(self):
        """Return the socket."""
        return self._sock

    @sock.setter
    def sock(self, value):
        """
        When modifying the socket, ensure that it is SSL wrapped.
        This is what makes implicit TLS work!
        """
        if value is not None and not isinstance(value, ssl.SSLSocket):
            value = self.context.wrap_socket(value)
        self._sock = value


def connect_to_bambu(host, access_code, timeout=10):
    """
    Connect to Bambu Lab printer via FTPS
    
    Args:
        host: Printer IP address
        access_code: Access code from printer LCD
        timeout: Connection timeout in seconds
    
    Returns:
        Connected ImplicitFTP_TLS instance
    """
    # Create SSL context (accept self-signed cert)
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    
    # Create and configure connection
    ftp = ImplicitFTP_TLS(context=context)
    ftp.set_pasv(True)  # MUST use passive mode
    
    # Connect and login
    ftp.connect(host=host, port=990, timeout=timeout)
    ftp.login('bblp', access_code)
    ftp.prot_p()  # Secure data connection
    
    return ftp


# Usage Examples
if __name__ == '__main__':
    # Connect
    ftp = connect_to_bambu('192.168.1.100', 'your_access_code')
    
    # List root directory
    print("Files on printer:")
    ftp.retrlines('LIST')
    
    # List timelapse directory
    print("\nTimelapses:")
    ftp.cwd('/timelapse')
    ftp.retrlines('LIST')
    
    # Upload a file
    print("\nUploading file...")
    ftp.cwd('/')  # Back to root
    with open('model.3mf', 'rb') as f:
        ftp.storbinary('STOR model.3mf', f)
    print("Upload complete!")
    
    # Download a file
    print("\nDownloading file...")
    with open('downloaded.3mf', 'wb') as f:
        ftp.retrbinary('RETR model.3mf', f.write)
    print("Download complete!")
    
    # Cleanup
    ftp.quit()
    print("Connection closed")
```

---

**Document Version:** 1.1  
**Last Updated:** 2025-01-16  
**Sources:** Bambu Lab Community Forums, bambu-lab-cloud-api, community findings
