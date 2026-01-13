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

# Ensure gcodes directory exists
if not os.path.exists(Config.GCODES_DIR):
    os.makedirs(Config.GCODES_DIR)
