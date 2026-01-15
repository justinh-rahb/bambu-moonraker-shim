import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    BAMBU_HOST = os.getenv("BAMBU_HOST", "192.168.1.100")
    BAMBU_SERIAL = os.getenv("BAMBU_SERIAL", "")
    BAMBU_ACCESS_CODE = os.getenv("BAMBU_ACCESS_CODE", "")
    BAMBU_MODE = os.getenv("BAMBU_MODE", "local")  # local | cloud
    
    # Cloud specific
    BAMBU_REGION = os.getenv("BAMBU_REGION", "us")
    BAMBU_EMAIL = os.getenv("BAMBU_EMAIL", "")
    BAMBU_TOKEN = os.getenv("BAMBU_TOKEN", "")

    # Server specific
    HTTP_PORT = int(os.getenv("HTTP_PORT", "7125")) # Default Moonraker port
    LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO")
    
    # FTPS specific
    BAMBU_FTPS_PORT = int(os.getenv("BAMBU_FTPS_PORT", "990"))
    BAMBU_FTPS_USER = os.getenv("BAMBU_FTPS_USER", "bblp")
    BAMBU_FTPS_PASS = os.getenv("BAMBU_FTPS_PASS", BAMBU_ACCESS_CODE)
    BAMBU_FTPS_UPLOADS_DIR = os.getenv("BAMBU_FTPS_UPLOADS_DIR", "/")

    # Heater commands
    BAMBU_NOZZLE_SET_CMD = os.getenv("BAMBU_NOZZLE_SET_CMD", "M104")
    BAMBU_NOZZLE_SET_WAIT_CMD = os.getenv("BAMBU_NOZZLE_SET_WAIT_CMD", "M109")
    BAMBU_BED_SET_CMD = os.getenv("BAMBU_BED_SET_CMD", "M140")
    BAMBU_BED_SET_WAIT_CMD = os.getenv("BAMBU_BED_SET_WAIT_CMD", "M190")

    # Paths
    GCODES_DIR = os.getenv("GCODES_DIR", "gcodes")

_ALLOWED_HEATER_CMDS = {"M104", "M109", "M140", "M190"}


def _validate_heater_cmd(env_name: str, value: str) -> None:
    if value not in _ALLOWED_HEATER_CMDS:
        raise ValueError(
            f"{env_name} must be one of {sorted(_ALLOWED_HEATER_CMDS)} (got {value!r})."
        )


_validate_heater_cmd("BAMBU_NOZZLE_SET_CMD", Config.BAMBU_NOZZLE_SET_CMD)
_validate_heater_cmd("BAMBU_NOZZLE_SET_WAIT_CMD", Config.BAMBU_NOZZLE_SET_WAIT_CMD)
_validate_heater_cmd("BAMBU_BED_SET_CMD", Config.BAMBU_BED_SET_CMD)
_validate_heater_cmd("BAMBU_BED_SET_WAIT_CMD", Config.BAMBU_BED_SET_WAIT_CMD)

# Ensure gcodes directory exists
if not os.path.exists(Config.GCODES_DIR):
    os.makedirs(Config.GCODES_DIR)
