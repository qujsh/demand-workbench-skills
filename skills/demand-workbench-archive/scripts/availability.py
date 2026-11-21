"""MCP可用性门禁：2秒整次检查、60秒离线缓存和一次性在线凭据，不接收问答内容。"""
import hashlib
import errno
import socket
from urllib.parse import urlsplit
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


def local_port_status(url):
    """仅回环地址做150ms内TCP探测，不发HTTP、凭据或问答；不能确认拒绝则返回unknown。"""
    try:
        parsed = urlsplit(url)
    except ValueError:
        return 'unknown'
    if parsed.scheme not in ('http', 'https'):
        return 'unknown'
    hosts = {'localhost': ['127.0.0.1', '::1'], '127.0.0.1': ['127.0.0.1'], '::1': ['::1']}.get(parsed.hostname)
    if not hosts:
        return 'unknown'
    try:
        port = parsed.port or (443 if parsed.scheme == 'https' else 80)
    except ValueError:
        return 'unknown'
    deadline = time.monotonic() + 0.15
    refused = 0
    for host in hosts:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return 'unknown'
        family = socket.AF_INET6 if ':' in host else socket.AF_INET
        try:
            with socket.socket(family, socket.SOCK_STREAM) as connection:
                connection.settimeout(remaining)
                connection.connect((host, port))
                return 'open'
        except OSError as error:
            if error.errno != errno.ECONNREFUSED:
                return 'unknown'
            refused += 1
    return 'closed' if refused == len(hosts) else 'unknown'


def check_status(config, timeout=PROBE_TIMEOUT):
    started = time.monotonic()
    if local_port_status(config['url']) == 'closed':
        return {'ready': False, 'reason': 'local_port_closed'}
    try:
        result = subprocess.run([sys.executable, str(Path(__file__).with_name('archive.py')), 'check-worker'],
                                cwd=os.getcwd(), capture_output=True, text=True,
                                timeout=max(0.001, timeout - (time.monotonic() - started)))
        ready = result.returncode == 0 and json.loads(result.stdout).get('ready') is True
        return {'ready': ready, 'reason': 'unavailable'}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'ready': False, 'reason': 'unavailable'}


def check_once(config):
    return check_status(config)['ready']


def probe(config, checker=None, now=None, force=False):
    started = time.monotonic()
    if not config.get('enabled'):
        return {'ready': False, 'reason': 'disabled', 'cached': False}
    moment = time.time() if now is None else now
    cache = directory(config) / 'offline.json'
    saved, remaining = {}, 0
    try:
        saved = json.loads(cache.read_text())
        remaining = saved.get('until', 0) - moment
    except (OSError, ValueError, TypeError):
        saved, remaining = {}, 0
    if remaining > 0 and not force:
        recovery = saved.get('reason') == 'local_port_closed' and local_port_status(config['url']) == 'open'
        if not recovery:
            return {'ready': False, 'reason': saved.get('reason', 'unavailable'),
                    'cached': True, 'retry_after': math.ceil(remaining)}
    status = {'ready': bool(checker(config)), 'reason': 'unavailable'} if checker else check_status(config, timeout=max(0.001, PROBE_TIMEOUT - (time.monotonic() - started)))
    if not status['ready']:
        # 提前恢复或强制检查失败不延长既有截止时间；原因更新防止端口已开但鉴权失败时反复握手。
        if remaining > 0:
            write_private(cache, {'until': saved['until'], 'reason': status['reason']})
        else:
            note_failure(config, reason=status['reason'], now=now)
        return {'ready': False, 'reason': status['reason'], 'cached': False,
                'retry_after': math.ceil(remaining) if remaining > 0 else OFFLINE_TTL}
    cache.unlink(missing_ok=True)
    token = uuid.uuid4().hex
    write_private(cache.parent / ('ready-' + token + '.json'), {'created': moment, 'project': config['state_dir']})
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
