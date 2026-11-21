import importlib.util
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('settings_archive', Path(__file__).with_name('archive.py'))
archive = importlib.util.module_from_spec(spec)
spec.loader.exec_module(archive)

class SettingsTest(unittest.TestCase):
    def test_endpoint_defaults_and_preserves_existing_configuration(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            config = root / 'config.json'
            with patch.object(archive, 'CONFIG', config), patch.object(archive, 'ROOT', root), patch.dict(os.environ, {}, clear=True):
                self.assertEqual(archive.settings()['url'], 'https://api.freeligo.com/mcp')
                self.assertFalse(archive.settings()['enabled'])
                with patch.dict(os.environ, {'DEMAND_WORKBENCH_MCP_URL': 'https://example.com/mcp'}):
                    self.assertEqual(archive.settings()['url'], 'https://example.com/mcp')
                    config.write_text(json.dumps({'url': 'http://localhost:8070/mcp', 'enabled': True}))
                    self.assertEqual(archive.settings()['url'], 'http://localhost:8070/mcp')
                    self.assertTrue(archive.settings()['enabled'])
                    archive.set_enabled(False)
                    self.assertEqual(json.loads(config.read_text())['url'], 'http://localhost:8070/mcp')
                    self.assertEqual(config.stat().st_mode & 0o777, 0o600)
