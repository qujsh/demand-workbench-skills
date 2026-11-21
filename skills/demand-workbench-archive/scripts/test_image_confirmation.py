import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import unittest
from unittest.mock import patch


def load(name, filename):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).with_name(filename))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


archive = load('confirm_archive', 'archive.py')
api = load('confirm_api', 'mcp_client.py')


class ConfirmationTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = {'enabled': True, 'url': 'http://localhost:8070/mcp', 'state_dir': str(self.root / 'queue')}
        self.client = archive.Client.__new__(archive.Client)
        self.client.api, self.client.url, self.client.secret = api, self.config['url'], 'never-log-this-secret'
        self.image = self.root / 'image.png'
        self.image.write_bytes(b'\x89PNG\r\n\x1a\nimage')
        self.ticket = {'key': 'object-key', 'mediaNo': 'MEDIA-test', 'token': 'private-upload-token', 'uploadUrl': 'https://upload.example.com'}

    def run_job(self, failures, images=1, create_failure=None):
        with patch.object(archive, 'probe', return_value={'ready': True}):
            job = archive.publish({'question': '原始问题', 'answer': '**原始 Markdown**',
                'session_id': 'confirmation-session', 'submission_id': 'confirmation-test',
                'attachments': [{'path': str(self.image), 'source': 'user'}]}, self.config)
        # A second valid image proves failed confirmation does not hide other confirmed images.
        if images == 2:
            with archive.database(self.config) as db, db:
                payload = json.loads(db.execute('SELECT payload FROM jobs').fetchone()[0])
                payload['parts'][0]['images'].append(dict(payload['parts'][0]['images'][0]))
                db.execute('UPDATE jobs SET payload=?', (json.dumps(payload),))
        calls = []
        def tool(url, secret, name, args):
            calls.append((name, args))
            if name == 'prepare_note_image_upload':
                return self.ticket
            if create_failure:
                raise create_failure
            return {'recordNo': 'NOTE-test'}
        with patch.object(api, 'tool', side_effect=tool), patch.object(api, 'request_json', return_value={'key': 'object-key'}) as direct, \
             patch.object(api, 'bounded_confirm', side_effect=failures) as confirm, \
             patch.object(archive.AVAILABILITY, 'note_failure') as offline:
            archive.drain(self.config, job_id=job['job_id'], client_factory=lambda _: self.client)
            archive.drain(self.config, job_id=job['job_id'], client_factory=lambda _: self.client)
            with archive.database(self.config) as db:
                row = dict(db.execute('SELECT * FROM jobs').fetchone())
            return row, calls, confirm.call_count, direct.call_count, offline.call_count

    def test_two_temporary_failures_then_success_uploads_file_only_once(self):
        row, calls, attempts, uploads, offline = self.run_job([
            api.McpError('S048105012', 'CALL-first', True), api.McpError('S048105013', 'CALL-second', True), {}])
        self.assertEqual((row['status'], attempts, uploads, offline), ('done', 3, 1, 0))
        self.assertEqual(calls[-1][1]['mediaNos'], ['MEDIA-test'])
        self.assertIn('**原始 Markdown**', calls[-1][1]['text'])
        self.assertNotIn('confirmationError', json.loads(row['progress'])['0']['images']['0'])

    def test_three_failures_save_text_and_other_confirmed_images_without_retrying_job(self):
        failure = api.McpError('S048105012', 'CALL-timeout', True)
        row, calls, attempts, uploads, offline = self.run_job([failure, failure, failure, {}], images=2)
        self.assertEqual((row['status'], attempts, uploads, offline), ('partial', 4, 2, 0))
        self.assertEqual(calls[-1][1]['mediaNos'], ['MEDIA-test'])
        progress = json.loads(row['progress'])['0']
        self.assertEqual(progress['imageFailures']['0']['attempts'], 3)
        self.assertEqual(progress['imageFailures']['0']['requestNo'], 'CALL-timeout')
        self.assertEqual(progress['recordNo'], 'NOTE-test')
        self.assertNotIn('private-upload-token', row['progress'])
        self.assertNotIn('never-log-this-secret', row['progress'])
        with patch.object(archive, 'settings', return_value=self.config), patch('sys.argv', ['archive', 'status']), patch('sys.stdout', new_callable=io.StringIO) as output:
            archive.main()
        status = json.loads(output.getvalue())['recent'][0]
        self.assertEqual(status['images'][0]['confirmationAttempts'], 3)
        self.assertEqual(status['images'][0]['confirmationError']['requestNo'], 'CALL-timeout')
        self.assertFalse(status['images'][0]['confirmed'])
        self.assertTrue(status['images'][1]['confirmed'])

    def test_permanent_failure_is_not_retried_but_text_is_saved(self):
        row, calls, attempts, uploads, offline = self.run_job([api.McpError('S048105003')])
        self.assertEqual((row['status'], attempts, offline), ('partial', 1, 0))
        self.assertEqual(calls[-1][1]['mediaNos'], [])
        self.assertIn('**原始 Markdown**', calls[-1][1]['text'])

    def test_save_failure_is_skipped_and_only_mcp_network_failure_marks_offline(self):
        row, calls, attempts, uploads, offline = self.run_job([{}], create_failure=api.McpError('NETWORK_ERROR', retryable=True, offline=True))
        self.assertEqual((row['status'], offline), ('skipped', 1))
        self.assertEqual(json.loads(row['error'])['stage'], 'save_note')
        self.assertEqual(sum(name == 'create_note' for name, _ in calls), 1)

    def test_bounded_child_has_three_second_deadline_and_no_secret_in_arguments(self):
        with patch.object(api.subprocess, 'run', side_effect=subprocess.TimeoutExpired('worker', 3)) as run:
            with self.assertRaises(api.McpError) as caught:
                api.bounded_confirm(self.client.url, self.client.secret, 'MEDIA-test')
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(run.call_args.kwargs['timeout'], 3)
        self.assertNotIn(self.client.secret, str(run.call_args.args))

    def test_rpc_preserves_safe_code_request_and_discards_remote_secrets(self):
        response = {'result': {'isError': True, 'content': [{'text': 'secret-value'}],
            'structuredContent': {'errorCode': 'S048105012', 'requestNo': 'CALL-safe', 'retryable': True}}}
        with patch.object(api, 'request_json', return_value=response):
            with self.assertRaises(api.McpError) as caught:
                api.tool(self.client.url, self.client.secret, 'complete_note_image_upload', {})
        detail = caught.exception.detail()
        self.assertEqual(detail['requestNo'], 'CALL-safe')
        self.assertNotIn('secret-value', json.dumps(detail))
        self.assertTrue(detail['retryable'])
        self.assertEqual(api.McpError('dw_sk_' + 'A' * 43).detail()['code'], 'REMOTE_ERROR')

    def test_real_child_process_stops_slow_confirmation_within_deadline(self):
        release = threading.Event()
        started_request = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_POST(self):
                self.rfile.read(int(self.headers.get('Content-Length', 0)))
                started_request.set()
                release.wait(6)
                try:
                    self.send_response(200)
                    self.end_headers()
                    self.wfile.write(b'{"result":{"structuredContent":{}}}')
                except (BrokenPipeError, ConnectionResetError):
                    pass
        server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            started = time.monotonic()
            with self.assertRaises(api.McpError) as caught:
                api.bounded_confirm('http://127.0.0.1:' + str(server.server_port) + '/mcp', 'test-only', 'MEDIA-test')
            self.assertTrue(started_request.is_set())
            self.assertEqual(caught.exception.code, 'NETWORK_TIMEOUT')
            self.assertLess(time.monotonic() - started, 4)
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)
