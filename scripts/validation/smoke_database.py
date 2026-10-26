"""Exercise HTTP/WebSocket database compatibility against a running shim.

Only writes and removes its own unique temporary namespace. No printer commands.
"""
import argparse
import asyncio
import uuid
import aiohttp


async def check(base_url):
    namespace = 'shim_validation_' + uuid.uuid4().hex
    async with aiohttp.ClientSession() as session:
        async def http(method, path, **kwargs):
            async with session.request(method, base_url + path, **kwargs) as response:
                return response.status, await response.json()
        async with session.ws_connect(base_url + '/websocket') as ws:
            index = 0
            async def rpc(method, **params):
                nonlocal index
                index += 1
                await ws.send_json({'jsonrpc': '2.0', 'id': index, 'method': method, 'params': params})
                while True:
                    message = await ws.receive_json(timeout=15)
                    if message.get('id') == index:
                        return message
            try:
                code, result = await http('POST', '/server/database/item', json={'namespace': namespace, 'key': 'settings.theme', 'value': 'dark'})
                assert code == 200, result
                result = await rpc('server.database.get_item', namespace=namespace)
                assert result['result']['value'] == {'settings': {'theme': 'dark'}}, result
                result = await rpc('server.database.post_item', namespace=namespace, key=['settings', 'part.3mf'], value=None)
                assert result['result']['value'] is None, result
                code, result = await http('GET', '/server/database/item', params={'namespace': namespace, 'key:json': '["settings","part.3mf"]'})
                assert code == 200 and result['result']['value'] is None, result
                code, result = await http('DELETE', '/server/database/item', json={'namespace': namespace, 'key': 'settings.theme'})
                assert code == 200 and result['result']['value'] == 'dark', result
                code, result = await http('GET', '/server/database/item', params={'namespace': namespace, 'key': 'settings.theme'})
                assert code == 404, result
                # A parse error must not close the socket.
                await ws.send_str('{broken')
                while True:
                    message = await ws.receive_json(timeout=15)
                    if message.get('error', {}).get('code') == -32700:
                        break
                result = await rpc('server.database.list')
                assert namespace in result['result']['namespaces'], result
                code, result = await http('GET', '/printer/objects/query?extruder=temperature,target&print_stats')
                assert code == 200 and set(result['result']['status']['extruder']) == {'temperature', 'target'}, result
                print('PASS: HTTP/WS settings interoperability, arrays, null, errors, socket recovery, and standard object queries')
            finally:
                await http('DELETE', '/server/database/item', json={'namespace': namespace, 'key': 'settings'})


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', default='http://localhost:7125')
    args = parser.parse_args()
    asyncio.run(check(args.url.rstrip('/')))
