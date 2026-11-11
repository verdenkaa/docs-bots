"""Contracts: docs/documents/infrastructure/upload-spool/contracts.md."""


from collections.abc import AsyncIterable
from datetime import datetime
from uuid import UUID

from rag.ingestion.contracts import StagedUpload, UploadRejected, UploadSource


async def stage_upload(
    revision_id: UUID,
    content: AsyncIterable[bytes],
    source: UploadSource,
) -> str | UploadRejected:
    """Сохранить поток и метаданные во временный рабочий том.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        revision_id: ID редакции, равный ID задания индексации.
        content: Содержимое файла из входного потока.
        source: Имя файла для парсинга и адрес для ссылки на источник.

    Returns:
        Внутренний staging_ref либо UploadRejected при ожидаемом отказе.
    """
    raise NotImplementedError


async def open_staged_upload(staging_ref: str) -> StagedUpload | None:
    """Открыть исходник и манифест временной загрузки для парсера.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        StagedUpload либо None при отсутствии загрузки.
    """
    raise NotImplementedError


async def remove_staged_upload(staging_ref: str) -> None:
    """Удалить временную загрузку после завершения или согласованной уборки.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        staging_ref: Внутренний ключ файла и манифеста во временном томе.

    Returns:
        None; отсутствие уже удалённой загрузки допустимо.
    """
    raise NotImplementedError


async def list_staging_candidates(older_than: datetime, limit: int) -> list[str]:
    """Найти старые загрузки для сверки с PostgreSQL перед приёмом новых.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        older_than: Порог возраста загрузки в UTC.
        limit: Положительный предел размера пачки.

    Returns:
        Список staging_ref; это кандидаты, а не разрешение на удаление.
    """
    raise NotImplementedError
