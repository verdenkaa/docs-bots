"""Contracts: docs/documents/retrieval/entrypoints.md.

Поиск и отбор фрагментов через Model Gateway и адаптер Qdrant Runtime.
"""


from __future__ import annotations

import re
from uuid import UUID

from pydantic import TypeAdapter, ValidationError

from rag.contracts import SearchFragment, StepError
from rag.dto import NonEmptyText
from rag.model_gateway import entrypoints as models
from rag.runtime import qdrant
from rag.runtime._state import _get


_query_adapter = TypeAdapter(NonEmptyText)
_revisions_adapter = TypeAdapter(frozenset[UUID])
_link = re.compile(
    r"\b[a-z][a-z\d+.-]*://[^\s<>]+|\bwww\.[^\s<>]+"
    r"|!?\[[^\]\n]*\]\(\s*<?[^)\s>]+"
    r"|(?<![\w:/])(?:/|\.\.?/)[^\s<>]+",
    re.IGNORECASE,
)


async def search(query: str, revision_ids: frozenset[UUID]) -> list[SearchFragment]:
    """Получить эмбеддинг, найти и отобрать фрагменты в закреплённых редакциях.

    Args:
        query: Нормализованный вопрос из Guardrails.
        revision_ids: Редакции из load_revision_ids для закреплённого поколения.

    Returns:
        Список объектов Pydantic `SearchFragment`, отсортированных по релевантности. `[]` означает, что кандидатов нет. `[]` в колонке поля обозначает элемент списка.
    """
    try:
        query = _query_adapter.validate_python(query, strict=True)
        revision_ids = _revisions_adapter.validate_python(revision_ids, strict=True)
    except ValidationError:
        raise StepError("search_invalid_input", False) from None
    if not revision_ids:
        return []

    try:
        settings = _get().settings
    except RuntimeError as exc:
        raise StepError("search_not_ready", False) from exc
    if not settings.embedding_profile:
        raise StepError("embedding_profile_not_configured", False)

    embedding = await models.embed_texts(
        [query], "query", timeout_s=settings.embedding_timeout_s
    )
    if embedding.profile_id != settings.embedding_profile:
        raise StepError("embedding_profile_mismatch", False)
    if len(embedding.vectors) != 1:
        raise StepError("model_invalid_response", False)
    query_vector = embedding.vectors[0]
    if len(query_vector) != settings.embedding_dimensions:
        raise StepError("embedding_profile_mismatch", False)

    hits = await qdrant.find_chunks(
        query, query_vector, revision_ids, settings.search_candidate_limit,
        lexical_priority=_link.search(query) is not None,
    )
    if not hits:
        return []

    if settings.use_reranker:
        result = await models.rerank(
            query, [hit.fragment.text for hit in hits],
            timeout_s=settings.rerank_timeout_s,
        )
        # Gateway validates the profile and one score per input candidate.
        # Retain original positions for stable ties even if scores arrive reordered.
        scores = {item.candidate_index: item.score for item in result.scores}
        if len(result.scores) != len(hits) or set(scores) != set(range(len(hits))):
            raise StepError("model_invalid_response", False)
        ranked = [(hit, scores[index]) for index, hit in enumerate(hits)]
        ranked.sort(key=lambda item: item[1], reverse=True)
    else:
        ranked = [(hit, hit.score) for hit in hits]

    threshold = settings.search_score_threshold
    return [
        hit.fragment for hit, score in ranked
        if threshold is None or score >= threshold
    ][:settings.search_top_k]
