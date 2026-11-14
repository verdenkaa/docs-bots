"""Contracts: docs/documents/telegram/module/service-entrypoints.md.

Допуск через Runtime; административная операция остаётся заглушкой.
"""


from __future__ import annotations

from rag.rag_api.contracts import AccessRecord, Principal
from rag.runtime.postgresql import support as repository
import re


def _normalize_username(username: object) -> str | None:
    if not isinstance(username, str) or not username.isascii():
        return None
    value = username.removeprefix('@').lower()
    return value if re.fullmatch(r'[a-z0-9_]{1,32}', value, re.ASCII) else None


async def check_access(username: str | None) -> AccessRecord | None:
    """Прочитать действующий допуск сотрудника; сбой БД не даёт разрешение.

    Args:
        username: Нормализованный Telegram from.username; None не даёт допуск.

    Returns:
        AccessRecord или None.
    """
    record = await repository.read_user_access(_normalize_username(username))
    return record if record is not None and record.enabled else None


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
