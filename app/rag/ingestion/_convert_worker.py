"""Один разбор документа в одноразовом процессе: stdin — оригинал, stdout — Markdown и картинки.

Запуск: python -m rag.ingestion._convert_worker <формат> <лимит памяти> <лимит входа> <метка картинок>

Формат stdout: 4 байта длины заголовка, JSON-заголовок {"markdown": N, "images": [N, ...]},
затем Markdown в UTF-8 и PNG-картинки подряд. Коды выхода: 0 — готово, 2 — conversion_failed,
3 — unsupported_format, остальное — сбой исполнителя. Причина отказа — последняя строка stderr.
"""

import json
import os
import struct
import sys

EXIT_CODES = {"conversion_failed": 2, "unsupported_format": 3}
EXIT_RESOURCES = 4


def _main() -> None:
    kind, memory, maximum, marker = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
    # библиотеки разбора могут печатать в stdout; результат идёт в отдельную копию дескриптора
    output = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)
    if sys.platform == "linux":
        import resource

        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))

    from rag.ingestion._parsers import PARSERS, ParseRefused

    images: list[bytes] = []

    def add_image(png: bytes) -> str:
        images.append(png)
        return marker.format(len(images) - 1)

    try:
        data = sys.stdin.buffer.read(maximum + 1)
        if len(data) > maximum:
            raise ParseRefused("conversion_failed", "вход больше лимита исполнителя")
        markdown = PARSERS[kind].parse(data, add_image).encode("utf-8")
    except ParseRefused as refusal:
        print(refusal.reason, file=sys.stderr, flush=True)
        sys.exit(EXIT_CODES[refusal.code])
    except MemoryError:
        print("превышен лимит памяти разбора", file=sys.stderr, flush=True)
        sys.exit(EXIT_RESOURCES)
    except Exception as error:  # noqa: BLE001 - повреждённый файл роняет парсер любым исключением
        # текст исключения может содержать фрагменты документа, в диагностику идёт только тип
        print(f"парсер не разобрал документ: {type(error).__name__}", file=sys.stderr, flush=True)
        sys.exit(EXIT_CODES["conversion_failed"])

    header = json.dumps({"markdown": len(markdown), "images": [len(image) for image in images]}).encode()
    output.write(struct.pack(">I", len(header)) + header + markdown)
    for image in images:
        output.write(image)
    output.flush()


if __name__ == "__main__":
    _main()
