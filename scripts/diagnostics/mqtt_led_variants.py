import asyncio
import ssl
import json
import aiomqtt
from bambu_moonraker_shim.config import Config

async def test_led_variants():
    print("Testing MQTT LED Control Variants...")
    
    host = Config.BAMBU_HOST
    serial = Config.BAMBU_SERIAL
    access_code = Config.BAMBU_ACCESS_CODE
    
    print(f"Host: {host}")
    
    tls_context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls_context.check_hostname = False
    tls_context.verify_mode = ssl.CERT_NONE

    try:
        async with aiomqtt.Client(
            hostname=host,
            port=8883,
            username="bblp",
            password=access_code,
            tls_context=tls_context
        ) as client:
            print("Connected to MQTT!")
            topic = f"device/{serial}/request"
            
            # Variant 1: "print" command (Current implementation)
            print("Trying Variant 1: 'print' command...")
            cmd_v1 = {
                "print": {
                    "sequence_id": "0",
                    "command": "ledctrl",
                    "led_node": "chamber_light",
                    "led_mode": "on",
                    "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0
                }
            }
            await client.publish(topic, json.dumps(cmd_v1))
            await asyncio.sleep(2)
            
            # Variant 2: "system" command
            print("Trying Variant 2: 'system' command...")
            cmd_v2 = {
                "system": {
                    "sequence_id": "0",
                    "command": "ledctrl",
                    "led_node": "chamber_light",
                    "led_mode": "on",
                    "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0
                }
            }
            await client.publish(topic, json.dumps(cmd_v2))
            await asyncio.sleep(2)
            
            # Turn off
            print("Resetting (turning off)...")
            cmd_off = {
                "print": {
                    "sequence_id": "0",
                    "command": "ledctrl",
                    "led_node": "chamber_light",
                    "led_mode": "off",
                    "led_on_time": 500, "led_off_time": 500, "loop_times": 0, "interval_time": 0
                }
            }
            await client.publish(topic, json.dumps(cmd_off))
            
            print("Done.")

    except Exception as e:
        print(f"Error: {e}")

if __name__ == "__main__":
    asyncio.run(test_led_variants())
