"""Validation shared by live calls, readiness probes and restarted executors."""

from collections.abc import Mapping, Sequence
import json
import math
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, TypeAdapter, ValidationError
from rag.contracts import ImageContent, StepError
from rag.model_gateway.contracts import (
    EmbeddingResult,
    GenerationOptions,
    ImageDescription,
    InjectionResult,
    ModelBinding,
    ModelGenerationResult,
    ModelMessage,
    ModelRole,
    OcrResult,
    RerankResult,
)

_roles = {
    "embedding": EmbeddingResult,
    "reranker": RerankResult,
    "injection": InjectionResult,
    "generation": ModelGenerationResult,
    "ocr": OcrResult,
    "vision": ImageDescription,
}
_temporary = {"model_timeout", "model_overloaded", "model_unavailable"}
_permanent = {
    "model_invalid_input",
    "model_not_ready",
    "model_profile_mismatch",
    "model_invalid_response",
    "model_configuration_error",
}


def _error(exc):
    if isinstance(exc, StepError) and exc.code in _temporary | _permanent:
        return StepError(exc.code, exc.code in _temporary)
    if isinstance(exc, TimeoutError):
        return StepError("model_timeout", True)
    return StepError("model_unavailable", True)


def _bindings(value):
    try:
        if not isinstance(value, Mapping):
            raise ValueError()
        copied = {
            key: item.model_dump() if isinstance(item, ModelBinding) else item
            for key, item in value.items()
        }
        bindings = TypeAdapter(dict[ModelRole, ModelBinding]).validate_python(copied)
        profiles = {}
        for role, binding in bindings.items():
            if (role == "embedding") != (binding.embedding_dimensions is not None):
                raise ValueError()
            if (
                binding.profile_id in profiles
                and profiles[binding.profile_id] != binding
            ):
                raise ValueError()
            profiles[binding.profile_id] = binding
            if binding.execution == "remote":
                endpoint = urlsplit(binding.endpoint)
                if (
                    endpoint.scheme not in ("http", "https")
                    or not endpoint.hostname
                    or endpoint.username
                    or endpoint.password
                    or endpoint.query
                    or endpoint.fragment
                ):
                    raise ValueError()
            elif binding.endpoint is not None:
                raise ValueError()
            if binding.credential_env is not None and not re.fullmatch(
                r"[A-Za-z_][A-Za-z0-9_]*", binding.credential_env
            ):
                raise ValueError()
        return bindings
    except (ValidationError, ValueError, TypeError, AttributeError):
        raise StepError("model_configuration_error", False) from None


def _timeout(value):
    try:
        valid = type(value) in (int, float) and math.isfinite(value) and value > 0
    except OverflowError:
        valid = False
    if not valid:
        raise StepError("model_invalid_input", False)
    return float(value)


def _texts(value, settings, *, allow_empty=True):
    if (
        not isinstance(value, Sequence)
        or isinstance(value, (str, bytes))
        or len(value) > settings.max_batch_items
        or (not allow_empty and not value)
    ):
        raise ValueError()
    if any(not isinstance(text, str) or not text.strip() for text in value):
        raise ValueError()
    if sum(len(text) for text in value) > settings.max_text_chars:
        raise ValueError()
    return list(value)


def _input(operation, payload, settings):
    try:
        if operation == "embed_texts":
            if payload["purpose"] not in ("query", "document"):
                raise ValueError()
            return dict(
                texts=_texts(payload["texts"], settings), purpose=payload["purpose"]
            )
        if operation == "rerank":
            return dict(
                query=_texts([payload["query"]], settings)[0],
                candidates=_texts(payload["candidates"], settings),
            )
        if operation == "classify_injection":
            return dict(text=_texts([payload["text"]], settings)[0])
        if operation == "generate":
            messages = payload["messages"]
            if (
                not isinstance(messages, Sequence)
                or isinstance(messages, (str, bytes))
                or not 0 < len(messages) <= settings.max_batch_items
            ):
                raise ValueError()
            messages = TypeAdapter(list[ModelMessage]).validate_python(
                [
                    (
                        message.model_dump()
                        if isinstance(message, ModelMessage)
                        else message
                    )
                    for message in messages
                ]
            )
            _texts(
                [message.content for message in messages], settings, allow_empty=False
            )
            options = payload["options"]
            options = GenerationOptions.model_validate(
                options.model_dump()
                if isinstance(options, GenerationOptions)
                else options
            )
            return dict(
                messages=[message.model_dump() for message in messages],
                options=options.model_dump(),
            )
        image = payload["image"]
        image = ImageContent.model_validate(
            image.model_dump() if isinstance(image, ImageContent) else image
        )
        if (
            not image.data
            or len(image.data) > settings.max_image_bytes
            or not image.mime_type.startswith("image/")
        ):
            raise ValueError()
        return dict(image=image.model_dump())
    except (ValidationError, ValueError, TypeError, KeyError, AttributeError):
        raise StepError("model_invalid_input", False) from None


def _result(role, binding, payload, value, maximum):
    try:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="json")
        encoded = json.dumps(value, allow_nan=False, ensure_ascii=False).encode()
        if len(encoded) > maximum:
            raise ValueError()
        result = _roles[role].model_validate_json(encoded)
    except (ValidationError, ValueError, TypeError, KeyError, OverflowError):
        raise StepError("model_invalid_response", False) from None
    if result.profile_id != binding.profile_id:
        raise StepError("model_profile_mismatch", False)
    if role == "embedding":
        if len(result.vectors) != len(payload["texts"]):
            raise StepError("model_invalid_response", False)
        if any(
            len(vector) != binding.embedding_dimensions for vector in result.vectors
        ):
            raise StepError("model_profile_mismatch", False)
    if role == "reranker":
        count = len(payload["candidates"])
        if sorted(score.candidate_index for score in result.scores) != list(
            range(count)
        ):
            raise StepError("model_invalid_response", False)
        result.scores.sort(key=lambda score: score.candidate_index)
    return result
