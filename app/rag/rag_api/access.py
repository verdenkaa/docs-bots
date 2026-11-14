"""Contracts: docs/documents/telegram/module/service-entrypoints.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from rag.rag_api.contracts import AccessRecord, Principal


async def check_access(username: str | None) -> AccessRecord | None:
    """Прочитать действующий допуск сотрудника; сбой БД не даёт разрешение.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        username: Нормализованный Telegram from.username; None не даёт допуск.

    Returns:
        AccessRecord или None.
    """
    raise NotImplementedError


async def set_user_access(actor: Principal, record: AccessRecord) -> AccessRecord:
    """Изменить допуск Telegram username от имени проверенного администратора.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        actor: Проверенный администратор.
        record: Нормализованный username и enabled.

    Returns:
        AccessRecord.
    """
    raise NotImplementedError
