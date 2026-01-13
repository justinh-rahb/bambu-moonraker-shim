# Bambu Moonraker Shim

A lightweight shim that bridges Bambu Lab printers (P1/X1 series) to Mainsail (and potentially other Moonraker clients) by emulating a subset of the Moonraker API.

This project allows you to use the beautiful Mainsail UI to monitor and control your Bambu printer without rooting it or installing full Klipper.

## Features

- **Connection**: Connects to the Bambu Printer via MQTT (LAN Mode or Cloud).
- **Moonraker API**: Emulates key Moonraker HTTP and WebSocket endpoints/methods.
- **Monitoring**:
    - Live temperatures (Extruder, Bed).
    - Print status (Printing, Paused, Ready).
    - Job progress and remaining time.
    - Filename display.
- **Control**:
    - Pause, Resume, and Cancel prints.
    - Start prints (basic implementation).
- **Files**:
    - Basic file listing (mocked/local).
    - File upload (stubbed).

## Prerequisites

- **Python 3.9+**
- **Mainsail**: A running instance of Mainsail (e.g., in a Docker container or hosted).
- **Bambu Printer**: A P1P, P1S, X1C, or A1 printer.

## Installation

1.  **Clone the repository:**
    ```bash
    git clone https://github.com/justinh-rahb/bambu-moonraker-shim.git
    cd bambu-moonraker-shim
    ```

2.  **Create a virtual environment (recommended):**
    ```bash
    python -m venv venv
    source venv/bin/activate  # On Windows: venv\Scripts\activate
    ```

3.  **Install dependencies:**
    ```bash
    pip install -r requirements.txt
    ```

## Configuration

Create a `.env` file in the root directory by copying the example or using the template below:

```bash
# Bambu Printer Config
BAMBU_HOST=192.168.1.100    # Printer IP Address
BAMBU_ACCESS_CODE=12345678  # LAN Access Code (found on printer screen)
BAMBU_SERIAL=01P00A12345678 # Printer Serial Number

# Connection Mode
BAMBU_MODE=local            # 'local' (LAN) or 'cloud' (not fully tested)

# Server Config
HTTP_PORT=7125              # Standard Moonraker Port
LOG_LEVEL=INFO
GCODES_DIR=gcodes           # Directory to store uploaded gcodes

# FTPS Config (file management)
FTPS_PORT=990
FTPS_USER=bblp
FTPS_PASSWORD=12345678      # Defaults to BAMBU_ACCESS_CODE when unset
FTPS_BASE_DIR=/
FTPS_VERIFY_CERT=false
FTPS_TIMEOUT=20
FTPS_ALLOWED_EXTENSIONS=.gcode,.gcode.3mf,.3mf
```

## Usage

1.  **Start the Shim:**
    ```bash
    python main.py
    ```
    The server will start on `http://0.0.0.0:7125`.

2.  **Configure Mainsail:**
    - Open your Mainsail instance.
    - Go to **Settings** -> **Printers**.
    - Add a new printer.
    - Set the **URL** to the IP address of the machine running this shim (e.g., `192.168.1.50`).
    - The port is `7125` by default, which Mainsail expects.

3.  **Enjoy:** Mainsail should connect, show the printer as "Ready" or "Printing," and display temperatures.

## Limitations / TODO

- **No full G-Code support**: You cannot send raw G-code commands via the console yet.
- **File Management**: File upload/listing uses FTPS; printing via file selection may still depend on model/firmware behavior.
- **Webcams**: No webcam stream bridging is implemented yet.
- **Macros**: Klipper macros are not supported.

## License

MIT License. See [LICENSE](LICENSE) for details.
