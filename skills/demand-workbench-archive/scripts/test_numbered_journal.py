import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('numbered_archive', Path(__file__).with_name('archive.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class NumberedJournalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'enabled': True, 'url': 'http://localhost:8070/mcp', 'state_dir': self.temp.name}
        self.calls = []
        self.revision = 0
        self.note_version = 0
        self.media = ['MEDIA-old']
        outer = self
        class Client:
            def tool(self, name, args):
                outer.calls.append((name, args))
                if name == 'get_codex_note':
                    return {'found': outer.revision > 0, 'sourceRevision': outer.revision,
                            'recordNo': 'NOTE-numbered', 'version': outer.revision}
                if name == 'get_note':
                    return {'version': outer.note_version, 'visibility': 'PRIVATE', 'status': 'SAVED',
                            'images': [{'mediaNo': no} for no in outer.media]}
                if name == 'sync_codex_note':
                    outer.revision += 1
                    outer.note_version = outer.revision
                    return {'recordNo': 'NOTE-numbered', 'sourceRevision': outer.revision}
                if name == 'create_note':
                    return {'recordNo': 'NOTE-normal'}
                raise AssertionError(name)
            def upload(self, image, progress, save):
                raise a.ImageConfirmationFailed({'code': 'S048105012', 'attempts': 3})
        self.client = Client()

    def submit(self, question, revision, drain=True, **extra):
        with patch.object(a, 'probe', return_value={'ready': True}):
            result = a.publish({'question': question, 'answer': '收到。', 'session_id': 'session-test',
                                'submission_id': revision, 'attachments': [], **extra}, self.config)
        if result.get('queued') and drain:
            a.drain(self.config, result['job_id'], client_factory=lambda _: self.client)
        return result

    def test_edit_same_marker_uses_sync_without_qa_or_marker_in_body(self):
        self.submit('[日志:abc123]\n第一次记录', 'numbered-create-001')
        self.submit('[日志:abc123]\n编辑后的记录', 'numbered-update-001')
        syncs = [args for name, args in self.calls if name == 'sync_codex_note']
        self.assertEqual([s['expectedRevision'] for s in syncs], [0, 1])
        self.assertEqual(syncs[0]['messageId'], syncs[1]['messageId'])
        self.assertTrue(syncs[1]['text'].startswith('编辑后的记录\n\n#'))
        self.assertNotIn('[日志:', syncs[1]['text'])
        self.assertNotIn('Codex 最终回答', syncs[1]['text'])
        self.assertNotIn('occurredAt', syncs[1])
        self.assertNotIn('mediaNos', syncs[1])

    def test_normal_message_and_marker_in_code_do_not_activate_mode(self):
        result = self.submit('示例：[日志:abc123] 正文', 'normal-message-001')
        with a.database(self.config) as db:
            payload = json.loads(db.execute('SELECT payload FROM jobs WHERE id=?', (result['job_id'],)).fetchone()[0])
        self.assertNotEqual(payload.get('mode'), 'numbered_journal')
        self.assertIn('create_note', [name for name, _ in self.calls])

    def test_duplicate_and_pending_revision_do_not_queue_more(self):
        first = self.submit('[日志:abc123] 第一版', 'numbered-create-001', drain=False)
        self.assertFalse(self.submit('[日志:abc123] 重复', 'numbered-create-001')['queued'])
        with self.assertRaises(ValueError):
            self.submit('[日志:abc123] 第二版', 'numbered-update-001')
        a.drain(self.config, first['job_id'], client_factory=lambda _: self.client)
        self.assertEqual(self.revision, 1)

    def test_website_conflict_does_not_fall_back_to_new_note(self):
        self.submit('[日志:abc123] 第一版', 'numbered-create-001')
        self.note_version = 8
        self.submit('[日志:abc123] 第二版', 'numbered-update-001')
        self.assertEqual(self.revision, 1)
        self.assertNotIn('create_note', [name for name, _ in self.calls])
        with a.database(self.config) as db:
            self.assertEqual(db.execute('SELECT status FROM jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0], 'skipped')

    def test_image_failure_saves_text_and_retains_existing_images(self):
        self.submit('[日志:abc123] 第一版', 'numbered-create-001')
        image = Path(self.temp.name) / 'sample.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\nimage')
        self.submit('[日志:abc123] 第二版', 'numbered-update-001', image_mode='replace',
                    attachments=[{'source': 'user', 'path': str(image)}])
        self.assertEqual(self.revision, 2)
        self.assertNotIn('mediaNos', self.calls[-1][1])
        with a.database(self.config) as db:
            self.assertEqual(db.execute('SELECT status FROM jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0], 'partial')

    def test_new_marker_is_independent_and_empty_body_is_rejected(self):
        self.submit('[日志:abc123] 第一条', 'numbered-create-001')
        self.submit('[日志:xyz789] 第二条', 'numbered-create-002')
        self.assertEqual(self.calls[-1][1]['messageId'], 'xyz789')
        with self.assertRaises(ValueError):
            self.submit('[日志:abc123]\n', 'numbered-empty-001')

    def test_malformed_marker_does_not_create_normal_qa_note(self):
        with self.assertRaises(ValueError):
            self.submit('[日志:abc] 编号过短', 'numbered-invalid-001')
        self.assertEqual(self.calls, [])

    def test_offline_does_not_persist_content(self):
        with patch.object(a, 'probe', return_value={'ready': False, 'reason': 'unavailable'}):
            result = a.publish({'question': '[日志:abc123] 离线正文', 'answer': '收到。'}, self.config)
        self.assertFalse(result['queued'])
        self.assertFalse((Path(self.temp.name) / 'queue.sqlite3').exists())


if __name__ == '__main__':
    unittest.main()
