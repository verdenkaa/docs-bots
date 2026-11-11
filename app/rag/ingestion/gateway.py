"""Contracts: docs/documents/ingestion/entrypoints.md."""


from rag.ingestion.contracts import UploadInput, UploadResult


async def accept_upload(upload: UploadInput) -> UploadResult:
    """Принять поток файла, сохранить временный исходник и зарегистрировать индексацию.

    Заглушка: реализация пока отсутствует (NotImplementedError).

    Args:
        upload: Объект загрузки с потоком байтов и метаданными.

    Returns:
        UploadResult: accepted с job_id/document_id либо ожидаемый rejected.
        После подтверждения исходник должен переживать рестарт Worker.
    """
    raise NotImplementedError
