"""Contracts: docs/documents/infrastructure/upload-spool/contracts.md.

Infrastructure adapters with explicit resource lifecycle and validated boundaries.
"""


from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from rag.dto import DTO, NonEmptyText
from rag.ingestion.contracts import ConvertedDocument, DocumentBatch, StagedUpload, UploadSource
from rag.runtime.qdrant import ChunkPoint, TopicPoint

import asyncio
from rag.runtime import _spool, _watchdog
from rag.runtime._state import _get
from rag.runtime._validation import _positive, _utc


class PreparedRevision(DTO):
    """Неизменяемый снимок чанков, примеров и векторов до записи в Qdrant."""
    revision_id: UUID = Field(description='UUID конкретной редакции, совпадающий с job_id индексации.')
    preparation_profile: NonEmptyText = Field(description='Неизменяемая версия разбиения и генерации примеров.')
    embedding_profile: NonEmptyText = Field(description='Общий профиль векторов чанков и примеров.')
    points: list[ChunkPoint] = Field(min_length=1, description='Полный упорядоченный набор точек чанков с готовыми векторами.')
    topic_points: list[TopicPoint] = Field(min_length=1, description='Полный набор примеров темы с готовыми векторами.')

    @model_validator(mode='after')
    def check_points(self) -> Self:
        """Проверить детерминированные ID и совместимость двух полных наборов."""
        document_ids = {point.document_id for point in self.points}
        if len(document_ids) != 1:
            raise ValueError('Редакция содержит один document_id')
        if len({point.document_version for point in self.points}) != 1:
            raise ValueError('Чанки редакции имеют одну версию документа')
        if len({point.access_level for point in self.points}) != 1 or len({point.source_name for point in self.points}) != 1:
            raise ValueError('Чанки редакции имеют общее имя и уровень доступа')
        if len({point.source_url for point in self.points}) != 1:
            raise ValueError('Чанки редакции имеют общий source_url')
        for number, point in enumerate(self.points):
            if point.revision_id != self.revision_id or point.chunk_no != number or point.chunk_id != uuid5(self.revision_id, str(number)):
                raise ValueError('Нарушена идентичность или нумерация чанков')
        for number, point in enumerate(self.topic_points):
            if point.revision_id != self.revision_id or point.question_no != number or point.point_id != uuid5(self.revision_id, 'question:' + str(number)):
                raise ValueError('Нарушена идентичность или нумерация примеров')
        dimensions = {len(point.dense) for point in [*self.points, *self.topic_points]}
        if len(dimensions) != 1 or 0 in dimensions:
            raise ValueError('Векторы двух наборов имеют одну ненулевую размерность')
        return self


async def stage_upload(revision_id: UUID, document: ConvertedDocument, source: UploadSource) -> str:
    """Устойчиво сохранить готовый Markdown и манифест, не оригинал документа.

    Args:
        revision_id: Редакция и ключ каталога.
        document: Готовый Markdown и ImageRef без байтов.
        source: Название оригинала для манифеста.

    Returns:
        Одна строка — внутренний ключ загрузки.
    """
    with _watchdog._step(_get().settings.io_timeout_s, "upload_spool"):
        return await asyncio.to_thread(_spool._call,_spool._stage,revision_id,document,source)


async def open_staged_upload(staging_ref: str) -> StagedUpload | None:
    """Открыть converted.md и манифест временной загрузки.

    Args:
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        Объект Pydantic `StagedUpload` или `None`.
    """
    with _watchdog._step(_get().settings.io_timeout_s, "upload_spool"):
        return await asyncio.to_thread(_spool._call,_spool._open_staged,staging_ref)


async def remove_staged_upload(staging_ref: str) -> None:
    """Удалить временную загрузку после завершения или согласованной уборки.

    Args:
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        Одно значение Python, `None`.
    """
    with _watchdog._step(_get().settings.io_timeout_s, "upload_spool"):
        return await asyncio.to_thread(_spool._call,_spool._remove,staging_ref)


async def list_staging_candidates(older_than: datetime, limit: int, after: str | None = None) -> list[str]:
    """Найти старые загрузки для сверки с PostgreSQL перед приёмом новых.

    Args:
        older_than: Порог возраста загрузки в UTC.
        limit: Положительный предел размера пачки.
        after: Исключающий курсор staging_ref; порядок лексикографический.

    Returns:
        Список строк, пустой список допустим.
    """
    _utc(older_than)
    _positive(limit)
    if after is not None:
        from rag.runtime._validation import _staging_revision
        _staging_revision(after)
    with _watchdog._step(_get().settings.io_timeout_s, "upload_spool"):
        return await asyncio.to_thread(_spool._call,_spool._candidates,older_than,limit,after)


async def store_document_batch(staging_ref: str, batch: DocumentBatch) -> None:
    """Устойчиво сохранить полное разбиение документа до регистрации задания.

    Совпадающий повтор безопасен, замена партии под тем же ключом запрещена.
    Источник и изображения должны совпадать с манифестом stage_upload.
    """
    with _watchdog._step(_get().settings.io_timeout_s, "upload_spool"):
        await asyncio.to_thread(_spool._call, _spool._batch, staging_ref, batch)


async def load_document_batch(staging_ref: str) -> DocumentBatch | None:
    """Прочитать неизменяемую партию чанков; None означает отсутствие chunks.json.

    Повреждённая существующая партия даёт StorageError, а не None.
    """
    with _watchdog._step(_get().settings.io_timeout_s, "upload_spool"):
        return await asyncio.to_thread(_spool._call, _spool._batch, staging_ref)


async def load_prepared_revision(staging_ref: str) -> PreparedRevision | None:
    """Прочитать сохранённый набор подготовки без повторной генерации.

    Args:
        staging_ref: Ключ уже принятого Markdown.

    Returns:
        PreparedRevision либо None.
    """
    with _watchdog._step(_get().settings.io_timeout_s, "upload_spool"):
        return await asyncio.to_thread(_spool._call,_spool._prepared,staging_ref)


async def store_prepared_revision(staging_ref: str, prepared: PreparedRevision) -> None:
    """Устойчиво закрепить полный неизменяемый набор до первой записи Qdrant.

    Args:
        staging_ref: Каталог принятой загрузки.
        prepared: Полные валидированные наборы и профили.

    Returns:
        None после устойчивого сохранения.
    """
    with _watchdog._step(_get().settings.io_timeout_s, "upload_spool"):
        await asyncio.to_thread(_spool._call,_spool._prepared,staging_ref,prepared)
