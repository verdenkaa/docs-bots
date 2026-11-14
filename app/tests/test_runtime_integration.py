"""Real PostgreSQL, Redis, RabbitMQ, Qdrant and S3; opt in with the isolated Compose stack."""

import asyncio
import base64
from datetime import UTC, datetime, timedelta
import json
import os
import sys
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from uuid import uuid4, uuid5

import aio_pika
from sqlalchemy import text

from rag.contracts import (AnsweredOutcome, ImageContent, NoticeOutcome, RagResult,
                           SourceRef, StepError, StorageError, SupportOutcome, TextQuestion)
from rag.ingestion.contracts import ConvertedDocument, UploadSource
from rag.rag_api.contracts import (AccessDenied, AccessRecord, Conflict, NewQuestion, Principal,
                                  SupportAction, SupportCommand, TextPart)
from rag.runtime import _state, qdrant, rabbitmq, redis, s3, upload_spool
from rag.runtime._http import _s3
from rag.runtime.lifecycle import (migrate_runtime, start_runtime, close_runtime,
                                   prepare_runtime_storage)
from rag.runtime.media import load_source_metadata
from rag.runtime.postgresql import cleanup, ingestion, outbox, rag_api, rag_core, support
from rag.runtime.postgresql._db import _transaction, _execute, _one
from rag.runtime.queue_relay import relay_outbox_batch


def now():
    return datetime.now(UTC)


def answer():
    revision=uuid4()
    return RagResult(answer_text="A documented answer", sources=[SourceRef(
        chunk_id=uuid5(revision,"0"),document_id=uuid4(),revision_id=revision,source_name="Guide")])


@unittest.skipUnless(os.environ.get("RUN_RUNTIME_INTEGRATION")=="1", "Use tests/runtime-compose.yaml")
class RuntimeIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        token=uuid4().hex
        self.schema="runtime_"+token
        self.temporary=tempfile.TemporaryDirectory(prefix="runtime-test-")
        self.env=patch.dict(os.environ,{
            "RAG_PG_SCHEMA":self.schema,"RAG_CHUNKS_COLLECTION":"runtime_chunks_"+token,
            "RAG_TOPICS_COLLECTION":"runtime_topics_"+token,"RAG_CACHE_PREFIX":"runtime:"+token+":",
            "RAG_QUEUE_PREFIX":"runtime."+token+".","RAG_S3_BUCKET":"runtime-"+token,
            "RAG_EMBEDDING_PROFILE":"runtime-test-v1","RAG_EMBEDDING_DIMENSIONS":"3",
            "RAG_IO_TIMEOUT_S":"10","RAG_BATCH_SIZE":"1","RAG_BROKER_RETRY_S":"0.01",
            "WORKER_SPOOL_DIR":self.temporary.name})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.addCleanup(self.temporary.cleanup)
        await migrate_runtime()
        await start_runtime()
        self.state=_state._get()
        await rabbitmq._connect_broker()
        await prepare_runtime_storage()
        async with _transaction() as connection:
            await _execute(connection,"INSERT INTO employees(user_id,enabled,role) VALUES(1,true,'admin'),(2,true,'employee'),(3,true,'employee')")

    async def asyncTearDown(self):
        import xml.etree.ElementTree as ET
        try:
            for collection in (self.state.settings.chunks_collection,self.state.settings.topics_collection):
                if await self.state.qdrant.collection_exists(collection):
                    await self.state.qdrant.delete_collection(collection)
            status,_,body=await _s3("GET")
            if status==200:
                for element in ET.fromstring(body).iter():
                    if element.tag.rsplit("}",1)[-1]=="Key":
                        await _s3("DELETE",element.text)
                await _s3("DELETE")
            for route in ("rag.questions","rag.results","ingestion.jobs"):
                queue=await self.state.channel.declare_queue(self.state.settings.queue_prefix+route,durable=True)
                await queue.delete(if_unused=False,if_empty=False)
            await self.state.engine.dispose()
            async with self.state.engine.begin() as connection:
                # Generated identifier, unique to this test. No deployed schema is touched.
                assert self.schema.startswith("runtime_") and len(self.schema)==40
                await connection.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        finally:
            await close_runtime()

    async def question(self,user=2,received=None,update=None):
        return await rag_api.register_question(NewQuestion(update_id=update or uuid4().int%10**15,
            user_id=user,chat_id=user,received_at=received or now(),input=TextQuestion(kind="text",text="Question")))

    async def finish(self,delivery_id):
        delivery=await rag_api.claim_delivery(delivery_id)
        self.assertIsNotNone(delivery)
        await rag_api.store_delivery_parts(delivery.attempt,[TextPart(kind="text",text="Delivered")])
        await rag_api.record_delivery_part(delivery.attempt,0,77,"normal")
        self.assertTrue(await rag_api.complete_delivery(delivery.attempt,True,False))
        return delivery

    async def publish(self,document=None):
        revision=uuid4()
        accepted=await ingestion.register_revision(revision,document,f"uploads/{revision}","Guide",[])
        job=await ingestion.claim_ingestion_job(revision)
        self.assertTrue(await ingestion.begin_revision_write(job.attempt,1,1))
        point=qdrant.ChunkPoint(chunk_id=uuid5(revision,"0"),document_id=accepted.document_id,
            revision_id=revision,source_name="Guide",text="Инструкция AUTH-17 установка",chunk_no=0,dense=[1.0,0.0,0.0])
        topic=qdrant.TopicPoint(point_id=uuid5(revision,"question:0"),revision_id=revision,
            question_no=0,text="Как установить AUTH-17?",dense=[1.0,0.0,0.0])
        await qdrant.write_revision_points(revision,[point])
        await qdrant.write_topic_points(revision,[topic])
        generation=await ingestion.publish_revision(job.attempt)
        return accepted,point,topic,generation

    async def test_atomic_admission_duplicates_and_revocation(self):
        results=await asyncio.gather(*(self.question() for _ in range(10)))
        self.assertEqual(sum(item.kind=="accepted" for item in results),1)
        self.assertEqual(sum(item.kind=="busy" for item in results),9)
        accepted=next(item for item in results if item.kind=="accepted")
        async with _transaction() as connection:
            row=await _one(connection,"SELECT update_id FROM rag_requests WHERE request_id=:id",id=accepted.request_id)
        await support.write_user_access(Principal(user_id=1,role="admin"),AccessRecord(user_id=2,enabled=False,role="employee"))
        duplicate=await self.question(update=row["update_id"])
        self.assertEqual((duplicate.kind,duplicate.request_id),("duplicate",accepted.request_id))
        self.assertEqual((await self.question()).kind,"denied")
        claimed=await rag_core.claim_question(accepted.request_id)
        self.assertIsNotNone(claimed)
        self.assertTrue(await rag_core.complete_question(claimed.attempt,NoticeOutcome(kind="notice",code="technical_error",text="Unavailable")))

    async def test_delivery_order_receipts_retry_and_slot_release(self):
        accepted=await self.question()
        claimed=await rag_core.claim_question(accepted.request_id)
        self.assertTrue(await rag_core.complete_question(claimed.attempt,AnsweredOutcome(kind="answered",result=answer())))
        rows=await outbox.list_ready_outbox(20)
        delivery_id=next(row.notice.event.delivery_id for row in rows if row.notice.route=="rag.results")
        delivery=await rag_api.claim_delivery(delivery_id)
        parts=[TextPart(kind="text",text="One"),TextPart(kind="text",text="Two")]
        self.assertTrue(await rag_api.store_delivery_parts(delivery.attempt,parts))
        with self.assertRaises(ValueError):
            await rag_api.complete_delivery(delivery.attempt,True,False)
        self.assertTrue(await rag_api.record_delivery_part(delivery.attempt,0,100,"normal"))
        async with _transaction() as connection:
            before=await _one(connection,"SELECT receipts FROM rag_deliveries WHERE delivery_id=:id",id=delivery_id)
        await rag_api.record_delivery_part(delivery.attempt,0,100,"normal")
        self.assertTrue(await rag_api.schedule_delivery_retry(delivery.attempt,now()-timedelta(seconds=1),True))
        retry=await rag_api.claim_delivery(delivery_id)
        self.assertEqual(retry.next_part,1)
        self.assertFalse(await rag_api.record_delivery_part(delivery.attempt,1,101,"normal"))
        await rag_api.record_delivery_part(retry.attempt,1,101,"normal")
        await rag_api.complete_delivery(retry.attempt,True,False)
        self.assertEqual((await self.question()).kind,"accepted")
        async with _transaction() as connection:
            after=await _one(connection,"SELECT receipts FROM rag_deliveries WHERE delivery_id=:id",id=delivery_id)
        def data(value):
            return json.loads(value) if isinstance(value,str) else value
        self.assertEqual(data(before["receipts"])[0],data(after["receipts"])[0])

    async def test_published_generation_pins_two_indexes_until_completion(self):
        old,point,topic,generation=await self.publish()
        accepted=await self.question()
        question=await rag_core.claim_question(accepted.request_id)
        self.assertEqual(question.corpus_generation,generation)
        new,_,_,new_generation=await self.publish(old.document_id)
        self.assertEqual(new_generation,generation+1)
        self.assertEqual(await rag_core.load_revision_ids(generation),frozenset({old.job_id}))
        self.assertEqual(await cleanup.claim_revision_cleanup(20),[])
        hits=await qdrant.find_chunks("AUTH-17",[1.0,0.0,0.0],frozenset({old.job_id}),5,lexical_priority=True)
        self.assertEqual([hit.fragment.revision_id for hit in hits],[old.job_id])
        self.assertEqual((await qdrant.find_topic_matches([1.0,0.0,0.0],frozenset({new.job_id})))[0].revision_id,new.job_id)
        await rag_core.complete_question(question.attempt,NoticeOutcome(kind="notice",code="blocked",text="Blocked"))
        self.assertEqual(await cleanup.claim_revision_cleanup(20),[old.job_id])
        await qdrant.delete_revision_points(old.job_id)
        self.assertTrue(await qdrant.verify_revision_points(old.job_id,0))
        self.assertTrue(await qdrant.verify_topic_points(old.job_id,0))
        self.assertTrue(await cleanup.finish_revision_cleanup(old.job_id))
        self.assertEqual((await load_source_metadata(old.document_id)).source_name,"Guide")

    async def test_pending_write_blocks_retry_and_cleanup(self):
        revision=uuid4()
        await ingestion.register_revision(revision,None,f"uploads/{revision}","Guide",[])
        job=await ingestion.claim_ingestion_job(revision)
        await ingestion.begin_revision_write(job.attempt,1,1)
        self.assertFalse(await ingestion.schedule_ingestion_retry(job.attempt,now()))
        self.assertFalse(await ingestion.fail_ingestion_job(job.attempt))
        async with _transaction() as connection:
            await _execute(connection,"UPDATE document_revisions SET resume_at=:past WHERE revision_id=:id",
                           past=now()-timedelta(seconds=1),id=revision)
        self.assertIsNone(await ingestion.claim_ingestion_job(revision))
        self.assertEqual(await outbox.recover_pending_work("ingestion.jobs",20),0)
        pending=await ingestion.list_pending_revision_writes(20)
        self.assertEqual(pending[0].revision_id,revision)
        self.assertFalse(await ingestion.clear_revision_write_pending(revision,999))
        self.assertTrue(await ingestion.clear_revision_write_pending(revision,job.attempt.attempt_no))
        retry=await ingestion.claim_ingestion_job(revision)
        with self.assertRaisesRegex(StepError,"topic_count_mismatch"):
            await ingestion.begin_revision_write(retry.attempt,1,2)
        self.assertTrue(await ingestion.fail_ingestion_job(retry.attempt))

    async def test_redis_compare_token_and_exact_large_generations(self):
        result=answer()
        value=redis.CachedAnswer(**result.model_dump(),corpus_generation=2**53+2)
        self.assertTrue(await redis.write_cached_answer_if_not_older("Question",value))
        old=await redis.read_cached_answer("Question")
        self.assertTrue(await redis.write_cached_answer_if_not_older("Question",value))
        self.assertFalse(await redis.delete_cached_answer_if_unchanged("Question",old.compare_token))
        older=redis.CachedAnswer(**result.model_dump(),corpus_generation=2**53+1)
        self.assertFalse(await redis.write_cached_answer_if_not_older("Question",older))
        current=await redis.read_cached_answer("Question")
        self.assertEqual(current.value.corpus_generation,2**53+2)
        self.assertTrue(await redis.delete_cached_answer_if_unchanged("Question",current.compare_token))
        self.assertIsNone(await redis.read_cached_answer("Question"))

    async def test_s3_idempotence_conflict_and_private_bucket(self):
        image_id=uuid4()
        png=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+jRZkAAAAASUVORK5CYII=")
        image=ImageContent(data=png,mime_type="image/png")
        await asyncio.gather(s3.put_image(image_id,image),s3.put_image(image_id,image))
        self.assertEqual(await s3.get_image(image_id),image)
        with self.assertRaisesRegex(StepError,"image_conflict"):
            await s3.put_image(image_id,ImageContent(data=png+b"different",mime_type="image/png"))
        with self.assertRaisesRegex(StepError,"image_not_found"):
            await s3.get_image(uuid4())
        response=await self.state.http.get(self.state.settings.s3_endpoint_url+"/"+self.state.settings.s3_bucket+"/images/"+str(image_id))
        self.assertEqual(response.status_code,403)

    async def test_spool_snapshot_is_durable_immutable_and_checked(self):
        revision=uuid4()
        ref=await upload_spool.stage_upload(revision,ConvertedDocument(markdown="Document"),UploadSource(filename="Guide"))
        self.assertEqual(ref,await upload_spool.stage_upload(revision,ConvertedDocument(markdown="Document"),UploadSource(filename="Guide")))
        staged=await upload_spool.open_staged_upload(ref)
        point=qdrant.ChunkPoint(chunk_id=uuid5(revision,"0"),document_id=uuid4(),revision_id=revision,
            source_name="Guide",text="Document",chunk_no=0,dense=[1.0,0.0,0.0])
        topic=qdrant.TopicPoint(point_id=uuid5(revision,"question:0"),revision_id=revision,
            question_no=0,text="Question",dense=[1.0,0.0,0.0])
        prepared=upload_spool.PreparedRevision(revision_id=revision,preparation_profile="ingestion-v1",
            embedding_profile="runtime-test-v1",points=[point],topic_points=[topic])
        await upload_spool.store_prepared_revision(ref,prepared)
        self.assertEqual(await upload_spool.load_prepared_revision(ref),prepared)
        changed = prepared.model_copy(update={"points": [point.model_copy(update={"text": "Changed"})]})
        with self.assertRaisesRegex(StepError, "prepared_revision_conflict"):
            await upload_spool.store_prepared_revision(ref, changed)
        (staged.path.parent/"prepared.json").write_text("invalid", encoding="utf-8")
        with self.assertRaises(StorageError):
            await upload_spool.load_prepared_revision(ref)
        self.assertIn(ref,await upload_spool.list_staging_candidates(now()+timedelta(seconds=1),10))
        with self.assertRaises(ValueError):
            await upload_spool.remove_staged_upload("uploads/../escape")
        await upload_spool.remove_staged_upload(ref)
        self.assertIsNone(await upload_spool.open_staged_upload(ref))

    async def test_automatic_support_authorization_and_message_idempotence(self):
        accepted=await self.question()
        question=await rag_core.claim_question(accepted.request_id)
        await rag_core.complete_question(question.attempt,SupportOutcome(kind="support",reason="no_evidence"))
        case=(await support.list_pending_support(now(),10))[0]
        self.assertEqual((await self.question()).kind,"busy")
        ticket=await support.create_support_ticket(case.case_id,"https://example.test/tickets/"+str(case.case_id))
        self.assertEqual(await support.create_support_ticket(case.case_id,"https://other.test/"),ticket)
        owner=Principal(user_id=2,role="employee")
        message_id=uuid4()
        first=await support.append_ticket_message(owner,case.case_id,message_id,"Help")
        self.assertEqual(await support.append_ticket_message(owner,case.case_id,message_id,"Help"),first)
        with self.assertRaises(Conflict):
            await support.append_ticket_message(owner,case.case_id,message_id,"Changed")
        with self.assertRaises(AccessDenied):
            await support.close_ticket_record(Principal(user_id=2,role="admin"),case.case_id)
        view=await support.read_ticket(owner,case.case_id)
        self.assertEqual(view.history[0].question_text,"Question")
        admin=Principal(user_id=1,role="admin")
        closed=await support.close_ticket_record(admin,case.case_id)
        self.assertEqual(await support.close_ticket_record(admin,case.case_id),closed)
        metrics=await support.read_metrics(admin,now()-timedelta(days=1),now()+timedelta(seconds=1))
        self.assertEqual((metrics.requests,metrics.escalated),(1,1))

    async def test_relay_persistent_body_and_recovery_window(self):
        accepted=await self.question()
        self.assertEqual(await relay_outbox_batch(20),1)
        queue=await self.state.channel.declare_queue(self.state.settings.queue_prefix+"rag.questions",durable=True)
        message=await queue.get(timeout=5)
        self.assertEqual(json.loads(message.body),{"request_id":accepted.request_id})
        self.assertEqual(message.delivery_mode,aio_pika.DeliveryMode.PERSISTENT)
        await message.ack()
        self.assertEqual(await outbox.recover_pending_work("rag.questions",20),0)
        async with _transaction() as connection:
            await _execute(connection,"UPDATE outbox SET published_at=:past",past=now()-timedelta(minutes=2))
        self.assertEqual(await outbox.recover_pending_work("rag.questions",20),1)
        self.assertEqual(await outbox.recover_pending_work("rag.questions",20),0)

    async def test_manual_support_timer_confirmation_and_ownership(self):
        accepted = await self.question()
        claimed = await rag_core.claim_question(accepted.request_id)
        await rag_core.complete_question(claimed.attempt, AnsweredOutcome(kind="answered", result=answer()))
        rows = await outbox.list_ready_outbox(20)
        delivery_id = next(row.notice.event.delivery_id for row in rows if row.notice.route == "rag.results")
        await self.finish(delivery_id)

        async def command(kind, user=2, **fields):
            return await support.record_support_action(SupportCommand(update_id=uuid4().int % 10**15,
                user_id=user, chat_id=user, received_at=now(), action=SupportAction(kind=kind, **fields)),
                deadline if kind == "start" else None)

        deadline = now() + timedelta(minutes=10)
        offered = await command("offer", reason="dissatisfied")  # Latest confirmed answer, no request_id.
        self.assertEqual(offered.request_id, accepted.request_id)
        started = await command("start", request_id=accepted.request_id)
        deadline += timedelta(minutes=10)
        repeated = await command("start", request_id=accepted.request_id)
        self.assertEqual(repeated.case_id, started.case_id)
        self.assertEqual(await support.list_pending_support(now(), 20), [])
        self.assertFalse(await support.queue_support_confirmation(started.case_id, now()))
        cases = await support.list_pending_support(deadline, 20)
        self.assertEqual(cases[0].check_at, deadline - timedelta(minutes=10))
        self.assertEqual((await command("confirm", case_id=started.case_id)).kind, "ignored")
        self.assertTrue(await support.queue_support_confirmation(started.case_id, deadline))
        self.assertEqual((await command("confirm", user=3, case_id=started.case_id)).kind, "ignored")
        self.assertEqual((await command("confirm", case_id=started.case_id)).kind, "handled")
        ticket = await support.create_support_ticket(started.case_id, "https://example.test/ticket")
        self.assertEqual(ticket.reason, "dissatisfied")
        self.assertEqual((await self.question()).kind, "accepted")  # Manual support holds no active slot.

    async def test_broker_consumer_invalid_event_retry_and_ack(self):
        from rag.contracts import RequestRef
        stop, finished = asyncio.Event(), asyncio.Event()
        calls = []

        async def handler(event):
            calls.append(event.request_id)
            if len(calls) == 1:
                raise RuntimeError("Injected storage failure")
            finished.set()
            return "ack"

        consumer = asyncio.create_task(rabbitmq._consume("rag.questions", handler, stop))
        try:
            await self.state.channel.default_exchange.publish(aio_pika.Message(body=b"invalid",
                message_id=str(uuid4()), content_type="application/json"),
                routing_key=self.state.settings.queue_prefix+"rag.questions", mandatory=True)
            await rabbitmq.publish_notice(rabbitmq.QuestionNotice(message_id=uuid4(), route="rag.questions",
                event=RequestRef(request_id=123)))
            await asyncio.wait_for(finished.wait(), timeout=10)
        finally:
            stop.set()
            await asyncio.wait_for(consumer, timeout=10)
        self.assertEqual(calls, [123, 123])
        queue = await self.state.channel.declare_queue(self.state.settings.queue_prefix+"rag.questions", durable=True)
        self.assertIsNone(await queue.get(fail=False, timeout=2))

    async def test_failed_confirm_keeps_outbox_and_unroutable_publish_fails(self):
        await self.question()
        with patch("rag.runtime.queue_relay.publish_notice", new=AsyncMock(side_effect=rabbitmq.BrokerPublishError("Injected"))):
            with self.assertRaises(rabbitmq.BrokerPublishError):
                await relay_outbox_batch(20)
        self.assertEqual(len(await outbox.list_ready_outbox(20)), 1)
        row = (await outbox.list_ready_outbox(20))[0]
        queue = await self.state.channel.declare_queue(self.state.settings.queue_prefix+"rag.questions", durable=True)
        await queue.delete(if_unused=False, if_empty=False)
        with self.assertRaises(rabbitmq.BrokerPublishError):
            await rabbitmq.publish_notice(row.notice)
        self.assertEqual(len(await outbox.list_ready_outbox(20)), 1)

    async def test_admission_cap_stale_status_and_expired_attempt(self):
        self.state.settings.max_pending_requests = 1
        accepted = await self.question(received=now()-timedelta(seconds=11))
        self.assertEqual((await self.question(user=3)).kind, "retry_later")
        self.assertEqual(await rag_api.queue_waiting_statuses(now(), 20), 1)
        self.assertEqual(await rag_api.queue_waiting_statuses(now(), 20), 0)
        claimed = await rag_core.claim_question(accepted.request_id)
        async with _transaction() as connection:
            await _execute(connection, "UPDATE rag_requests SET resume_at=:past WHERE request_id=:id",
                id=accepted.request_id, past=now()-timedelta(seconds=1))
        self.assertFalse(await rag_core.complete_question(claimed.attempt, AnsweredOutcome(kind="answered", result=answer())))
        retry = await rag_core.claim_question(accepted.request_id)
        self.assertEqual(retry.corpus_generation, claimed.corpus_generation)
        self.assertEqual(retry.attempt.attempt_no, claimed.attempt.attempt_no+1)
        await rag_core.complete_question(retry.attempt, AnsweredOutcome(kind="answered", result=answer()))
        async with _transaction() as connection:
            status = await _one(connection, "SELECT delivery_id FROM rag_deliveries WHERE purpose='status'")
        self.assertIsNone(await rag_api.claim_delivery(status["delivery_id"]))
        self.assertEqual((await self.question(user=3)).kind, "retry_later")  # Terminal delivery still holds slot.

    async def test_missing_index_is_recovery_error_without_recreating_it(self):
        await self.publish()
        await self.state.qdrant.delete_collection(self.state.settings.topics_collection)
        with self.assertRaises(StepError):
            await prepare_runtime_storage()
        self.assertFalse(await self.state.qdrant.collection_exists(self.state.settings.topics_collection))

    async def test_start_is_idempotent_and_admission_does_not_connect_broker(self):
        await close_runtime()
        with patch("rag.runtime.rabbitmq.aio_pika.connect_robust", new=AsyncMock(side_effect=OSError("Injected offline broker"))) as connect:
            await asyncio.gather(start_runtime(), start_runtime(), start_runtime())
            self.state = _state._get()
            self.assertIsNone(self.state.broker)
            self.assertEqual((await self.question()).kind, "accepted")
            connect.assert_not_awaited()
            with self.assertRaises(rabbitmq.BrokerPublishError):
                await relay_outbox_batch(20)
            self.assertEqual(len(await outbox.list_ready_outbox(20)), 1)
        await rabbitmq._connect_broker()  # Restore test resources for its isolated teardown.

    async def test_operational_commands_relay_shutdown_and_singleton(self):
        from rag.runtime.runner import _singleton

        async def command(*arguments):
            process = await asyncio.create_subprocess_exec(sys.executable, "-m", "rag.runtime", *arguments,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
            try:
                output, error = await asyncio.wait_for(process.communicate(), timeout=15)
            except BaseException:
                process.kill()
                await process.wait()
                raise
            return process.returncode, output, error

        for operation in ("migrate", "migrate", "prepare", "pending-writes"):
            code, _, error = await command(operation)
            self.assertEqual(code, 0, error.decode())
        async with _transaction() as connection:
            await _execute(connection, "DELETE FROM employees WHERE user_id=1")
        self.assertEqual((await command("bootstrap-admin", "1"))[0], 0)
        self.assertNotEqual((await command("bootstrap-admin", "1"))[0], 0)
        async with _singleton("relay"):
            with self.assertRaises(RuntimeError):
                async with _singleton("relay"):
                    self.fail("A second relay must not become active")
        await self.question()
        relay = await asyncio.create_subprocess_exec(sys.executable, "-m", "rag.runtime", "serve", "relay",
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            queue = await self.state.channel.declare_queue(self.state.settings.queue_prefix+"rag.questions", durable=True)
            async with asyncio.timeout(15):
                message = None
                while message is None:
                    message = await queue.get(fail=False, timeout=2)
                    if message is None:
                        await asyncio.sleep(0.05)
            await message.ack()
        finally:
            relay.terminate()
            try:
                _, error = await asyncio.wait_for(relay.communicate(), timeout=15)
            except BaseException:
                relay.kill()
                await relay.wait()
                raise
        self.assertEqual(relay.returncode, 0, error.decode())
        self.assertEqual(await outbox.list_ready_outbox(20), [])
