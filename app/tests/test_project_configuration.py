"""One env source across modules, CWDs and explicit process overrides."""

import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from rag import _configuration
from rag.contracts import StepError
from rag.runtime._config import RuntimeSettings
from rag.rag_api.config import _WebhookSettings
from rag.rag_core._settings import _Settings as CoreSettings
from rag.guardrails._config import GuardrailsSettings
from rag.ingestion._config import ConverterSettings
from rag.ingestion._gateway_settings import _GatewaySettings
from rag.ingestion._settings import _Settings as WorkerSettings
from rag.ingestion._chunking import _ChunkSettings
from rag.model_gateway._config import _Settings as ModelSettings
from rag.model_gateway.ollama import _Options


class ProjectConfigurationTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.path = self.root / '.env'
        self.path.write_text(
            'PGUSER=file-user\nPGPASSWORD=file-password\n'
            'TELEGRAM_WEBHOOK_SECRET=file-webhook\n'
            'RAG_CORE_MAX_ATTEMPTS=7\nRAG_GUARDRAILS_MAX_QUESTION_CHARS=123\n'
            'RAG_CONVERTER_MAX_IMAGES=17\nRAG_INGESTION_TOKEN=file-token\n'
            'MODEL_GATEWAY_QUEUE_SIZE=19\nOLLAMA_GATEWAY_KEEP_ALIVE=5m\n'
            'INGESTION_CHUNK_CHARACTERS=300\nINGESTION_CHUNK_OVERLAP=30\n'
            'INGESTION_EMBEDDING_PROFILE=embedding-test\n'
            'INGESTION_GENERATION_PROFILE=generation-test\n'
            'INGESTION_PREPARATION_PROFILE=preparation-test\n'
            'INGESTION_EMBEDDING_DIMENSIONS=3\n'
            'INGESTION_EMBEDDING_INPUT_CHARACTERS=400\n'
            'INGESTION_TOPIC_INPUT_CHARACTERS=500\n'
            'INGESTION_GENERATION_MAX_OUTPUT_TOKENS=100\n'
            'SELECTED_LOGIN=${PGUSER}\n', encoding='utf-8',
        )
        self.enterContext(patch.object(_configuration, '_DEFAULT_ENV_FILE', self.path))
        self.enterContext(patch.dict(os.environ, {}, clear=True))

    def test_every_settings_reader_uses_root_even_from_an_unrelated_cwd(self):
        other = self.root / 'elsewhere'
        other.mkdir()
        (other / '.env').write_text('PGUSER=wrong-local-user\n', encoding='utf-8')
        previous = Path.cwd()
        try:
            os.chdir(other)
            self.assertEqual(RuntimeSettings().pg_user, 'file-user')
            self.assertEqual(_WebhookSettings().secret.get_secret_value(), 'file-webhook')
            self.assertEqual(CoreSettings().max_attempts, 7)
            self.assertEqual(GuardrailsSettings().max_question_chars, 123)
            self.assertEqual(ConverterSettings().max_images, 17)
            self.assertEqual(_GatewaySettings().token.get_secret_value(), 'file-token')
            self.assertEqual(ModelSettings().queue_size, 19)
            self.assertEqual(_Options().keep_alive, '5m')
            self.assertEqual(WorkerSettings.from_environment().embedding_dimensions, 3)
            self.assertEqual(_ChunkSettings.from_environment().characters, 300)
            self.assertNotIn('PGPASSWORD', os.environ)
        finally:
            os.chdir(previous)

    def test_explicit_environment_path_and_process_values_take_priority(self):
        alternate = self.root / 'override.env'
        alternate.write_text('PGUSER=other-file\nRAG_CORE_MAX_ATTEMPTS=4\n', encoding='utf-8')
        os.environ.update(RAG_ENV_FILE=str(alternate), PGUSER='process-user')
        self.assertEqual(RuntimeSettings().pg_user, 'process-user')
        self.assertEqual(CoreSettings().max_attempts, 4)
        self.assertEqual(_configuration.environment_values()['PGUSER'], 'process-user')
        # _env_file=None must really disable file loading for isolated callers.
        self.assertEqual(CoreSettings(_env_file=None).max_attempts, 3)

    def test_worker_and_chunk_validation_still_apply_to_file_values(self):
        os.environ['INGESTION_EMBEDDING_INPUT_CHARACTERS'] = '100'
        with self.assertRaises(StepError):
            _ChunkSettings.from_environment()
        os.environ['INGESTION_EMBEDDING_DIMENSIONS'] = 'invalid'
        with self.assertRaises(StepError):
            WorkerSettings.from_environment()

    def test_interpolation_uses_explicit_process_login(self):
        os.environ['PGUSER'] = 'process-login'
        self.assertEqual(_configuration.environment_values()['SELECTED_LOGIN'], 'process-login')
