"""Passively capture this printer's MQTT requests and selected report deltas.

Subscribes only. Never publishes commands or changes printer state.
Run in the configured container, then operate the Panda Touch manually.
"""
import argparse
import asyncio
import json
import ssl
import time

import aiomqtt
from bambu_moonraker_shim.config import Config

REPORT_FIELDS = {
    'command', 'param', 'sequence_id', 'result', 'reason', 'msg', 'code', 'print_error',
    'gcode_state', 'nozzle_temper', 'nozzle_target_temper', 'bed_temper',
    'bed_target_temper', 'chamber_temper', 'chamber_target_temper',
    'cooling_fan_speed', 'big_fan1_speed', 'big_fan2_speed', 'heatbreak_fan_speed',
    'fan_gear', 'mc_print_stage', 'mc_print_line_number',
}


def redact(value):
    if isinstance(value, dict):
        return {key: '<redacted>' if any(word in key.lower() for word in ('password', 'access_code', 'token', 'secret', 'user_id', 'serial', 'ssid', 'url')) else redact(item)
                for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item) for item in value]
    return value


async def capture(seconds, requests=False):
    if not Config.BAMBU_SERIAL or not Config.BAMBU_ACCESS_CODE:
        raise SystemExit('Configured printer credentials are required; no commands will be sent')
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    prefix = f'device/{Config.BAMBU_SERIAL}/'
    started = time.monotonic()
    last_report = {}
    async with aiomqtt.Client(hostname=Config.BAMBU_HOST, port=8883,
                             username='bblp', password=Config.BAMBU_ACCESS_CODE,
                             tls_context=tls, identifier=f'shim-passive-heaters-{time.time_ns()}') as client:
        await client.subscribe(prefix + 'report', qos=0)
        if requests:
            await client.subscribe(prefix + 'request', qos=0, timeout=5)
        print(json.dumps({'event': 'ready', 'seconds': seconds, 'mode': 'subscribe-only', 'requests': requests}), flush=True)
        try:
            async with asyncio.timeout(seconds):
                async for message in client.messages:
                    try:
                        payload = json.loads(message.payload)
                    except (ValueError, UnicodeDecodeError):
                        continue
                    kind = str(message.topic).rsplit('/', 1)[-1]
                    if kind == 'report':
                        fields = payload.get('print', {})
                        if not isinstance(fields, dict):
                            continue
                        selected = {key: value for key, value in fields.items() if key in REPORT_FIELDS}
                        delta = {key: value for key, value in selected.items() if last_report.get(key) != value}
                        last_report.update(selected)
                        if not delta:
                            continue
                        payload = {'print': delta}
                    print(json.dumps({'elapsed': round(time.monotonic() - started, 3),
                                      'topic': kind, 'qos': message.qos, 'payload': redact(payload)}), flush=True)
        except TimeoutError:
            print(json.dumps({'event': 'finished', 'seconds': seconds}), flush=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--seconds', type=int, default=180)
    parser.add_argument('--requests', action='store_true', help='Also subscribe to requests (unsupported on the development P1S; report ACKs echo G-code)')
    args = parser.parse_args()
    if not 1 <= args.seconds <= 3600:
        parser.error('--seconds must be between 1 and 3600')
    asyncio.run(capture(args.seconds, args.requests))
