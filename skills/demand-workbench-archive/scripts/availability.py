"""MCP可用性门禁：2秒整次检查、60秒离线缓存和一次性在线凭据，不接收问答内容。"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import uuid

PROBE_TIMEOUT = 2
OFFLINE_TTL = 60
TICKET_TTL = 120


def directory(config):
    root = Path(config.get('availability_dir', str(Path(config['state_dir']).parent / 'availability')))
    root = root / hashlib.sha256(config['url'].encode()).hexdigest()[:24]
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(root, 0o700)
    return root


def write_private(path, value):
    temporary = path.with_name(path.name + '.' + uuid.uuid4().hex + '.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream)
    temporary.replace(path)


def note_failure(config, reason='unavailable', now=None):
    moment = time.time() if now is None else now
    write_private(directory(config) / 'offline.json', {'until': moment + OFFLINE_TTL, 'reason': reason})


def check_once(config):
    # 整个子进程（含钥匙串、握手和工具检查）共用2秒上限，超时即终止。
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('archive.py')), 'check-worker'],
                                cwd=os.getcwd(), capture_output=True, text=True, timeout=PROBE_TIMEOUT)
        return result.returncode == 0 and json.loads(result.stdout).get('ready') is True
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return False


def probe(config, checker=None, now=None):
    if not config.get('enabled'):
        return {'ready': False, 'reason': 'disabled', 'cached': False}
    moment = time.time() if now is None else now
    root = directory(config)
    cache = root / 'offline.json'
    try:
        saved = json.loads(cache.read_text())
        remaining = saved.get('until', 0) - moment
        if remaining > 0:
            return {'ready': False, 'reason': saved.get('reason', 'unavailable'),
                    'cached': True, 'retry_after': math.ceil(remaining)}
    except (OSError, ValueError, TypeError):
        pass
    if not (checker or check_once)(config):
        # 从失败被观察到的时刻起缓存60秒；命中缓存不延长过期时间。
        note_failure(config, now=now)
        return {'ready': False, 'reason': 'unavailable', 'cached': False, 'retry_after': OFFLINE_TTL}
    cache.unlink(missing_ok=True)
    token = uuid.uuid4().hex
    write_private(root / ('ready-' + token + '.json'), {'created': moment, 'project': config['state_dir']})
    return {'ready': True, 'cached': False, 'availability_token': token}


def consume_ticket(config, token, now=None):
    if not isinstance(token, str) or not re.fullmatch(r'[a-f0-9]{32}', token):
        return False
    path = directory(config) / ('ready-' + token + '.json')
    used = path.with_suffix('.used')
    try:
        path.rename(used)
    except OSError:
        return False
    try:
        saved = json.loads(used.read_text())
        age = (time.time() if now is None else now) - saved['created']
        return saved['project'] == config['state_dir'] and 0 <= age <= TICKET_TTL
    except (OSError, ValueError, KeyError, TypeError):
        return False
    finally:
        used.unlink(missing_ok=True)
