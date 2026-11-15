"""API imports stay lazy; canonical Runtime settings are checked at use time."""
import os
from functools import partial
import subprocess
import sys
import unittest
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from rag.rag_api import config


@contextmanager
def _settings_file(path):
    # Both settings classes read the same optional file, without mutating env.
    with patch.object(config, 'RuntimeSettings', partial(config.RuntimeSettings, _env_file=path)), \
         patch.object(config, '_WebhookSettings', partial(config._WebhookSettings, _env_file=path)):
        yield


class ApiConfigurationTests(unittest.TestCase):
    def test_imports_do_not_read_dotenv_or_require_credentials(self):
        script = """
import os
from pathlib import Path
from unittest.mock import patch
import fastapi
read_text = Path.read_text
def forbid_dotenv(path, *args, **kwargs):
    if path.name == '.env':
        raise AssertionError('dotenv read during import')
    return read_text(path, *args, **kwargs)
with patch.dict(os.environ, {}, clear=True), patch.object(Path, 'read_text', forbid_dotenv):
    import rag.rag_api.application
    import rag.rag_api.telegram.webhook
    import rag.rag_api.telegram.router
"""
        result = subprocess.run([sys.executable, '-B', '-c', script], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_environment_without_dotenv_and_validation(self):
        with TemporaryDirectory() as directory, _settings_file(Path(directory) / '.env'):
            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(ValueError, 'TELEGRAM_BOT_TOKEN'):
                    config.get_settings()
            values = {'TELEGRAM_BOT_TOKEN': '123:test-token', 'TELEGRAM_WEBHOOK_SECRET': 'test_secret',
                      'TELEGRAM_WEBHOOK_URL': 'https://example.test/'}
            with patch.dict(os.environ, values, clear=True):
                settings = config.get_settings()
                self.assertEqual(settings.public_url, 'https://example.test')
                self.assertEqual(settings.telegram_url, 'https://api.telegram.org/bot123:test-token')
                with patch.dict(os.environ, {'TELEGRAM_WEBHOOK_SECRET': 'invalid secret'}):
                    with self.assertRaisesRegex(ValueError, 'TELEGRAM_WEBHOOK_SECRET'):
                        config.get_settings()
                with patch.dict(os.environ, {'BOT_TOKEN': 'ignored-legacy-token'}):
                    self.assertEqual(config.get_settings().token, '123:test-token')

    def test_optional_dotenv_does_not_override_environment(self):
        with TemporaryDirectory() as directory:
            path = Path(directory) / '.env'
            path.write_text('TELEGRAM_BOT_TOKEN=123:local-token\nTELEGRAM_WEBHOOK_SECRET=local_secret\nTELEGRAM_WEBHOOK_URL=https://example.test\n', encoding='utf-8')
            with _settings_file(path), patch.dict(os.environ, {'TELEGRAM_BOT_TOKEN': '123:env-token'}, clear=True):
                settings = config.get_settings()
                self.assertEqual(settings.token, '123:env-token')
                self.assertEqual(settings.webhook_secret, 'local_secret')
                self.assertNotIn('TELEGRAM_WEBHOOK_SECRET', os.environ)
