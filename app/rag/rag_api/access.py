"""Contracts: docs/documents/telegram/module/service-entrypoints.md.

Каркас модуля: DTO валидируются, прикладные точки входа реализует владелец.
"""


from __future__ import annotations

from rag.rag_api.contracts import AccessRecord, Principal


async def check_access(user_id: int) -> AccessRecord | None:
    """Прочитать действующий допуск сотрудника; сбой БД не даёт разрешение.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        user_id: Telegram from.id.

    Returns:
        AccessRecord или None.
    """
    raise NotImplementedError


async def set_user_access(actor: Principal, record: AccessRecord) -> AccessRecord:
    """Изменить допуск или роль сотрудника от имени проверенного администратора.

    Заглушка: реализация прикладной операции пока отсутствует (NotImplementedError).

    Args:
        actor: Проверенный администратор.
        record: Целевой сотрудник, допуск и роль.

    Returns:
        AccessRecord.
    """
    raise NotImplementedError
