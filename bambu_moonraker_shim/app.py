from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from bambu_moonraker_shim.bambu_client import bambu_client
from bambu_moonraker_shim.database_manager import database_manager
from bambu_moonraker_shim.moonraker_api import router as moonraker_router

app = FastAPI(title="Bambu Moonraker Shim", version="0.0.1")

# CORS is required for Mainsail
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(moonraker_router)


@app.on_event("startup")
async def startup_event():
    print("Starting Bambu Moonraker Shim...")
    database_manager.ensure_namespaces(["fluidd"])
    # Start the Bambu Client (MQTT loop)
    await bambu_client.start()


@app.get("/")
async def root():
    return {"message": "Bambu Moonraker Shim is running"}
