"""Contracts: docs/documents/infrastructure/upload-spool/contracts.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from datetime import datetime
from typing import Self
from uuid import UUID, uuid5

from pydantic import Field, model_validator

from rag.dto import DTO, NonEmptyText
from rag.ingestion.contracts import ConvertedDocument, StagedUpload, UploadSource
from rag.runtime.qdrant import ChunkPoint, TopicPoint


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

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        revision_id: Редакция и ключ каталога.
        document: Готовый Markdown и ImageRef без байтов.
        source: Название оригинала для манифеста.

    Returns:
        Одна строка — внутренний ключ загрузки.
    """
    raise NotImplementedError


async def open_staged_upload(staging_ref: str) -> StagedUpload | None:
    """Открыть converted.md и манифест временной загрузки.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        Объект Pydantic `StagedUpload` или `None`.
    """
    raise NotImplementedError


async def remove_staged_upload(staging_ref: str) -> None:
    """Удалить временную загрузку после завершения или согласованной уборки.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        Одно значение Python, `None`.
    """
    raise NotImplementedError


async def list_staging_candidates(older_than: datetime, limit: int) -> list[str]:
    """Найти старые загрузки для сверки с PostgreSQL перед приёмом новых.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        older_than: Порог возраста загрузки в UTC.
        limit: Положительный предел размера пачки.

    Returns:
        Список строк, пустой список допустим.
    """
    raise NotImplementedError


async def load_prepared_revision(staging_ref: str) -> PreparedRevision | None:
    """Прочитать сохранённый набор подготовки без повторной генерации.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        staging_ref: Ключ уже принятого Markdown.

    Returns:
        PreparedRevision либо None.
    """
    raise NotImplementedError


async def store_prepared_revision(staging_ref: str, prepared: PreparedRevision) -> None:
    """Устойчиво закрепить полный неизменяемый набор до первой записи Qdrant.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        staging_ref: Каталог принятой загрузки.
        prepared: Полные валидированные наборы и профили.

    Returns:
        None после устойчивого сохранения.
    """
    raise NotImplementedError
