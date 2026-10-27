"""Check file deletion over a real WebSocket in a mock shim only."""
import asyncio
import uuid
import aiohttp
from bambu_moonraker_shim.config import Config


async def main():
    if Config.BAMBU_SERIAL:
        raise RuntimeError('Deletion smoke test requires mock mode (BAMBU_SERIAL unset)')
    filename = 'delete-validation-'+uuid.uuid4().hex+'.gcode'
    base = 'http://127.0.0.1:80'
    async with aiohttp.ClientSession() as session:
        form = aiohttp.FormData()
        form.add_field('file', b'; disposable deletion validation\n', filename=filename)
        async with session.post(base+'/server/files/upload', data=form) as response:
            assert response.status == 200, await response.text()
        async with session.ws_connect(base+'/websocket') as ws:
            await ws.send_json({'jsonrpc': '2.0', 'id': 1, 'method': 'server.files.delete_file',
                                'params': {'path': 'gcodes/'+filename}})
            result = notification = None
            while result is None or notification is None:
                message = await ws.receive_json(timeout=10)
                if message.get('id') == 1:
                    result = message
                    assert 'error' not in result, result
                if message.get('method') == 'notify_filelist_changed':
                    notification = message['params'][0]
            assert result['result'] == notification
            assert notification['action'] == 'delete_file'
            assert notification['item']['path'] == filename
        async with session.get(base+'/server/files/list') as response:
            files = (await response.json())['result']
            assert filename not in [item['path'] for item in files]
        async with session.delete(base+'/server/files/gcodes/'+filename) as response:
            assert response.status == 404, await response.text()
    print('PASS: disposable upload, WS deletion, file-list notification, refreshed list, and missing-file error')


if __name__ == '__main__':
    asyncio.run(main())
