"""消息开头的固定日志编号决定更新目标，不读取 Codex 历史或猜测编辑关系。"""
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
import re
import uuid

class JournalConflict(ValueError):
    def detail(self):
        return {'code': 'JOURNAL_CONFLICT', 'requestNo': ''}


MARKER = re.compile(r'^\s*\[日志[:：]([A-Za-z0-9_-]{6,64})\][ \t]*(?:\r?\n)?')


def parse(text):
    if not isinstance(text, str):
        return None
    match = MARKER.match(text)
    if match:
        return match[1], text[match.end():]
    return None


def queue(value, config, api, internal=None, on_enqueue=None):
    parsed = (internal[0], internal[1]) if internal else parse(value.get('journal_text', value.get('question')))
    if not parsed:
        raise ValueError('固定编号日志需以[日志:六至六十四位编号]开头')
    entry, text = parsed
    if not text.strip() or len(text.encode('utf-16-le')) // 2 > 14500:
        raise ValueError('日志正文需非空，最多14500个UTF-16单位')
    session = value.get('session_id') or os.environ.get('CODEX_THREAD_ID')
    revision = value.get('submission_id') or str(uuid.uuid4())
    if not isinstance(session, str) or not session.strip() or len(session) > 128:
        raise ValueError('固定编号日志需要当前会话编号')
    if not isinstance(revision, str) or not re.fullmatch(r'[A-Za-z0-9_-]{8,128}', revision):
        raise ValueError('修订编号无效')
    attachments = value.get('attachments', [])
    image_mode = value.get('image_mode', 'preserve')
    if image_mode not in ('preserve', 'replace', 'clear') or not isinstance(attachments, list) or len(attachments) > 9:
        raise ValueError('图片操作无效或超过9张')
    if image_mode == 'clear' and attachments:
        raise ValueError('清空图片时不能同时传图片')
    if any(not isinstance(a, dict) or a.get('source') not in (('user', 'assistant') if internal and internal[2] else ('user',)) or not isinstance(a.get('path'), str)
           or not Path(a['path']).is_absolute() for a in attachments):
        raise ValueError('日志只接受本轮用户明确提供的绝对路径图片')
    title = value.get('thread_title', '')
    if not isinstance(title, str):
        raise ValueError('聊天名称无效')
    project = config.get('project_id') or hashlib.sha256(str(api['ROOT']).encode()).hexdigest()
    if not isinstance(project, str) or not project.strip() or len(project) > 200:
        raise ValueError('项目固定标识无效')
    source = {'projectId': project, 'sessionId': session, 'messageId': entry}
    with api['database'](config) as db, db:
        # 先获得写锁，确保两个进程不能同时为同一日志排入相互覆盖的修订。
        db.execute('BEGIN IMMEDIATE')
        job_id = api['identity']({'session_id': session, 'turn_id': revision})
        if db.execute('SELECT 1 FROM jobs WHERE id=?', (job_id,)).fetchone():
            return {'queued': False, 'reason': 'duplicate_submission', 'job_id': job_id}
        pending = db.execute("SELECT payload FROM jobs WHERE status IN ('pending','processing') "
                             "AND json_extract(payload,'$.mode')='numbered_journal'").fetchall()
        if any((p := json.loads(row['payload'])).get('endpoint') == config['url'] and p.get('source') == source for row in pending):
            raise ValueError('同一编号的上一版日志还在保存，本轮不覆盖')
        images, warnings = api['snapshot_images'](text if internal and internal[2] else '', Path(config['state_dir']) / 'images' / job_id, attachments)
        if warnings:
            raise ValueError('附件读取失败，本轮日志未提交')
        if title:
            db.execute('INSERT OR REPLACE INTO sessions VALUES (?,?)', (session, api['redact'](title)[:200]))
        cached = db.execute('SELECT title FROM sessions WHERE id=?', (session,)).fetchone()
        clean = api['redact'](text)
        now = datetime.now(timezone.utc).isoformat()
        payload = {'mode': 'numbered_journal', 'deliveryPolicy': 'skip', 'source': source,
                   'revisionId': revision, 'endpoint': config['url'], 'createdAt': now, 'image_mode': image_mode,
                   'parts': [{'text': clean + api['footer_tags'](clean, cached['title'] if cached else ''),
                              'images': images, 'title': '', 'occurredAt': now}]}
        if on_enqueue:
            on_enqueue(db, payload)
        db.execute('INSERT INTO jobs(id,payload) VALUES (?,?)', (job_id, json.dumps(payload, ensure_ascii=False)))
        return {'queued': True, 'job_id': job_id, 'entry_id': entry, 'submission_id': revision}


def execute(payload, config, client, db, progress, save, api):
    if payload['endpoint'] != config['url']:
        raise ValueError('日志服务地址变化，停止本轮提交')
    source = payload['source']
    mapping = client.tool('get_codex_note', source)
    if payload.get('native', {}).get('requires_existing') and not mapping.get('found'):
        raise JournalConflict('原笔记尚未保存，停止编辑同步')
    original = client.tool('get_note', {'recordNo': mapping['recordNo']}) if mapping.get('found') else None
    # 在发送图片前拒绝已被网页修改的记录；后端在事务中再次校验防止并发覆盖。
    if original and (original.get('version') != mapping['version'] or original.get('deletedAt')
                     or original.get('workingCopy') or original.get('visibility') != 'PRIVATE'
                     or original.get('requestedVisibility') == 'PUBLIC' or original.get('status') != 'SAVED'):
        raise JournalConflict('原笔记已被修改')
    part = payload['parts'][0]
    saved = progress.setdefault('0', {'images': {}})
    media = []
    for index, image in enumerate(part['images']):
        try:
            media.append(client.upload(image, saved['images'].setdefault(str(index), {}), save))
        except api['ImageConfirmationFailed'] as error:
            saved.setdefault('imageFailures', {})[str(index)] = error.detail
            save()
    args = {**source, 'revisionId': payload['revisionId'], 'expectedRevision': mapping['sourceRevision'], 'text': part['text']}
    old = [i['mediaNo'] for i in original.get('images', [])] if original else []
    if payload['image_mode'] == 'clear':
        args['mediaNos'] = []
    elif payload['image_mode'] == 'replace' and not saved.get('imageFailures'):
        args['mediaNos'] = media
    elif media:
        args['mediaNos'] = list(dict.fromkeys(old + media))
    # 没有新图时省略mediaNos，保留网站已关联图片，离线不补传。
    if len(args.get('mediaNos', [])) > 9:
        raise ValueError('JOURNAL_IMAGE_LIMIT')
    if not original:
        args['occurredAt'] = part['occurredAt']
    result = client.tool('sync_codex_note', args)
    saved.update({'recordNo': result['recordNo'], 'sourceRevision': result['sourceRevision']})
    save()
