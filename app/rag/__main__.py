"""Запуск роли каркаса с HTTP-проверками живости процесса."""

import argparse
from datetime import UTC, datetime
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from signal import SIGINT, SIGTERM, signal
from time import monotonic
from uuid import uuid4

from rag.runtime.events import emit_event


class HealthHandler(BaseHTTPRequestHandler):
    """HTTP-проверки живости каркаса; зависимости и бизнес-готовность пока не проверяются."""

    def do_GET(self) -> None:
        """Вернуть 200 для health-маршрутов каркаса, 404 для остальных путей."""
        if self.path in ("/health/live", "/health/ready"):
            self.send_response(HTTPStatus.OK)
        else:
            self.send_response(HTTPStatus.NOT_FOUND)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def log_message(self, format: str, *args: object) -> None:
        """Подавить стандартный HTTP access log; события каркаса пишет emit_event."""
        pass


def main() -> None:
    """Запустить health-сервер роли каркаса и писать события старта/штатной остановки."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("role", choices=("core", "worker"))
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    component = {"core": "rag_core", "worker": "ingestion_worker"}[args.role]
    host = "0.0.0.0" if args.role == "core" else "127.0.0.1"
    stopping = False

    def stop(_signum: int, _frame: object) -> None:
        """Установить флаг штатной остановки; основной цикл завершит health-сервер."""
        nonlocal stopping
        stopping = True

    signal(SIGTERM, stop)
    signal(SIGINT, stop)
    run_id = str(uuid4())
    started_at = datetime.now(UTC).isoformat()
    started = monotonic()

    with ThreadingHTTPServer((host, args.port), HealthHandler) as server:
        server.timeout = 0.5
        emit_event(
            component, "bootstrap", "started", run_id,
            started_at=started_at, mode="scaffold", outcome="ready",
        )
        while not stopping:
            server.handle_request()

    emit_event(
        component, "bootstrap", "finished", run_id,
        started_at=started_at, finished_at=datetime.now(UTC).isoformat(),
        duration_ms=round((monotonic() - started) * 1000),
        mode="scaffold", outcome="stopped",
    )


if __name__ == "__main__":
    main()
