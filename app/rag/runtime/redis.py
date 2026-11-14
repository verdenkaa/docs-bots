"""Contracts: docs/documents/infrastructure/redis/contracts.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from pydantic import Field

from rag.contracts import RagResult
from rag.dto import DTO, NonNegativeInt


class CacheUnavailable(Exception):
    """Операция Redis недоступна; Exact Cache допускает работу без кеша."""


class CachedAnswer(RagResult):
    """Ответ Exact Cache с поколением корпуса для проверки актуальности."""
    corpus_generation: NonNegativeInt = Field(description='Поколение набора доступных редакций; 0 допустим для пустого корпуса.')


class CacheRead(DTO):
    """Прочитанный ответ и токен, защищающий условное удаление от гонки с записью."""
    value: CachedAnswer = Field(description='Ответ и поколение, прочитанные из Redis.')
    compare_token: bytes = Field(description='Непрозрачный токен прочитанного значения для атомарного условного удаления.')


async def read_cached_answer(query: str) -> CacheRead | None:
    """Прочитать точный кеш и токен его наблюдаемого значения.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        query: Нормализованный вопрос из Guardrails.

    Returns:
        Объект Pydantic `CacheRead` или `None`.
    """
    raise NotImplementedError


async def delete_cached_answer_if_unchanged(query: str, compare_token: bytes) -> bool:
    """Атомарно удалить только ранее прочитанное значение кеша.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        query: Нормализованный вопрос из Guardrails.
        compare_token: Токен из CacheRead, идентифицирующий прочитанное значение.

    Returns:
        Одно логическое значение.
    """
    raise NotImplementedError


async def write_cached_answer_if_not_older(query: str, value: CachedAnswer) -> bool:
    """Атомарно записать ответ с TTL без перезаписи более нового поколения.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        query: Нормализованный вопрос из Guardrails.
        value: Сохранённый результат и поколение, по которому он получен.

    Returns:
        Одно логическое значение.
    """
    raise NotImplementedError
