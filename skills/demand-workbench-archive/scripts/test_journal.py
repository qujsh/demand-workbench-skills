import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('journal_archive', Path(__file__).with_name('archive.py'))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)


class JournalTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.config = {'enabled': True, 'url': 'http://localhost:8070/mcp', 'state_dir': self.temp.name}
        self.version = 1
        self.text = ''
        self.images = [{'mediaNo': 'MEDIA-old'}]
        self.calls = []
        self.original_version = None
        outer = self
        class Client:
            def tool(self, name, args):
                outer.calls.append((name, args))
                if name == 'get_note':
                    return {'version': outer.original_version or outer.version, 'draftVersion': None,
                            'visibility': 'PRIVATE', 'images': outer.images}
                if name == 'create_note':
                    outer.text = args['text']
                elif name == 'update_note':
                    outer.version += 1
                    outer.text = args['text']
                return {'recordNo': 'NOTE-test', 'version': outer.version, 'draftVersion': None}
            def upload(self, image, progress, save):
                raise a.ImageConfirmationFailed({'code': 'S048105012', 'attempts': 3})
        self.client = Client()

    def submit(self, operation, text, revision, **extra):
        with patch.object(a, 'probe', return_value={'ready': True}):
            result = a.publish({'mode': 'journal', 'operation': operation, 'journal_text': text,
                'session_id': 'journal-session', 'entry_id': 'test-entry-001' if operation == 'create' else 'latest',
                'submission_id': revision, **extra}, self.config)
        if result.get('queued'):
            a.drain(self.config, result['job_id'], client_factory=lambda _: self.client)
        return result

    def test_create_then_edit_updates_same_note_without_qa_wrapper(self):
        self.submit('create', '第一次尝试', 'journal-create-001')
        self.assertTrue(self.text.startswith('第一次尝试\n\n#'))
        self.assertNotIn('Codex 最终回答', self.text)
        self.assertFalse(self.submit('create', '重复创建', 'journal-create-001')['queued'])
        self.submit('update', '第一次尝试，验证编辑效果', 'journal-update-001')
        self.assertTrue(self.text.startswith('第一次尝试，验证编辑效果\n\n#'))
        self.assertEqual([name for name, _ in self.calls], ['create_note', 'get_note', 'update_note'])
        update = self.calls[-1][1]
        self.assertEqual(update['recordNo'], 'NOTE-test')
        self.assertEqual(update['expectedVersion'], 1)
        self.assertEqual(update['mediaNos'], ['MEDIA-old'])
        duplicate = self.submit('update', '重复不覆盖', 'journal-update-001')
        self.assertFalse(duplicate['queued'])
        self.assertEqual(self.version, 2)

    def test_plain_log_without_user_identifier_generates_internal_id(self):
        with patch.object(a, 'probe', return_value={'ready': True}):
            result = a.publish({'mode': 'journal', 'journal_text': '直接输入文案即可',
                'session_id': 'plain-log-session', 'submission_id': 'plain-log-revision-001'}, self.config)
        self.assertTrue(result['queued'])
        self.assertRegex(result['entry_id'], r'^[0-9a-f-]{36}$')
        a.drain(self.config, result['job_id'], client_factory=lambda _: self.client)
        self.assertTrue(self.text.startswith('直接输入文案即可\n\n#'))
        self.assertNotIn(result['entry_id'], self.text)
        self.assertNotIn('用户问题', self.text)
        self.assertNotIn('Codex 最终回答', self.text)
        self.assertEqual(self.calls[0][0], 'create_note')

    def test_website_edit_conflict_stops_without_creating_replacement(self):
        self.submit('create', '第一次', 'journal-create-001')
        self.original_version = 9
        self.submit('update', '不能覆盖网页修改', 'journal-update-001')
        self.assertEqual([name for name, _ in self.calls], ['create_note', 'get_note'])
        with a.database(self.config) as db:
            error = json.loads(db.execute('SELECT error FROM jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0])
        self.assertEqual(error['code'], 'JOURNAL_CONFLICT')

    def test_no_target_and_other_session_do_not_modify_any_note(self):
        with self.assertRaises(ValueError):
            self.submit('update', '没有原记录', 'journal-update-001')
        self.submit('create', '第一次', 'journal-create-001')
        with self.assertRaises(ValueError):
            self.submit('update', '不能跨会话', 'journal-update-002', session_id='different-session')

    def test_failed_replacement_image_keeps_original_and_saves_text(self):
        self.submit('create', '第一次', 'journal-create-001')
        image = Path(self.temp.name) / 'test.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\nimage')
        self.submit('update', '修改正文', 'journal-update-001', image_mode='replace',
                     attachments=[{'source': 'user', 'path': str(image)}])
        self.assertEqual(self.calls[-1][1]['mediaNos'], ['MEDIA-old'])
        with a.database(self.config) as db:
            self.assertEqual(db.execute('SELECT status FROM jobs ORDER BY rowid DESC LIMIT 1').fetchone()[0], 'partial')

    def test_explicit_clear_and_offline_do_not_create_unexpected_content(self):
        self.submit('create', '第一次', 'journal-create-001')
        self.submit('update', '清空图片', 'journal-update-001', image_mode='clear')
        self.assertEqual(self.calls[-1][1]['mediaNos'], [])
        with patch.object(a, 'probe', return_value={'ready': False, 'reason': 'unavailable'}):
            result = a.publish({'mode': 'journal', 'journal_text': '离线不保存'}, self.config)
        self.assertFalse(result['queued'])
        with a.database(self.config) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 2)
