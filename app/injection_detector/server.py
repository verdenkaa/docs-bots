"""HTTP-сервис детектора prompt injection на ONNX Runtime (без torch).

Отдельный контейнер рядом с приложением: образ приложения собран на Alpine, где ONNX Runtime недоступен.
Model Gateway обращается к сервису через адаптер rag/model_gateway/injection_detector.py.
Описание и запуск: docs/documents/model-gateway/injection-detector.md.

    python model_files.py   # скачать закреплённые файлы модели и сверить SHA-256 (шаг сборки образа)
    python server.py        # загрузить модель и принимать запросы

Сервис возвращает только оценку: разность логитов «инъекция − безопасно», больше — опаснее. Порогов и
решения о допуске здесь нет: они принадлежат Guardrails (docs/documents/guardrails/detector.md).
"""

from __future__ import annotations

import json
import math
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from model_files import MODEL_ID, MODEL_REVISION, model_directory, verify

# окна те же, что при подборе порога: при их смене порог подбирается заново
WINDOW_TOKENS, WINDOW_STRIDE = 512, 64
INJECTION_INDEX, SAFE_INDEX = 1, 0  # id2label модели: 0 — SAFE, 1 — INJECTION


class Scorer:
    """Модель в памяти процесса; score потокобезопасен (сессия ONNX Runtime допускает параллельные вызовы)."""

    def __init__(self, directory: Path, threads: int) -> None:
        import numpy
        import onnxruntime
        from tokenizers import Tokenizer

        verify(directory)
        self._numpy = numpy
        self._tokenizer = Tokenizer.from_file(str(directory / "tokenizer.json"))
        self._tokenizer.enable_truncation(max_length=WINDOW_TOKENS, stride=WINDOW_STRIDE)
        self._tokenizer.no_padding()
        options = onnxruntime.SessionOptions()
        if threads > 0:
            options.intra_op_num_threads = threads
        self._session = onnxruntime.InferenceSession(str(directory / "model.onnx"), options,
                                                     providers=["CPUExecutionProvider"])
        self._inputs = {item.name for item in self._session.get_inputs()}

    def score(self, text: str) -> float:
        """Разность логитов; для длинного текста — максимум по перекрывающимся окнам."""
        first = self._tokenizer.encode(text)
        scores = []
        for window in (first, *first.overflowing):
            feed = {"input_ids": [window.ids], "attention_mask": [window.attention_mask],
                    "token_type_ids": [window.type_ids]}
            logits = self._session.run(None, {name: self._numpy.asarray(value, dtype=self._numpy.int64)
                                              for name, value in feed.items() if name in self._inputs})[0][0]
            scores.append(float(logits[INJECTION_INDEX] - logits[SAFE_INDEX]))
        return max(scores)


def make_handler(score, max_body_bytes: int):
    """Обработчик HTTP; score — функция text -> float, передаётся отдельно ради тестов без модели."""
    identity = {"model_id": MODEL_ID, "model_revision": MODEL_REVISION}

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args) -> None:
            # тексты вопросов в журнал не попадают; стандартный журнал запросов отключён
            return

        def _reply(self, status: int, body: dict) -> None:
            encoded = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            if self.path == "/health/ready":
                # сервер начинает слушать только после загрузки модели: ответ означает готовность
                self._reply(200, {"status": "ready", **identity})
            else:
                self._reply(404, {"error": "not_found"})

        def do_POST(self) -> None:
            if self.path != "/classify":
                return self._reply(404, {"error": "not_found"})
            try:
                length = int(self.headers.get("Content-Length", ""))
            except ValueError:
                self.close_connection = True
                return self._reply(411, {"error": "length_required"})
            if not 0 < length <= max_body_bytes:
                self.close_connection = True  # тело не читаем
                return self._reply(413, {"error": "too_large"})
            try:
                text = json.loads(self.rfile.read(length))["text"]
                if not isinstance(text, str) or not text.strip():
                    raise ValueError()
            except (ValueError, KeyError, TypeError):
                return self._reply(400, {"error": "invalid_input"})
            try:
                value = score(text)
                if not math.isfinite(value):
                    raise ValueError()
            except Exception:
                # причина остаётся в stderr контейнера без текста вопроса
                print("inference failed", file=sys.stderr, flush=True)
                return self._reply(500, {"error": "inference_failed"})
            self._reply(200, {"score": value, **identity})

    return Handler


def main() -> None:
    scorer = Scorer(model_directory(), int(os.environ.get("DETECTOR_THREADS", "0")))
    scorer.score("Как оформить командировку?")  # прогрев до приёма запросов
    handler = make_handler(scorer.score, int(os.environ.get("DETECTOR_MAX_BODY_BYTES", "262144")))
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("DETECTOR_PORT", "8080"))), handler)
    server.daemon_threads = True
    print(f"detector ready: {MODEL_ID}@{MODEL_REVISION}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
