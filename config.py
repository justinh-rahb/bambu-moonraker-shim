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
    
    # Paths
    GCODES_DIR = os.getenv("GCODES_DIR", "gcodes")

    # FTPS (file management)
    FTPS_PORT = int(os.getenv("FTPS_PORT", "990"))
    FTPS_USER = os.getenv("FTPS_USER", "bblp")
    FTPS_PASSWORD = os.getenv("FTPS_PASSWORD", BAMBU_ACCESS_CODE)
    FTPS_BASE_DIR = os.getenv("FTPS_BASE_DIR", "/")
    FTPS_VERIFY_CERT = os.getenv("FTPS_VERIFY_CERT", "false").lower() == "true"
    FTPS_TIMEOUT = float(os.getenv("FTPS_TIMEOUT", "20"))
    FTPS_ALLOWED_EXTENSIONS = [
        ext.strip() if ext.startswith(".") else f".{ext.strip()}"
        for ext in os.getenv("FTPS_ALLOWED_EXTENSIONS", ".gcode,.gcode.3mf,.3mf").split(",")
        if ext.strip()
    ]

# Ensure gcodes directory exists
if not os.path.exists(Config.GCODES_DIR):
    os.makedirs(Config.GCODES_DIR)
