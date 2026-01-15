> [!WARNING] 
> **DISCLAIMER**
>
> This project is an **unofficial, experimental shim** that communicates with Bambu Lab printers using their documented and undocumented LAN interfaces.
>
> - This is **NOT** supported by Bambu Lab.
> - This project may break with firmware updates.
> - There is **NO WARRANTY** of safety, correctness, or fitness for any purpose.
> - You are responsible for **anything that happens to your printer** while using this software.
>
> By using this project, you acknowledge that you are doing so **at your own risk**.
> If that makes you uncomfortable, this project is not for you.

# Bambu Moonraker Shim

A lightweight shim that bridges **Bambu Lab printers** (P1 / X1 / A1 series) to **Mainsail** (and potentially other Moonraker clients) by emulating a **subset** of the Moonraker API.

This allows you to use the Mainsail UI to **monitor and partially control** a Bambu printer **without rooting it, flashing firmware, or installing Klipper**.

## What This Project Is (and Is Not)

**This is:**

* A protocol shim
* A compatibility layer
* A “Moonraker impersonator” that translates requests to Bambu’s LAN MQTT / FTPS interfaces

**This is not:**

* Klipper
* A firmware replacement
* A complete Moonraker implementation
* Endorsed or supported by Bambu Lab

## Features (Current State)

### Connection

* Connects to Bambu printers over **LAN MQTT**
* Supports **LAN mode** (cloud mode is experimental)

### Monitoring (Working)

* Live temperature **display**:

  * Extruder
  * Bed
* Print state:

  * Ready
  * Printing
  * Paused
  * Complete
* Job progress
* Currently loaded filename

### Control (Working)

* Manual G-code command sending (limited)
* XYZ movement
* Homing
* Pause / Resume / Cancel prints
* Start prints (basic implementation)
* Fan control:

  * Part cooling
  * Auxiliary
  * Chamber / exhaust
* Chamber LED on / off

### Files (Partial / Experimental)

* File listing (via printer FTPS)
* File upload (via FTPS)

## What Does NOT Work (Yet)

* ❌ Setting heater target temperatures (display only)
* ❌ Full interactive G-code console
* ❌ Klipper macros
* ❌ Webcam bridging
* ❌ Job queueing
* ❌ Full Moonraker API parity

If you are expecting a drop-in Klipper replacement, this is not that.

## Prerequisites

* **Python 3.9+**
* **Mainsail**
* **Bambu Lab printer**:

  * P1P
  * P1S
  * X1 / X1C
  * A1 (experimental)

## Installation

1. **Clone the repository**

   ```bash
   git clone https://github.com/justinh-rahb/bambu-moonraker-shim.git
   cd bambu-moonraker-shim
   ```

2. **Create a virtual environment (recommended)**

   ```bash
   python -m venv venv
   source venv/bin/activate  # Windows: venv\Scripts\activate
   ```

3. **Install dependencies**

   ```bash
   pip install -r requirements.txt
   ```

## Configuration

Create a `.env` file in the project root:

```bash
# Bambu Printer Config
BAMBU_HOST=192.168.1.100
BAMBU_ACCESS_CODE=12345678
BAMBU_SERIAL=01P00A12345678

# Connection Mode
BAMBU_MODE=local

# Server Config
HTTP_PORT=7125
LOG_LEVEL=INFO
GCODES_DIR=gcodes
```

## Usage

1. **Start the shim**

   ```bash
   python main.py
   ```

2. **Configure Mainsail**

   * Open Mainsail
   * Go to **Settings → Printers**
   * Add a new printer
   * Set the URL to the machine running the shim (e.g. `http://192.168.1.50:7125`)

3. **Connect**

   * Mainsail should connect
   * The printer should appear as Ready / Printing
   * Temperatures and basic controls should function

## Docker

A Docker image is provided that bundles:

* This shim
* Mainsail UI
* Nginx reverse proxy

### Build

```bash
docker build -t bambu-moonraker-shim .
```

### Run

```bash
docker run \
  --env-file .env \
  -p 8080:80 \
  bambu-moonraker-shim
```

Access Mainsail at:

```
http://localhost:8080
```

### Networking Notes

The container **must be able to reach the printer IP directly**.

If bridge networking causes issues, use host mode:

```bash
docker run \
  --env-file .env \
  --network host \
  bambu-moonraker-shim
```

## Fan Control

The shim maps Mainsail fan controls to Bambu’s MQTT G-code interface:

| Fan       | G-code    | Description           |
| --------- | --------- | --------------------- |
| `part`    | `M106 P1` | Part cooling fan      |
| `aux`     | `M106 P2` | Auxiliary fan         |
| `chamber` | `M106 P3` | Chamber / exhaust fan |

* Fan speeds are clamped to `0–255`
* Percent inputs are converted automatically
* All commands are sent with a trailing newline (required by Bambu MQTT)

## Limitations / Roadmap

* Heater target control
* Safer / richer G-code handling
* Webcam bridging
* More complete Moonraker API coverage
* Better error handling and state reconciliation

This project is evolving quickly and may break at any time.

## License

MIT License — see [LICENSE](LICENSE).
