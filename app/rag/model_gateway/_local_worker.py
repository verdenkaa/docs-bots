"""One long-lived local backend; imported by a dedicated child, never by startup."""

import importlib
import os
from pathlib import Path
import sys

from rag.contracts import StepError
from rag.model_gateway.contracts import ModelBinding
from rag.model_gateway.providers import GenerationStream, LocalModel
from rag.model_gateway._streams import _Accumulator
from rag.model_gateway._validation import _error
from rag.model_gateway._wire import _decode, _encode


def _read(stream, maximum):
    header = stream.read(4)
    if not header:
        return None
    if len(header) != 4:
        raise ValueError()
    size = int.from_bytes(header, "big")
    if size > maximum:
        raise ValueError()
    body = stream.read(size)
    if len(body) != size:
        raise ValueError()
    return _decode(body)


def _main():
    maximum = int(sys.argv[1])
    incoming = sys.stdin.buffer
    outgoing = os.fdopen(os.dup(sys.stdout.fileno()), "wb")
    # Provider imports, native Python prints and model output cannot corrupt IPC
    # or expose payloads to the parent's logs. OS stderr is DEVNULL as well.
    os.dup2(sys.stderr.fileno(), sys.stdout.fileno())
    sys.stdout = sys.stderr
    model = None
    try:
        initial = _read(incoming, maximum)
        binding = ModelBinding.model_validate(initial["binding"])
        memory = initial["memory_bytes"]
        if memory is not None and sys.platform == "linux":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        module, attribute = initial["loader"].split(":")
        factory = getattr(importlib.import_module(module), attribute)
        model = factory(
            binding=binding, role=initial["role"], cache_dir=Path(initial["cache_dir"])
        )
        if not isinstance(model, LocalModel):
            raise ValueError()
        outgoing.write(_encode({"ok": True}, maximum))
        outgoing.flush()
    except BaseException:
        outgoing.write(
            _encode({"error": {"code": "model_configuration_error"}}, maximum)
        )
        outgoing.flush()
        return
    try:
        while True:
            request = _read(incoming, maximum)
            if request is None:
                return
            try:
                if request["kind"] == "close":
                    model.close()
                    model = None
                    response = {"ok": True}
                else:
                    value = model.invoke(request["operation"], request["payload"])
                    if isinstance(value, GenerationStream):
                        if request["operation"] != "generate":
                            raise StepError("model_invalid_response", False)
                        accumulator = _Accumulator(value.profile_id, maximum)
                        chunks = value.chunks
                        try:
                            for event in chunks:
                                accumulator._append(event)
                            value = accumulator._result()
                        finally:
                            close = getattr(chunks, "close", None)
                            if close is not None:
                                close()
                    response = {"result": value}
                encoded = _encode(response, maximum)
            except Exception as exc:
                error = _error(exc)
                if isinstance(exc, (TypeError, ValueError)):
                    error = StepError("model_invalid_response", False)
                encoded = _encode({"error": {"code": error.code}}, maximum)
            outgoing.write(encoded)
            outgoing.flush()
            if request["kind"] == "close":
                return
    finally:
        if model is not None:
            model.close()


if __name__ == "__main__":
    _main()
