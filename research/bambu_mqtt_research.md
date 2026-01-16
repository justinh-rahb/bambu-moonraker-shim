# Bambu MQTT Bridge Research - HomeAssistant & OpenBambuAPI Findings

## Executive Summary
Comprehensive analysis of the HomeAssistant Bambu Lab integration and OpenBambuAPI documentation reveals extensive command structures, connection patterns, and implementation strategies for our Moonraker bridge.

---

## 1. MQTT Command Discovery

### Control Commands (Already Known + New)

#### Print Control
```json
// Pause
{"print": {"sequence_id": "0", "command": "pause"}}

// Resume  
{"print": {"sequence_id": "0", "command": "resume"}}

// Stop
{"print": {"sequence_id": "0", "command": "stop"}}

// Print Speed (NEW)
{"print": {"sequence_id": "2004", "command": "print_speed", "param": "1"}, "user_id": "1234567890"}
// Param: 1=Silent, 2=Standard, 3=Sport, 4=Ludicrous

// GCode Line (NEW - POWERFUL)
{"print": {"sequence_id": "2006", "command": "gcode_line", "param": "M140 S90 \n"}, "user_id": "1234567890"}
// CRITICAL: Requires trailing \n
// Can send ANY GCode command
```

#### Fan Control (via GCode)
```json
// Part Cooling 100%
{"print": {"sequence_id": "0", "command": "gcode_line", "param": "M106 P1 S255 \n"}, "user_id": "1234567890"}

// Aux Cooling 100%
{"print": {"sequence_id": "0", "command": "gcode_line", "param": "M106 P2 S255 \n"}, "user_id": "1234567890"}
```

#### Temperature Control (via GCode)
```json
// Set Bed Temp (preheat)
{"print": {"sequence_id": "0", "command": "gcode_line", "param": "M140 S90 \n"}, "user_id": "1234567890"}
```

### Information Requests

#### Get Version
```json
{"info": {"sequence_id": "0", "command": "get_version"}}
```

#### Push All (Full Status)
```json
{"pushing": {"sequence_id": "0", "command": "pushall"}}
```

### Calibration Commands

#### Start Calibration
```json
{
  "print": {
    "sequence_id": "0",
    "command": "calibration",
    "option": 0, // Bitmask
    "user_id": "1234"
  }
}
```
**Options Bitmask:**
- `if (lidarCalibration) bitmask |= 1;`
- `if (bedLevelling) bitmask |= 1 << 1;`
- `if (vibrationCompensation) bitmask |= 1 << 2;`
- `if (motorCancellation) bitmask |= 1 << 3;`

**Note:** Some printers need:
```json
{"print": {"command": "gcode_file", "param": "/usr/etc/print/auto_cali_for_user.gcode"}}
```

#### Filament Unload
```json
{
  "print": {
    "sequence_id": "0",
    "command": "unload_filament",
    "user_id": "1234"
  }
}
```
**Note:** Some printers need:
```json
{"print": {"command": "gcode_file", "param": "/usr/etc/print/filament_unload.gcode"}}
```

### Print Job Commands

#### Start Print from SD/File
```json
{
  "print": {
    "sequence_id": "0",
    "command": "project_file",
    "param": "Metadata/plate_X.gcode",
    "project_id": "0",        // Always 0 for local prints
    "profile_id": "0",        // Always 0 for local prints
    "task_id": "0",           // Always 0 for local prints
    "subtask_id": "0",        // Always 0 for local prints
    "subtask_name": "",
    "file": "",               // Filename (not needed if url specified)
    "url": "file:///mnt/sdcard", // Root path, varies by location
    // Can be: "ftp:///myfile.3mf", "ftp:///cache/myotherfile.3mf"
    "md5": "",                // Optional but recommended
    "timelapse": true,
    "bed_type": "auto",       // Always "auto" for local
    "bed_levelling": true,
    "flow_cali": true,
    "vibration_cali": true,
    "layer_inspect": true,
    "ams_mapping": "",        // Crucial for multi-color
    "use_ams": false
  }
}
```

**AMS Mapping Format:**
The `ams_mapping` parameter maps filament colors in the G-code to AMS slots. Critical for multi-color prints.

### Light Control

#### LED Control (Bambu-specific)
```json
{
  "system": {
    "sequence_id": "0", 
    "command": "ledctrl",
    "led_node": "chamber_light", // or "heatbed_light"
    "led_mode": "on",           // "on", "off", "flashing"
    "led_on_time": 500,         // ms, for flashing
    "led_off_time": 500         // ms, for flashing
  }
}
```

### AMS Commands

#### AMS Filament Settings
```json
{
  "print": {
    "sequence_id": "0",
    "command": "ams_filament_setting",
    "ams_id": "0",
    "tray_id": "0",
    "tray_info_idx": "",
    "tray_id": "",
    "tray_type": "",
    "tray_sub_brands": "",
    "tray_color": "",
    "tray_weight": "",
    "tray_diameter": "",
    "tray_temp": "",
    "tray_time": "",
    "bed_temp_type": "",
    "bed_temp": "",
    "nozzle_temp_max": "",
    "nozzle_temp_min": "",
    "xcam_info": "",
    "tray_uuid": ""
  }
}
```

---

## 2. Connection Architecture

### MQTT Topics
- **Subscribe:** `device/{DEVICE_ID}/report`
- **Publish:** `device/{DEVICE_ID}/request`

### Connection Methods

#### Local Connection
```
Host: <printer_ip>
Port: 8883 (TLS)
Username: bblp
Password: <access_code>
TLS: Required, cert_reqs=CERT_NONE
Protocol: MQTT v3.1.1
```

#### Cloud Connection
```
Host: us.mqtt.bambulab.com (or regional)
Port: 8883 (TLS)
Username: u_{USER_ID}
Password: {ACCESS_TOKEN}
Topics: Must be fully qualified
```

### Important Connection Notes

1. **Single Client Limitation (P1/A1/A1 Mini)**
   - These printers only support ONE local MQTT client at firmware 1.0.4.0+
   - X1 series can handle multiple connections
   - This is a CRITICAL constraint for bridge design
   
2. **Watchdog Thread**
   - HA integration uses a watchdog thread to detect connection issues
   - Monitors for disconnect events (codes 0, 7, 16)
   - Auto-reconnect logic required

3. **Message Parsing**
   - Messages come as JSON in `b'...'` byte strings
   - Must handle Unicode encoding errors (see UTF-8 decode issues in HA logs)
   - Empty payloads `{}` should be handled gracefully

---

## 3. Status/Report Message Structure

### Print Status Messages
```json
{
  "print": {
    "bed_temper": 25.625,
    "nozzle_temper": 28,
    "wifi_signal": "-56dBm",
    "command": "push_status",
    "msg": 1,
    "sequence_id": "42065",
    "gcode_state": "RUNNING", // or "PREPARE", "PAUSE", "IDLE"
    "gcode_file_prepare_percent": 100,
    "mc_percent": 45, // Print progress
    "layer_num": 10,
    "total_layer_num": 200,
    "mc_remaining_time": 3600, // seconds
    // ... many more fields
  }
}
```

### Info Response Messages
```json
{
  "info": {
    "command": "get_version",
    "module": [
      {
        "name": "ota",
        "sw_ver": "01.08.05.00",
        "hw_ver": "..."
      },
      // ... other modules
    ]
  }
}
```

### System Messages
```json
{
  "system": {
    "command": "ledctrl",
    "led_node": "heatbed_light",
    "led_mode": "on"
  }
}
```

### AMS Status
```json
{
  "print": {
    "ams": {
      "ams": [
        {
          "id": "0",
          "humidity": "3",
          "temp": "0.0",
          "tray": [
            {
              "id": "0",
              "remain": 100,
              "k": 0.02,
              "n": 1.37,
              "tag_uid": "...",
              "tray_id_name": "...",
              "tray_info_idx": "GFL99",
              "tray_type": "PLA",
              "tray_sub_brands": "",
              "tray_color": "00AE42FF",
              "tray_weight": "1000",
              "tray_diameter": "1.75",
              "tray_temp": "220",
              "tray_time": "0",
              "bed_temp_type": "0",
              "bed_temp": "55",
              "nozzle_temp_max": "240",
              "nozzle_temp_min": "190",
              "xcam_info": "...",
              "tray_uuid": "..."
            }
            // ... more trays
          ]
        }
      ]
    }
  }
}
```

---

## 4. HomeAssistant Implementation Patterns

### Key Architecture Components

1. **BambuClient Class** (`pybambu/bambu_client.py`)
   - Uses `paho-mqtt` library
   - Threaded MQTT listener
   - Callback system for updates
   - Connection state management

2. **Models Class** (`pybambu/models.py`)
   - Parses and stores printer state
   - Feature detection based on printer type
   - Version comparison for feature support
   - Supports X1, X1C, X1E, P1P, P1S, A1, A1 Mini, H2D

3. **Coordinator Pattern**
   - HA uses DataUpdateCoordinator
   - Manual updates triggered on MQTT messages
   - Prevents excessive state updates (1.3M/17hr issue!)

4. **Error Handling**
   ```python
   # From utils.py
   def safe_json_loads(raw_bytes):
       try:
           json_data = json.loads(raw_bytes)
       except UnicodeDecodeError:
           # Handle encoding issues
           # Saw this with Chinese characters in filenames
   ```

### Connection Flow

```python
# 1. Connect
client.connect(host, port, keepalive=60)
client.tls_set(tls_version=ssl.PROTOCOL_TLS, cert_reqs=ssl.CERT_NONE)

# 2. Subscribe on connect
def on_connect(client, userdata, flags, rc):
    client.subscribe(f"device/{device_id}/report")
    # Request version info
    publish({"info": {"sequence_id": "0", "command": "get_version"}})
    # Request full status
    publish({"pushing": {"sequence_id": "0", "command": "pushall"}})

# 3. Handle messages
def on_message(client, userdata, msg):
    data = json.loads(msg.payload)
    # Update models
    # Trigger callbacks
    
# 4. Handle disconnects
def on_disconnect(client, userdata, rc):
    # rc codes: 0=clean, 7=transport error, 16=unknown
    # Trigger reconnect logic
```

### Rate Limiting Considerations

**Issue:** HA integration saw 1.3M state changes in 17 hours from ONE printer!
- That's ~1,195 state changes per minute
- Caused event backlogs and delays

**Solution for Bridge:**
- Debounce rapid updates
- Only publish state changes, not heartbeats
- Aggregate multiple field updates
- Consider Moonraker's update rate expectations

---

## 5. Feature Detection & Printer Differences

### Printer Models
```python
class Printers:
    X1 = "BL-P001"
    X1C = "BL-P002" 
    P1P = "BL-P003"
    P1S = "BL-P004"
    X1E = "BL-P005"
    A1_MINI = "BL-P006"
    A1 = "BL-P007"
    H2D = "H2D"
```

### Feature Matrix

| Feature | X1/X1C/X1E | P1P/P1S | A1/A1 Mini | H2D |
|---------|------------|---------|------------|-----|
| Aux Fan | ✅ | ✅ | ❌ | ✅ |
| Chamber Fan | ✅ | ✅ | ❌ | ✅ |
| Chamber Temp | ✅ | ❌ | ❌ | ✅ |
| Lidar | ✅ | Varies | Varies | ? |
| Door Sensor | ✅ | ❌ | ❌ | ? |

### Firmware Version Checks

HA checks versions for feature availability:
```python
# Example from models.py
if printer_type == A1 and supports_sw_version("01.05.00.00"):
    # Feature X available
elif printer_type == P1S and supports_sw_version("01.08.02.00"):
    # Feature Y available
```

**Critical Firmware Notes:**
- 01.08.05.00+ introduced "Bambu Lab Authorization Control"
- Blocks write operations if printer is on Bambu Cloud
- LAN-only mode preserves all functionality
- Temperature setting blocked on some models/firmwares

---

## 6. Security & Authentication

### Bambu Lab Authorization Control (Firmware 01.08.05.00+)

**Impact:**
- If printer is on Bambu Cloud: **WRITE COMMANDS DISABLED**
- Read commands always work
- LAN-only mode: Full control retained

**Workaround Options:**
1. Use LAN-only mode (disconnect from cloud)
2. Some users stay on older firmware
3. Hybrid connection (cloud for monitoring, local for control)

### Access Code Management
- Local access requires printer access code
- Can be viewed in printer settings
- Changed when switching modes
- Must be stored securely

---

## 7. Chamber Camera / FTP Access

### P1/A1 Camera (Different from X1)
```python
# Connection requires:
- Printer IP
- Access code
- Different protocol than X1
# Connection often rejected with wrong credentials
```

### FTP Access
```python
# File operations via FTP
Host: <printer_ip>
Port: 990 (FTPS) 
User: bblp
Pass: <access_code>

# Can list/upload/download .3mf files
# Used for sending prints
# Often timeout issues reported
```

### File Locations
- `/mnt/sdcard/` - SD card root
- `/cache/` - Temp storage
- FTP paths in print commands must match actual file locations

---

## 8. Known Issues & Gotchas

### From HA Integration Experience

1. **Unicode in Filenames**
   ```
   UTF-8 decode errors with Chinese/special characters
   Saw: 大天使-�印帝帝labubu：上形�印可�印转�印�印。�印可活�印翔�印��印：超大�印�印系�印-�印�印�印�印3
   ```

2. **Connection Stability (P1/A1)**
   - Single client limitation causes frequent disconnects
   - Reconnection backoff required
   - Watchdog thread essential

3. **State Update Spam**
   - Printer sends updates CONSTANTLY
   - Even when idle
   - Need aggressive deduplication

4. **Empty Message Handling**
   ```json
   {"print": {}}  // Empty messages happen, don't crash
   ```

5. **GCode State Transitions**
   - `PREPARE` -> `RUNNING` -> `PAUSE` -> `RUNNING` -> `IDLE`
   - Sometimes skip intermediate states
   - Track print start/end carefully

6. **AMS Removal Mid-Print**
   - Can leave gaps in indices
   - Need robust index handling

7. **Chamber Image Connection**
   - Often rejected with generic errors
   - Requires exact IP from MQTT payload
   - Not from config

8. **Cloud API Throttling**
   - Cloudflare rate limits
   - Only fetch slicer settings on successful connection
   - Cache aggressively

---

## 9. Useful External Resources

### OpenBambuAPI
- Primary reference: https://github.com/Doridian/OpenBambuAPI/blob/main/mqtt.md
- Most comprehensive MQTT command documentation
- Community-maintained
- Includes cloud HTTP API docs too

### Related Projects
1. **OctoPrint-BambuPrinter**
   - https://github.com/jneilliii/OctoPrint-BambuPrinter
   - Print starting logic reference
   
2. **PulsePrint Desktop**
   - Cross-platform monitoring app
   - Uses OpenBambuAPI docs
   - Good UI/state management reference

3. **WolfWithSword Flows**
   - https://github.com/WolfwithSword/Bambu-HomeAssistant-Flows
   - Node-RED implementation
   - Dashboard examples

---

## 10. Recommendations for Moonraker Bridge

### Architecture Suggestions

1. **Connection Strategy**
   ```python
   # For P1/A1 users (single client limitation):
   - Option A: Be the ONLY client (disconnect apps)
   - Option B: Implement MQTT proxy/forwarder
   - Option C: Poll-based approach (less ideal)
   
   # For X1 users:
   - Direct MQTT connection works fine
   ```

2. **State Management**
   ```python
   # Don't blindly forward ALL updates to Moonraker
   - Debounce: 100-500ms window
   - Deduplicate: Only send changed values
   - Aggregate: Batch multiple fields
   - Rate limit: Max X updates/second
   ```

3. **Command Translation Layer**
   ```python
   # Moonraker -> Bambu
   moonraker_command = "PAUSE"
   bambu_command = {
       "print": {
           "sequence_id": generate_sequence(),
           "command": "pause"
       }
   }
   ```

4. **Feature Parity**
   ```python
   # Map Moonraker concepts to Bambu
   - print_stats -> parse from status messages
   - temperature -> bed_temper, nozzle_temper
   - fan speed -> gcode_line with M106
   - print speed -> print_speed command
   ```

5. **Error Handling**
   ```python
   - Graceful reconnection with backoff
   - Handle empty payloads
   - Unicode-safe JSON parsing
   - Version compatibility checks
   ```

### Security Considerations

1. Store credentials securely
2. Warn users about LAN-only mode requirement for full control
3. Document firmware limitations clearly
4. SSL/TLS validation (even though cert check is off)

### Testing Strategy

1. Test with multiple printer models
2. Test single-client limitation on P1/A1
3. Stress test state update volume
4. Test during actual prints
5. Test connection recovery

---

## 11. Next Steps for Implementation

### Phase 1: Basic Connection
- [x] MQTT connection to printer
- [ ] Subscribe to report topic
- [ ] Parse basic status messages
- [ ] Handle disconnection/reconnection

### Phase 2: State Translation
- [ ] Map Bambu state to Moonraker objects
- [ ] Implement debouncing
- [ ] State change detection
- [ ] Moonraker API integration

### Phase 3: Command Implementation
- [ ] Pause/Resume/Stop
- [ ] Temperature control
- [ ] Fan control  
- [ ] Print speed
- [ ] GCode forwarding

### Phase 4: Advanced Features
- [ ] AMS support
- [ ] Print job start/select
- [ ] Calibration commands
- [ ] Camera integration?
- [ ] File management?

---

## 12. Code Snippets for Reference

### Basic MQTT Connection (Python)
```python
import paho.mqtt.client as mqtt
import ssl
import json

def on_connect(client, userdata, flags, rc):
    print(f"Connected with result code {rc}")
    client.subscribe(f"device/{DEVICE_ID}/report")
    # Request initial state
    client.publish(
        f"device/{DEVICE_ID}/request",
        json.dumps({"pushing": {"sequence_id": "0", "command": "pushall"}})
    )

def on_message(client, userdata, msg):
    try:
        data = json.loads(msg.payload)
        print(f"Received: {data}")
        # Process data
    except json.JSONDecodeError as e:
        print(f"Failed to decode: {e}")

client = mqtt.Client()
client.on_connect = on_connect
client.on_message = on_message

# TLS setup
client.tls_set(
    cert_reqs=ssl.CERT_NONE,
    tls_version=ssl.PROTOCOL_TLS
)

client.username_pw_set("bblp", ACCESS_CODE)
client.connect(PRINTER_IP, 8883, 60)
client.loop_forever()
```

### Command Sender
```python
def send_command(client, device_id, command_dict):
    """Send command to printer"""
    topic = f"device/{device_id}/request"
    payload = json.dumps(command_dict)
    client.publish(topic, payload)

# Examples
send_command(client, device_id, {
    "print": {"sequence_id": "0", "command": "pause"}
})

send_command(client, device_id, {
    "print": {
        "sequence_id": "0",
        "command": "gcode_line",
        "param": "M140 S60 \n"
    },
    "user_id": "1234567890"
})
```

---

## Appendix: Message Examples from Real Logs

### Status Update (Idle)
```json
{
  "print": {
    "bed_temper": 25.625,
    "command": "push_status",
    "msg": 1,
    "sequence_id": "42065"
  }
}
```

### Status Update (Printing)
```json
{
  "print": {
    "gcode_state": "RUNNING",
    "mc_percent": 45,
    "mc_remaining_time": 3600,
    "layer_num": 50,
    "total_layer_num": 200,
    "nozzle_temper": 220,
    "bed_temper": 60,
    "command": "push_status",
    "msg": 1
  }
}
```

### Version Response
```json
{
  "info": {
    "command": "get_version",
    "module": [
      {
        "name": "ota",
        "sw_ver": "01.08.05.00",
        "hw_ver": "AP05"
      }
    ]
  }
}
```

---

## Summary of Key Discoveries

### Most Important Findings

1. **`gcode_line` command is EXTREMELY powerful**
   - Can send any GCode
   - Requires trailing `\n`
   - This is how apps control everything

2. **Single client limitation on P1/A1 is CRITICAL**
   - Must design around this
   - Bridge needs to be THE client or implement proxy

3. **State update volume is MASSIVE**
   - 1,195 updates/minute observed
   - MUST implement aggressive filtering

4. **Firmware 01.08.05.00+ blocks cloud+local control**
   - Users must choose LAN-only for full control
   - Document this clearly

5. **OpenBambuAPI is the gold standard reference**
   - Most complete command documentation
   - Community maintains it
   - Trust it over reverse engineering

### Commands We Can Implement Immediately

✅ Pause/Resume/Stop (known)
✅ Print Speed (NEW)
✅ Temperature via GCode (NEW)
✅ Fan Control via GCode (NEW)
✅ Any GCode via `gcode_line` (NEW)

### Commands That Need More Research

🔍 Print job start/select from SD
🔍 File listing
🔍 AMS detailed control
🔍 Camera access patterns
🔍 Timelapse control

---

**Document Version:** 1.0
**Last Updated:** 2025-01-16
**Sources:** HomeAssistant ha-bambulab integration, OpenBambuAPI, community findings
