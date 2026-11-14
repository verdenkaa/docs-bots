"""Qdrant schema/profile checks and bounded sequential index operations."""

import asyncio
from contextlib import asynccontextmanager
from uuid import UUID, uuid5

from pydantic import ValidationError
from qdrant_client import models
from qdrant_client.http.exceptions import UnexpectedResponse, ResponseHandlingException

from rag.contracts import StepError
from rag.runtime._state import _get
from rag.runtime import _watchdog


@asynccontextmanager
async def _operation():
    try:
        with _watchdog._step(_get().settings.io_timeout_s, "qdrant"):
            async with asyncio.timeout(_get().settings.io_timeout_s):
                yield
    except UnexpectedResponse as exc:
        raise StepError("qdrant_unavailable" if exc.status_code>=500 or exc.status_code==429 else "qdrant_schema_error",
                        exc.status_code>=500 or exc.status_code==429) from exc
    except (ResponseHandlingException,OSError,TimeoutError) as exc:
        raise StepError("qdrant_unavailable",True) from exc
    except (ValidationError, ValueError, KeyError, TypeError) as exc:
        raise StepError("qdrant_invalid_data",False) from exc


def _filter(revision_ids):
    return models.Filter(must=[models.FieldCondition(key="revision_id",
        match=models.MatchAny(any=[str(revision) for revision in sorted(revision_ids,key=str)]))])


def _text_index():
    language=_get().settings.text_language
    return models.TextIndexParams(type=models.TextIndexType.TEXT,tokenizer=models.TokenizerType.WORD,
        lowercase=True,scoring=True,stemmer=models.SnowballParams(type=models.Snowball.SNOWBALL,language=language))


async def _check(collection, *, create=False, chunks=False):
    state=_get()
    settings=state.settings
    if not settings.embedding_profile:
        raise StepError("embedding_profile_not_configured",False)
    exists=await state.qdrant.collection_exists(collection)
    if not exists:
        if not create:
            raise StepError("qdrant_index_missing",False)
        try:
            await state.qdrant.create_collection(collection,vectors_config={"dense":models.VectorParams(
                size=settings.embedding_dimensions,distance=models.Distance.COSINE)},
                optimizers_config=models.OptimizersConfigDiff(prevent_unoptimized=False),
                metadata={"embedding_profile":settings.embedding_profile})
        except UnexpectedResponse as exc:
            if exc.status_code != 409:
                raise
    info=await state.qdrant.get_collection(collection)
    vectors=info.config.params.vectors
    if (not isinstance(vectors,dict) or set(vectors)!={"dense"} or
        vectors["dense"].size!=settings.embedding_dimensions or vectors["dense"].distance!=models.Distance.COSINE or
        (info.config.metadata or {}).get("embedding_profile")!=settings.embedding_profile or
        info.config.optimizer_config.prevent_unoptimized):
        raise StepError("qdrant_profile_mismatch",False)
    expected={"revision_id":models.PayloadSchemaType.KEYWORD}
    if chunks:
        expected["text"]=models.PayloadSchemaType.TEXT
    for field,kind in expected.items():
        current=info.payload_schema.get(field)
        if current is None and create:
            await state.qdrant.create_payload_index(collection,field_name=field,
                field_schema=_text_index() if field=="text" else kind,wait=True)
        elif current is None or current.data_type!=kind:
            raise StepError("qdrant_schema_error",False)
        elif field=="text":
            parameters=current.params
            expected_text=_text_index()
            if (parameters is None or not parameters.scoring or parameters.tokenizer!=expected_text.tokenizer or
                parameters.lowercase!=expected_text.lowercase or parameters.stemmer!=expected_text.stemmer):
                raise StepError("qdrant_text_profile_mismatch",False)


async def _prepare():
    from rag.runtime.postgresql._db import _transaction,_corpus,_execute,_one
    state=_get()
    settings=state.settings
    if not settings.embedding_profile:
        raise StepError("embedding_profile_not_configured",False)
    async with _transaction() as connection:
        row=await _corpus(connection)
        new=row["generation"]==0 and not await _one(connection,
            "SELECT revision_id FROM document_revisions WHERE visible_from IS NOT NULL LIMIT 1")
        if row["embedding_profile"] is None:
            if not new:
                raise StepError("embedding_profile_unknown",False)
            await _execute(connection,"""UPDATE corpus_state SET embedding_profile=:profile,
                embedding_dimensions=:dimensions WHERE singleton_id=1""",
                profile=settings.embedding_profile,dimensions=settings.embedding_dimensions)
        elif (row["embedding_profile"]!=settings.embedding_profile or
              row["embedding_dimensions"]!=settings.embedding_dimensions):
            raise StepError("embedding_profile_mismatch",False)
    async with _operation():
        await _check(settings.chunks_collection,create=new,chunks=True)
        await _check(settings.topics_collection,create=new)


async def _records(collection,revision):
    state=_get()
    offset=None
    records=[]
    offsets=set()
    while True:
        page,next_offset=await state.qdrant.scroll(collection,scroll_filter=_filter(frozenset({revision})),
            limit=state.settings.batch_size,offset=offset,with_payload=True,with_vectors=False)
        records.extend(page)
        if next_offset is None:
            return records
        if str(next_offset) in offsets:
            raise StepError("qdrant_invalid_scroll",False)
        offsets.add(str(next_offset))
        offset=next_offset


async def _verify(collection,revision,expected,topic=False):
    if type(expected) is not int or expected<0:
        raise ValueError("Invalid expected count")
    records=await _records(collection,revision)
    if len(records)!=expected:
        return False
    ids=set()
    for record in records:
        payload=record.payload or {}
        number=payload.get("question_no" if topic else "chunk_no")
        if type(number) is not int or not 0<=number<expected or payload.get("revision_id")!=str(revision):
            return False
        try:
            identifier=UUID(str(record.id))
        except ValueError:
            return False
        if identifier!=uuid5(revision,("question:" if topic else "")+str(number)):
            return False
        ids.add(identifier)
    return len(ids)==expected


async def _write(collection,revision,points,topic=False):
    from rag.runtime.qdrant import TopicPoint,ChunkPoint
    state=_get()
    parsed=[]
    for index,item in enumerate(points):
        point=(TopicPoint if topic else ChunkPoint).model_validate_json(item.model_dump_json())
        number=point.question_no if topic else point.chunk_no
        identifier=point.point_id if topic else point.chunk_id
        if point.revision_id!=revision or number!=index or identifier!=uuid5(revision,("question:" if topic else "")+str(index)):
            raise StepError("invalid_revision_points",False)
        if len(point.dense)!=state.settings.embedding_dimensions:
            raise StepError("embedding_profile_mismatch",False)
        payload=point.model_dump(mode="json",exclude={"dense"})
        parsed.append(models.PointStruct(id=str(identifier),vector={"dense":point.dense},payload=payload))
    if not parsed:
        raise StepError("invalid_revision_points",False)
    if not topic and len({item.payload["document_id"] for item in parsed})!=1:
        raise StepError("invalid_revision_points",False)
    await _check(collection,chunks=not topic)
    for start in range(0,len(parsed),state.settings.batch_size):
        result=await state.qdrant.upsert(collection,points=parsed[start:start+state.settings.batch_size],wait=True)
        if result.status!=models.UpdateStatus.COMPLETED:
            raise StepError("qdrant_write_unconfirmed",True)
    if not await _verify(collection,revision,len(parsed),topic):
        raise StepError("topic_points_mismatch" if topic else "revision_points_mismatch",True)
