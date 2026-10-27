"""Read a fixture into a mock shim only; never send it to a real printer."""
import asyncio
import json
from pathlib import Path
import sys
from urllib.parse import quote

import aiohttp
from bambu_moonraker_shim.config import Config


async def main():
    if Config.BAMBU_SERIAL:
        raise SystemExit('Refusing metadata smoke test: BAMBU_SERIAL must be empty')
    fixture = Path(sys.argv[1])
    base = 'http://127.0.0.1:80'
    async with aiohttp.ClientSession() as session:
        form = aiohttp.FormData()
        form.add_field('file', fixture.read_bytes(), filename=fixture.name)
        form.add_field('print', 'false')
        async with session.post(base + '/server/files/upload', data=form) as response:
            uploaded = await response.json()
            assert response.status == 200 and not uploaded['result']['print_started'], uploaded
        async with session.get(base + '/server/files/metadata', params={'filename': fixture.name}) as response:
            result = await response.json()
            assert response.status == 200, result
            metadata = result['result']
            assert metadata['size'] == fixture.stat().st_size
            assert metadata['thumbnails'], metadata
        async with session.ws_connect(base + '/websocket') as ws:
            await ws.send_json(dict(jsonrpc='2.0', id=1, method='server.files.metadata', params={'filename': fixture.name}))
            while True:
                reply = await ws.receive_json(timeout=15)
                if reply.get('id') == 1:
                    assert reply['result'] == metadata
                    break
        for thumbnail in metadata['thumbnails']:
            async with session.get(base + '/server/files/gcodes/' + quote(thumbnail['relative_path'])) as response:
                image = await response.read()
                assert response.status == 200 and response.headers['Content-Type'].startswith('image/')
                assert len(image) == thumbnail['size']
        async with session.post(base + '/server/files/metascan', params={'filename': fixture.name}) as response:
            scanned = (await response.json())['result']
            assert scanned['uuid'] == metadata['uuid']
        async with session.get(base + '/server/files/metadata', params={'filename': 'missing-validation.gcode'}) as response:
            assert response.status == 404
        print('PASS: mock upload without print, HTTP/WS metadata, preview downloads, forced rescan, and missing-file error')
        print(json.dumps({key: metadata[key] for key in ('filename', 'size', 'slicer', 'estimated_time', 'layer_count', 'object_height', 'filament_total', 'filament_weight_total') if key in metadata}))


if __name__ == '__main__':
    asyncio.run(main())
