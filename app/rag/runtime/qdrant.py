"""Contracts: docs/documents/infrastructure/qdrant/contracts.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""


from __future__ import annotations

import json

from collections.abc import Sequence
from uuid import UUID

from pydantic import Field

from rag.contracts import SearchFragment
from rag.dto import DTO, NonEmptyText, NonNegativeInt

from qdrant_client import models
from rag.contracts import StepError
from rag.runtime._state import _get
from rag.runtime._validation import _positive, _vector, _text
from rag.runtime import _qdrant


class ChunkPoint(SearchFragment):
    """Точка Qdrant: текст, метаданные, номер чанка и готовый плотный вектор."""
    chunk_no: NonNegativeInt = Field(description='Порядковый номер чанка с 0 внутри редакции; участвует в детерминированном ID.')
    document_version: NonNegativeInt = Field(description='Номер версии документа с 0, закреплённый PostgreSQL за revision_id; не поколение корпуса.')
    access_level: NonEmptyText = Field(description='Метка доступа документа, скопированная из DocumentBatch.source без изменения.')
    dense: list[float] = Field(description='Готовый плотный вектор общего embedding-профиля индекса и поиска.')


class QdrantHit(DTO):
    """Кандидат Qdrant до окончательного отбора и реранкинга Searcher."""
    fragment: SearchFragment = Field(description='Текст кандидата и метаданные его источника.')
    score: float = Field(description='Оценка модели или поиска; шкала определяется конкретным профилем.')


class TopicHit(DTO):
    """Косинусное сходство с примером темы для решения Guardrails."""
    revision_id: UUID = Field(description='UUID конкретной редакции, совпадающий с job_id индексации.')
    score: float = Field(description='Оценка поиска или модели; шкала определяется конкретным профилем.')


class TopicPoint(DTO):
    """Пример вопроса одной редакции; не является источником ответа."""
    point_id: UUID = Field(description='Детерминированный UUIDv5 редакции и question:<номер>.')
    revision_id: UUID = Field(description='UUID конкретной редакции, совпадающий с job_id индексации.')
    question_no: NonNegativeInt = Field(description='Порядковый номер примера с 0 внутри редакции.')
    text: NonEmptyText = Field(description='Сгенерированный пример вопроса по редакции; не источник ответа.')
    dense: list[float] = Field(description='Готовый конечный вектор общего embedding-профиля.')


async def find_chunks(query: str, query_vector: Sequence[float], revision_ids: frozenset[UUID], candidate_limit: int, *, lexical_priority: bool=False) -> list[QdrantHit]:
    """Найти кандидатов Qdrant только в разрешённых редакциях.

    Args:
        query: Нормализованный вопрос из Guardrails.
        query_vector: Вектор для плотной ветки поиска.
        revision_ids: Разрешённые редакции для обеих веток.
        candidate_limit: Положительный предел числа кандидатов до отбора.
        lexical_priority: Выбранный Searcher профиль: больший вес BM25 для вопроса со ссылкой.

    Returns:
        Список объектов Pydantic `QdrantHit` в порядке поискового профиля. `[]` при пустом наборе редакций или отсутствии совпадений.
    """
    _positive(candidate_limit)
    _text(query)
    if not revision_ids:
        return []
    state=_get()
    vector=_vector(query_vector,state.settings.embedding_dimensions)
    async with _qdrant._operation():
        await _qdrant._check(state.settings.chunks_collection,chunks=True)
        filters=_qdrant._filter(revision_ids)
        options=dict(collection_name=state.settings.chunks_collection,limit=candidate_limit,with_payload=True,with_vectors=False)
        if state.settings.search_mode=="dense":
            response=await state.qdrant.query_points(query=vector,using="dense",query_filter=filters,**options)
        else:
            response=await state.qdrant.query_points(prefetch=[
                models.Prefetch(query=vector,using="dense",filter=filters,limit=candidate_limit),
                models.Prefetch(query=models.TextQuery(text=query),using="text",filter=filters,limit=candidate_limit)],
                query=models.RrfQuery(rrf=models.Rrf(k=state.settings.rrf_k,weights=[
                    state.settings.dense_weight,state.settings.priority_lexical_weight if lexical_priority else state.settings.lexical_weight])),
                query_filter=filters,**options)
        hits=[]
        for point in response.points:
            payload=point.payload or {}
            fragment=SearchFragment.model_validate_json(json.dumps(
                {field:payload[field] for field in SearchFragment.model_fields if field in payload}))
            if fragment.revision_id not in revision_ids or str(fragment.chunk_id)!=str(point.id):
                raise StepError("qdrant_invalid_data",False)
            hits.append(QdrantHit(fragment=fragment,score=point.score))
        return hits


async def write_revision_points(revision_id: UUID, points: Sequence[ChunkPoint]) -> None:
    """Записать детерминированные точки одной редакции и дождаться подтверждения.

    Args:
        revision_id: ID редакции, равный ID задания индексации.
        points: Полный непустой набор точек одной редакции. Размер равен сохранённому expected_chunk_count.

    Returns:
        Одно значение Python, `None`.
    """
    async with _qdrant._operation():
        await _qdrant._write(_get().settings.chunks_collection,revision_id,points)


async def verify_revision_points(revision_id: UUID, expected_chunk_count: int) -> bool:
    """Проверить полный ожидаемый набор детерминированных ID чанков редакции.

    Args:
        revision_id: Редакция для точного фильтра payload.
        expected_chunk_count: Число чанков из манифеста. При проверке удаления передаём 0.

    Returns:
        Одно логическое значение.
    """
    async with _qdrant._operation():
        await _qdrant._check(_get().settings.chunks_collection,chunks=True)
        return await _qdrant._verify(_get().settings.chunks_collection,revision_id,expected_chunk_count)


async def delete_revision_points(revision_id: UUID) -> None:
    """Удалить точки редакции после разрешения владельца состояния.

    Args:
        revision_id: ID редакции, равный ID задания индексации.

    Returns:
        Одно значение Python, `None`.
    """
    state=_get()
    async with _qdrant._operation():
        for collection,topic in ((state.settings.chunks_collection,False),(state.settings.topics_collection,True)):
            await _qdrant._check(collection,chunks=not topic)
            records=await _qdrant._records(collection,revision_id)
            for start in range(0,len(records),state.settings.batch_size):
                result=await state.qdrant.delete(collection,points_selector=models.PointIdsList(
                    points=[record.id for record in records[start:start+state.settings.batch_size]]),wait=True)
                if result.status!=models.UpdateStatus.COMPLETED:
                    raise StepError("qdrant_delete_unconfirmed",True)
            if not await _qdrant._verify(collection,revision_id,0,topic):
                raise StepError("qdrant_delete_unconfirmed",True)


async def find_topic_matches(query_vector: Sequence[float], revision_ids: frozenset[UUID], limit: int=1) -> list[TopicHit]:
    """Найти ближайшие примеры темы только в закреплённых редакциях.

    Args:
        query_vector: Вектор нормализованного вопроса.
        revision_ids: Тот же набор, который Core передаст Searcher.
        limit: Максимум ближайших примеров.

    Returns:
        Список TopicHit.
    """
    _positive(limit)
    if not revision_ids:
        return []
    state=_get()
    vector=_vector(query_vector,state.settings.embedding_dimensions)
    async with _qdrant._operation():
        await _qdrant._check(state.settings.topics_collection)
        response=await state.qdrant.query_points(state.settings.topics_collection,query=vector,using="dense",
            query_filter=_qdrant._filter(revision_ids),limit=limit,with_payload=True,with_vectors=False)
        if not response.points:
            raise StepError("topic_index_incomplete",False)
        result=[]
        for point in response.points:
            revision=UUID((point.payload or {})["revision_id"])
            if revision not in revision_ids:
                raise StepError("qdrant_invalid_data",False)
            result.append(TopicHit(revision_id=revision,score=point.score))
        return result


async def write_topic_points(revision_id: UUID, points: Sequence[TopicPoint]) -> None:
    """Записать полный набор примеров одной редакции и проверить подтверждения.

    Args:
        revision_id: Подготовленная редакция.
        points: Полный непустой набор с детерминированными ID.

    Returns:
        None.
    """
    async with _qdrant._operation():
        await _qdrant._write(_get().settings.topics_collection,revision_id,points,True)


async def verify_topic_points(revision_id: UUID, expected_topic_count: int) -> bool:
    """Проверить точный набор детерминированных ID примеров, а не только их число.

    Args:
        revision_id: Проверяемая редакция.
        expected_topic_count: Ожидание из PostgreSQL, 0 для очистки.

    Returns:
        bool.
    """
    async with _qdrant._operation():
        await _qdrant._check(_get().settings.topics_collection)
        return await _qdrant._verify(_get().settings.topics_collection,revision_id,expected_topic_count,True)
