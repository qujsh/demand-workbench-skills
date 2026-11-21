import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch


def load(name):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module

n, a, numbered = load('native_edit'), load('archive'), load('numbered_journal')


def snapshot(ids, trigger='composer'):
    return {'session': 'session-native', 'turn': ids[-1], 'trigger': trigger, 'ids': ids}


class RoutingTest(unittest.TestCase):
    def test_repeated_and_earlier_edit_target_first_removed_turn(self):
        bindings = {'turn0001': 'entry-a', 'turn0002': 'entry-b', 'turn0003': 'entry-c'}
        first = snapshot(['turn0001', 'turn0004'], 'edit_user_message')
        self.assertEqual(n.choose(['turn0001', 'turn0002', 'turn0003'], first, bindings), 'entry-b')
        bindings['turn0004'] = 'entry-b'
        self.assertEqual(n.choose(first['ids'], snapshot(['turn0001', 'turn0005'], 'edit_user_message'), bindings), 'entry-b')

    def test_new_composer_is_new_even_after_offline_history_change(self):
        entry = n.choose(['turn0001', 'turn0002'], snapshot(['offline1', 'turn0003']), {'turn0002': 'entry-b'})
        self.assertNotEqual(entry, 'entry-b')

    def test_duplicate_uses_existing_entry(self):
        self.assertEqual(n.choose(['turn0001'], snapshot(['turn0001']), {'turn0001': 'entry-a'}), 'entry-a')

    def test_missing_baseline_unmapped_or_ambiguous_edit_fails_closed(self):
        for previous, current, bindings in [
            (None, snapshot(['turn0002'], 'edit_user_message'), {}),
            (['turn0001'], snapshot(['turn0002'], 'edit_user_message'), {}),
            (['turn0001'], snapshot(['offline1', 'turn0002'], 'edit_user_message'), {'turn0001': 'entry-a'}),
        ]:
            with self.assertRaises(n.NativeEditError): n.choose(previous, current, bindings)

    def test_stale_and_unsupported_metadata_rejected(self):
        with self.assertRaises(n.NativeEditError): n.choose(None, {**snapshot(['turn0001']), 'turn': 'other'}, {})
        with self.assertRaises(n.NativeEditError): n.choose(None, snapshot(['turn0001'], 'unknown'), {})


class QueueTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.config = {'enabled': True, 'url': 'http://localhost:8070/mcp', 'state_dir': self.temp.name}

    def queue(self, ids, trigger='composer', **extra):
        return n.queue({'question': '问题', 'answer': '完整回答', 'attachments': [],
                        'session_id': 'session-native', **extra}, self.config, vars(a), numbered, snapshot(ids, trigger))

    def finish(self, result):
        with a.database(self.config) as db, db:
            db.execute("UPDATE jobs SET status='done' WHERE id=?", (result['job_id'],))

    def payload(self, result):
        with a.database(self.config) as db:
            return json.loads(db.execute('SELECT payload FROM jobs WHERE id=?', (result['job_id'],)).fetchone()[0])

    def test_edit_reuses_source_and_deduplicates_actual_turn(self):
        first = self.queue(['turn0001']); self.finish(first)
        second = self.queue(['turn0002'], 'edit_user_message', question='修改后的问题')
        p = self.payload(second)
        self.assertEqual(self.payload(first)['source'], p['source'])
        self.assertIn('修改后的问题', p['parts'][0]['text'])
        self.assertTrue(p['native']['requires_existing'])
        self.assertFalse(self.queue(['turn0002'], 'edit_user_message', submission_id='different000')['queued'])

    def test_journal_has_no_qa_wrapper_and_id_is_not_in_body(self):
        result = self.queue(['turn0001'], mode='journal', journal_text='只保存这段文字')
        p = self.payload(result)
        self.assertTrue(p['parts'][0]['text'].startswith('只保存这段文字\n\n#'))
        self.assertNotIn(p['source']['messageId'], p['parts'][0]['text'])
        self.assertNotIn('Codex 最终回答', p['parts'][0]['text'])

    def test_failed_enqueue_does_not_advance_snapshot(self):
        first = self.queue(['turn0001'])
        with self.assertRaises(ValueError): self.queue(['turn0002'], 'edit_user_message')
        with a.database(self.config) as db:
            ids = json.loads(db.execute('SELECT ids FROM native_snapshots').fetchone()[0])
            self.assertEqual(ids, ['turn0001'])
        self.finish(first)
        self.assertTrue(self.queue(['turn0002'], 'edit_user_message')['queued'])

    def test_explicit_latest_update_keeps_native_record(self):
        first = self.queue(['turn0001'], mode='journal', journal_text='原记录'); self.finish(first)
        qa = self.queue(['turn0001', 'turn0002']); self.finish(qa)
        updated = self.queue(['turn0001', 'turn0002', 'turn0003'], mode='journal', operation='update',
                             entry_id='latest', journal_text='明确修改刚才的记录')
        self.assertEqual(self.payload(first)['source'], self.payload(updated)['source'])
        self.assertTrue(self.payload(updated)['native']['requires_existing'])

    def test_explicit_unknown_update_does_not_create(self):
        with self.assertRaises(n.NativeEditError):
            self.queue(['turn0001'], mode='journal', operation='update', entry_id='latest', journal_text='修改')
        with a.database(self.config) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM jobs').fetchone()[0], 0)

    def test_missing_original_stops_without_new_note_or_image_upload(self):
        first = self.queue(['turn0001']); self.finish(first)
        edited = self.queue(['turn0002'], 'edit_user_message')
        calls = []
        class Client:
            def tool(self, name, args):
                calls.append(name)
                if name == 'get_codex_note': return {'found': False, 'sourceRevision': 0}
                raise AssertionError(name)
        a.drain(self.config, edited['job_id'], client_factory=lambda _: Client())
        self.assertEqual(calls, ['get_codex_note'])
        with a.database(self.config) as db:
            self.assertEqual(db.execute('SELECT status FROM jobs WHERE id=?', (edited['job_id'],)).fetchone()[0], 'skipped')

    def test_offline_skips_metadata_and_content(self):
        with patch.object(a, 'probe', return_value={'ready': False, 'reason': 'unavailable'}):
            result = a.publish({'question': '不落盘', 'answer': '不落盘'}, {**self.config, 'native_edit_enabled': True})
        self.assertFalse(result['queued'])
        self.assertFalse((Path(self.temp.name) / 'queue.sqlite3').exists())


if __name__ == '__main__': unittest.main()
