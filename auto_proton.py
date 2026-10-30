#!/usr/bin/env python3
"""Local Steam compatibility manager. Python 3.10+, standard library only."""
from __future__ import annotations
import argparse
import contextlib
import fcntl
import hashlib
import io
import json
import os
from pathlib import Path
import re
import struct
import sys
import tempfile
import time
import urllib.request
import live_steam
import preferences
from dataclasses import dataclass

VERSION = '0.2.0'
DAY = 86400


@dataclass
class Node:
    key: str
    value: str | list
    start: int
    end: int
    close: int = 0


def parse(text):
    """Strict KeyValues parser retaining exact spans for surgical edits."""
    tokens = []
    pattern = re.compile(r'\s+|//[^\n]*|"(?:\\.|[^"\\])*"|[{}]|[^\s{}"]+')
    end = 0
    for m in pattern.finditer(text):
        if m.start() != end:
            raise ValueError('Invalid KeyValues text')
        end = m.end()
        t = m.group()
        if t.isspace() or t.startswith('//'):
            continue
        tokens.append((t, m.start(), m.end()))
    if end != len(text):
        raise ValueError('Unterminated KeyValues string')
    pos = 0

    def decode(t):
        if t.startswith('"'):
            return re.sub(r'\\(["\\])', r'\1', t[1:-1])
        return t

    def block(nested=False):
        nonlocal pos
        result = []
        seen = set()
        while pos < len(tokens):
            token, start, _ = tokens[pos]
            if token == '}':
                if not nested:
                    raise ValueError('Unexpected closing brace')
                pos += 1
                return result, start, tokens[pos - 1][2]
            if token == '{' or pos + 1 >= len(tokens):
                raise ValueError('Missing KeyValues value')
            key = decode(token)
            if key.lower() in seen:
                raise ValueError(f'Duplicate KeyValues key: {key}')
            seen.add(key.lower())
            pos += 1
            val, _, stop = tokens[pos]
            pos += 1
            if val == '{':
                children, close, stop = block(True)
                result.append(Node(key, children, start, stop, close))
            elif val == '}':
                raise ValueError('Missing KeyValues value')
            else:
                result.append(Node(key, decode(val), start, stop))
        if nested:
            raise ValueError('Unclosed KeyValues object')
        return result, 0, len(text)
    return block()[0]


def child(nodes, key):
    return next((n for n in nodes if n.key.lower() == key.lower()), None)


def values(nodes):
    return {n.key.lower(): values(n.value) if isinstance(n.value, list) else n.value for n in nodes}


def load_vdf(path):
    return values(parse(path.read_text()))


def quote(value):
    return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'


def render_entry(key, value):
    if value is None:
        return ''
    inner = '\n'.join('\t' + quote(k) + '\t\t' + quote(v) for k, v in value.items())
    return quote(key) + '\n{\n' + inner + '\n}'


def mapping_node(text):
    nodes = parse(text)
    for key in ('InstallConfigStore', 'Software', 'Valve', 'Steam'):
        node = child(nodes, key)
        if node is None or not isinstance(node.value, list):
            raise ValueError(f'Missing config object {key}')
        nodes = node.value
    return node, child(nodes, 'CompatToolMapping')


def mappings(text):
    _, node = mapping_node(text)
    if node is None:
        return {}
    if not isinstance(node.value, list):
        raise ValueError('Invalid CompatToolMapping')
    return values(node.value)


def edit_mapping(text, changes):
    parent, node = mapping_node(text)
    if node is None:
        entries = '\n'.join(render_entry(k, v) for k, v in changes.items() if v is not None)
        if not entries:
            return text
        return text[:parent.close] + '\n"CompatToolMapping"\n{\n' + entries + '\n}\n' + text[parent.close:]
    edits = []
    for key, value in changes.items():
        old = child(node.value, key)
        new = render_entry(key, value)
        if old:
            edits.append((old.start, old.end, new))
        elif value is not None:
            edits.append((node.close, node.close, '\n' + new + '\n'))
    for start, stop, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[stop:]
    parse(text)
    return text


class Binary:
    def __init__(self, data, table=None):
        self.f = io.BytesIO(data)
        self.table = table

    def read(self, n):
        b = self.f.read(n)
        if len(b) != n:
            raise ValueError('Truncated binary VDF')
        return b

    def unpack(self, fmt):
        return struct.unpack('<' + fmt, self.read(struct.calcsize('<' + fmt)))[0]

    def string(self):
        out = bytearray()
        while True:
            b = self.read(1)
            if b == b'\0':
                return out.decode('utf-8')
            out.extend(b)

    def object(self, depth=0):
        if depth > 64:
            raise ValueError('Binary VDF too deeply nested')
        result = {}
        while True:
            kind = self.unpack('B')
            if kind == 8:
                return result
            key = self.table[self.unpack('I')] if self.table is not None else self.string()
            key = key.lower()
            if kind == 0:
                value = self.object(depth + 1)
            elif kind == 1:
                value = self.string()
            elif kind in (2, 3, 4, 6, 7, 10):
                value = self.unpack({2: 'i', 3: 'f', 4: 'I', 6: 'I', 7: 'Q', 10: 'q'}[kind])
            else:
                raise ValueError(f'Unsupported binary VDF type {kind}')
            result[key] = value


def appinfo(path):
    b = Binary(path.read_bytes())
    magic = b.unpack('I')
    version = magic & 255
    if magic >> 8 != 0x075644 or version not in (39, 40, 41):
        raise ValueError('Unsupported appinfo format; no changes permitted')
    b.unpack('I')
    if version == 41:
        offset = b.unpack('Q')
        b.f.seek(offset)
        count = b.unpack('I')
        if count > 1000000:
            raise ValueError('Invalid string table')
        b.table = [b.string() for _ in range(count)]
        b.f.seek(16)
    result = {}
    while True:
        appid = b.unpack('I')
        if not appid:
            break
        size = b.unpack('I')
        end = b.f.tell() + size
        if end > len(b.f.getbuffer()):
            raise ValueError('Truncated appinfo record')
        b.read(40 + (20 if version >= 40 else 0))
        result[str(appid)] = b.object().get('appinfo', {})
        if b.f.tell() != end:
            raise ValueError('Invalid appinfo record boundary')
    return result


def steam_roots():
    candidates = [Path.home()/p for p in ('.local/share/Steam', '.steam/steam', '.steam/root', '.var/app/com.valvesoftware.Steam/data/Steam')]
    return sorted({p.resolve() for p in candidates if (p/'steamapps').is_dir()})


def inventory(root):
    libraries = {root}
    lf = root/'steamapps/libraryfolders.vdf'
    if lf.exists():
        for item in load_vdf(lf).get('libraryfolders', {}).values():
            if isinstance(item, dict) and 'path' in item:
                libraries.add(Path(item['path']))
    missing = [str(p) for p in libraries if not (p/'steamapps').is_dir()]
    games = {}
    for lib in sorted(libraries):
        for manifest in (lib/'steamapps').glob('appmanifest_*.acf'):
            state = load_vdf(manifest)['appstate']
            appid = state['appid']
            if not appid.isdecimal() or appid == '0':
                raise ValueError('Invalid app ID')
            games[appid] = state | {'library': str(lib)}
    return games, missing


def installed_tools(root, games, info):
    tools = {}
    # Steam publishes the official tool registry in a separate local metadata app.
    for meta in info.values():
        entries = meta.get('extended', {}).get('compat_tools', {})
        for name, tool in entries.items():
            state = games.get(str(tool.get('appid')))
            if state is None:
                continue
            path = Path(state['library'])/'steamapps/common'/state['installdir']
            if tool.get('from_oslist') == 'windows' and tool.get('to_oslist') == 'linux' and (path/'proton').is_file() and int(state.get('stateflags', '0')) & 4:
                tools[name] = {'name': name, 'display': tool.get('display_name', state['name'])}
    for parent in (root/'compatibilitytools.d', Path.home()/'.local/share/Steam/compatibilitytools.d', Path('/usr/share/steam/compatibilitytools.d')):
        for manifest in list(parent.glob('*/compatibilitytool.vdf')) + list(parent.glob('compatibilitytool.vdf')):
            tree = parse(manifest.read_text())
            container = child(tree, 'compatibilitytools')
            registry = child(container.value, 'compat_tools') if container else None
            for entry in registry.value if registry else []:
                name, tool = entry.key, values(entry.value)
                path = manifest.parent/tool.get('install_path', '.')
                if tool.get('from_oslist') == 'windows' and tool.get('to_oslist') == 'linux' and (path/'proton').is_file():
                    tools[name] = {'name': name, 'display': tool.get('display_name', name)}
    return tools


def gpu_vendor():
    vendors = set()
    for path in Path('/sys/class/drm').glob('card[0-9]*/device/vendor'):
        vendors.add({'0x10de': 'nvidia', '0x1002': 'amd', '0x8086': 'intel'}.get(path.read_text().strip(), 'unknown'))
    return next(iter(vendors)) if len(vendors) == 1 else 'unknown'


def report_vendor(report):
    gpu = report.get('device', {}).get('inferred', {}).get('steam', {}).get('gpu', '').lower()
    return next((v for v, words in [('nvidia', ('nvidia', 'geforce')), ('amd', ('amd', 'radeon')), ('intel', ('intel',))] if any(w in gpu for w in words)), 'unknown')


class ProtonDB:
    def __init__(self, cache, offline=False):
        self.cache = cache
        self.offline = offline
        self.counts = None

    def get(self, route, ttl=DAY):
        path = self.cache/(hashlib.sha256(route.encode()).hexdigest() + '.json')
        if path.exists() and time.time() - path.stat().st_mtime < ttl:
            return json.loads(path.read_text())
        if self.offline:
            raise ValueError('No fresh cached ProtonDB data (offline)')
        # No arbitrary URLs, redirects, telemetry, Steam credentials, or third-party hosts.
        if not route.startswith('/data/'):
            raise ValueError('Invalid ProtonDB route')
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                raise ValueError('ProtonDB redirect refused')
        request = urllib.request.Request('https://www.protondb.com' + route, headers={'User-Agent': 'Auto-Proton/' + VERSION})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=15) as response:
            raw = response.read(8_000_001)
        if len(raw) > 8_000_000:
            raise ValueError('ProtonDB response too large')
        data = json.loads(raw)
        atomic(path, raw)
        time.sleep(0.3)
        return data

    def reports(self, appid):
        if self.counts is None:
            self.counts = self.get('/data/counts.json', 3600)
        c = self.counts
        def r(e, t, n):
            return str(t) + 'p' + str(e * (t % n))
        token = 'p' + r(int(appid), c['reports'], c['timestamp']) + '*vRT' + r(1, int(appid), c['timestamp']) + 'undefinedm'
        h = 0
        for char in token:
            h = ((h << 5) - h + ord(char)) & 0xffffffff
        if h >= 2**31:
            h -= 2**32
        data = self.get(f'/data/reports/pc/app/{abs(h)}.json')
        if not isinstance(data, dict) or not isinstance(data.get('reports'), list):
            raise ValueError('ProtonDB detailed-report format changed')
        reports = data['reports']
        if any(str(row.get('responses', {}).get('answerToWhatGame', appid)) != appid for row in reports):
            raise ValueError('ProtonDB returned reports for another game')
        return reports


def version_key(name):
    return tuple(map(int, re.findall(r'\d+', name)))


def report_tool(response, tools):
    variant = response.get('variant')
    if variant == 'experimental':
        return 'proton_experimental' if 'proton_experimental' in tools else None
    if variant == 'ge':
        label = response.get('customProtonVersion', '')
        # Exact GE release only. A later release is not assumed to work.
        norm = lambda s: re.sub(r'(?i)(-x86_64)$', '', s).lower()
        return next((k for k, v in tools.items() if label and norm(label) in (norm(k), norm(v['display']))), None)
    if variant in ('official', 'older'):
        label = response.get('protonVersion', '')
        m = re.fullmatch(r'(?:Proton\s+)?(\d+)\.(\d+)(?:-\d+)?', label)
        if m:
            major, minor = m.groups()
            key = 'proton_' + (major if int(major) >= 7 or minor == '0' else major + minor)
            return key if key in tools else None
    return None


def recommend(reports, tools, vendor, now=None):
    now = now or time.time()
    votes = {}
    seen = set()
    considered = 0
    for row in sorted(reports, key=lambda r: r.get('timestamp', 0), reverse=True):
        stamp = row.get('timestamp', 0)
        if not isinstance(stamp, (int, float)) or not 0 <= now - stamp <= 180 * DAY:
            continue
        if row.get('device', {}).get('hardwareType') != 'pc':
            continue
        response = row.get('responses', {})
        # Require GPU-vendor agreement when local GPU is known.
        if vendor != 'unknown' and report_vendor(row) != vendor:
            continue
        tool = report_tool(response, tools)
        if not tool:
            continue
        contributor = row.get('contributor', {}).get('id')
        if not contributor or (tool, contributor) in seen:
            continue
        seen.add((tool, contributor))
        bucket = votes.setdefault(tool, {'positive': 0, 'negative': 0, 'latest': 0})
        considered += 1
        negative = response.get('verdict') == 'no' or any(response.get(k) == 'no' for k in ('opens', 'startsPlay'))
        # No launch options/tweaks are copied or executed. Such reports cannot prove out-of-box success.
        positive = (response.get('verdict') == 'yes' and not response.get('launchOptions', '').strip()
                    and response.get('tinkerOverride') != 'yes'
                    and response.get('verdictOob') != 'no'
                    and all(response.get(k) == 'no' for k in ('stabilityFaults', 'significantBugs')))
        if negative:
            bucket['negative'] += 1
        elif positive:
            bucket['positive'] += 1
            bucket['latest'] = max(bucket['latest'], stamp)
    eligible = [(k, v) for k, v in votes.items() if v['positive'] >= 2 and not v['negative']]
    if not eligible:
        return None, {'reason': 'No installed version has two recent independent clean reports without failures', 'votes': votes, 'considered': considered}
    # Prefer consensus; newest numbered stable release breaks equal-consensus ties.
    key, vote = max(eligible, key=lambda kv: (kv[1]['positive'], version_key(kv[0]), kv[1]['latest']))
    return key, {'reason': 'Recent independent clean PC reports; matching GPU vendor when known', 'votes': votes, 'considered': considered}


def atomic(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix='.auto-proton-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
        d = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(d)
        finally:
            os.close(d)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def save_json(path, obj):
    atomic(path, (json.dumps(obj, indent=2, sort_keys=True) + '\n').encode())


def steam_running():
    # All visible users, all Steam installations. Permission failures fail closed.
    for proc in Path('/proc').glob('[0-9]*'):
        try:
            comm = (proc/'comm').read_text().strip().lower()
            if comm in ('steam', 'steam.exe', 'steamwebhelper', 'steam-runtime-l'):
                return True
        except FileNotFoundError:
            continue
        except PermissionError:
            return True
    return False


def state_dir():
    return Path(os.environ.get('XDG_STATE_HOME', Path.home()/'.local/state'))/'auto-proton'


def cache_dir():
    return Path(os.environ.get('XDG_CACHE_HOME', Path.home()/'.cache'))/'auto-proton'


@contextlib.contextmanager
def locked(state):
    state.mkdir(parents=True, exist_ok=True)
    with (state/'lock').open('a') as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('Another Auto-Proton process is running')
        yield


def plan(root, db, state, only_appid=None):
    games, missing = inventory(root)
    info = appinfo(root/'appcache/appinfo.vdf')
    tools = installed_tools(root, games, info)
    current = mappings((root/'config/config.vdf').read_text())
    live_error = None
    live = False
    if steam_running():
        try:
            ids = [k for k in games if info.get(k, {}).get('common', {}).get('type', '').lower() in ('game', 'application')]
            current = live_mappings(root, ids + list(current))
            live = True
        except (ValueError, OSError, KeyError, TypeError) as exc:
            live_error = str(exc)
    owned_path = state/'owned.json'
    owned = json.loads(owned_path.read_text()) if owned_path.exists() else {}
    overrides = preferences.read(state)
    rows = []
    vendor = gpu_vendor()
    for appid, game in sorted(games.items(), key=lambda kv: kv[1]['name'].lower()):
        if only_appid and appid != only_appid:
            continue
        meta = info.get(appid, {})
        common = meta.get('common', {})
        row = {'appid': appid, 'name': game['name'], 'before': current.get(appid), 'action': 'skip'}
        native = 'linux' in common.get('oslist', '').lower().split(',')
        inferred_manual = not native and appid in owned and current.get(appid) != owned[appid]['after']
        manual = overrides.get(appid, inferred_manual)
        row.update(native=native, manual=manual, recommendation_known=False, recommended=None)
        if common.get('type', '').lower() not in ('game', 'application'):
            row['reason'] = 'Not a known game/application in local Steam metadata'
        elif not int(game.get('stateflags', '0')) & 4:
            row['reason'] = 'Installation incomplete'
        elif native:
            row['recommendation_known'] = True
            row['reason'] = 'Native Linux support; never force Proton'
            before = current.get(appid)
            name = before.get('name', '') if isinstance(before, dict) else ''
            if name and ('proton' in name.lower() or name in tools):
                row.update(action='set', after=None)
        else:
            try:
                reports = db.reports(appid)
                tool, evidence = recommend(reports, tools, vendor)
                row.update(evidence)
                if tool:
                    row['recommendation_known'] = True
                    after = {'name': tool, 'config': '', 'priority': '250'}
                    row['recommended'] = after
                    if current.get(appid) != after:
                        row.update(action='set', after=after)
                    else:
                        row['reason'] = 'Already uses recommended installed tool'
            except (ValueError, OSError, KeyError, TypeError) as exc:
                row['reason'] = 'ProtonDB unavailable/invalid: ' + str(exc)
        if manual:
            row.pop('after', None)
            row.update(action='skip', reason='Manual change detected; user choice preserved' if inferred_manual and appid not in overrides else 'Manual override enabled; user choice preserved')
        rows.append(row)
    if not missing and not only_appid:
        for appid, record in owned.items():
            if appid not in games and current.get(appid) == record['after']:
                rows.append({'appid': appid, 'name': '(uninstalled)', 'before': current.get(appid), 'after': record['before'], 'action': 'set', 'reason': 'Restore original setting for removed game'})
    result = {'version': VERSION, 'root': str(root), 'created': time.time(), 'gpu_vendor': vendor, 'missing_libraries': missing, 'live': live, 'live_error': live_error, 'tools': tools, 'games': rows}
    save_json(state/'plan.json', result)
    return result


def apply(root, result, state):
    if steam_running():
        if result.get('live'):
            return apply_live(root, result, state)
        return 'Steam is running: plan saved; live connection unavailable; changes deferred. ' + str(result.get('live_error') or '')
    overrides = preferences.read(state)
    changes = {r['appid']: r for r in result['games'] if r['action'] == 'set' and not overrides.get(r['appid'], False)}
    if not changes:
        return 'No compatibility changes needed.'
    config = root/'config/config.vdf'
    raw = config.read_bytes()
    current = mappings(raw.decode())
    if any(current.get(k) != row['before'] for k, row in changes.items()):
        raise ValueError('Steam settings changed since scan; rescan required')
    updated = edit_mapping(raw.decode(), {k: row['after'] for k, row in changes.items()}).encode()
    owned_path = state/'owned.json'
    owned = json.loads(owned_path.read_text()) if owned_path.exists() else {}
    for k, row in changes.items():
        owned[k] = {'before': owned.get(k, {}).get('before', row['before']), 'after': row['after']}
    # Durable backup + intent before modifying Steam, to recover a crash after replacement.
    atomic(state/'config-before-last-apply.vdf', raw)
    save_json(state/'transaction.json', {'sha256_before': hashlib.sha256(raw).hexdigest(), 'sha256_after': hashlib.sha256(updated).hexdigest(), 'owned': owned})
    if steam_running() or config.read_bytes() != raw:
        raise ValueError('Steam started or settings changed; write cancelled')
    mode = config.stat().st_mode & 0o777
    atomic(config, updated)
    os.chmod(config, mode)
    save_json(owned_path, owned)
    (state/'transaction.json').unlink()
    return f'Applied {len(changes)} compatibility changes.'


def live_mappings(root, ids):
    ws = live_steam.connect(root)
    try:
        return canonical_live(live_steam.snapshot(ws, [k for k in set(ids) if k.isdecimal() and k != '0']))
    finally:
        ws.close()


def canonical_live(snapshot):
    return {k: {'name': v['name'], 'config': '', 'priority': str(v['priority'])}
            for k, v in snapshot.items() if v.get('priority', 0) >= 250 and v.get('name')}


def recover_live(root, state, ws):
    path = state/'live-transaction.json'
    if not path.exists():
        return
    txn = json.loads(path.read_text())
    current = canonical_live(live_steam.snapshot(ws, [txn['appid']])).get(txn['appid'])
    if current == txn['after']:
        save_json(state/'owned.json', txn['owned'])
    elif current != txn['before']:
        raise ValueError('Interrupted live change conflicts with current Steam choice; inspect live-transaction.json')
    path.unlink()


def apply_live(root, result, state):
    rows = [r for r in result['games'] if r['action'] == 'set']
    if not rows:
        return 'No compatibility changes needed.'
    ws = live_steam.connect(root)
    try:
        recover_live(root, state, ws)
        path = state/'owned.json'
        owned = json.loads(path.read_text()) if path.exists() else {}
        applied = 0
        for row in rows:
            appid = row['appid']
            if preferences.read(state).get(appid, False):
                continue
            current = canonical_live(live_steam.snapshot(ws, [appid])).get(appid)
            if current != row['before']:
                raise ValueError('Steam choice changed since scan for '+appid+'; rescan required')
            after = row['after']
            updated_owned = owned | {appid: {'before': owned.get(appid, {}).get('before', current), 'after': after}}
            save_json(state/'live-transaction.json', {'appid': appid, 'before': current, 'after': after, 'owned': updated_owned})
            try:
                live_steam.set_tool(ws, appid, after['name'] if after else '')
                actual = canonical_live(live_steam.snapshot(ws, [appid])).get(appid)
                if actual != after:
                    raise ValueError('Steam returned an unexpected compatibility choice')
            except (OSError, ValueError, KeyError, TypeError):
                # Undo a rejected/unrecognized tool rather than silently leaving Steam at a fallback.
                live_steam.set_tool(ws, appid, current['name'] if current else '')
                if canonical_live(live_steam.snapshot(ws, [appid])).get(appid) == current:
                    (state/'live-transaction.json').unlink()
                raise
            save_json(path, updated_owned)
            owned = updated_owned
            (state/'live-transaction.json').unlink()
            applied += 1
        return f'Applied and verified {applied} live Steam compatibility changes. Running games use them on the next launch.'
    finally:
        ws.close()


def recover(root, state):
    live_path = state/'live-transaction.json'
    if live_path.exists():
        if steam_running():
            ws = live_steam.connect(root)
            try:
                recover_live(root, state, ws)
            finally:
                ws.close()
        else:
            txn = json.loads(live_path.read_text())
            current = mappings((root/'config/config.vdf').read_text()).get(txn['appid'])
            if current == txn['after']:
                save_json(state/'owned.json', txn['owned'])
            elif current != txn['before']:
                raise ValueError('Interrupted live change needs review; inspect live-transaction.json')
            live_path.unlink()
    path = state/'transaction.json'
    if not path.exists():
        return
    txn = json.loads(path.read_text())
    digest = hashlib.sha256((root/'config/config.vdf').read_bytes()).hexdigest()
    if digest == txn['sha256_after']:
        save_json(state/'owned.json', txn['owned'])
    elif digest != txn['sha256_before']:
        raise ValueError('Unresolved interrupted transaction: inspect transaction.json and Steam config before continuing')
    path.unlink()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('scan', 'sync', 'status', 'restore', 'ui', 'override'))
    parser.add_argument('--steam-root', type=Path)
    parser.add_argument('--mode', choices=('auto', 'manual'), help='Mode for the override command')
    parser.add_argument('--offline', action='store_true')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--appid', help='Only manage this app ID during this run')
    args = parser.parse_args(argv)
    state = state_dir()
    try:
        roots = steam_roots() if args.steam_root is None else [args.steam_root.resolve()]
        if len(roots) != 1:
            raise ValueError('Specify --steam-root: expected exactly one Steam installation')
        root = roots[0]
        if args.command == 'ui':
            import ui_daemon
            return ui_daemon.run(root, state)
        if args.command == 'override':
            if not args.appid or not args.mode:
                raise ValueError('override needs --appid and --mode auto|manual')
            preferences.set_manual(state, args.appid, args.mode == 'manual')
            print('Override saved for ' + args.appid + ': ' + args.mode)
            return 0
        # State is tied to one installation so a future different root cannot inherit ownership.
        with locked(state):
            binding = state/'steam-root.json'
            if binding.exists() and json.loads(binding.read_text()) != str(root):
                raise ValueError('This installation manages a different Steam root')
            save_json(binding, str(root))
            recover(root, state)
            if args.command == 'status':
                result = json.loads((state/'plan.json').read_text())
            elif args.command == 'restore':
                owned_path = state/'owned.json'
                owned = json.loads(owned_path.read_text()) if owned_path.exists() else {}
                current = mappings((root/'config/config.vdf').read_text())
                live = steam_running()
                if live:
                    current = live_mappings(root, list(owned))
                result = {'live': live, 'games': [{'appid': k, 'name': k, 'action': 'set', 'before': current.get(k), 'after': v['before']} for k, v in owned.items() if current.get(k) == v['after']]}
                print(apply(root, result, state))
                save_json(owned_path, {})
                return 0
            else:
                result = plan(root, ProtonDB(cache_dir(), args.offline), state, args.appid)
                if args.appid:
                    result['games'] = [r for r in result['games'] if r['appid'] == args.appid]
                    save_json(state/'plan.json', result)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                for row in result['games']:
                    target = row.get('after')
                    label = target['name'] if target else 'native/original'
                    print(f"{row['appid']:>8}  {row['name']}: {row['action']} {label if row['action'] == 'set' else ''} — {row.get('reason', '')}")
            if args.command == 'sync':
                result['last_result'] = apply(root, result, state)
                result['finished'] = time.time()
                save_json(state/'plan.json', result)
                print(result['last_result'])
            elif args.command == 'status' and result.get('last_result'):
                print(result['last_result'])
        return 0
    except (OSError, ValueError, KeyError, TypeError, IndexError, struct.error, RecursionError) as exc:
        print('Auto-Proton: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
