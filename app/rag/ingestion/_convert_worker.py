"""One memory-bounded parse. stdin: original; stdout: framed Markdown and PNGs.

The process never creates a local original or image directory. Its parent owns
the lifetime of the process group. Gateway stores the returned images in S3.
"""

import json
import os
import struct
import sys


_EXIT_CODES = {"conversion_failed": 2, "unsupported_format": 3, "too_large": 5}


def _main() -> None:
    kind, marker = sys.argv[1], sys.argv[4]
    memory, maximum, markdown_limit, image_count, image_limit, output_limit = (
        int(sys.argv[index]) for index in (2, 3, 5, 6, 7, 8)
    )
    output = os.fdopen(os.dup(1), "wb")
    os.dup2(2, 1)  # third-party diagnostics must never corrupt the wire format
    if sys.platform == "linux":
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))

    from rag.ingestion._parsers import PARSERS, ParseRefused

    images, image_bytes = [], 0

    def add_image(png):
        nonlocal image_bytes
        if len(images) >= image_count or len(png) > image_limit:
            raise ParseRefused("too_large", "Image limit exceeded")
        image_bytes += len(png)
        if image_bytes > output_limit:
            raise ParseRefused("too_large", "Parser output limit exceeded")
        images.append(png)
        return marker.format(len(images) - 1)

    try:
        data = sys.stdin.buffer.read(maximum + 1)
        if len(data) > maximum:
            raise ParseRefused("too_large", "Input limit exceeded")
        markdown = PARSERS[kind].parse(data, add_image).encode("utf-8")
        if len(markdown) > markdown_limit:
            raise ParseRefused("too_large", "Markdown limit exceeded")
        header = json.dumps({"markdown": len(markdown), "images": [len(image) for image in images]}).encode()
        if 4 + len(header) + len(markdown) + image_bytes > output_limit:
            raise ParseRefused("too_large", "Parser output limit exceeded")
    except ParseRefused as error:
        sys.exit(_EXIT_CODES[error.code])
    except ImportError:
        sys.exit(6)  # missing deployment dependency is not a malformed document
    except MemoryError:
        sys.exit(4)
    except Exception:
        sys.exit(2)  # do not emit an exception that might contain document text

    with output:
        output.write(struct.pack(">I", len(header)) + header + markdown)
        for image in images:
            output.write(image)
        output.flush()


if __name__ == "__main__":
    _main()
