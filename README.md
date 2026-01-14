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
# Printer IP Address
BAMBU_HOST=192.168.1.100
# LAN Access Code (found on printer screen)
BAMBU_ACCESS_CODE=12345678
# Printer Serial Number
BAMBU_SERIAL=01P00A12345678

# Connection Mode
# 'local' (LAN) or 'cloud' (not fully tested)
BAMBU_MODE=local

# Server Config
HTTP_PORT=7125
LOG_LEVEL=INFO
# Directory to store uploaded gcodes
GCODES_DIR=gcodes
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

## Docker

Build a container image that bundles this shim with a fully baked copy of the Mainsail UI (served via Nginx and proxied to the shim):

```bash
docker build -t bambu-moonraker-shim .
```

> **Tip:** Override the bundled UI by supplying build args, e.g. `docker build --build-arg MAINSAIL_VERSION=2.18.0 --build-arg MAINSAIL_SHA256=<sha> ...`.

Run the image, providing your usual `.env` values (or individual `-e` flags). The container exposes port 80 for the combined UI+API surface:

```bash
docker run \
    --env-file .env \
    -p 8080:80 \
    bambu-moonraker-shim
```

Open `http://localhost:8080` to access Mainsail. All API/WebSocket traffic is already proxied to the shim—no extra printer configuration inside Mainsail is required.

### Networking (Bridge vs Host)

The shim must reach `BAMBU_HOST` directly. If your Docker host can’t access the printer network from the default bridge, run the container in host mode:

```bash
docker run \
    --env-file .env \
    --network host \
    bambu-moonraker-shim
```

With `--network host`, omit `-p` (nginx listens on port 80, the shim on 7125). If you stick with bridge mode, ensure the host can resolve the printer IP or set `--add-host printer:192.168.2.240` when you run the container.

## Limitations / TODO

- **No full G-Code support**: You cannot send raw G-code commands via the console yet.
- **File Management**: File management is currently local to the shim, not synced with the printer's SD card.
- **Webcams**: No webcam stream bridging is implemented yet.
- **Macros**: Klipper macros are not supported.

## License

MIT License. See [LICENSE](LICENSE) for details.
