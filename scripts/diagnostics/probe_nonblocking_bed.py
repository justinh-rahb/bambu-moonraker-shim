"""Direct-MQTT bed probe. Dry-run by default; --execute heats then switches off.

Uses literal M140 payloads and raw reports, bypassing shim heater translation.
Never moves axes, changes fans or starts prints. Attempts bed-off cleanup on failure;
the operator must check the panel if delivery cannot be confirmed.
"""
import argparse
import asyncio
import json
import ssl
import time

import aiomqtt
from bambu_moonraker_shim.config import Config

IDLE_STATES = {'IDLE', 'FINISH', 'FAILED'}


def plan(target, sequence=2005):
    if not isinstance(target, int) or not 30 <= target <= 60:
        raise ValueError('Probe target must be an integer from 30 to 60 Celsius')
    if not isinstance(sequence, int) or sequence < 1:
        raise ValueError('Sequence ID must be positive')
    return [dict(print=dict(command='gcode_line', param=f'M140 S{value}\n', sequence_id=str(sequence + index)))
            for index, value in enumerate((target, 0))]


def idle_baseline(state, target):
    if state.get('gcode_state') not in IDLE_STATES:
        raise RuntimeError('Refusing probe: printer state is active or unconfirmed')
    if state.get('bed_target_temper') != 0:
        raise RuntimeError('Refusing probe: bed already has a nonzero or unknown target')
    current = state.get('bed_temper')
    if not isinstance(current, (int, float)) or not 0 < current < target - 3:
        raise RuntimeError('Bed must be below the probe target by more than 3 degrees')


async def probe(target, sequence):
    commands = plan(target, sequence)
    if not Config.BAMBU_SERIAL or not Config.BAMBU_ACCESS_CODE:
        raise RuntimeError('Real printer credentials required')
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    tls.check_hostname = False
    tls.verify_mode = ssl.CERT_NONE
    state, acknowledgements = {}, {}
    topic = f'device/{Config.BAMBU_SERIAL}/'
    clock_start = time.monotonic()
    touched = False
    off_confirmed = False
    async with aiomqtt.Client(hostname=Config.BAMBU_HOST, port=8883, username='bblp',
                             password=Config.BAMBU_ACCESS_CODE, tls_context=tls, timeout=5) as client:
        await client.subscribe(topic + 'report', qos=0)

        async def collect():
            async for message in client.messages:
                try:
                    fields = json.loads(message.payload).get('print', {})
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(fields, dict):
                    continue
                state.update({key: fields[key] for key in ('gcode_state', 'bed_temper', 'bed_target_temper') if key in fields})
                if fields.get('command') == 'gcode_line':
                    acknowledgements[str(fields.get('sequence_id'))] = fields
                    print(json.dumps({'event': 'command_reply', 'elapsed': round(time.monotonic()-clock_start, 3),
                                      'param': fields.get('param'), 'sequence_id': fields.get('sequence_id'),
                                      'result': fields.get('result'), 'reason': fields.get('reason')}), flush=True)
                if 'bed_target_temper' in fields or 'gcode_state' in fields:
                    print(json.dumps({'event': 'raw_state', 'elapsed': round(time.monotonic()-clock_start, 3), **state}), flush=True)

        async def until(predicate, seconds=10):
            async with asyncio.timeout(seconds):
                while not predicate():
                    if reader.done():
                        await reader
                        raise RuntimeError('Report listener stopped')
                    await asyncio.sleep(0.05)

        async def send(command):
            print(json.dumps({'event': 'send', 'elapsed': round(time.monotonic()-clock_start, 3), 'qos': 0, 'payload': command}), flush=True)
            await client.publish(topic + 'request', json.dumps(command), qos=0)

        def accepted(command):
            reply = acknowledgements.get(command['print']['sequence_id'], {})
            if reply.get('result') == 'fail' or reply.get('reason') not in (None, '', 'success'):
                raise RuntimeError(f'Printer rejected command: {reply.get("reason")}')
            return reply.get('result') == 'success' and reply.get('param') == command['print']['param']

        reader = asyncio.create_task(collect())
        try:
            await client.publish(topic + 'request', json.dumps({'pushing': {'command': 'pushall', 'sequence_id': '0'}}), qos=0)
            await until(lambda: all(key in state for key in ('gcode_state', 'bed_temper', 'bed_target_temper')))
            idle_baseline(state, target)
            print(json.dumps({'event': 'baseline', **state}), flush=True)
            touched = True
            await send(commands[0])
            await until(lambda: accepted(commands[0]) and state.get('bed_target_temper') == target)
            before_off = state.get('bed_temper', target)
            if state.get('gcode_state') not in IDLE_STATES:
                raise RuntimeError('Print state changed; stopping probe')
            await send(commands[1])
            await until(lambda: accepted(commands[1]) and state.get('bed_target_temper') == 0)
            off_confirmed = True
            below_target = before_off < target - 1 and state.get('bed_temper', target) < target - 1
            print(json.dumps({'event': 'result', 'nonblocking_verified': below_target,
                              'temperature_before_off': before_off, 'temperature_after_off': state.get('bed_temper'),
                              'target_after_off': state.get('bed_target_temper')}), flush=True)
            if not below_target:
                raise RuntimeError('Commands worked, but bed reached target before off confirmation; nonblocking result is inconclusive')
        finally:
            if touched and not off_confirmed and state.get('gcode_state') in IDLE_STATES:
                await send(commands[1])
                try:
                    await until(lambda: state.get('bed_target_temper') == 0, seconds=5)
                except TimeoutError:
                    print('Bed-off report not confirmed; check the printer panel.', flush=True)
            reader.cancel()
            await asyncio.gather(reader, return_exceptions=True)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--target', type=int, default=45)
    parser.add_argument('--sequence', type=int, default=2005)
    parser.add_argument('--execute', action='store_true', help='Actually heat the bed; operator must be present')
    args = parser.parse_args()
    commands = plan(args.target, args.sequence)
    if args.execute:
        asyncio.run(probe(args.target, args.sequence))
    else:
        print(json.dumps({'mode': 'dry-run; no connection or printer command', 'qos': 0, 'commands': commands}, indent=2))
