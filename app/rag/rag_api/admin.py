"""Authenticated administrative HTTP routes; identity never comes from JSON."""

import base64
import binascii
import secrets
from datetime import datetime
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import ValidationError

from rag.rag_api import access, support
from rag.rag_api._http import _message_body
from rag.rag_api.config import _WebhookSettings, trusted_base_url
from rag.rag_api.contracts import AccessRecord, Principal
from rag.runtime._config import RuntimeSettings

router = APIRouter()


async def _authenticate(request: Request):
    settings = RuntimeSettings()
    login, password = settings.admin_login.get_secret_value(), settings.admin_password.get_secret_value()
    if not login or not password:
        raise HTTPException(503, 'auth_not_configured')
    scheme, _, value = request.headers.get('authorization', '').partition(' ')
    try:
        decoded = base64.b64decode(value, validate=True).decode('utf-8') if scheme.lower() == 'basic' else ''
        supplied_login, supplied_password = decoded.split(':', 1)
    except (ValueError, UnicodeError, binascii.Error):
        supplied_login = supplied_password = ''
    valid = (secrets.compare_digest(supplied_login.encode(), login.encode())
             & secrets.compare_digest(supplied_password.encode(), password.encode()))
    if not valid:
        raise HTTPException(401, 'unauthorized', headers={'WWW-Authenticate': 'Basic realm="Support"'})
    if request.method not in ('GET', 'HEAD'):
        from urllib.parse import urlsplit
        try:
            configured = urlsplit(trusted_base_url(_WebhookSettings().public_url))
        except ValueError:
            raise HTTPException(503, 'auth_not_configured') from None
        expected = f'{configured.scheme}://{configured.netloc}'
        if request.headers.get('origin') != expected:
            raise HTTPException(403, 'invalid_origin')
    return Principal(login=login)


@router.get('/tickets')
async def _tickets(limit: int = Query(50, ge=1, le=100), actor: Principal = Depends(_authenticate)):
    return await support.list_tickets(actor, limit)


@router.get('/admin/metrics')
async def _metrics(from_at: datetime, to_at: datetime, actor: Principal = Depends(_authenticate)):
    if from_at.tzinfo is None or to_at.tzinfo is None or from_at >= to_at:
        raise HTTPException(422, 'invalid_period')
    return await support.get_metrics(actor, from_at, to_at)


@router.put('/admin/users/{username}')
async def _set_access(username: str, request: Request, actor: Principal = Depends(_authenticate)):
    # This small body has the same resource cap as messages, before JSON parsing.
    from rag.rag_api._http import _json_body
    body = await _json_body(request)
    try:
        import json
        value = json.loads(body)
        if not isinstance(value, dict) or set(value) != {'enabled'}:
            raise ValueError
        normalized = access._normalize_username(username)
        if normalized is None:
            raise ValueError
        record = AccessRecord(username=normalized, enabled=value['enabled'])
    except (ValueError, ValidationError):
        raise HTTPException(422, 'invalid_access_record') from None
    return await access.set_user_access(actor, record)


@router.post('/tickets/{case_id}/close')
async def _close(case_id: UUID, actor: Principal = Depends(_authenticate)):
    return await support.close_ticket(actor, case_id)


@router.post('/admin/tickets/{case_id}/messages')
async def _message(case_id: UUID, request: Request, actor: Principal = Depends(_authenticate)):
    body = await _message_body(request)
    return await support.add_ticket_message(actor, case_id, body.client_message_id, body.text)


@router.get('/admin')
async def _panel(actor: Principal = Depends(_authenticate)):
    return HTMLResponse('''<!doctype html><html lang="ru"><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1"><title>Поддержка</title>
<style>body{font:18px/1.6 system-ui;max-width:900px;margin:32px auto;padding:0 16px}pre{white-space:pre-wrap}textarea{width:100%;min-height:100px}button{margin:8px}article{border-bottom:1px solid #ccc;padding:16px 0}</style>
<h1>Поддержка</h1><p id="status" role="status"></p><section id="tickets"></section>
<h2>Допуск сотрудника</h2><form id="access"><input id="username" required placeholder="Telegram username">
<label><input type="checkbox" id="enabled" checked>Разрешён</label><button>Сохранить</button></form>
<h2>Метрики</h2><form id="metrics"><input id="from" type="date" required><input id="to" type="date" required><button>Показать</button></form><pre id="stats"></pre>
<script>
const status = document.querySelector('#status');
async function api(path, options={}) {
 const response = await fetch(path, {...options, headers:{'Content-Type':'application/json',...options.headers}});
 if (!response.ok) throw Error('Не удалось выполнить запрос. Код: '+response.status);
 return response.json();
}
function element(tag, text, parent) { const e=document.createElement(tag); e.textContent=text; parent.append(e); return e; }
async function load() {
 const root=document.querySelector('#tickets'); root.replaceChildren();
 for (const ticket of await api('/tickets?limit=100')) {
  const article=element('article','',root); element('h2',ticket.case_id+' · '+ticket.state,article);
  const link=element('a','История и переписка',article); link.href='/tickets/'+ticket.case_id; link.target='_blank'; link.rel='noopener';
  if(ticket.state==='open') {
   const form=element('form','',article), text=element('textarea','',form); text.required=true; text.maxLength=4096;
   element('button','Ответить как оператор',form); let pending=null;
   form.onsubmit=async event=>{event.preventDefault(); if(!pending||pending.text!==text.value) pending={client_message_id:crypto.randomUUID(),text:text.value};
    try {await api('/admin/tickets/'+ticket.case_id+'/messages',{method:'POST',body:JSON.stringify(pending)});pending=null;text.value='';status.textContent='Ответ сохранён';}catch(e){status.textContent=e.message;}};
   const close=element('button','Закрыть тикет',article); close.onclick=async()=>{try{await api('/tickets/'+ticket.case_id+'/close',{method:'POST'});await load();}catch(e){status.textContent=e.message;}};
  }
 }
}
document.querySelector('#access').onsubmit=async event=>{event.preventDefault();try{
 await api('/admin/users/'+encodeURIComponent(document.querySelector('#username').value),{method:'PUT',body:JSON.stringify({enabled:document.querySelector('#enabled').checked})});status.textContent='Допуск сохранён';}catch(e){status.textContent=e.message;}};
document.querySelector('#metrics').onsubmit=async event=>{event.preventDefault();try{
 const from=document.querySelector('#from').value+'T00:00:00Z',to=document.querySelector('#to').value+'T00:00:00Z';
 document.querySelector('#stats').textContent=JSON.stringify(await api('/admin/metrics?from_at='+encodeURIComponent(from)+'&to_at='+encodeURIComponent(to)),null,2);}catch(e){status.textContent=e.message;}};
load().catch(e=>status.textContent=e.message);
</script></html>''', headers={'Cache-Control':'no-store', 'Referrer-Policy':'no-referrer',
                            'X-Content-Type-Options':'nosniff'})
