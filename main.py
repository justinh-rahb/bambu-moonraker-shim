import uvicorn
from bambu_moonraker_shim.app import app
from bambu_moonraker_shim.config import Config

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=Config.HTTP_PORT, reload=True, ws_ping_interval=None, ws_ping_timeout=300)
