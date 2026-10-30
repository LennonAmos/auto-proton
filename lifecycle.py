#!/usr/bin/env python3
"""Install/remove only Auto-Proton's per-user components; never edit Steam here."""
import argparse
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import fcntl

MARKER = 'auto-proton-install-v1'
PROGRAM_FILES = ('auto_proton.py', 'live_steam.py', 'lifecycle.py', 'preferences.py', 'ui_daemon.py', 'ui.js')


def locations():
    home = Path.home()
    return {
        'app': Path(os.environ.get('XDG_DATA_HOME', home/'.local/share'))/'auto-proton',
        'state': Path(os.environ.get('XDG_STATE_HOME', home/'.local/state'))/'auto-proton',
        'cache': Path(os.environ.get('XDG_CACHE_HOME', home/'.cache'))/'auto-proton',
        'units': Path(os.environ.get('XDG_CONFIG_HOME', home/'.config'))/'systemd/user',
        'bin': home/'.local/bin',
    }


def control(*args, check=True):
    return subprocess.run(['systemctl', '--user', *args], check=check, capture_output=True, text=True)


def unit_quote(s):
    return '"' + str(s).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%') + '"'


def write(path, content, mode=0o600):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(dir=path.parent, prefix='.auto-proton-')
    try:
        with os.fdopen(fd, 'w') as file:
            file.write(content)
        os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def ui_unit(launcher, paths):
    env = ''.join('Environment=' + unit_quote(k + '=' + str(v.parent)) + '\n' for k, v in [('XDG_STATE_HOME', paths['state']), ('XDG_CACHE_HOME', paths['cache'])])
    return ('[Unit]\nDescription=Auto-Proton Steam compatibility controls\n[Service]\nType=simple\nExecStart='
            + unit_quote(launcher) + ' ui\n' + env + 'Restart=on-failure\nRestartSec=3\nTimeoutStopSec=12\nUMask=0077\nNoNewPrivileges=true\nStandardOutput=null\nStandardError=null\n[Install]\nWantedBy=default.target\n')


def upgrade(root=None):
    current = locations()
    receipt = current['app']/'install.json'
    if not receipt.exists():
        raise ValueError('Destination exists without a valid installation receipt')
    data = json.loads(receipt.read_text())
    if data.get('marker') != MARKER:
        raise ValueError('Invalid installation receipt')
    p = {k: Path(v) for k, v in data['paths'].items()}
    if p['app'].resolve() != current['app'].resolve():
        raise ValueError('Installation path mismatch')
    bound = p['state']/'steam-root.json'
    installed_root = data.get('steam_root') or (json.loads(bound.read_text()) if bound.exists() else None)
    if root and installed_root and str(root.resolve()) != installed_root:
        raise ValueError('Upgrade must use the installed Steam root')
    source = Path(__file__).resolve().parent
    unit = p['units']/'auto-proton-ui.service'
    targets = [p['app']/name for name in PROGRAM_FILES] + [unit, receipt]
    backups = {path: (path.read_text(), path.stat().st_mode & 0o777) if path.exists() else None for path in targets}
    try:
        if unit.exists():
            control('stop', 'auto-proton-ui.service')
        for name in PROGRAM_FILES:
            write(p['app']/name, (source/name).read_text())
        write(unit, ui_unit(p['bin']/'auto-proton', p))
        data.update(version='0.2.0', steam_root=installed_root)
        write(receipt, json.dumps(data))
        control('daemon-reload')
        control('enable', '--now', 'auto-proton.timer', 'auto-proton-ui.service')
        print('Updated Auto-Proton. Per-game overrides, existing choices, and ownership records were preserved.')
    except Exception:
        control('stop', 'auto-proton-ui.service', check=False)
        if backups[unit] is None:
            control('disable', 'auto-proton-ui.service', check=False)
        for path, old in backups.items():
            if old is None:
                path.unlink(missing_ok=True)
            else:
                write(path, *old)
        control('daemon-reload', check=False)
        if backups[unit] is not None:
            control('start', 'auto-proton-ui.service', check=False)
        raise


def install(root=None):
    p = locations()
    if p['app'].exists():
        return upgrade(root)
    if not shutil.which('systemctl'):
        raise ValueError('Automatic monitoring needs systemd user services. Use auto_proton.py scan/sync manually otherwise.')
    control('show-environment')
    files = [p['bin']/'auto-proton', p['bin']/'auto-proton-uninstall', p['units']/'auto-proton.service', p['units']/'auto-proton.timer', p['units']/'auto-proton-ui.service']
    if any(f.exists() or f.is_symlink() for f in files):
        raise ValueError('An Auto-Proton launcher or unit already exists; refusing to overwrite')
    if root is None:
        from auto_proton import steam_roots
        roots = steam_roots()
        if len(roots) != 1:
            raise ValueError('Specify --steam-root: expected exactly one Steam installation')
        root = roots[0]
    if root:
        root = root.expanduser().resolve()
        if not (root/'config/config.vdf').is_file():
            raise ValueError('Steam root must contain config/config.vdf')
    created = []
    debug_marker = root/'.cef-enable-remote-debugging'
    created_marker = False
    source = Path(__file__).resolve().parent
    try:
        p['app'].mkdir(parents=True)
        for name in PROGRAM_FILES:
            write(p['app']/name, (source/name).read_text())
        if not debug_marker.exists():
            with debug_marker.open('x'):
                pass
            created_marker = True
        write(p['app']/'install.json', json.dumps({'marker': MARKER, 'paths': {k: str(v) for k, v in p.items()}, 'debug_marker': str(debug_marker) if created_marker else None, 'steam_root': str(root), 'version': '0.2.0'}))
        prefix = '#!/bin/sh\nexec ' + shlex.quote(sys.executable) + ' -B '
        cmd = prefix + shlex.quote(str(p['app']/'auto_proton.py')) + ' "$@"'
        if root:
            cmd += ' --steam-root ' + shlex.quote(str(root))
        write(files[0], cmd + '\n', 0o755)
        created.append(files[0])
        write(files[1], prefix + shlex.quote(str(p['app']/'lifecycle.py')) + ' uninstall "$@"\n', 0o755)
        created.append(files[1])
        env = ''.join('Environment=' + unit_quote(k + '=' + str(v.parent)) + '\n' for k, v in [('XDG_STATE_HOME', p['state']), ('XDG_CACHE_HOME', p['cache'])])
        write(files[2], '[Unit]\nDescription=Auto-Proton per-game compatibility sync\n[Service]\nType=oneshot\nExecStart=' + unit_quote(files[0]) + ' sync\n' + env + 'UMask=0077\nNoNewPrivileges=true\nStandardOutput=null\nStandardError=null\nTimeoutStartSec=20min\n')
        created.append(files[2])
        write(files[3], '[Unit]\nDescription=Check installed Steam games every two minutes\n[Timer]\nOnActiveSec=10s\nOnUnitInactiveSec=2min\nAccuracySec=10s\n[Install]\nWantedBy=timers.target\n')
        created.append(files[3])
        write(files[4], ui_unit(files[0], p))
        created.append(files[4])
        control('daemon-reload')
        control('enable', '--now', 'auto-proton.timer', 'auto-proton-ui.service')
        print('Installed. Automatic per-game sync is enabled every two minutes; live choices apply to the next game launch.')
        if created_marker:
            print('Live mode needs one normal Steam restart to enable its local debugging connection. Steam has not been stopped.')
        print('Inspect: ~/.local/bin/auto-proton status')
        print('Remove everything installed here: ~/.local/bin/auto-proton-uninstall')
    except Exception:
        control('disable', '--now', 'auto-proton.timer', check=False)
        control('disable', '--now', 'auto-proton-ui.service', check=False)
        control('stop', 'auto-proton.service', check=False)
        for f in reversed(created):
            f.unlink(missing_ok=True)
        shutil.rmtree(p['app'])
        if created_marker:
            debug_marker.unlink(missing_ok=True)
        control('daemon-reload', check=False)
        raise


def uninstall():
    # Receipt captures XDG paths at installation, even if the environment later changes.
    own = Path(__file__).resolve().parent
    receipt = own/'install.json'
    if not receipt.exists():
        own = locations()['app']
        receipt = own/'install.json'
    if not receipt.exists():
        print('Auto-Proton is not installed.')
        return
    data = json.loads(receipt.read_text())
    if data.get('marker') != MARKER:
        raise ValueError('Invalid installation receipt; refusing removal')
    p = {k: Path(v) for k, v in data['paths'].items()}
    if p['app'].resolve() != own.resolve():
        raise ValueError('Installation path mismatch')
    if any(p[k].name != 'auto-proton' or p[k].is_symlink() for k in ('app', 'cache', 'state')):
        raise ValueError('Unsafe installation paths')
    # Stop only our timer/service. Steam and all other services are untouched.
    control('disable', '--now', 'auto-proton.timer')
    if (p['units']/'auto-proton-ui.service').exists():
        control('disable', '--now', 'auto-proton-ui.service')
    control('stop', 'auto-proton.service')
    p['state'].mkdir(parents=True, exist_ok=True)
    with (p['state']/'lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ValueError('A manual Auto-Proton command is running; let it finish and retry uninstall')
        for name in ('auto-proton.service', 'auto-proton.timer', 'auto-proton-ui.service'):
            (p['units']/name).unlink(missing_ok=True)
        control('daemon-reload')
        control('reset-failed', 'auto-proton.service', 'auto-proton.timer', 'auto-proton-ui.service', check=False)
        for name in ('auto-proton', 'auto-proton-uninstall'):
            (p['bin']/name).unlink(missing_ok=True)
        marker = data.get('debug_marker')
        if marker:
            marker = Path(marker)
            if marker.name != '.cef-enable-remote-debugging':
                raise ValueError('Invalid owned debugger marker')
            if marker.exists() and not marker.is_symlink() and marker.read_bytes() == b'':
                marker.unlink()
        for key in ('cache', 'state', 'app'):
            if p[key].exists():
                shutil.rmtree(p[key])
    print('Removed Auto-Proton code, launchers, timer, service, cache, plans, ownership records, and backups.')
    print('Steam compatibility settings were not modified. Existing compatibility choices are kept. Your source checkout/download remains yours.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('install', 'uninstall'))
    parser.add_argument('--steam-root', type=Path)
    args = parser.parse_args()
    if sys.version_info < (3, 10):
        parser.error('Python 3.10 or newer is required')
    try:
        install(args.steam_root) if args.command == 'install' else uninstall()
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print('Auto-Proton: ' + str(exc), file=sys.stderr)
        if isinstance(exc, subprocess.CalledProcessError):
            print(exc.stderr.strip(), file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
