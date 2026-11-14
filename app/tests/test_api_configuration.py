"""API imports work without secrets; configuration is required at use time."""
import os
import subprocess
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from rag.rag_api import config

class ApiConfigurationTests(unittest.TestCase):
    def test_imports_do_not_read_dotenv_or_require_credentials(self):
        script = """
import os
from pathlib import Path
from unittest.mock import patch
import fastapi
with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'read_text', side_effect=AssertionError('dotenv read during import')):
    import rag.rag_api.application
    import rag.rag_api.telegram.webhook
    import rag.rag_api.telegram.router
"""
        result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_environment_without_dotenv_and_validation(self):
        with TemporaryDirectory() as directory, patch.object(config, 'ENV_PATH', Path(directory)/'.env'):
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, 'BOT_TOKEN'):
                    config._get_values()
            values = {'BOT_TOKEN':'test-token', 'TELEGRAM_WEBHOOK_SECRET':'test_secret',
                      'TELEGRAM_WEBHOOK_URL':'https://example.test/'}
            with patch.dict(os.environ, values, clear=True):
                self.assertEqual(config.WEBHOOK_BASE_URL, 'https://example.test')
                self.assertEqual(config.TELEGRAM_API_URL, 'https://api.telegram.org/bottest-token')
                with patch.dict(os.environ, {'TELEGRAM_WEBHOOK_SECRET':'invalid secret'}):
                    with self.assertRaisesRegex(ValueError, 'TELEGRAM_WEBHOOK_SECRET'):
                        config._get_values()

    def test_optional_dotenv_does_not_override_environment(self):
        with TemporaryDirectory() as directory:
            path = Path(directory)/'.env'
            path.write_text('BOT_TOKEN=local-token\nTELEGRAM_WEBHOOK_SECRET=local_secret\nTELEGRAM_WEBHOOK_URL=https://example.test\n', encoding='utf-8')
            with patch.object(config, 'ENV_PATH', path), patch.dict(os.environ, {'BOT_TOKEN':'env-token'}, clear=True):
                self.assertEqual(config.BOT_TOKEN, 'env-token')
                self.assertEqual(config.WEBHOOK_SECRET, 'local_secret')
