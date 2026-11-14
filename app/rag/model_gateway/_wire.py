"""Private, size-bounded IPC. Model payloads never enter logs or RabbitMQ."""

import base64
import json
from pydantic import BaseModel


def _pack(value):
    if isinstance(value, BaseModel):
        value = value.model_dump()
    if isinstance(value, bytes):
        return {"$bytes": base64.b64encode(value).decode("ascii")}
    if isinstance(value, dict):
        return {key: _pack(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_pack(item) for item in value]
    return value


def _unpack(value):
    if isinstance(value, dict):
        if set(value) == {"$bytes"}:
            return base64.b64decode(value["$bytes"], validate=True)
        return {key: _unpack(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_unpack(item) for item in value]
    return value


def _encode(value, maximum):
    encoded = json.dumps(_pack(value), ensure_ascii=False, allow_nan=False).encode()
    if len(encoded) > maximum:
        raise ValueError("IPC size limit")
    return len(encoded).to_bytes(4, "big") + encoded


def _decode(value):
    return _unpack(json.loads(value))
