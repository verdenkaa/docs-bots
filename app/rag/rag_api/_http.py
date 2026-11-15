"""Bounded JSON parsing shared by public and administrative ticket routes."""

from fastapi import HTTPException
from pydantic import ValidationError
from rag.rag_api.contracts import TicketMessageInput


async def _json_body(request):
    return await _bounded_body(request, 32 * 1024)


async def _bounded_body(request, maximum):
    """Bound both declared and streamed bytes before decoding untrusted JSON."""
    length = request.headers.get('content-length')
    if length is not None:
        if not length.isascii() or not length.isdecimal():
            raise HTTPException(400, 'invalid_content_length')
        normalized = length.lstrip('0') or '0'
        if len(normalized) > len(str(maximum)) or int(normalized) > maximum:
            raise HTTPException(413, 'message_body_too_large')
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > maximum:
            raise HTTPException(413, 'message_body_too_large')
        chunks.append(chunk)
    return b''.join(chunks)


async def _message_body(request):
    body = await _json_body(request)
    try:
        return TicketMessageInput.model_validate_json(body)
    except ValidationError:
        raise HTTPException(422, 'invalid_ticket_message') from None
