"""Общие примеры и проверка сигнатур без исполнения прикладных заглушек."""

import inspect
from datetime import datetime, timezone
from typing import get_type_hints
from uuid import UUID, uuid5

from pydantic import TypeAdapter

from rag.contracts import ImageRef, ProcessingAttempt, SourceRef
from rag.ingestion.contracts import UploadInput


NOW = datetime(2026, 11, 14, 12, tzinfo=timezone.utc)
REVISION = UUID("bb32ecbf-082b-4497-a41f-4b61aa56a139")
DOCUMENT = UUID("43f95f66-7343-4cbc-beb5-19fd36a8ca77")
IMAGE = ImageRef(image_id=uuid5(REVISION, "image:0"), description="Схема согласования")
MARKDOWN = f"Откройте фильтр. ![Схема согласования](image://{IMAGE.image_id})"


def validate_call_arguments(function, *args, **kwargs):
    """Проверить типы и обязательность параметров, не вызывая прикладную операцию."""
    bound = inspect.signature(function).bind(*args, **kwargs)
    hints = get_type_hints(function, include_extras=True)
    for name, value in bound.arguments.items():
        if hints[name] is UploadInput:
            if not isinstance(value, UploadInput):
                raise TypeError("Технический вход загрузки должен быть UploadInput")
        else:
            TypeAdapter(hints[name]).validate_python(value)


def returned(function, value):
    """Проверить пример возвращаемого значения по фактической аннотации функции."""
    return TypeAdapter(get_type_hints(function, include_extras=True)["return"]).validate_python(value)


def processing_attempt():
    """Вернуть отдельную попытку Core, не попытку доставки."""
    return ProcessingAttempt(request_id=1, attempt_no=1, deadline_at=NOW)


def source():
    """Вернуть источник из подготовленного чанка; URL оригинала не требуется."""
    return SourceRef(chunk_id=uuid5(REVISION, "0"), document_id=DOCUMENT,
                     revision_id=REVISION, source_name="Руководство.docx")
