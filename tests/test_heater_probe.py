import asyncio
import json
import unittest
from unittest.mock import patch

from scripts.diagnostics import probe_nonblocking_bed as diagnostic


class Reports:
    def __init__(self):
        self.queue = asyncio.Queue()

    def __aiter__(self):
        return self

    async def __anext__(self):
        return await self.queue.get()

    async def put(self, fields):
        class Message:
            payload = json.dumps({'print': fields}).encode()
        await self.queue.put(Message())


class FakeMQTT:
    def __init__(self, state='FINISH', target=0):
        self.messages = Reports()
        self.published = []
        self.state = state
        self.target = target

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def subscribe(self, *args, **kwargs):
        pass

    async def publish(self, topic, payload, qos):
        body = json.loads(payload)
        self.published.append((body, qos))
        if 'pushing' in body:
            await self.messages.put(dict(gcode_state=self.state, bed_temper=20, bed_target_temper=self.target))
        else:
            command = body['print']
            await self.messages.put({**command, 'result': 'success', 'reason': 'success'})
            await self.messages.put(dict(bed_temper=21, bed_target_temper=45 if 'S45' in command['param'] else 0))


class HeaterProbeTests(unittest.IsolatedAsyncioTestCase):
    def test_literal_panda_parameters_and_target_bounds(self):
        commands = diagnostic.plan(45)
        self.assertEqual(commands[0]['print']['param'], 'M140 S45\n')
        self.assertEqual(commands[1]['print']['param'], 'M140 S0\n')
        self.assertNotIn('user_id', commands[0])
        for target in (0, 20, 61, float('nan'), '45'):
            with self.assertRaises(ValueError):
                diagnostic.plan(target)

    def test_unknown_hot_or_active_baselines_are_rejected(self):
        for state in ({}, {'gcode_state': 'RUNNING'}, {'gcode_state': 'PAUSE'},
                      {'gcode_state': 'FINISH', 'bed_target_temper': 60, 'bed_temper': 20},
                      {'gcode_state': 'FINISH', 'bed_target_temper': 0, 'bed_temper': 44}):
            with self.assertRaises(RuntimeError):
                diagnostic.idle_baseline(state, 45)

    async def test_probe_uses_raw_ack_and_target_then_turns_off_before_warm(self):
        fake = FakeMQTT()
        with patch.object(diagnostic.aiomqtt, 'Client', return_value=fake), patch.object(diagnostic.Config, 'BAMBU_SERIAL', 'TEST'), patch.object(diagnostic.Config, 'BAMBU_ACCESS_CODE', 'TEST'), patch('builtins.print') as output:
            await diagnostic.probe(45, 2005)
        commands = [body['print']['param'] for body, qos in fake.published if 'print' in body]
        self.assertEqual(commands, ['M140 S45\n', 'M140 S0\n'])
        self.assertTrue(all(qos == 0 for _, qos in fake.published))
        events = [json.loads(call.args[0]) for call in output.call_args_list]
        result = next(event for event in events if event['event'] == 'result')
        self.assertTrue(result['nonblocking_verified'])
        self.assertEqual(result['target_after_off'], 0)

    async def test_active_state_prevents_any_heater_publish(self):
        fake = FakeMQTT(state='RUNNING')
        with patch.object(diagnostic.aiomqtt, 'Client', return_value=fake), patch.object(diagnostic.Config, 'BAMBU_SERIAL', 'TEST'), patch.object(diagnostic.Config, 'BAMBU_ACCESS_CODE', 'TEST'), patch('builtins.print'):
            with self.assertRaises(RuntimeError):
                await diagnostic.probe(45, 2005)
        self.assertEqual(len(fake.published), 1)
        self.assertIn('pushing', fake.published[0][0])
