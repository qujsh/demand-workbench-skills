import importlib.util
import json
from pathlib import Path
import socket
import tempfile
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('recovery', Path(__file__).with_name('availability.py'))
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)


class RecoveryTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.listener = socket.socket()
        self.listener.bind(('127.0.0.1', 0))
        self.addCleanup(self.listener.close)
        port = self.listener.getsockname()[1]
        self.config = {'enabled': True, 'url': f'http://127.0.0.1:{port}/mcp', 'state_dir': self.temp.name}

    def cache(self):
        return json.loads((api.directory(self.config) / 'offline.json').read_text())

    def test_actual_closed_then_listening_port_recovers_before_expiry(self):
        # macOS may time out on bound-but-not-listening sockets; close it to model a stopped server.
        address = self.listener.getsockname()
        self.listener.close()
        with patch.object(api.subprocess, 'run') as run:
            result = api.probe(self.config, now=100)
        self.assertEqual(result['reason'], 'local_port_closed')
        run.assert_not_called()
        checker = Mock(return_value=True)
        result = api.probe(self.config, checker=checker, now=110)
        self.assertTrue(result['cached'])
        checker.assert_not_called()
        self.assertEqual(self.cache()['until'], 160)
        self.listener = socket.socket()
        self.addCleanup(self.listener.close)
        self.listener.bind(address)
        self.listener.listen()
        result = api.probe(self.config, checker=checker, now=120)
        self.assertTrue(result['ready'])
        checker.assert_called_once()
        self.assertTrue(api.consume_ticket(self.config, result['availability_token'], now=121))
        self.assertFalse((api.directory(self.config) / 'offline.json').exists())

    def test_port_open_but_mcp_failure_does_not_extend_or_recheck_cache(self):
        api.note_failure(self.config, reason='local_port_closed', now=100)
        self.listener.listen()
        checker = Mock(return_value=False)
        self.assertFalse(api.probe(self.config, checker=checker, now=120)['ready'])
        self.assertEqual(self.cache(), {'until': 160, 'reason': 'unavailable'})
        with patch.object(api, 'local_port_status') as port:
            result = api.probe(self.config, checker=checker, now=130)
        self.assertEqual(result['retry_after'], 30)
        self.assertEqual(checker.call_count, 1)
        port.assert_not_called()

    def test_generic_cache_skips_port_and_force_checks_once_without_extending(self):
        api.note_failure(self.config, now=100)
        checker = Mock(return_value=False)
        with patch.object(api, 'local_port_status') as port:
            self.assertTrue(api.probe(self.config, checker=checker, now=120)['cached'])
            port.assert_not_called()
        checker.assert_not_called()
        result = api.probe(self.config, checker=checker, now=130, force=True)
        self.assertFalse(result['ready'])
        self.assertEqual(result['retry_after'], 30)
        self.assertEqual(self.cache()['until'], 160)
        checker.return_value = True
        self.assertTrue(api.probe(self.config, checker=checker, now=140, force=True)['ready'])

    def test_force_cannot_enable_disabled_archive(self):
        with patch.object(api, 'check_status') as check, patch.object(api, 'local_port_status') as port:
            self.assertEqual(api.probe({**self.config, 'enabled': False}, force=True)['reason'], 'disabled')
            check.assert_not_called()
            port.assert_not_called()

    def test_remote_urls_and_unknown_local_errors_do_not_trigger_early_recovery(self):
        with patch.object(api.socket, 'socket') as connection:
            self.assertEqual(api.local_port_status('https://api.freeligo.com/mcp'), 'unknown')
            connection.assert_not_called()
        api.note_failure(self.config, reason='local_port_closed', now=100)
        checker = Mock(return_value=True)
        with patch.object(api, 'local_port_status', return_value='unknown'):
            self.assertTrue(api.probe(self.config, checker=checker, now=120)['cached'])
        checker.assert_not_called()

    def test_force_cli_checks_without_publishing(self):
        import io
        spec = importlib.util.spec_from_file_location('recovery_archive', Path(__file__).with_name('archive.py'))
        archive = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(archive)
        with patch.object(archive, 'settings', return_value=self.config), patch.object(archive.AVAILABILITY, 'probe', return_value={'ready': False}) as check, patch.object(archive, 'kick') as worker, patch('sys.argv', ['archive', 'check', '--force']), patch('sys.stdout', new_callable=io.StringIO):
            archive.main()
        check.assert_called_once_with(self.config, force=True)
        worker.assert_not_called()
