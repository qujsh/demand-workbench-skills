"""当前会话的只读元数据适配：不加载历史消息正文，不按内容相似度匹配。"""
import contextlib
import fcntl
import json
import os
from pathlib import Path
import selectors
import shutil
import sqlite3
import subprocess
import time
import uuid


class NativeEditError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)
    def detail(self):
        return {'code': self.code, 'requestNo': ''}


class Bridge:
    """独立 app-server 只读访问持久化历史；绝不 resume/start 正在使用的会话。"""
    def __init__(self, binary, env=None, timeout=4):
        self.deadline = time.monotonic() + timeout
        self.process = subprocess.Popen([binary, 'app-server'], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=env)
        self.selector = selectors.DefaultSelector()
        self.selector.register(self.process.stdout, selectors.EVENT_READ)
        self.buffer = b''
        self.sequence = 0
        self.events = []
    def close(self):
        self.selector.close()
        self.process.terminate()
        try:
            self.process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
        self.process.stdin.close()
        self.process.stdout.close()
    def request(self, method, params):
        self.sequence += 1
        self.process.stdin.write((json.dumps({'id': self.sequence, 'method': method, 'params': params}) + '\n').encode())
        self.process.stdin.flush()
        while time.monotonic() < self.deadline:
            while b'\n' in self.buffer:
                raw, self.buffer = self.buffer.split(b'\n', 1)
                value = json.loads(raw)
                if value.get('id') == self.sequence:
                    if 'error' in value:
                        raise NativeEditError('NATIVE_METADATA_UNAVAILABLE')
                    return value['result']
                # Only needed by isolated integration checks. Never persisted or uploaded.
                self.events.append(value.get('method'))
            if len(self.buffer) > 4 * 1024 * 1024:
                break
            if not self.selector.select(max(0, self.deadline - time.monotonic())):
                break
            chunk = os.read(self.process.stdout.fileno(), 65536)
            if not chunk:
                break
            self.buffer += chunk
        raise NativeEditError('NATIVE_METADATA_UNAVAILABLE')
    def initialize(self):
        self.request('initialize', {'clientInfo': {'name': 'workbench-metadata', 'version': '0.1'},
                                   'capabilities': {'experimentalApi': True}})
    def turns(self, session):
        ids, cursor, cursors = [], None, set()
        for _ in range(100):
            params = {'threadId': session, 'limit': 100, 'itemsView': 'notLoaded', 'sortDirection': 'desc'}
            if cursor:
                params['cursor'] = cursor
            result = self.request('thread/turns/list', params)
            ids.extend(t['id'] for t in result['data'])
            cursor = result.get('nextCursor')
            if not cursor:
                if len(ids) != len(set(ids)):
                    break
                return list(reversed(ids))
            if cursor in cursors:
                break
            cursors.add(cursor)
        raise NativeEditError('NATIVE_METADATA_UNAVAILABLE')


def metadata(config, api):
    session = os.environ.get('CODEX_THREAD_ID')
    if not session:
        raise NativeEditError('NATIVE_METADATA_UNAVAILABLE')
    home = api['CODEX_DIR'].resolve()
    # Exact current-session lookup only, no thread listing or title/content lookup.
    try:
        with sqlite3.connect('file:' + str(home / 'state_5.sqlite') + '?mode=ro', uri=True) as db:
            row = db.execute('SELECT rollout_path FROM threads WHERE id=?', (session,)).fetchone()
        path = Path(row[0]).resolve() if row else None
        if not path or home not in path.parents:
            raise NativeEditError('NATIVE_METADATA_UNAVAILABLE')
        current = None
        with path.open() as stream:
            for line in stream:
                # Ignore content-bearing item lines before JSON decoding.
                if '"task_started"' not in line and '"turn_started"' not in line:
                    continue
                event = json.loads(line)
                q = event.get('payload', {})
                if event.get('type') == 'event_msg' and q.get('type') in ('task_started', 'turn_started'):
                    attribution = q.get('turn_attribution') or {}
                    current = (q.get('turn_id'), attribution.get('turn_trigger'))
        if not current or current[1] not in ('composer', 'edit_user_message'):
            raise NativeEditError('NATIVE_TRIGGER_UNSUPPORTED')
        bundled = '/Applications/ChatGPT.app/Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex'
        binary = config.get('codex_binary') or (bundled if Path(bundled).exists() else shutil.which('codex'))
        if not binary:
            raise NativeEditError('NATIVE_METADATA_UNAVAILABLE')
        with contextlib.closing(Bridge(binary)) as bridge:
            bridge.initialize()
            ids = bridge.turns(session)
        if not ids or ids[-1] != current[0]:
            raise NativeEditError('NATIVE_SNAPSHOT_STALE')
        return {'session': session, 'turn': current[0], 'trigger': current[1], 'ids': ids}
    except NativeEditError:
        raise
    except (OSError, ValueError, KeyError, sqlite3.Error, TypeError) as error:
        raise NativeEditError('NATIVE_METADATA_UNAVAILABLE') from error


def choose(previous, snapshot, bindings):
    """只接受精确有效历史的前缀关系；较早编辑定位第一个被撤回轮次，不取最后一条。"""
    ids, turn = snapshot['ids'], snapshot['turn']
    if not ids or ids[-1] != turn or len(ids) != len(set(ids)):
        raise NativeEditError('NATIVE_SNAPSHOT_STALE')
    if snapshot['trigger'] not in ('composer', 'edit_user_message'):
        raise NativeEditError('NATIVE_TRIGGER_UNSUPPORTED')
    if turn in bindings:
        return bindings[turn]
    if previous is None:
        if snapshot['trigger'] == 'edit_user_message':
            raise NativeEditError('NATIVE_EDIT_BASELINE_MISSING')
        return uuid.uuid4().hex
    common = 0
    while common < min(len(previous), len(ids)) and previous[common] == ids[common]:
        common += 1
    if snapshot['trigger'] == 'composer':
        return uuid.uuid4().hex
    if (snapshot['trigger'] == 'edit_user_message' and common < len(previous)
            and ids[common:] == [turn] and previous[common] in bindings):
        return bindings[previous[common]]
    raise NativeEditError('NATIVE_EDIT_TARGET_UNKNOWN')


def tables(db):
    db.execute('CREATE TABLE IF NOT EXISTS native_snapshots (endpoint TEXT, session TEXT, ids TEXT NOT NULL, PRIMARY KEY(endpoint,session))')
    db.execute('CREATE TABLE IF NOT EXISTS native_bindings (endpoint TEXT, session TEXT, turn TEXT, entry TEXT NOT NULL, kind TEXT NOT NULL DEFAULT "qa", PRIMARY KEY(endpoint,session,turn))')


def queue(value, config, api, numbered, snapshot=None):
    snapshot = snapshot or metadata(config, api)
    if (value.get('session_id') or os.environ.get('CODEX_THREAD_ID')) != snapshot['session']:
        raise NativeEditError('NATIVE_SNAPSHOT_STALE')
    journal = value.get('mode') == 'journal'
    text = value.get('journal_text') if journal else None
    if not journal:
        question, answer = value.get('question'), value.get('answer')
        if not isinstance(question, str) or not question.strip() or not isinstance(answer, str) or not answer.strip():
            raise ValueError('question和answer必须非空')
        text = '用户问题：\n' + question + '\n\nCodex 最终回答：\n' + answer
    if not isinstance(text, str) or not text.strip():
        raise ValueError('日志正文必须非空')
    root = Path(config['state_dir']); root.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(root / 'native-edit.lock', os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, 'w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        session, endpoint = snapshot['session'], config['url']
        with api['database'](config) as db, db:
            tables(db)
            row = db.execute('SELECT ids FROM native_snapshots WHERE endpoint=? AND session=?', (endpoint, session)).fetchone()
            previous = json.loads(row['ids']) if row else None
            bindings = dict(db.execute('SELECT turn,entry FROM native_bindings WHERE endpoint=? AND session=?', (endpoint, session)))
        explicit_update = journal and value.get('operation') == 'update'
        if explicit_update:
            target = value.get('entry_id')
            with api['database'](config) as db:
                logs = dict(db.execute("SELECT turn,entry FROM native_bindings WHERE endpoint=? AND session=? AND kind='journal'", (endpoint, session)))
            entry = next((logs[t] for t in reversed(previous or []) if t in logs), None) if target == 'latest' else target
            if not entry or entry not in logs.values():
                raise NativeEditError('NATIVE_EDIT_TARGET_UNKNOWN')
        else:
            entry = choose(previous, snapshot, bindings)
        # The binding and snapshot are committed atomically with the background job.
        def commit(db, payload):
            tables(db)
            payload['native'] = {'turn': snapshot['turn'], 'trigger': snapshot['trigger'],
                                 'requires_existing': explicit_update or snapshot['trigger'] == 'edit_user_message'}
            db.execute('INSERT OR REPLACE INTO native_snapshots VALUES (?,?,?)', (endpoint, session, json.dumps(snapshot['ids'])))
            db.execute('INSERT OR REPLACE INTO native_bindings VALUES (?,?,?,?,?)', (endpoint, session, snapshot['turn'], entry, 'journal' if journal else 'qa'))
        data = {**value, 'submission_id': snapshot['turn']}
        result = numbered.queue(data, config, api, internal=(entry, text, not journal), on_enqueue=commit)
        result['native_edit'] = explicit_update or snapshot['trigger'] == 'edit_user_message'
        return result
