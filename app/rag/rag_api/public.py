"""Public per-ticket pages and document title placeholders. No Admin routes."""
from html import escape
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict, StringConstraints

from rag.contracts import StorageError
from rag.rag_api import support
from rag.rag_api.contracts import Conflict, NotFound
from rag.runtime import media
from rag.rag_api._observability import _call

router = APIRouter()


class VisitorMessage(BaseModel):
    model_config = ConfigDict(extra='forbid', hide_input_in_errors=True)
    client_message_id: UUID
    text: Annotated[str, StringConstraints(strict=True, min_length=1, pattern=r'\S')]


def page(title, body):
    return HTMLResponse('<!doctype html><html lang="ru"><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width, initial-scale=1">'
        f'<title>{escape(title)}</title><style>'
        'body{font:18px/1.6 system-ui;margin:40px auto;padding:0 20px;max-width:800px;color:#17232b}'
        'article{padding:16px 0;border-bottom:1px solid #ddd}pre{white-space:pre-wrap;overflow-wrap:anywhere;font:inherit}'
        'textarea{width:100%;min-height:120px}button{padding:10px 20px}small{color:#54616a}'
        f'</style><body><h1>{escape(title)}</h1>{body}</body></html>',
        headers={'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff',
                 'Referrer-Policy': 'no-referrer'})


@router.get('/tickets/{case_id}')
async def _ticket_page(case_id: UUID):
    try:
        view = await support.get_ticket(case_id)
    except NotFound:
        return Response(status_code=404)
    except StorageError:
        return Response(status_code=503)
    if view.case.state not in ('open', 'closed'):
        return Response(status_code=404)
    body = '<p>' + ('Тикет закрыт. Доступен для чтения.' if view.case.state == 'closed' else 'Тикет открыт.') + '</p>'
    for entry in view.history:
        body += '<article><h2>Исходный вопрос</h2><pre>' + escape(entry.question_text) + '</pre>'
        if entry.answer_text is not None:
            body += '<h3>Ответ бота</h3><pre>' + escape(entry.answer_text) + '</pre>'
        body += '</article>'
    for message in view.messages:
        role = 'Оператор' if message.author_role == 'operator' else 'Посетитель'
        body += f'<article><strong>{role}</strong> <small>{escape(message.created_at.isoformat())}</small><pre>{escape(message.text)}</pre></article>'
    if view.case.state == 'open':
        body += '''<form id="reply"><h2>Сообщение посетителя</h2>
<textarea id="text" required aria-label="Сообщение"></textarea><br>
<button>Отправить</button><p id="status" role="status"></p></form>
<script>
const form = document.getElementById('reply');
let pending = null;
form.addEventListener('submit', async event => {
  event.preventDefault();
  const text = document.getElementById('text').value;
  if (!text.trim()) return;
  if (!pending || pending.text !== text) pending = {client_message_id: crypto.randomUUID(), text};
  const button = form.querySelector('button');
  button.disabled = true;
  try {
    const response = await fetch(location.pathname.replace(/\\/$/, '') + '/messages', {
      method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(pending)
    });
    if (response.ok) { pending = null; location.reload(); }
    else document.getElementById('status').textContent = 'Не удалось отправить. Код: ' + response.status;
  } catch { document.getElementById('status').textContent = 'Связь прервана. Повторите отправку.'; }
  finally { button.disabled = false; }
});
</script>'''
    return page('Обращение в поддержку', body)


@router.post('/tickets/{case_id}/messages')
async def _ticket_message(case_id: UUID, body: VisitorMessage):
    try:
        return await support.add_ticket_message(None, case_id, body.client_message_id, body.text)
    except NotFound:
        return Response(status_code=404)
    except Conflict:
        return Response(status_code=409)
    except StorageError:
        return Response(status_code=503)


@router.get('/sources/{document_id}')
async def _source_page(document_id: UUID):
    try:
        metadata = await _call('load_source_metadata', media.load_source_metadata, document_id)
    except StorageError:
        return Response(status_code=503)
    if metadata is None:
        return Response(status_code=404)
    return page('Источник', '<p>Тут должен быть хостинг документа „' + escape(metadata.source_name) + '“.</p>')
