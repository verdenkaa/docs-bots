"""Deterministic test backends; not distributed as production model providers."""

import json
import os
import subprocess
import sys
import time

from rag.model_gateway.providers import LocalModel, GenerationStream


def response(binding, role, operation, payload):
    profile = binding.profile_id
    if role == "embedding":
        sign = 1 if payload["purpose"] == "query" else -1
        return dict(
            profile_id=profile,
            vectors=[
                [float(sign * len(text))] * binding.embedding_dimensions
                for text in payload["texts"]
            ],
        )
    if role == "reranker":
        return dict(
            profile_id=profile,
            scores=[
                dict(candidate_index=n, score=float(n))
                for n in reversed(range(len(payload["candidates"])))
            ],
        )
    if role == "injection":
        return dict(profile_id=profile, label="benign", score=None)
    if role == "generation":
        return dict(
            profile_id=profile,
            text="OK",
            finish_reason="completed",
            usage=dict(input_tokens=1, output_tokens=1),
        )
    if role == "ocr":
        return dict(profile_id=profile, text="")
    return dict(profile_id=profile, description="Fixture image")


class FixtureLocalModel(LocalModel):
    def __init__(self, binding, role, cache_dir):
        self.binding, self.role, self.cache_dir = binding, role, cache_dir
        print("Private backend startup output", flush=True)
        os.write(1, b"Native backend startup output\n")
        self.record("load")

    def record(self, operation, **fields):
        with (self.cache_dir / "fixture-calls.jsonl").open(
            "a", encoding="utf-8"
        ) as file:
            file.write(
                json.dumps(dict(operation=operation, pid=os.getpid(), **fields)) + "\n"
            )

    def invoke(self, operation, payload):
        marker = (payload.get("texts") or [""])[0]
        self.record(operation, marker=marker)
        if marker == "DELAY":
            time.sleep(0.2)
        if marker == "SPAWN_HANG":
            child = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"]
            )
            self.record("child", child=child.pid)
            time.sleep(30)
        if marker == "HANG":
            time.sleep(30)
        if self.role == "generation" and payload["messages"][0]["content"] == "BAD_STREAM":

            def chunks():
                try:
                    yield dict(text="partial", finish_reason="unknown")
                    yield dict(text="must not be consumed")
                finally:
                    self.record("stream_closed")

            return GenerationStream(self.binding.profile_id, chunks())
        if self.role == "generation" and payload["messages"][0]["content"] == "STREAM":
            return GenerationStream(
                self.binding.profile_id,
                iter(
                    [
                        dict(text="O"),
                        dict(text="K", finish_reason="completed", usage=None),
                    ]
                ),
            )
        return response(self.binding, self.role, operation, payload)

    def close(self):
        self.record("close")


def create_model(*, binding, role, cache_dir):
    return FixtureLocalModel(binding, role, cache_dir)
