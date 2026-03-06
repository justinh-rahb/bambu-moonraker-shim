import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    BAMBU_HOST = os.getenv("BAMBU_HOST", "192.168.1.100")
    BAMBU_SERIAL = os.getenv("BAMBU_SERIAL", "")
    BAMBU_ACCESS_CODE = os.getenv("BAMBU_ACCESS_CODE", "")
    BAMBU_USER_ID = os.getenv("BAMBU_USER_ID", "1234567890")
    BAMBU_MODEL = os.getenv("BAMBU_MODEL", "")
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

    # Paths
    GCODES_DIR = os.getenv("GCODES_DIR", "gcodes")

# Ensure gcodes directory exists
if not os.path.exists(Config.GCODES_DIR):
    os.makedirs(Config.GCODES_DIR)


def parse_bool(value: str) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def normalized_model_name(model: str | None = None) -> str:
    value = Config.BAMBU_MODEL if model is None else model
    return str(value or "").strip().upper()


def model_supports_chamber_temperature(model: str | None = None) -> bool:
    """
    Determine whether chamber temperature should be exposed in Moonraker state.

    P1/A1 series do not provide a useful chamber temperature sensor in this shim
    context, so we hide chamber heater/sensor objects when model hints indicate
    those families.
    """
    model_name = normalized_model_name(model)
    if not model_name:
        # Preserve existing behavior when model is unknown.
        return True
    return "P1" not in model_name and "A1" not in model_name
