#!/usr/bin/env python3
"""跨项目 Codex 问答归档：本地入队、私密图文上传，不依赖业务仓库。"""
import argparse
import contextlib
from datetime import datetime, timezone
import fcntl
import getpass
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time
import uuid

SKILL_ROOT = Path(__file__).resolve().parents[1]
CODEX_DIR = Path(os.environ.get('CODEX_HOME', str(Path.home() / '.codex'))).expanduser()


def project_root():
    directory = Path.cwd().resolve()
    try:
        result = subprocess.run(['git', 'rev-parse', '--show-toplevel'], cwd=str(directory),
                                capture_output=True, text=True, timeout=5)
        if result.returncode == 0:
            return Path(result.stdout.strip()).resolve()
    except (OSError, subprocess.TimeoutExpired):
        pass
    return directory


ROOT = project_root()
CONFIG = CODEX_DIR / 'demand-workbench-archive/config.json'
KEY_SERVICE = 'demand-workbench-codex-archive'
_spec = importlib.util.spec_from_file_location('archive_availability', SKILL_ROOT / 'scripts/availability.py')
AVAILABILITY = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(AVAILABILITY)


def probe(config):
    return AVAILABILITY.probe(config)



def redact(text):
    """落盘前清除已知令牌、私钥及带敏感字段的赋值；不承诺识别所有自然语言秘密。"""
    text = re.sub(r'-----BEGIN [^-]*PRIVATE KEY-----.*?-----END [^-]*PRIVATE KEY-----',
                  '[已隐藏私钥]', str(text), flags=re.S)
    text = re.sub(r'\b(?:dw_sk_|sk-)[A-Za-z0-9_-]+', '[已隐藏密钥]', text)
    text = re.sub(r'(?i)\bBearer\s+[A-Za-z0-9_.~+/-]+=*', 'Bearer [已隐藏]', text)
    text = re.sub(r'''(?ix)(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|secret|authorization|cookie|密码|密钥|令牌)["']?\s*[:=：]\s*)(?:"[^"\n]*"|'[^'\n]*'|[^\s,;\}\n]+)''',
                  lambda match: match[1] + '[已隐藏]', text)
    text = re.sub(r'(?i)(https?://)[^\s/@:]+:[^\s/@]+@', r'\1[已隐藏]@', text)
    return text


def settings():
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    config.setdefault('enabled', False)
    config.setdefault('url', os.environ.get('DEMAND_WORKBENCH_MCP_URL') or 'https://api.freeligo.com/mcp')
    config['upload_helper'] = str(SKILL_ROOT / 'scripts/mcp_client.py')
    config['mode'] = 'assistant'
    config['availability_dir'] = str(CODEX_DIR / 'demand-workbench-archive/availability')
    config['state_dir'] = str(CODEX_DIR / 'archives' / ('demand-workbench-' + hashlib.sha256(str(ROOT).encode()).hexdigest()[:12]))
    local = ROOT / '.codex/archive.local.json'
    if local.exists():
        project_config = json.loads(local.read_text())
        if project_config.get('enabled') is False:
            config['enabled'] = False
        if 'project_id' in project_config:
            config['project_id'] = project_config['project_id']
    return config


def set_enabled(enabled, url=None):
    config = json.loads(CONFIG.read_text()) if CONFIG.exists() else {}
    if url:
        spec = importlib.util.spec_from_file_location('archive_mcp_check', SKILL_ROOT / 'scripts/mcp_client.py')
        api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(api)
        api.check_url(url, local=True)
        config['url'] = url
    config['enabled'] = enabled
    CONFIG.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = CONFIG.with_suffix('.tmp')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(config, stream, ensure_ascii=False, indent=2)
    temporary.replace(CONFIG)
    print(json.dumps({'enabled': enabled}))


@contextlib.contextmanager
def database(config):
    directory = Path(config['state_dir']).expanduser()
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(directory, 0o700)
    path = directory / 'queue.sqlite3'
    # 创建文件前使用私有权限；SQLite journal继承数据库权限。
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    os.close(fd)
    os.chmod(path, 0o600)
    connection = sqlite3.connect(str(path), timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute('CREATE TABLE IF NOT EXISTS prompts (id TEXT PRIMARY KEY, value TEXT NOT NULL)')
    connection.execute('CREATE TABLE IF NOT EXISTS sessions (id TEXT PRIMARY KEY, title TEXT NOT NULL)')
    connection.execute('''CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, payload TEXT NOT NULL, progress TEXT NOT NULL DEFAULT '{}',
        status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
        retry_at REAL NOT NULL DEFAULT 0, error TEXT)''')
    try:
        yield connection
    finally:
        connection.close()


def identity(event):
    if not event.get('session_id') or not event.get('turn_id'):
        raise ValueError('Hook缺少session_id或turn_id，未归档')
    return hashlib.sha256((str(ROOT) + '\0' + str(event['session_id']) + '\0' + str(event['turn_id'])).encode()).hexdigest()


def split_text(text, limit=15000):
    """按Java UTF-16长度拆分，保留所有文字，给每段元数据留出空间。"""
    chunks, chunk, count = [], [], 0
    for char in text:
        width = 2 if ord(char) > 0xffff else 1
        if count + width > limit:
            chunks.append(''.join(chunk)); chunk, count = [], 0
        chunk.append(char); count += width
    if chunk:
        chunks.append(''.join(chunk))
    return chunks or ['']


def markdown_images(text, source):
    # 代码示例里的图片语法不是附件，不能把示例路径误当成待上传文件。
    visible, fence = [], None
    for line in text.splitlines():
        marker = re.match(r'^\s*(`{3,}|~{3,})', line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            continue
        if fence is None:
            visible.append(re.sub(r'(`+).*?\1', '', line))
    return [{'path': match[1] or match[2], 'source': source}
            for match in re.finditer(r'!\[[^\]]*\]\((?:<([^>]+)>|([^\s)]+))(?:\s+"[^"]*")?\)', '\n'.join(visible))
            if (match[1] or match[2]).startswith('/')]


def archive_tag_path(thread_title):
    """来源/项目/聊天三级路径；每级30字、完整路径最多92字，正常项目名和聊天名保留原文。"""
    import unicodedata

    def segment(value):
        value = unicodedata.normalize('NFC', redact(value))
        # 名称中的斜线不能意外产生额外层级。
        return re.sub(r'[\s#\x00-\x1f\x7f]+', '', value).replace('/', '／')

    def shorten(value, limit):
        if len(value) <= limit:
            return value
        digest = hashlib.sha256(value.lower().encode('utf-8')).hexdigest()[:4]
        return value[:limit - 5] + '~' + digest

    project = shorten(segment(ROOT.name) or '项目', 30)
    parts = ['Codex', project]
    title = segment(thread_title)
    if title:
        parts.append(shorten(title, 30))
    return '/'.join(parts)


def footer_tags(text, thread_title):
    # 归档元数据只占一个标签；正文仍按原文保留。
    existing = re.findall(r'(?:^|\s)#([^\s#]{1,92})(?=\s|$)', text)
    import unicodedata
    key = lambda value: unicodedata.normalize('NFC', value).lower()
    seen = {key(value) for value in existing}
    tag = archive_tag_path(thread_title)
    return '\n\n#' + tag if key(tag) not in seen and len(seen) < 10 else ''


def snapshot_images(text, directory, attachments=None):
    """复制明确提供的用户/助手附件；兼容回复里的Markdown，不扫描目录。"""
    images, warnings, seen = [], [], set()
    candidates = list(attachments or []) + markdown_images(text, 'assistant')
    for attachment in candidates:
        name = attachment['path']
        source_kind = attachment['source']
        if not name.startswith('/'):
            continue
        source = Path(name)
        if source.is_symlink():
            warnings.append('有一张本地配图是符号链接，未自动归档。')
            continue
        source = source.resolve()
        if str(source) in seen:
            continue
        seen.add(str(source))
        try:
            if source.is_symlink() or not source.is_file() or not 1 <= source.stat().st_size <= 10485760:
                raise ValueError('无效图片')
            data = source.read_bytes()
            mime = ('image/png' if data.startswith(b'\x89PNG\r\n\x1a\n') else
                    'image/jpeg' if data.startswith(b'\xff\xd8\xff') else
                    'image/webp' if data[:4] == b'RIFF' and data[8:12] == b'WEBP' else None)
            if mime is None:
                raise ValueError('不支持的图片')
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            target = directory / (str(len(images)) + '.bin')
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(data)
            images.append({'path': str(target), 'mimeType': mime, 'size': len(data), 'source': source_kind})
        except (OSError, ValueError):
            warnings.append('有一张明确引用的本地图片无法归档（不存在、格式不支持或超过10MiB）。')
    return images, warnings


def capture(event, config):
    """只采集当前项目的主线程问题和最终回答；任务一旦入队即冻结，重复事件不重复创建。"""
    cwd = Path(event.get('cwd', '/')).resolve()
    if ROOT != cwd and ROOT not in cwd.parents:
        return
    kind = event.get('hook_event_name')
    if kind not in ('UserPromptSubmit', 'Stop') or event.get('agent_id'):
        return
    job_id = identity(event)
    with database(config) as db, db:
        if kind == 'UserPromptSubmit':
            if db.execute('SELECT 1 FROM jobs WHERE id=?', (job_id,)).fetchone():
                return
            prompt = redact(event.get('prompt', ''))
            old = db.execute('SELECT value FROM prompts WHERE id=?', (job_id,)).fetchone()
            values = json.loads(old['value']) if old else []
            if prompt and prompt not in values:
                values.append(prompt)
            db.execute('INSERT OR REPLACE INTO prompts VALUES (?,?)', (job_id, json.dumps(values, ensure_ascii=False)))
            return
        answer = event.get('last_assistant_message')
        if not answer or not str(answer).strip() or db.execute('SELECT 1 FROM jobs WHERE id=?', (job_id,)).fetchone():
            return
        old = db.execute('SELECT value FROM prompts WHERE id=?', (job_id,)).fetchone()
        prompt = '\n\n'.join(json.loads(old['value'])) if old else '（本轮问题未捕获；可能在启用Hook前已开始。）'
        supplied_title = event.get('thread_title')
        if supplied_title:
            db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?)',
                       (event['session_id'], redact(supplied_title)[:200]))
        cached = db.execute('SELECT title FROM sessions WHERE id=?', (event['session_id'],)).fetchone()
        thread_title = cached['title'] if cached else ''
        attachments = event.get('attachments', []) + markdown_images(prompt, 'user')
        images, warnings = snapshot_images(str(answer), Path(config['state_dir']) / 'images' / job_id, attachments)
        body = '用户问题：\n' + prompt + '\n\nCodex 最终回答：\n' + redact(answer)
        if warnings:
            body += '\n\n归档说明：\n' + '\n'.join(warnings)
        chunks = split_text(body)
        total = max(len(chunks), (len(images) + 8) // 9)
        occurred_at = datetime.now(timezone.utc).isoformat()
        parts = []
        for index in range(total):
            chunk = chunks[index] if index < len(chunks) else '本轮补充配图。'
            parts.append({'title': '', 'text': chunk + footer_tags(chunk, thread_title),
                          'images': images[index * 9:(index + 1) * 9],
                          'clientRequestNo': hashlib.sha256((job_id + ':' + str(index)).encode()).hexdigest(),
                          'occurredAt': occurred_at})
        payload = {'deliveryPolicy': 'skip', 'parts': parts, 'createdAt': occurred_at,
                   'session_id': event['session_id'], 'turn_id': event['turn_id'], 'thread_title': thread_title}
        db.execute('INSERT INTO jobs(id,payload) VALUES (?,?)', (job_id, json.dumps(payload, ensure_ascii=False)))
        db.execute('DELETE FROM prompts WHERE id=?', (job_id,))



def publish(value, config):
    """在线才提交本轮；离线时不落问答或图片，失败不补传。"""
    if not config.get('enabled'):
        return {'queued': False, 'reason': 'disabled'}
    if not AVAILABILITY.consume_ticket(config, value.get('availability_token')):
        ready = probe(config)
        if not ready['ready']:
            return {'queued': False, 'skipped': True, 'reason': ready['reason']}
        AVAILABILITY.consume_ticket(config, ready.get('availability_token'))
    spec = importlib.util.spec_from_file_location('archive_numbered_journal', SKILL_ROOT / 'scripts/numbered_journal.py')
    numbered = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(numbered)
    numbered_text = value.get('journal_text', value.get('question'))
    if (isinstance(numbered_text, str) and re.match(r'^\s*\[日志[:：]', numbered_text)) or value.get('mode') == 'numbered_journal':
        return numbered.queue(value, config, globals())
    if config.get('native_edit_enabled'):
        spec = importlib.util.spec_from_file_location('archive_native_edit', SKILL_ROOT / 'scripts/native_edit.py')
        native = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(native)
        return native.queue(value, config, globals(), numbered)
    if value.get('mode') == 'journal':
        spec = importlib.util.spec_from_file_location('archive_journal', SKILL_ROOT / 'scripts/journal.py')
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.queue(value, config, globals())
    question, answer = value.get('question'), value.get('answer')
    if not isinstance(question, str) or not question.strip() or not isinstance(answer, str) or not answer.strip():
        raise ValueError('question和answer必须为非空文本')
    session = value.get('session_id') or os.environ.get('CODEX_THREAD_ID')
    if not isinstance(session, str) or not session.strip():
        raise ValueError('缺少会话编号')
    submission = value.get('submission_id') or str(uuid.uuid4())
    if not isinstance(submission, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', submission):
        raise ValueError('submission_id格式无效')
    attachments = value.get('attachments', [])
    if not isinstance(attachments, list) or any(
            not isinstance(item, dict) or item.get('source') not in ('user', 'assistant')
            or not isinstance(item.get('path'), str) or not item['path'].startswith('/')
            for item in attachments):
        raise ValueError('attachments必须包含source（user/assistant）和绝对path')
    title = value.get('thread_title', '')
    if not isinstance(title, str):
        raise ValueError('thread_title必须为文本')
    event = {'cwd': str(ROOT), 'session_id': session, 'turn_id': submission,
             'attachments': attachments, 'thread_title': title}
    capture({**event, 'hook_event_name': 'UserPromptSubmit', 'prompt': question}, config)
    capture({**event, 'hook_event_name': 'Stop', 'last_assistant_message': answer}, config)
    return {'queued': True, 'job_id': identity(event), 'submission_id': submission}


def credential():
    secret = os.environ.get('DEMAND_WORKBENCH_API_KEY')
    if secret:
        return secret
    result = subprocess.run(['/usr/bin/security', 'find-generic-password', '-s', KEY_SERVICE, '-a', getpass.getuser(), '-w'],
                            capture_output=True, text=True, timeout=15)
    if result.returncode or not result.stdout.strip():
        raise ValueError('缺少归档密钥，请设置环境变量或使用set-key')
    return result.stdout.strip()


class ImageConfirmationFailed(Exception):
    """仅图片确认可降级，原始异常及供应商响应不写入日志。"""
    def __init__(self, detail):
        self.detail = detail
        super().__init__('图片未确认')


def safe_error(error, stage, tool='', elapsed_ms=0):
    detail = error.detail() if callable(getattr(error, 'detail', None)) else {}
    code = detail.get('code', type(error).__name__)
    if not (re.fullmatch(r'(?:S[0-9]{9}|-[0-9]{5}|HTTP_[1-5][0-9]{2})', str(code)) or code in {'NETWORK_TIMEOUT', 'NETWORK_ERROR', 'CONFIRM_WORKER_FAILED', 'INVALID_RESPONSE', 'REMOTE_ERROR', 'OSError', 'TimeoutError', 'ValueError', 'FileNotFoundError', 'KeyError', 'JSONDecodeError', 'JOURNAL_CONFLICT', 'NATIVE_METADATA_UNAVAILABLE', 'NATIVE_TRIGGER_UNSUPPORTED', 'NATIVE_SNAPSHOT_STALE', 'NATIVE_EDIT_BASELINE_MISSING', 'NATIVE_EDIT_TARGET_UNKNOWN'}):
        code = 'UNKNOWN_ERROR'
    request_no = detail.get('requestNo', '')
    return {'stage': stage, 'tool': tool, 'code': code,
            'message': {'S048105012': '图片存储核验超时', 'S048105013': '图片存储核验暂不可用',
                        'JOURNAL_CONFLICT': '原笔记版本或状态已变更，未覆盖', 'NETWORK_TIMEOUT': '请求超时', 'NETWORK_ERROR': '网络请求失败'}.get(code, '操作失败'),
            'requestNo': request_no if isinstance(request_no, str) and re.fullmatch(r'CALL-[A-Za-z0-9-]{1,80}', request_no) else '',
            'elapsedMs': max(0, int(elapsed_ms))}


class Client:
    def __init__(self, config):
        spec = importlib.util.spec_from_file_location('archive_mcp_upload', config['upload_helper'])
        self.api = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.api)
        self.api.check_url(config['url'], local=True)
        self.url, self.secret = config['url'], credential()
        self.api.rpc(self.url, self.secret, 'initialize', {'protocolVersion': '2025-11-25', 'capabilities': {},
            'clientInfo': {'name': 'codex-project-archive', 'version': '1'}})
        self.api.rpc(self.url, self.secret, 'notifications/initialized', {})

    def tool(self, name, arguments):
        return self.api.tool(self.url, self.secret, name, arguments)

    def upload(self, image, progress, save):
        if not progress.get('mediaNo'):
            ticket = self.tool('prepare_note_image_upload', {'filename': 'codex-image',
                'mimeType': image['mimeType'], 'size': image['size']})
            self.api.check_url(ticket['uploadUrl'])
            boundary = '----CodexArchive' + uuid.uuid4().hex
            parts = []
            for name in ('token', 'key'):
                parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="' + name +
                              '"\r\n\r\n' + ticket[name] + '\r\n').encode())
            parts.append(('--' + boundary + '\r\nContent-Disposition: form-data; name="file"; filename="image"\r\nContent-Type: ' + image['mimeType'] + '\r\n\r\n').encode())
            parts.extend([Path(image['path']).read_bytes(), ('\r\n--' + boundary + '--\r\n').encode()])
            uploaded = self.api.request_json(ticket['uploadUrl'], b''.join(parts), {'Content-Type': 'multipart/form-data; boundary=' + boundary})
            if uploaded.get('key') != ticket['key']:
                raise ValueError('云端返回图片编号不一致')
            progress['mediaNo'] = ticket['mediaNo']
            save()
        if not progress.get('completed'):
            started = time.monotonic()
            for attempt in range(1, 4):
                try:
                    self.api.bounded_confirm(self.url, self.secret, progress['mediaNo'])
                    progress['completed'] = True
                    progress.pop('confirmationError', None)
                    progress['confirmationAttempts'] = attempt
                    save()
                    break
                except Exception as error:
                    detail = safe_error(error, 'confirm_image', 'complete_note_image_upload',
                                        (time.monotonic() - started) * 1000)
                    detail['attempts'] = attempt
                    progress['confirmationAttempts'] = attempt
                    progress['confirmationError'] = detail
                    save()
                    if not getattr(error, 'retryable', False) or attempt == 3:
                        raise ImageConfirmationFailed(detail) from error
        return progress['mediaNo']


def drain(config, job_id=None, client_factory=Client):
    """只对本次指定任务上传一次；旧队列、失败与中断任务都不补传。"""
    if not config.get('enabled') or not job_id:
        return
    with database(config) as db:
        row = db.execute("SELECT * FROM jobs WHERE id=? AND status='pending'", (job_id,)).fetchone()
        if not row:
            return
        if json.loads(row['payload']).get('deliveryPolicy') != 'skip':
            with db:
                db.execute("UPDATE jobs SET status='skipped',error='LegacyNoRetry' WHERE id=?", (job_id,))
            return
        lock_path = Path(config['state_dir']) / 'worker.lock'
        with lock_path.open('a') as lock:
            os.chmod(lock_path, 0o600)
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                with db:
                    db.execute("UPDATE jobs SET status='skipped',error='WorkerBusy' WHERE id=? AND status='pending'", (job_id,))
                return
            # 重新检查防止相同提交号派生的多个进程重复发送。
            with db:
                changed = db.execute("UPDATE jobs SET status='processing',attempts=1 WHERE id=? AND status='pending'", (job_id,)).rowcount
            if not changed:
                return
            progress = json.loads(row['progress'])
            def save():
                with db:
                    db.execute('UPDATE jobs SET progress=? WHERE id=?', (json.dumps(progress), job_id))
            stage, tool_name = 'initialize', 'initialize'
            started = time.monotonic()
            try:
                client = client_factory(config)
                payload = json.loads(row['payload'])
                if payload.get('mode') == 'numbered_journal':
                    spec = importlib.util.spec_from_file_location('archive_numbered_journal', SKILL_ROOT / 'scripts/numbered_journal.py')
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    stage, tool_name = 'numbered_journal', 'sync_codex_note'
                    module.execute(payload, config, client, db, progress, save, globals())
                    with db:
                        db.execute('UPDATE jobs SET status=?,error=NULL WHERE id=?',
                                   ('partial' if progress.get('0', {}).get('imageFailures') else 'done', job_id))
                    return
                if payload.get('mode') == 'journal':
                    spec = importlib.util.spec_from_file_location('archive_journal', SKILL_ROOT / 'scripts/journal.py')
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    stage, tool_name = 'journal', 'update_note' if payload['operation'] == 'update' else 'create_note'
                    module.execute(payload, config, client, db, progress, save, globals())
                    with db:
                        db.execute('UPDATE jobs SET status=?,error=NULL WHERE id=?',
                                   ('partial' if progress.get('0', {}).get('imageFailures') else 'done', job_id))
                    return
                for index, part in enumerate(json.loads(row['payload'])['parts']):
                    saved = progress.setdefault(str(index), {'images': {}})
                    if saved.get('recordNo'):
                        continue
                    media_nos = []
                    for image_index, image in enumerate(part['images']):
                        stage, tool_name = 'upload_image', 'prepare_note_image_upload'
                        try:
                            media_nos.append(client.upload(image, saved['images'].setdefault(str(image_index), {}), save))
                        except ImageConfirmationFailed as error:
                            saved.setdefault('imageFailures', {})[str(image_index)] = error.detail
                            save()
                    args = {key: part[key] for key in ('title', 'text', 'clientRequestNo', 'occurredAt')}
                    stage, tool_name = 'save_note', 'create_note'
                    result = client.tool('create_note', {**args, 'mediaNos': media_nos, 'action': 'SAVE'})
                    saved['recordNo'] = result['recordNo']
                    save()
                partial = any(part.get('imageFailures') for part in progress.values())
                with db:
                    db.execute("UPDATE jobs SET status=?,error=? WHERE id=?",
                               ('partial' if partial else 'done',
                                json.dumps({'code': 'IMAGE_CONFIRMATION_FAILED', 'message': '正文已保存，部分图片未关联'}, ensure_ascii=False) if partial else None,
                                job_id))
            except Exception as error:
                with db:
                    db.execute("UPDATE jobs SET status='skipped',retry_at=0,error=? WHERE id=?", (json.dumps(safe_error(error, stage, tool_name, (time.monotonic() - started) * 1000), ensure_ascii=False), job_id))
                if getattr(error, 'offline', False) or isinstance(error, (OSError, TimeoutError)):
                    AVAILABILITY.note_failure(config)


def kick(job_id):
    subprocess.Popen([sys.executable, str(Path(__file__).resolve()), 'drain', '--job-id', job_id],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True, cwd=str(ROOT))


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=['hook', 'publish', 'drain', 'retry', 'status', 'set-key', 'enable', 'disable', 'check', 'gate', 'check-worker'])
    parser.add_argument('--force', action='store_true', help='gate/check忽略离线缓存检查一次，不补传')
    parser.add_argument('--reset-failed', action='store_true')
    parser.add_argument('--job-id', help=argparse.SUPPRESS)
    parser.add_argument('--url', help='enable时设置MCP地址，仅HTTPS或本机HTTP')
    args = parser.parse_args()
    if args.command in ('enable', 'disable'):
        set_enabled(args.command == 'enable', args.url)
        return
    if args.command == 'set-key':
        secret = (getpass.getpass('构想台 API Key（输入隐藏）：') if sys.stdin.isatty() else sys.stdin.readline()).strip()
        if not re.fullmatch(r'dw_sk_[A-Za-z0-9_-]{43}', secret):
            raise ValueError('密钥格式无效')
        result = subprocess.run(['/usr/bin/security', 'add-generic-password', '-U', '-s', KEY_SERVICE,
                                 '-a', getpass.getuser(), '-w', secret], capture_output=True, timeout=15)
        if result.returncode:
            raise ValueError('钥匙串保存失败')
        print('归档密钥已保存到macOS钥匙串。')
        return
    config = settings()
    if args.command == 'check-worker':
        client = Client(config)
        available = {tool['name'] for tool in client.api.rpc(client.url, client.secret, 'tools/list', {})['tools']}
        required = {'create_note', 'prepare_note_image_upload', 'complete_note_image_upload'}
        if config.get('native_edit_enabled'):
            required |= {'get_note', 'get_codex_note', 'sync_codex_note'}
        print(json.dumps({'ready': required <= available}))
        return
    if args.command in ('gate', 'check'):
        print(json.dumps(AVAILABILITY.probe(config, force=True) if args.force else probe(config)))
        return
    if args.command == 'retry':
        print(json.dumps({'retried': False, 'reason': 'no_retry_policy'}))
        return
    if args.command == 'status':
        with database(config) as db:
            counts = dict(db.execute('SELECT status,COUNT(*) FROM jobs GROUP BY status').fetchall())
            jobs = [{'id': row['id'][:12], 'status': row['status'], 'attempts': row['attempts'], 'error': row['error'],
                     'recordNos': [part['recordNo'] for part in json.loads(row['progress']).values() if part.get('recordNo')],
                     'images': [{'part': part_index, 'image': image_index, 'confirmed': image.get('completed', False),
                                 'confirmationAttempts': image['confirmationAttempts'],
                                 'confirmationError': image.get('confirmationError')}
                                for part_index, part in json.loads(row['progress']).items()
                                for image_index, image in part.get('images', {}).items() if 'confirmationAttempts' in image]}
                    for row in db.execute('SELECT * FROM jobs ORDER BY rowid DESC LIMIT 10')]
        print(json.dumps({'enabled': config['enabled'], 'project': ROOT.name, 'mode': config.get('mode', 'hooks'), 'counts': counts, 'recent': jobs}, ensure_ascii=False))
        return
    if not config['enabled']:
        if args.command == 'publish':
            print(json.dumps({'queued': False, 'enabled': False}))
        return
    if args.command == 'publish':
        result = publish(json.loads(sys.stdin.read(8 * 1024 * 1024)), config)
        if result.get('queued'):
            kick(result['job_id'])
        print(json.dumps(result))
    elif args.command == 'hook':
        return  # 不允许旧Hook重新启用队列补传。
    else:
        drain(config, job_id=args.job_id)


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(json.dumps({'queued': False, 'error': safe_error(error, 'publish')}, ensure_ascii=False), file=sys.stderr)
        sys.exit(1)
