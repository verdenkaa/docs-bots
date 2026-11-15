"""Worker business steps use Runtime tracing and real Model Gateway instrumentation."""

import asyncio
from contextlib import redirect_stdout
from io import StringIO
import json
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import test_model_gateway as model_fixtures
from rag.contracts import StepError
from rag.ingestion._indexing import _Indexer
from rag.ingestion._tests._support import _FakeDependencies, _Fixtures
from rag.ingestion.contracts import JobRef
from rag.model_gateway import entrypoints as models
from rag.model_gateway.contracts import EmbeddingResult
from rag.runtime import events


class WorkerLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fixture = model_fixtures.ModelGatewayTests()
        await self.fixture.asyncSetUp()
        await self.fixture.start('embedding', 'generation')
        self.fixture.adapters['generation'].override = lambda operation, payload: dict(
            profile_id=self.fixture.bindings['generation'].profile_id,
            text='{"questions":["Как настроить сеть?"]}', finish_reason='completed',
            usage=dict(input_tokens=12, output_tokens=4),
        )
        self.settings = _Fixtures.settings(
            embedding_profile=self.fixture.bindings['embedding'].profile_id,
            generation_profile=self.fixture.bindings['generation'].profile_id,
            embedding_dimensions=3,
        )

    async def asyncTearDown(self):
        try:
            await self.fixture.asyncTearDown()
        finally:
            self.fixture.redirect.__exit__(None, None, None)
            self.fixture.env.stop()
            self.fixture.temp.cleanup()

    async def run_job(self, deps, message_id='message'):
        with events._event_context(message_id=message_id, route='ingestion.jobs',
                                   received_at='2026-11-15T00:00:00Z'):
            parent = events._context.get()
            self.assertEqual(await _Indexer(self.settings, deps).handle(
                JobRef(job_id=deps.job.attempt.job_id)), 'ack')
            self.assertIs(events._context.get(), parent)
        return deps.events[-1]

    async def test_real_models_are_nested_in_worker_steps_with_metrics(self):
        deps = _FakeDependencies()
        deps.models = models
        final = await self.run_job(deps)
        self.assertEqual(final['outcome'], 'published')
        self.assertEqual(final['corpus_generation'], 42)
        self.assertEqual(final['document_version'], 0)
        self.assertEqual(final['attempt_no'], 1)
        self.assertEqual(final['message_id'], 'message')
        self.assertEqual(final['received_at'], '2026-11-15T00:00:00Z')
        self.assertEqual(deps.events[0]['run_id'], final['run_id'])
        self.assertEqual(deps.events[0]['started_at'], final['started_at'])
        for step in final['steps']:
            if step['name'] not in ('embed_document', 'embed_query', 'generate_topic_questions'):
                continue
            child, = step['steps']
            self.assertEqual(child['name'], 'generate' if step['name'] == 'generate_topic_questions' else 'embed_texts')
            self.assertEqual(child['execution'], 'remote')
            self.assertEqual(child['outcome'], 'completed')
            self.assertGreaterEqual(child['offset_ms'], step['offset_ms'])
            for field in ('duration_ms', 'wait_ms', 'inference_ms'):
                self.assertGreaterEqual(child[field], 0)
            if child['name'] == 'generate':
                self.assertEqual((child['input_tokens'], child['output_tokens']), (12, 4))
        logs = self.fixture.output.getvalue()
        self.assertNotIn('Как настроить сеть?', logs)
        self.assertNotIn('Сеть.docx', logs)
        self.assertIsNone(events._trace.get())
        self.assertIsNone(events._parent_step.get())

    async def test_parallel_attempts_keep_separate_trees_and_model_contexts(self):
        first, second = _FakeDependencies(), _FakeDependencies()
        second._update_job(attempt={**second.job.attempt.model_dump(), 'job_id': uuid4(), 'attempt_no': 2})
        for deps in (first, second):
            deps.models = models
        a, b = await asyncio.gather(self.run_job(first, 'first'), self.run_job(second, 'second'))
        self.assertIsNot(a['steps'], b['steps'])
        self.assertIsNot(a['steps'][0], b['steps'][0])
        emitted = [json.loads(line) for line in self.fixture.output.getvalue().splitlines()]
        for deps, message in ((first, 'first'), (second, 'second')):
            own = [r for r in emitted if r.get('job_id') == str(deps.job.attempt.job_id)]
            self.assertTrue(own)
            self.assertTrue(all(r['message_id'] == message for r in own))
            self.assertTrue(all(r['attempt_no'] == deps.job.attempt.attempt_no for r in own))

    async def test_profile_validation_marks_business_step_failed(self):
        deps = _FakeDependencies()
        bad = EmbeddingResult(profile_id='foreign', vectors=[[1.0, 2.0, 3.0]])
        with patch.object(deps, 'embed_texts', new=AsyncMock(return_value=bad)):
            final = await self.run_job(deps)
        step = next(s for s in final['steps'] if s['name'] == 'embed_document')
        self.assertEqual(step['outcome'], 'failed')
        self.assertEqual(step['error_code'], 'model_profile_mismatch')
        self.assertNotIn('begin', deps.calls)

    async def test_cancelled_model_leaves_finished_trace_without_broker_decision(self):
        deps = _FakeDependencies()
        deps.errors['embed_document'] = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await _Indexer(self.settings, deps).handle(JobRef(job_id=deps.job.attempt.job_id))
        final = deps.events[-1]
        self.assertEqual(final['outcome'], 'cancelled')
        self.assertNotIn('consume_decision', final)
        self.assertEqual(final['steps'][-1]['outcome'], 'cancelled')
        self.assertIsNone(events._trace.get())
        self.assertIsNone(events._parent_step.get())

    async def test_corrupt_storage_response_is_not_logged_as_completed(self):
        deps = _FakeDependencies()
        with patch.object(deps, 'load_document_batch', new=AsyncMock(return_value={'bad':'PRIVATE'})):
            final = await self.run_job(deps)
        step = next(s for s in final['steps'] if s['name'] == 'load_document_batch')
        self.assertEqual(step['outcome'], 'failed')
        self.assertEqual(step['error_code'], 'staging_invalid_data')
        self.assertNotIn('PRIVATE', json.dumps(final))


class GatewayLoggingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        import test_ingestion_gateway_http as gateway_fixtures
        self.fixture = gateway_fixtures.GatewayTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.stack.close)
        # Inspect emitted JSON too: broker context is merged by Runtime's emitter.
        self.output = StringIO()
        self.fixture.stack.enter_context(redirect_stdout(self.output))
        self.fixture.events.side_effect = events.emit_event

    async def test_gateway_steps_and_broker_context_use_runtime_lifecycle(self):
        from rag.ingestion import gateway
        with events._event_context(message_id='upstream', route='upload'):
            parent = events._context.get()
            await gateway.accept_upload(self.fixture._input())
            self.assertIs(events._context.get(), parent)
        calls = self.fixture.events.call_args_list
        self.assertEqual([c.args[2] for c in calls], ['started', 'finished'])
        self.assertEqual(calls[0].args[3], calls[-1].args[3])
        emitted = [json.loads(line) for line in self.output.getvalue().splitlines()]
        final = emitted[-1]
        self.assertEqual(final['outcome'], 'accepted')
        self.assertEqual(final['message_id'], 'upstream')
        self.assertTrue(all(record['message_id'] == 'upstream' for record in emitted))
        self.assertTrue(all(record['route'] == 'upload' for record in emitted))
        self.assertEqual(final['job_id'], str(self.fixture.revision))
        self.assertEqual([s['name'] for s in final['steps']], [
            'convert_document', 'put_image', 'stage_upload', 'open_staged_upload',
            'prepare_chunks', 'store_document_batch', 'register_revision'])
        self.assertTrue(all(s['outcome'] == 'completed' for s in final['steps']))
        self.assertNotIn('manual.pdf', str(calls))
        self.assertIsNone(events._trace.get())

    async def test_gateway_metadata_failure_prevents_registration(self):
        from rag.ingestion import gateway
        self.fixture.staged.images = []
        from rag.contracts import StorageError
        with self.assertRaises(StorageError):
            await gateway.accept_upload(self.fixture._input())
        final = self.fixture.events.call_args.kwargs
        self.assertEqual(final['outcome'], 'technical_error')
        self.assertEqual(final['error_code'], 'storage_unconfirmed')
        self.assertEqual(final['steps'][-1]['name'], 'open_staged_upload')
        self.fixture.store_document_batch.assert_not_awaited()
        self.fixture.register_revision.assert_not_awaited()
        self.assertIsNone(events._trace.get())

    async def test_gateway_storage_failure_and_cancellation_are_finished(self):
        from rag.ingestion import gateway
        from rag.contracts import StorageError
        self.fixture.open_staged_upload.side_effect = StorageError('PRIVATE')
        with self.assertRaises(StorageError):
            await gateway.accept_upload(self.fixture._input())
        final = self.fixture.events.call_args.kwargs
        self.assertEqual(final['outcome'], 'technical_error')
        self.assertEqual(final['error_code'], 'storage_unconfirmed')
        self.assertEqual(final['steps'][-1]['outcome'], 'failed')
        self.assertNotIn('PRIVATE', self.output.getvalue())
        self.fixture.convert_document.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await gateway.accept_upload(self.fixture._input())
        final = self.fixture.events.call_args.kwargs
        self.assertEqual(final['outcome'], 'cancelled')
        self.assertEqual(final['steps'][-1]['outcome'], 'cancelled')
        self.assertIsNone(events._parent_step.get())
