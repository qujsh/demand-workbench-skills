"""显式日志新增/修改；仅映射当前项目、服务地址和会话，不猜测历史消息。"""
import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path


class JournalConflict(ValueError):
    def detail(self):
        return {'code': 'JOURNAL_CONFLICT', 'requestNo': ''}


def table(db):
    db.execute('''CREATE TABLE IF NOT EXISTS journal_entries (
        endpoint TEXT NOT NULL, session TEXT NOT NULL, entry_id TEXT NOT NULL,
        record_no TEXT NOT NULL, version INTEGER NOT NULL, draft_version INTEGER,
        updated_at TEXT NOT NULL, PRIMARY KEY(endpoint,session,entry_id))''')


def queue(value, config, api):
    text = value.get('journal_text')
    if not isinstance(text, str) or not text.strip() or len(text.encode('utf-16-le')) // 2 > 14500:
        raise ValueError('日志正文需为非空文本，初版最多14500个UTF-16单位')
    session = value.get('session_id') or os.environ.get('CODEX_THREAD_ID')
    operation = value.get('operation', 'create')
    entry = value.get('entry_id') or (str(uuid.uuid4()) if operation == 'create' else 'latest')
    revision = value.get('submission_id') or str(uuid.uuid4())
    if not isinstance(session, str) or not session.strip() or operation not in ('create', 'update'):
        raise ValueError('日志需要会话编号及明确的create/update操作')
    for identifier in (entry, revision):
        if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}|latest', identifier):
            raise ValueError('日志或提交编号无效')
    image_mode = value.get('image_mode', 'preserve')
    attachments = value.get('attachments', [])
    if image_mode not in ('preserve', 'replace', 'clear') or not isinstance(attachments, list) or len(attachments) > 9:
        raise ValueError('图片操作无效或超过9张')
    if image_mode == 'clear' and attachments:
        raise ValueError('清空图片时不能同时传图片')
    if any(not isinstance(a, dict) or a.get('source') != 'user' or not isinstance(a.get('path'), str)
           or not Path(a['path']).is_absolute() for a in attachments):
        raise ValueError('日志只接受当前用户明确提供的绝对路径图片')
    title = value.get('thread_title', '')
    if not isinstance(title, str):
        raise ValueError('聊天名称无效')
    with api['database'](config) as db, db:
        table(db)
        existing = None
        if entry == 'latest':
            if operation != 'update':
                raise ValueError('latest仅用于明确修改上一条日志')
            existing = db.execute('SELECT * FROM journal_entries WHERE endpoint=? AND session=? ORDER BY updated_at DESC,rowid DESC LIMIT 1',
                                  (config['url'], session)).fetchone()
            if not existing:
                raise ValueError('本会话没有已成功保存的日志，不自动改写问答记录')
            entry = existing['entry_id']
        if existing is None:
            existing = db.execute('SELECT * FROM journal_entries WHERE endpoint=? AND session=? AND entry_id=?',
                                  (config['url'], session, entry)).fetchone()
        job_id = api['identity']({'session_id': session, 'turn_id': revision})
        old = db.execute('SELECT payload FROM jobs WHERE id=?', (job_id,)).fetchone()
        if old:
            return {'queued': False, 'reason': 'duplicate_submission', 'entry_id': entry, 'job_id': job_id}
        if (operation == 'create' and existing) or (operation == 'update' and not existing):
            raise ValueError('日志已存在或修改目标尚未成功保存')
        if db.execute("SELECT 1 FROM jobs WHERE status IN ('pending','processing') AND json_extract(payload,'$.mode')='journal' AND json_extract(payload,'$.entry_id')=? AND json_extract(payload,'$.session_id')=? AND json_extract(payload,'$.endpoint')=?",
                      (entry, session, config['url'])).fetchone():
            raise ValueError('上一版日志还在保存，请完成后再修改')
        images, warnings = api['snapshot_images']('', Path(config['state_dir']) / 'images' / job_id, attachments)
        if warnings:
            raise ValueError('附件读取失败，本轮日志未提交')
        if title:
            db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?)', (session, api['redact'](title)[:200]))
        cached = db.execute('SELECT title FROM sessions WHERE id=?', (session,)).fetchone()
        clean = api['redact'](text)
        now = datetime.now(timezone.utc).isoformat()
        payload = {'mode': 'journal', 'deliveryPolicy': 'skip', 'operation': operation, 'entry_id': entry,
                   'session_id': session, 'endpoint': config['url'], 'createdAt': now, 'image_mode': image_mode,
                   'expected': dict(existing) if existing else None,
                   'parts': [{'title': '', 'text': clean + api['footer_tags'](clean, cached['title'] if cached else ''),
                              'images': images, 'occurredAt': now,
                              'clientRequestNo': hashlib.sha256(json.dumps([config['url'], session, entry]).encode()).hexdigest()}]}
        db.execute('INSERT INTO jobs(id,payload) VALUES (?,?)', (job_id, json.dumps(payload, ensure_ascii=False)))
        return {'queued': True, 'job_id': job_id, 'entry_id': entry, 'operation': operation, 'submission_id': revision}


def execute(payload, config, client, db, progress, save, api):
    """先校验映射及版本，图片失败保留原图；更新不重试、不改公开或已删除笔记。"""
    if payload['endpoint'] != config['url']:
        raise JournalConflict('服务地址已变更，停止提交')
    expected = payload['expected']
    original = None
    if expected:
        original = client.tool('get_note', {'recordNo': expected['record_no']})
        if (original.get('version') != expected['version'] or original.get('draftVersion') != expected['draft_version']
                or original.get('deletedAt') or original.get('visibility') != 'PRIVATE'
                or original.get('requestedVisibility') == 'PUBLIC' or original.get('workingCopy')):
            raise JournalConflict('原笔记已在网站修改或不再是私密正式记录')
    part = payload['parts'][0]
    saved = progress.setdefault('0', {'images': {}})
    media = []
    for index, image in enumerate(part['images']):
        try:
            media.append(client.upload(image, saved['images'].setdefault(str(index), {}), save))
        except api['ImageConfirmationFailed'] as error:
            saved.setdefault('imageFailures', {})[str(index)] = error.detail
            save()
    args = {'title': '', 'text': part['text'], 'action': 'SAVE'}
    old_media = [i['mediaNo'] for i in original.get('images', [])] if original else []
    if payload['image_mode'] == 'clear':
        args['mediaNos'] = []
    elif payload['image_mode'] == 'preserve' or saved.get('imageFailures'):
        args['mediaNos'] = list(dict.fromkeys(old_media + media))
    else:
        args['mediaNos'] = media
    if len(args['mediaNos']) > 9:
        raise ValueError('JOURNAL_IMAGE_LIMIT')
    if expected:
        args.update({'recordNo': expected['record_no'], 'expectedVersion': expected['version']})
        if expected['draft_version'] is not None:
            args['expectedDraftVersion'] = expected['draft_version']
        result = client.tool('update_note', args)
    else:
        args.update({'clientRequestNo': part['clientRequestNo'], 'occurredAt': part['occurredAt']})
        result = client.tool('create_note', args)
    saved['recordNo'] = result['recordNo']
    save()
    # Missing version must not permit an unsafe later update.
    if not isinstance(result.get('version'), int):
        raise ValueError('JOURNAL_VERSION_MISSING')
    table(db)
    with db:
        db.execute('INSERT OR REPLACE INTO journal_entries VALUES (?,?,?,?,?,?,?)',
                   (config['url'], payload['session_id'], payload['entry_id'], result['recordNo'],
                    result['version'], result.get('draftVersion'), payload['createdAt']))
