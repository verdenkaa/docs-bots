"""Contracts: docs/documents/telegram/module/http-adapter-entrypoints.md.

Telegram HTTP boundary with one final request event and timed steps."""
import hmac
import json
from fastapi import APIRouter, Request, Response
from rag.rag_api import entrypoints
from rag.rag_api.config import get_settings
from rag.rag_api._observability import _operation, _step, _utc_now


router = APIRouter(prefix="/telegram", tags=["Telegram"])

@router.post("/webhook")
async def telegram_webhook(request: Request) -> Response:
    with _operation("handle_telegram_update", received_at=_utc_now()) as trace:
        trace.operation = "handle_telegram_update"
        trace.fields.setdefault("received_at", _utc_now())
        status, outcome, error = 503, "retry_later", None
        try:
            with _step("validate_webhook_secret") as step:
                secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
                valid = hmac.compare_digest(secret.encode(), get_settings().webhook_secret.encode())
                step["outcome"] = "allowed" if valid else "denied"
            if not valid:
                status, outcome, error = 403, "denied", "invalid_webhook_secret"
            else:
                with _step("validate_content_type") as step:
                    valid = request.headers.get("content-type", "").split(";", 1)[0].strip().lower() == "application/json"
                    step["outcome"] = "valid" if valid else "invalid"
                if not valid:
                    status, outcome, error = 400, "invalid_update", "invalid_content_type"
                else:
                    with _step("decode_update", error_code="invalid_json"):
                        update = json.loads(await request.body())
                    with _step("validate_update_id") as step:
                        valid = (isinstance(update, dict) and type(update.get("update_id")) is int
                                 and 0 <= update["update_id"] <= 2 ** 63 - 1)
                        step["outcome"] = "valid" if valid else "invalid"
                    if not valid:
                        status, outcome, error = 400, "invalid_update", "invalid_update_id"
                    else:
                        trace.fields["update_id"] = update["update_id"]
                        result = await entrypoints.handle_telegram_update(update)
                        outcome = result.kind
                        trace.fields.update(request_id=result.request_id, delivery_id=result.delivery_id,
                                            case_id=str(result.case_id) if result.case_id else None)
                        status = {"invalid_update": 400, "retry_later": 503}.get(outcome, 200)
                        if status != 200:
                            error = outcome
        except (json.JSONDecodeError, UnicodeDecodeError):
            status, outcome, error = 400, "invalid_update", "invalid_json"
        except Exception:
            status, outcome, error = 503, "retry_later", "intake_error"
        finally:
            request.state.intake_logged = True
            if error == "retry_later":
                error = trace.fields.get("error_code", error)
            trace.fields.update(outcome=outcome, error_code=error, http_status=status,
                                method="POST", route="/telegram/webhook")
        return Response(status_code=status)
