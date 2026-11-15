r"""Load the three demo PDFs through HTTP and enable @verdenkaa.

Run from app: .venv\Scripts\python.exe scripts/load_demo_documents.py
Requires running RAG API (8000), Ingestion HTTP (8001), and their dependencies.
Secrets come from the root .env (or RAG_ENV_FILE); process environment wins.
202 means durably accepted, not indexed. Relay and Worker run separately.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from dotenv import dotenv_values


ROOT = Path(__file__).resolve().parents[2]
FILENAMES = (
    "Функциональные_возможности_работы_с_БАРС_Офисом.pdf",
    "БП Командировка.pdf",
    "Реестр мебели.pdf",
)


def save_state(path, state):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def base_url(value):
    parsed = urlsplit(value)
    if (parsed.scheme not in {"http", "https"} or not parsed.hostname
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError("URL должен быть HTTP(S), без credentials, query и fragment")
    if parsed.scheme == "http" and parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Для удалённого сервера используйте HTTPS")
    return value.rstrip("/")


def run(args, config, client):
    token = config.get("RAG_INGESTION_TOKEN", "")
    login, password = config.get("ADMIN_LOGIN", ""), config.get("ADMIN_PASSWORD", "")
    if not token or not login or not password:
        raise RuntimeError("Заполните RAG_INGESTION_TOKEN, ADMIN_LOGIN, ADMIN_PASSWORD в .env")
    from rag.rag_api.config import trusted_base_url

    public = urlsplit(trusted_base_url(config.get("TELEGRAM_WEBHOOK_URL", "")))
    origin = f"{public.scheme}://{public.netloc}"
    api, ingestion = base_url(args.api_url), base_url(args.ingestion_url)
    state = json.loads(args.state.read_text(encoding="utf-8")) if args.state.exists() else {}
    # Check every input before changing anything remotely.
    inputs = []
    for filename in FILENAMES:
        path = args.downloads / filename
        content = path.read_bytes()
        if not content.startswith(b"%PDF-"):
            raise RuntimeError(f"Не PDF: {filename}")
        mime = "application/pdf"
        if getattr(args, "text_only_pdf", False):
            from rag.ingestion._parsers import PdfParser
            # Explicitly requested text-only import; original PDFs stay untouched.
            content = PdfParser().parse(content, lambda image: "").encode("utf-8")
            filename = path.stem + ".md"
            mime = "text/markdown"
        key = hashlib.sha256((ingestion + "\n" + filename + "\n").encode() + content).hexdigest()
        previous = state.get(key)
        if previous and previous.get("status") != "accepted":
            raise RuntimeError(
                f"{filename}: исход прошлого POST неизвестен. Проверьте логи/БД и запись в "
                f"{args.state} перед повтором: автоматический повтор может создать дубликат."
            )
        inputs.append((filename, key, content, mime))

    response = client.put(
        api + "/admin/users/verdenkaa", json={"enabled": True},
        auth=httpx.BasicAuth(login, password), headers={"Origin": origin},
    )
    if response.status_code != 200:
        raise RuntimeError(f"Регистрация @verdenkaa: HTTP {response.status_code}")
    record = response.json()
    if record.get("username") != "verdenkaa" or record.get("enabled") is not True:
        raise RuntimeError("API не подтвердил допуск @verdenkaa")
    print("Допуск @verdenkaa включён.")

    for filename, key, content, mime in inputs:
        previous = state.get(key)
        new_revision = bool(previous and getattr(args, "new_revision", False))
        if previous and not new_revision:
            print(f"Пропущен ранее принятый: {filename}; job_id={state[key]['job_id']}")
            continue
        # Persist before POST: a timeout/crash may happen after a successful commit.
        state[key] = {"filename": filename, "status": "unconfirmed"}
        params = {"filename": filename, "access_level": "employees"}
        if new_revision:
            params["document_id"] = previous["document_id"]
            state[key].update(document_id=previous["document_id"], previous_job_id=previous["job_id"])
        save_state(args.state, state)
        print(f"Загрузка: {filename}", flush=True)
        response = client.post(
            ingestion + "/admin/ingestion/upload",
            params=params,
            headers={"Authorization": f"Bearer {token}", "Content-Type": mime},
            content=content,
        )
        if response.status_code != 202:
            raise RuntimeError(
                f"{filename}: HTTP {response.status_code}. Приём не подтверждён; "
                f"проверьте сервер и {args.state} перед повтором."
            )
        accepted = response.json()
        if accepted.get("kind") != "accepted":
            raise RuntimeError("Некорректное подтверждение загрузки")
        document_id, job_id = str(UUID(accepted["document_id"])), str(UUID(accepted["job_id"]))
        if params.get("document_id") and document_id != params["document_id"]:
            raise RuntimeError("API не подтвердил идентичность обновляемого документа")
        state[key].update(status="accepted", document_id=document_id, job_id=job_id)
        save_state(args.state, state)
        print(f"Принят: document_id={document_id}; job_id={job_id}")
    print("Все три документа приняты. Для индексации должны работать Queue Relay и Worker.")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="http://127.0.0.1:8000")
    parser.add_argument("--ingestion-url", default="http://127.0.0.1:8001")
    parser.add_argument("--downloads", type=Path, default=Path.home() / "Downloads")
    parser.add_argument("--state", type=Path, default=ROOT / "app/.demo-upload-state.json")
    parser.add_argument("--text-only-pdf", action="store_true",
                        help="Extract Markdown from the PDFs and upload it without images")
    parser.add_argument("--new-revision", action="store_true",
                        help="Create new revisions of accepted documents; inspect previous jobs first")
    args = parser.parse_args()
    config = {**dotenv_values(os.environ.get("RAG_ENV_FILE") or ROOT / ".env"), **os.environ}
    try:
        # No redirects, implicit retries or environment proxying of local credentials.
        with httpx.Client(timeout=httpx.Timeout(900, connect=10), trust_env=False,
                          follow_redirects=False) as client:
            run(args, config, client)
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(1) from None
    except (ValueError, KeyError, OSError, httpx.HTTPError):
        # Never print exception bodies: upstream replies/URLs can contain secrets.
        print("Операция остановлена. Проверьте файлы, настройки и доступность HTTP-сервисов.")
        print(f"Состояние: {args.state}. Не повторяйте неподтверждённый POST без проверки сервера.")
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
