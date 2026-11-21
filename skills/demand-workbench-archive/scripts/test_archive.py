"""归档工具专项验证：脱敏、轮次去重、失败恢复、分段和本地配图。"""
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location('codex_archive', Path(__file__).with_name('archive.py'))
ARCHIVE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ARCHIVE)


class ArchiveTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'enabled': True, 'url': 'http://localhost:8070/mcp', 'state_dir': str(self.root / 'queue')}
        ready = patch.object(ARCHIVE, 'probe', return_value={'ready': True})
        ready.start()
        self.addCleanup(ready.stop)
        self.event = {'cwd': str(ARCHIVE.ROOT), 'session_id': 'test-session', 'turn_id': 'test-turn'}

    def capture(self, kind, **values):
        ARCHIVE.capture({**self.event, 'hook_event_name': kind, **values}, self.config)

    def jobs(self):
        with ARCHIVE.database(self.config) as db:
            return db.execute('SELECT * FROM jobs ORDER BY rowid').fetchall()

    def test_secrets_redacted_before_disk_and_duplicate_stops_create_one_job(self):
        self.capture('UserPromptSubmit', prompt='问题 dw_sk_' + 'A' * 43 + '\npassword="secret-value"\n密码：中文秘密\ntoken=other-secret')
        self.capture('UserPromptSubmit', prompt='补充要求')
        self.capture('Stop', last_assistant_message='回答 Authorization: Bearer very-secret-token')
        self.capture('Stop', last_assistant_message='重复事件不覆盖冻结内容')
        self.capture('UserPromptSubmit', prompt='冻结后的重复提问不重新落盘')
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        payload = jobs[0]['payload']
        self.assertIn('补充要求', payload)
        self.assertNotIn('dw_sk_', payload)
        self.assertNotIn('secret-value', payload)
        self.assertNotIn('中文秘密', payload)
        self.assertNotIn('other-secret', payload)
        self.assertNotIn('very-secret-token', payload)
        self.assertNotIn('重复事件', payload)
        self.assertEqual((Path(self.config['state_dir']) / 'queue.sqlite3').stat().st_mode & 0o777, 0o600)
        with ARCHIVE.database(self.config) as db:
            self.assertEqual(db.execute('SELECT COUNT(*) FROM prompts').fetchone()[0], 0)

    def test_full_text_and_images_split_without_loss(self):
        paths = []
        for index in range(10):
            path = self.root / ('test' + str(index) + '.png')
            path.write_bytes(b'\x89PNG\r\n\x1a\nimage')
            paths.append(path)
        answer = '😀' * 16000 + '\n' + '\n'.join('![图](<' + str(path) + '>)' for path in paths)
        self.capture('Stop', last_assistant_message=answer)
        parts = json.loads(self.jobs()[0]['payload'])['parts']
        self.assertGreaterEqual(len(parts), 3)
        self.assertEqual(sum(len(part['images']) for part in parts), 10)
        self.assertTrue(all(len(part['images']) <= 9 for part in parts))
        self.assertTrue(all(part['title'] == '' for part in parts))
        self.assertTrue(all(len(part['text'].encode('utf-16-le')) // 2 <= 20000 for part in parts))
        copied = parts[0]['images'][0]['path']
        paths[0].unlink()
        self.assertTrue(Path(copied).exists())

    def test_foreign_project_subagent_empty_response_not_archived(self):
        ARCHIVE.capture({**self.event, 'cwd': str(self.root), 'hook_event_name': 'Stop', 'last_assistant_message': 'foreign'}, self.config)
        self.capture('Stop', last_assistant_message='subagent', agent_id='child')
        self.capture('Stop', last_assistant_message='')
        self.assertEqual(self.jobs(), [])

    def test_failure_is_skipped_and_never_retried_even_after_recovery(self):
        self.capture('Stop', last_assistant_message='完成')
        calls = []
        class Client:
            def tool(self, name, arguments):
                calls.append(arguments)
                raise OSError('do-not-log-this-secret')
        job_id = self.jobs()[0]['id']
        ARCHIVE.drain(self.config, job_id=job_id, client_factory=lambda config: Client())
        self.assertEqual(self.jobs()[0]['status'], 'skipped')
        self.assertEqual(json.loads(self.jobs()[0]['error'])['code'], 'OSError')
        self.assertNotIn('do-not-log-this-secret', self.jobs()[0]['error'])
        ARCHIVE.drain(self.config, job_id=job_id, client_factory=lambda config: Client())
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.jobs()[0]['attempts'], 1)

    def test_partial_success_is_retained_without_resuming_remaining_parts(self):
        image = self.root / 'test.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\nimage')
        self.capture('Stop', last_assistant_message='![图](<' + str(image) + '>)\n' + 'x' * 20000)
        calls, uploads = [], []
        class Client:
            def upload(self, image, progress, save):
                uploads.append(image)
                return 'MEDIA-test'
            def tool(self, name, arguments):
                calls.append(arguments)
                if len(calls) == 2:
                    raise OSError('offline')
                return {'recordNo': 'NOTE-first'}
        job_id = self.jobs()[0]['id']
        ARCHIVE.drain(self.config, job_id=job_id, client_factory=lambda config: Client())
        ARCHIVE.drain(self.config, job_id=job_id, client_factory=lambda config: Client())
        self.assertEqual(len(uploads), 1)
        self.assertEqual(len(calls), 2)
        self.assertEqual(self.jobs()[0]['status'], 'skipped')
        self.assertEqual(json.loads(self.jobs()[0]['progress'])['0']['recordNo'], 'NOTE-first')

    def test_generic_drain_and_legacy_jobs_never_upload(self):
        self.capture('Stop', last_assistant_message='旧队列')
        job_id = self.jobs()[0]['id']
        with ARCHIVE.database(self.config) as db, db:
            payload = json.loads(self.jobs()[0]['payload'])
            payload.pop('deliveryPolicy')
            db.execute('UPDATE jobs SET payload=? WHERE id=?', (json.dumps(payload), job_id))
        client = __import__('unittest.mock').mock.Mock()
        ARCHIVE.drain(self.config, client_factory=client)
        ARCHIVE.drain(self.config, job_id=job_id, client_factory=client)
        client.assert_not_called()
        self.assertEqual(self.jobs()[0]['status'], 'skipped')

    def test_hook_and_retry_do_not_revive_old_queue(self):
        import io
        self.capture('Stop', last_assistant_message='旧任务')
        with patch.object(ARCHIVE, 'settings', return_value=self.config), patch.object(ARCHIVE, 'kick') as kick:
            for command in ('hook', 'retry'):
                with patch('sys.argv', ['archive', command]), patch('sys.stdin', io.StringIO('{}')), patch('sys.stdout', io.StringIO()):
                    ARCHIVE.main()
            kick.assert_not_called()
        self.assertEqual(self.jobs()[0]['attempts'], 0)

    def test_assistant_publish_redacts_and_deduplicates_submission(self):
        value = {'question': '问题 dw_sk_' + 'X' * 43, 'answer': '完整最终回复',
                 'session_id': 'assistant-session', 'submission_id': 'assistant-turn-001'}
        first = ARCHIVE.publish(value, self.config)
        second = ARCHIVE.publish(value, self.config)
        self.assertEqual(first, second)
        self.assertTrue(first['queued'])
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        text = json.loads(jobs[0]['payload'])['parts'][0]['text']
        self.assertIn('完整最终回复', text)
        self.assertIn('[已隐藏密钥]', text)
        self.assertNotIn('dw_sk_', text)

    def test_assistant_publish_uses_current_thread_and_preserves_answer(self):
        with patch.dict('os.environ', {'CODEX_THREAD_ID': 'current-thread'}):
            result = ARCHIVE.publish({'question': '这轮问题', 'answer': '第一段\n\n第二段'}, self.config)
        self.assertTrue(result['queued'])
        text = json.loads(self.jobs()[0]['payload'])['parts'][0]['text']
        self.assertNotIn('current-thread', text)
        self.assertIn('第一段\n\n第二段', text)
        self.assertTrue(text.endswith('#' + ARCHIVE.archive_tag_path('')))
        self.assertEqual(json.loads(self.jobs()[0]['payload'])['session_id'], 'current-thread')
        with self.assertRaises(ValueError):
            ARCHIVE.publish({'question': '问题', 'answer': ''}, self.config)

    def test_user_attachments_snapshot_deduplicate_and_keep_source(self):
        image = self.root / '用户截图.png'
        image.write_bytes(b'\x89PNG\r\n\x1a\nimage')
        other = self.root / '回复图.png'
        other.write_bytes(b'\x89PNG\r\n\x1a\nother')
        ARCHIVE.publish({'question': '截图问题', 'answer': '![同图](' + str(image) + ')',
                         'session_id': 'images-session', 'submission_id': 'images-test-001',
                         'attachments': [{'source': 'user', 'path': str(image)},
                                         {'source': 'assistant', 'path': str(other)}]}, self.config)
        images = json.loads(self.jobs()[0]['payload'])['parts'][0]['images']
        self.assertEqual([item['source'] for item in images], ['user', 'assistant'])
        image.unlink()
        self.assertTrue(Path(images[0]['path']).is_file())
        self.assertEqual(Path(images[0]['path']).stat().st_mode & 0o777, 0o600)

    def test_markdown_image_examples_are_not_uploaded(self):
        value = '`![示例](/tmp/example.png)`\n```md\n![示例](/tmp/inside.png)\n```\n![实际](/tmp/real.png)'
        self.assertEqual(ARCHIVE.markdown_images(value, 'assistant'),
                         [{'path': '/tmp/real.png', 'source': 'assistant'}])

    def test_clean_title_footer_and_session_title_cache(self):
        value = {'question': '自然的问题标题', 'answer': '完整回复', 'session_id': 'named-session',
                 'submission_id': 'named-test-001', 'thread_title': '查看后端 MCP 是否可用'}
        ARCHIVE.publish(value, self.config)
        part = json.loads(self.jobs()[0]['payload'])['parts'][0]
        self.assertEqual(part['title'], '')
        self.assertEqual(part['text'], '用户问题：\n自然的问题标题\n\nCodex 最终回答：\n完整回复'
                         '\n\n#' + ARCHIVE.archive_tag_path('查看后端 MCP 是否可用'))
        ARCHIVE.publish({**value, 'thread_title': '', 'submission_id': 'named-test-002'}, self.config)
        self.assertTrue(json.loads(self.jobs()[1]['payload'])['parts'][0]['text'].endswith('#' + ARCHIVE.archive_tag_path('查看后端 MCP 是否可用')))

    def test_attachment_errors_preserve_text_and_bad_contract_rejected(self):
        ARCHIVE.publish({'question': '问题', 'answer': '回复', 'session_id': 'missing-session',
                         'attachments': [{'source': 'user', 'path': str(self.root / 'missing.png')}]}, self.config)
        part = json.loads(self.jobs()[0]['payload'])['parts'][0]
        self.assertEqual(part['images'], [])
        self.assertIn('无法归档', part['text'])
        with self.assertRaises(ValueError):
            ARCHIVE.publish({'question': '问题', 'answer': '回复', 'session_id': 'bad-session',
                             'attachments': [{'source': 'user', 'path': 'relative.png'}]}, self.config)
        self.assertEqual(len(self.jobs()), 1)

    def test_hierarchy_paths_are_bounded_stable_and_keep_segments_separate(self):
        from unittest.mock import patch
        with patch.object(ARCHIVE, 'ROOT', Path('/tmp/demand-workbench')):
            first = ARCHIVE.archive_tag_path('查看后端 MCP 是否可用')
            self.assertEqual(first.split('/')[:2], ['Codex', 'demand-workbench'])
            self.assertEqual(len(first.split('/')), 3)
            self.assertEqual(first, 'Codex/demand-workbench/查看后端MCP是否可用')
            self.assertEqual(first, ARCHIVE.archive_tag_path('查看后端 MCP 是否可用'))
            self.assertNotEqual(first, ARCHIVE.archive_tag_path('查看后端 MCP 另外的话题'))
            self.assertEqual(ARCHIVE.archive_tag_path(''), 'Codex/demand-workbench')
            self.assertEqual(ARCHIVE.footer_tags('#' + first, '查看后端 MCP 是否可用'), '')
            self.assertEqual(len(ARCHIVE.archive_tag_path('话题/子话题').split('/')), 3)
        with patch.object(ARCHIVE, 'ROOT', Path('/tmp/' + '很长的项目名称' * 10)):
            short = ARCHIVE.archive_tag_path('很长的聊天名称' * 10)
            self.assertLessEqual(len(short), 92)
            self.assertTrue(all(len(part) <= 30 for part in short.split('/')))
            self.assertEqual(short.split('/')[1], ARCHIVE.archive_tag_path('').split('/')[1])
        full = ' '.join('#业务' + str(i) for i in range(10))
        self.assertEqual(ARCHIVE.footer_tags(full, '新话题'), '')

    def test_tags_respect_backend_budget_and_long_thread_name(self):
        text = ' '.join('#业务' + str(i) for i in range(9))
        self.assertEqual(ARCHIVE.footer_tags(text, '聊天' * 50), '\n\n#' + ARCHIVE.archive_tag_path('聊天' * 50))
        tags = ARCHIVE.footer_tags('', '聊天 # 空格' * 30).split()
        self.assertTrue(all(len(tag[1:]) <= 92 and tag.count('#') == 1 for tag in tags))

    def test_assistant_mode_disables_cached_hooks_and_disabled_publish_does_not_queue(self):
        import io
        config = {**self.config, 'mode': 'assistant'}
        with patch.object(ARCHIVE, 'settings', return_value=config), patch.object(ARCHIVE, 'kick') as kick, \
                patch.object(ARCHIVE, 'capture') as capture, patch('sys.argv', ['archive', 'hook']), \
                patch('sys.stdin', io.StringIO('{}')):
            ARCHIVE.main()
            capture.assert_not_called()
            kick.assert_not_called()
        with patch.object(ARCHIVE, 'settings', return_value={**config, 'enabled': False}), \
                patch('sys.argv', ['archive', 'publish']), patch('sys.stdout', new_callable=io.StringIO) as output:
            ARCHIVE.main()
        self.assertFalse(json.loads(output.getvalue())['queued'])
        self.assertEqual(self.jobs(), [])

    def test_global_pause_cannot_be_overridden_and_client_is_bundled(self):
        global_config = self.root / 'global.json'
        global_config.write_text('{"enabled": false, "upload_helper": "/unavailable/backend.py"}')
        project = self.root / 'project'
        (project / '.codex').mkdir(parents=True)
        local = project / '.codex/archive.local.json'
        local.write_text('{"enabled": true}')
        with patch.object(ARCHIVE, 'CONFIG', global_config), patch.object(ARCHIVE, 'ROOT', project):
            config = ARCHIVE.settings()
            self.assertFalse(config['enabled'])
            self.assertTrue(Path(config['upload_helper']).is_file())
            self.assertNotIn('/unavailable/', config['upload_helper'])
            global_config.write_text('{"enabled": true}')
            local.write_text('{"enabled": false}')
            self.assertFalse(ARCHIVE.settings()['enabled'])

    def test_separate_project_queues_and_tags_with_same_thread_and_turn(self):
        value = {'question': '跨项目测试', 'answer': '原文', 'session_id': 'shared-session',
                 'submission_id': 'same-turn-001'}
        outputs = []
        global_config = self.root / 'global.json'
        global_config.write_text('{"enabled": true}')
        with patch.object(ARCHIVE, 'CONFIG', global_config), patch.object(ARCHIVE, 'CODEX_DIR', self.root / 'codex'):
            for name in ('project-one', 'project-two'):
                project = self.root / name
                project.mkdir()
                with patch.object(ARCHIVE, 'ROOT', project.resolve()):
                    config = ARCHIVE.settings()
                    result = ARCHIVE.publish(value, config)
                    with ARCHIVE.database(config) as db:
                        payload = json.loads(db.execute('SELECT payload FROM jobs').fetchone()[0])
                    self.assertTrue(payload['parts'][0]['text'].endswith('#Codex/' + name))
                    outputs.append((result['job_id'], config['state_dir']))
        self.assertNotEqual(outputs[0][0], outputs[1][0])
        self.assertNotEqual(outputs[0][1], outputs[1][1])

    def test_enable_disable_validates_url_without_network_and_keeps_config_private(self):
        import io
        config = self.root / 'settings/config.json'
        with patch.object(ARCHIVE, 'CONFIG', config), patch('sys.stdout', new_callable=io.StringIO):
            with self.assertRaises(ValueError):
                ARCHIVE.set_enabled(True, 'http://example.com/mcp')
            self.assertFalse(config.exists())
            ARCHIVE.set_enabled(True, 'http://localhost:8070/mcp')
            self.assertEqual(config.stat().st_mode & 0o777, 0o600)
            ARCHIVE.set_enabled(False)
            saved = json.loads(config.read_text())
            self.assertFalse(saved['enabled'])
            self.assertEqual(saved['url'], 'http://localhost:8070/mcp')

    def test_subdirectory_resolves_git_root_and_non_git_uses_cwd(self):
        import os
        import subprocess
        project = self.root / 'git-project'
        child = project / 'child'
        child.mkdir(parents=True)
        subprocess.run(['git', 'init', '--quiet', str(project)], check=True)
        previous = Path.cwd()
        try:
            os.chdir(child)
            self.assertEqual(ARCHIVE.project_root(), project.resolve())
            os.chdir(self.root)
            self.assertEqual(ARCHIVE.project_root(), self.root.resolve())
        finally:
            os.chdir(previous)

    def test_offline_publish_does_not_create_queue_or_copy_attachments(self):
        with patch.object(ARCHIVE, 'probe', return_value={'ready': False, 'reason': 'unavailable'}), patch.object(ARCHIVE, 'snapshot_images') as snapshot:
            result = ARCHIVE.publish({'question': '不应落盘', 'answer': '回复'}, self.config)
        self.assertFalse(result['queued'])
        snapshot.assert_not_called()
        self.assertFalse(Path(self.config['state_dir']).exists())

    def test_offline_cache_expires_without_sliding_or_background_polling(self):
        calls = []
        def checker(config):
            calls.append(1)
            return len(calls) > 1
        api = ARCHIVE.AVAILABILITY
        self.assertFalse(api.probe(self.config, checker=checker, now=100)['ready'])
        result = api.probe(self.config, checker=checker, now=150)
        self.assertTrue(result['cached'])
        self.assertEqual(result['retry_after'], 10)
        self.assertEqual(len(calls), 1)
        result = api.probe(self.config, checker=checker, now=160)
        self.assertTrue(result['ready'])
        self.assertEqual(len(calls), 2)
        self.assertTrue(api.consume_ticket(self.config, result['availability_token'], now=161))
        self.assertFalse(api.consume_ticket(self.config, result['availability_token'], now=161))

    def test_offline_cache_is_shared_across_projects_but_isolated_by_endpoint(self):
        api = ARCHIVE.AVAILABILITY
        other = {**self.config, 'state_dir': str(self.root / 'other-queue')}
        api.note_failure(self.config, now=100)
        checker = __import__('unittest.mock').mock.Mock(return_value=True)
        self.assertFalse(api.probe(other, checker=checker, now=101)['ready'])
        checker.assert_not_called()
        self.assertTrue(api.probe({**other, 'url': 'http://localhost:8071/mcp'}, checker=checker, now=101)['ready'])
        checker.assert_called_once()

    def test_online_ticket_avoids_second_probe_and_is_single_use_project_bound(self):
        api = ARCHIVE.AVAILABILITY
        ready = api.probe(self.config, checker=lambda config: True)
        value = {'question': '问题', 'answer': '回复', 'session_id': 'ticket-session',
                 'availability_token': ready['availability_token']}
        with patch.object(ARCHIVE, 'probe') as probe:
            result = ARCHIVE.publish(value, self.config)
            self.assertTrue(result['queued'])
            probe.assert_not_called()
        ready = api.probe(self.config, checker=lambda config: True, now=100)
        self.assertFalse(api.consume_ticket(self.config, ready['availability_token'], now=221))
        ready = api.probe(self.config, checker=lambda config: True, now=100)
        other = {**self.config, 'state_dir': str(self.root / 'other')}
        self.assertFalse(api.consume_ticket(other, ready['availability_token'], now=101))

    def test_worker_uploads_only_requested_current_job_once(self):
        self.capture('Stop', last_assistant_message='历史任务')
        current = ARCHIVE.publish({'question': '新问题', 'answer': '新回复', 'session_id': 'current-session',
                                   'submission_id': 'current-job-001'}, self.config)
        calls = []
        class Client:
            def tool(self, name, arguments):
                calls.append(arguments)
                return {'recordNo': 'NOTE-current'}
        ARCHIVE.drain(self.config, job_id=current['job_id'], client_factory=lambda config: Client())
        ARCHIVE.drain(self.config, job_id=current['job_id'], client_factory=lambda config: Client())
        self.assertEqual(len(calls), 1)
        self.assertIn('新问题', calls[0]['text'])
        self.assertEqual(self.jobs()[0]['attempts'], 0)
        self.assertEqual(self.jobs()[1]['status'], 'done')

    def test_complete_probe_process_has_two_second_deadline(self):
        import subprocess
        api = ARCHIVE.AVAILABILITY
        with patch.object(api.subprocess, 'run', side_effect=subprocess.TimeoutExpired('probe', 2)) as run:
            self.assertFalse(api.check_once(self.config))
            self.assertEqual(run.call_args.kwargs['timeout'], 2)

    def test_publish_cli_does_not_start_worker_when_offline(self):
        import io
        with patch.object(ARCHIVE, 'settings', return_value=self.config), patch.object(ARCHIVE, 'probe', return_value={'ready': False, 'reason': 'unavailable'}), patch.object(ARCHIVE, 'kick') as kick, patch('sys.argv', ['archive', 'publish']), patch('sys.stdin', io.StringIO('{"question":"问题","answer":"回复"}')), patch('sys.stdout', io.StringIO()) as output:
            ARCHIVE.main()
        kick.assert_not_called()
        self.assertFalse(json.loads(output.getvalue())['queued'])
        self.assertFalse(Path(self.config['state_dir']).exists())


if __name__ == '__main__':
    unittest.main()
