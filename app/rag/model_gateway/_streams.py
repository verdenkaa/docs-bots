"""A stream is successful only after an explicit, final terminal event."""

from collections.abc import Mapping
from rag.contracts import StepError


class _Accumulator:
    def __init__(self, profile, maximum):
        self.profile = profile
        self.maximum = maximum
        self.size = 0
        self.text = []
        self.finish = None
        self.usage = None

    def _append(self, event):
        if (
            not isinstance(event, Mapping)
            or set(event) - {"text", "finish_reason", "usage"}
            or self.finish is not None
            or not isinstance(event.get("text"), str)
        ):
            raise StepError("model_invalid_response", False)
        self.size += len(event["text"].encode())
        if self.size > self.maximum:
            raise StepError("model_invalid_response", False)
        self.text.append(event["text"])
        if event.get("finish_reason") is not None:
            if event["finish_reason"] not in ("completed", "length", "filtered"):
                raise StepError("model_invalid_response", False)
            self.finish, self.usage = event["finish_reason"], event.get("usage")
        elif "usage" in event:
            raise StepError("model_invalid_response", False)

    def _result(self):
        if self.finish is None:
            raise StepError("model_invalid_response", False)
        return dict(
            profile_id=self.profile,
            text="".join(self.text),
            finish_reason=self.finish,
            usage=self.usage,
        )


async def _collect(stream, maximum):
    result = _Accumulator(stream.profile_id, maximum)
    if not hasattr(stream.chunks, "__aiter__"):
        raise StepError("model_invalid_response", False)
    try:
        async for event in stream.chunks:
            result._append(event)
        return result._result()
    finally:
        close = getattr(stream.chunks, "aclose", None)
        if close is not None:
            await close()
