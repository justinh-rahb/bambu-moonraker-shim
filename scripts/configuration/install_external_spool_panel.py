"""Add external-spool controls to an existing Mainsail expert-mode layout.

Uses only Moonraker database APIs. Does not invoke any printer command.
Basic-mode Mainsail discovers these macros automatically.
"""
import argparse
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid

MACROS = (
    'LOAD_EXTERNAL_FILAMENT', 'UNLOAD_EXTERNAL_FILAMENT',
    'EXTERNAL_FILAMENT_EXTRUDED', 'EXTERNAL_FILAMENT_RETRY',
    'SET_PLA', 'SET_PETG', 'SET_ABS', 'SET_ASA', 'SET_TPU', 'SET_PC', 'SET_PA',
    'SET_EXTERNAL_FILAMENT',
)
GROUP_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, 'bambu-moonraker-shim/external-spool'))


def install(base_url):
    def get(key):
        query = urllib.parse.urlencode({'namespace': 'mainsail', 'key': key})
        try:
            with urllib.request.urlopen(base_url+'/server/database/item?'+query, timeout=10) as response:
                return json.load(response)['result']['value']
        except urllib.error.HTTPError as error:
            if error.code == 404:
                return None
            raise

    def put(key, value):
        payload = json.dumps({'namespace': 'mainsail', 'key': key, 'value': value}).encode()
        request = urllib.request.Request(base_url+'/server/database/item', data=payload,
                                         headers={'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(request, timeout=10) as response:
            json.load(response)

    settings = get('macros') or {}
    if settings.get('mode') != 'expert':
        print('Mainsail basic mode discovers the external-spool macro buttons automatically.')
        return
    existing = (settings.get('macrogroups') or {}).get(GROUP_ID, {})
    group = {
        'name': 'External Spool', 'color': 'primary', 'colorCustom': '#fff',
        'showInStandby': True, 'showInPause': False, 'showInPrinting': False,
        **existing,
    }
    macros = list(group.get('macros') or [])
    for name in MACROS:
        if not any(item.get('name') == name for item in macros):
            macros.append({'pos': len(macros)+1, 'name': name, 'color': 'primary',
                           'showInStandby': True, 'showInPause': False, 'showInPrinting': False})
    group['macros'] = macros
    put('macros.macrogroups.'+GROUP_ID, group)
    dashboard = get('dashboard') or {}
    panel = 'macrogroup_'+GROUP_ID
    for layout in ('widescreenLayout1', 'desktopLayout1', 'tabletLayout1', 'mobileLayout'):
        # Do not replace an absent layout with a partial layout: Mainsail's
        # built-in defaults will include the newly registered group.
        if layout in dashboard and isinstance(dashboard[layout], list):
            if not any(item.get('name') == panel for item in dashboard[layout]):
                dashboard[layout].insert(0, {'name': panel, 'visible': True})
    put('dashboard', dashboard)
    print('Installed External Spool macro group; refresh Mainsail. No printer command was sent.')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--url', default='http://localhost:7125')
    args = parser.parse_args()
    install(args.url.rstrip('/'))
