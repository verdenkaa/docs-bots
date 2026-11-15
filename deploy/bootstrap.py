"""Idempotent storage preparation and optional initial employee access."""

import json

from rag._configuration import environment_values
from rag.rag_api.contracts import AccessRecord, Principal
from rag.runtime._config import RuntimeSettings
from rag.runtime.lifecycle import start_runtime, close_runtime, prepare_runtime_storage
from rag.runtime.postgresql.support import write_user_access


async def prepare_storage():
    await start_runtime()
    try:
        if environment_values().get("ALLOW_EMPTY_EMBEDDING_PROFILE_SWITCH", "false").lower() == "true":
            from rag.runtime._qdrant import _switch_empty_embedding_profile
            await _switch_empty_embedding_profile()
        await prepare_runtime_storage()
        settings = RuntimeSettings()
        for username in json.loads(environment_values().get("RAG_BOOTSTRAP_USERNAMES", "[]")):
            await write_user_access(Principal(login=settings.admin_login.get_secret_value()),
                                    AccessRecord(username=username.removeprefix("@").lower(), enabled=True))
        print("Storage preparation completed", flush=True)
    finally:
        await close_runtime()
