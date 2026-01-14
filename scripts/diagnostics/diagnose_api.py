from bambu_moonraker_shim.moonraker_api import get_directory, success_response, error_response
from bambu_moonraker_shim.config import Config
from bambu_moonraker_shim.ftps_client import ftps_client

# Mock generic helper functions that might depend on router context if needed
# But verify_ftps just imports straight from classes. 
# We need to test the "get_directory" function. But it is async and uses "request" object if called via websocket.
# Wait, get_directory in moonraker_api.py is an async function (HTTP endpoint).
# The websocket handler is inside handle_jsonrpc.

# Let's verify the logic we put in get_directory (HTTP) first.

import asyncio
import json

async def test_logic():
    print("Testing get_directory logic...")
    
    # We can't easily call get_directory because it returns a JSONResponse object which might key error if not in fastapi context?
    # Actually success_response returns a simple dict: {"result": data}
    # But get_directory is decorated with @router.get, but we can still call the function.
    
    try:
        response = await get_directory(path="gcodes")
        # success_response returns: {"result": ...}
        result = response["result"]
        
        print(f"Dirs: {len(result['dirs'])}")
        print(f"Files: {len(result['files'])}")
        print(f"Root Info: {result['root_info']}")
        
        if len(result['files']) > 0:
            print("First file:", result['files'][0])
        else:
            print("No files found!")
            
    except Exception as e:
        print(f"Error calling get_directory: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_logic())
