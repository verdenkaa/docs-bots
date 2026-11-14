"""Required deployment profiles and fail-fast configuration validation."""

import os
import unittest
from unittest.mock import patch

from pydantic import ValidationError

from rag.contracts import StepError
from rag.ingestion._settings import _Settings
from rag.ingestion._tests._support import _Fixtures


class SettingsTests(unittest.TestCase):
    def test_required_settings_are_read_only_when_requested(self):
        values = {
            "INGESTION_EMBEDDING_PROFILE": "embedding-test",
            "INGESTION_GENERATION_PROFILE": "generation-test",
            "INGESTION_PREPARATION_PROFILE": "preparation-test-v1",
            "INGESTION_EMBEDDING_DIMENSIONS": "2",
            "INGESTION_EMBEDDING_INPUT_CHARACTERS": "4000",
            "INGESTION_TOPIC_INPUT_CHARACTERS": "4000",
            "INGESTION_GENERATION_MAX_OUTPUT_TOKENS": "128",
        }
        with patch.dict(os.environ, values, clear=True):
            settings = _Settings.from_environment()
        self.assertEqual(settings.embedding_dimensions, 2)
        self.assertEqual(settings.generation_profile, "generation-test")

    def test_missing_profiles_do_not_select_a_default_model(self):
        with patch.dict(os.environ, {}, clear=True), self.assertRaises(StepError) as result:
            _Settings.from_environment()
        self.assertEqual(result.exception.code, "ingestion_configuration_error")
        self.assertFalse(result.exception.retryable)

    def test_timeouts_and_backoff_are_finite_and_positive(self):
        for values in ({"embedding_timeout_s": float("inf")}, {"step_timeout_s": 0},
                       {"retry_initial_s": 3, "retry_max_s": 1}):
            with self.subTest(values=values), self.assertRaises(ValidationError):
                _Fixtures.settings(**values)
