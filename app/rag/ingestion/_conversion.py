"""Converter configuration, a bounded one-pass input and image enrichment."""

from __future__ import annotations

from collections.abc import AsyncIterable
import asyncio
import importlib
import inspect
import os
import re
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from rag.contracts import ImageContent, ImageRef, StepError
from rag.dto import NonEmptyText, PositiveId
from rag.ingestion.contracts import UploadInput, UploadSource
from rag.model_gateway import entrypoints as models
from rag.model_gateway.contracts import ImageDescription, OcrResult
from rag.runtime import s3


class _ConversionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    converter_callable: NonEmptyText
    ocr_profile: NonEmptyText
    vision_profile: NonEmptyText
    timeout_s: float = Field(default=300, gt=0)
    image_timeout_s: float = Field(default=10, gt=0)
    model_timeout_s: float = Field(default=60, gt=0)
    max_upload_bytes: PositiveId = 50 * 1024 * 1024
    max_markdown_bytes: PositiveId = 20 * 1024 * 1024
    max_image_bytes: PositiveId = 20 * 1024 * 1024
    max_images: PositiveId = 1000
    max_description_characters: PositiveId = 8000

    @classmethod
    def from_environment(cls) -> _ConversionSettings:
        aliases = dict(converter_callable="INGESTION_CONVERTER_CALLABLE",
                       ocr_profile="INGESTION_OCR_PROFILE",
                       vision_profile="INGESTION_VISION_PROFILE")
        values = {}
        for field in cls.model_fields:
            key = aliases.get(field, "INGESTION_CONVERSION_" + field.upper())
            if key in os.environ:
                values[field] = os.environ[key]
        try:
            return cls.model_validate(values)
        except ValidationError:
            raise StepError("ingestion_configuration_error", False) from None


def _validate_upload(upload: UploadInput, revision_id: UUID) -> UploadInput:
    try:
        if (not isinstance(upload, UploadInput) or not isinstance(revision_id, UUID)
                or not isinstance(upload.content, AsyncIterable)
                or (upload.document_id is not None and not isinstance(upload.document_id, UUID))):
            raise ValueError()
        source = UploadSource.model_validate(upload.source.model_dump()
                                            if isinstance(upload.source, UploadSource) else upload.source)
        return UploadInput(content=upload.content, source=source, document_id=upload.document_id)
    except (ValueError, TypeError, AttributeError):
        raise StepError("invalid_upload", False) from None


def _load_converter(path: str):
    try:
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*:[A-Za-z_]\w*", path):
            raise ValueError()
        module, name = path.split(":")
        adapter = getattr(importlib.import_module(module), name)
        if not inspect.iscoroutinefunction(adapter):
            raise ValueError()
        inspect.signature(adapter).bind(None, None)
        return adapter
    except Exception:
        raise StepError("ingestion_configuration_error", False) from None


class _LimitedContent:
    """The adapter gets one iterator; finish also checks unread trailing bytes."""

    def __init__(self, content: AsyncIterable[bytes], maximum: int):
        self.iterator = aiter(content)
        self.maximum, self.total = maximum, 0
        self.started = self.finished = False

    def __aiter__(self):
        if self.started:
            raise StepError("invalid_upload", False)
        self.started = True
        return self

    async def __anext__(self):
        if self.finished:
            raise StopAsyncIteration
        try:
            value = await anext(self.iterator)
        except StopAsyncIteration:
            self.finished = True
            if not self.total:
                raise StepError("invalid_upload", False) from None
            raise
        if not isinstance(value, bytes):
            raise StepError("invalid_upload", False)
        self.total += len(value)
        if self.total > self.maximum:
            raise StepError("too_large", False)
        return value

    async def finish(self):
        while not self.finished:
            try:
                await self.__anext__()
            except StopAsyncIteration:
                break
        if not self.total:
            raise StepError("invalid_upload", False)


def _validated(model, raw, error_code):
    try:
        return model.model_validate(raw.model_dump(warnings=False) if isinstance(raw, model) else raw)
    except (ValidationError, ValueError, TypeError):
        raise StepError(error_code, False) from None


class _ImageEnricher:
    def __init__(self, settings: _ConversionSettings):
        self.settings = settings

    async def describe(self, links) -> list[ImageRef]:
        images = []
        # Process one unique image at a time, retaining descriptions rather than bytes.
        for image_id in dict.fromkeys(link[2] for link in links):
            images.append(await self._describe(image_id))
        return images

    async def _describe(self, image_id: UUID) -> ImageRef:
        settings = self.settings
        try:
            async with asyncio.timeout(settings.image_timeout_s):
                image = _validated(ImageContent, await s3.get_image(image_id), "image_invalid_data")
        except TimeoutError:
            raise StepError("image_timeout", True) from None
        if not image.data or not image.mime_type.startswith("image/"):
            raise StepError("image_invalid_data", False)
        if len(image.data) > settings.max_image_bytes:
            raise StepError("too_large", False)
        try:
            async with asyncio.timeout(settings.model_timeout_s):
                ocr = _validated(OcrResult, await models.recognize_text(
                    image, timeout_s=settings.model_timeout_s), "model_invalid_response")
            async with asyncio.timeout(settings.model_timeout_s):
                vision = _validated(ImageDescription, await models.describe_image(
                    image, timeout_s=settings.model_timeout_s), "model_invalid_response")
        except TimeoutError:
            raise StepError("model_timeout", True) from None
        if ocr.profile_id != settings.ocr_profile or vision.profile_id != settings.vision_profile:
            raise StepError("model_profile_mismatch", False)
        description = vision.description
        if ocr.text.strip():
            description += "\nРаспознанный текст: " + ocr.text
        description = " ".join(description.split())
        if len(description) > settings.max_description_characters:
            raise StepError("image_description_too_large", False)
        return ImageRef(image_id=image_id, description=description)
