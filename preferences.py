"""Explicit per-game manual controls, shared by the UI and sync process."""
import fcntl
import json
import os
from pathlib import Path
import tempfile


def read(state):
    path = state/'overrides.json'
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    if not isinstance(data, dict) or any(not k.isdecimal() or k == '0' or type(v) is not bool for k, v in data.items()):
        raise ValueError('Invalid per-game override settings')
    return data


def set_manual(state, appid, enabled):
    appid = str(appid)
    if not appid.isdecimal() or appid == '0' or type(enabled) is not bool:
        raise ValueError('Invalid manual override')
    state.mkdir(parents=True, exist_ok=True)
    with (state/'overrides.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        data = read(state)
        data[appid] = enabled
        fd, name = tempfile.mkstemp(dir=state, prefix='.auto-proton-')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(data, f, sort_keys=True)
                f.write('\n')
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, state/'overrides.json')
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return data
