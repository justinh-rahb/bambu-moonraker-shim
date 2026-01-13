from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn
import asyncio
from config import Config
from moonraker_api import router as moonraker_router
from bambu_client import bambu_client

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
    # Start the Bambu Client (MQTT loop)
    await bambu_client.start()

@app.get("/")
async def root():
    return {"message": "Bambu Moonraker Shim is running"}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=Config.HTTP_PORT, reload=True)
