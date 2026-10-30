"""Maintain the removable Auto-Proton control in Steam's Compatibility panel."""
import json
from pathlib import Path
import signal
import subprocess
import threading
import time
import live_steam
import preferences


def payload(state):
    path = state/'plan.json'
    plan = json.loads(path.read_text()) if path.exists() else {'games': [], 'tools': {}}
    explicit = preferences.read(state)
    games = {}
    for row in plan['games']:
        appid = row['appid']
        recommendation = row.get('recommended')
        if row.get('native'):
            choice = 'Native Linux'
        elif recommendation:
            tool = recommendation['name']
            choice = plan.get('tools', {}).get(tool, {}).get('display', tool)
        else:
            choice = 'No supported choice yet'
        games[appid] = {'manual': explicit.get(appid, row.get('manual', False)), 'choice': choice,
                        'detail': 'Auto-Proton manages this game. Changes take effect on its next launch.' if row.get('recommendation_known') else row.get('reason', 'Waiting for a report scan.')}
    return {'games': games}


def request_sync():
    subprocess.run(['systemctl', '--user', 'start', '--no-block', 'auto-proton.service'],
                   check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def run(root, state):
    stopped = threading.Event()
    def stop(*_):
        stopped.set()
    previous = {sig: signal.signal(sig, stop) for sig in (signal.SIGTERM, signal.SIGINT)}
    ws = None
    source = (Path(__file__).parent/'ui.js').read_text()
    try:
        while not stopped.is_set():
            try:
                if ws is None:
                    ws = live_steam.connect(root)
                if not ws.evaluate("Boolean(globalThis.__autoProtonUI)"):
                    ws.evaluate(source)
                data = payload(state)
                events = ws.evaluate('globalThis.__autoProtonUI.tick('+json.dumps(data)+')')
                changed = False
                for event in events or []:
                    appid = event.get('appid')
                    if appid not in data['games'] or type(event.get('enabled')) is not bool:
                        continue
                    preferences.set_manual(state, appid, event['enabled'])
                    changed = True
                if changed:
                    request_sync()
                stopped.wait(0.8)
            except (ValueError, OSError, KeyError, TypeError):
                if ws:
                    ws.close()
                    ws = None
                stopped.wait(2)
    finally:
        if ws:
            try:
                ws.evaluate('globalThis.__autoProtonUI?.cleanup()')
            except (ValueError, OSError):
                pass
            ws.close()
        for sig, handler in previous.items():
            signal.signal(sig, handler)
    return 0
