"""Render embedded EMF/WMF on Linux, keeping the source bytes in a memory file.

Called only inside the bounded parser process. Inkscape inherits its memory limit
and process group, so parser timeout/cancellation also stops the renderer.
"""

from __future__ import annotations

import math
import os
import posixpath
import shutil
import struct
import subprocess
import tempfile

from rag.ingestion._parsers import ParseRefused, to_png

MAX_SIDE = 4096
MAX_RENDER_BYTES = 128 * 1024 * 1024


def _check_header(data: bytes, suffix: str) -> None:
    if suffix == ".emf":
        valid = len(data) >= 88 and data[:4] == b"\x01\x00\x00\x00" and data[40:44] == b" EMF"
    else:
        offset = 22 if data.startswith(b"\xd7\xcd\xc6\x9a") else 0
        valid = (len(data) >= offset + 18 and
                 struct.unpack_from("<HHH", data, offset) in ((1, 9, 0x100), (1, 9, 0x300),
                                                            (2, 9, 0x100), (2, 9, 0x300)))
    if not valid:
        raise ParseRefused("conversion_failed", "DOCX metafile header is invalid")


def _run(command: list[str], descriptor: int, env: dict[str, str], maximum: int) -> bytes:
    # No diagnostics from the renderer: they can contain document text or paths.
    with subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL, pass_fds=(descriptor,), env=env) as process:
        try:
            output = process.stdout.read(maximum + 1)
            if len(output) > maximum:
                raise ParseRefused("too_large", "DOCX metafile output exceeds limit")
            if process.wait() != 0:
                raise ParseRefused("conversion_failed", "DOCX metafile cannot be rendered")
            return output
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()


def metafile_to_png(data: bytes, suffix: str) -> bytes:
    _check_header(data, suffix)
    executable = shutil.which("inkscape")
    if executable is None or not hasattr(os, "memfd_create"):
        raise ImportError("DOCX metafile rendering requires Inkscape and Linux memfd")

    descriptor = os.memfd_create("docx-metafile", os.MFD_CLOEXEC)
    with (os.fdopen(descriptor, "w+b") as source,
          tempfile.TemporaryDirectory(prefix="rag-metafile-",
                                      dir=os.environ.get("_RAG_CONVERTER_WORK_DIRECTORY")) as directory):
        source.write(data)
        source.flush()
        source.seek(0)
        # The importer selects the format by suffix. The symlink contains no
        # document bytes; /proc/self/fd refers to the descriptor inherited below.
        name = posixpath.join(directory, "image" + suffix)
        os.symlink(f"/proc/self/fd/{descriptor}", name)
        env = dict(os.environ, INKSCAPE_PROFILE_DIR=posixpath.join(directory, "profile"),
                   XDG_CONFIG_HOME=posixpath.join(directory, "config"),
                   XDG_CACHE_HOME=posixpath.join(directory, "cache"),
                   XDG_DATA_HOME=posixpath.join(directory, "data"))
        dimensions = _run([executable, name, "--query-width", "--query-height"], descriptor, env, 1024)
        try:
            width, height = map(float, dimensions.split())
            if not all(math.isfinite(value) and value > 0 for value in (width, height)):
                raise ValueError
        except ValueError:
            raise ParseRefused("conversion_failed", "DOCX metafile dimensions are invalid") from None
        # Inkscape reports CSS pixels (96 dpi); render at 144 dpi, with a bound
        # on the longer side. Preserve the original aspect ratio.
        side = max(1, round(min(MAX_SIDE, max(width, height) * 1.5)))
        axis = "width" if width >= height else "height"
        png = _run([executable, name, "--export-type=png", "--export-filename=-",
                    "--export-area-drawing", f"--export-{axis}={side}",
                    "--export-background=#ffffff", "--export-background-opacity=1"],
                   descriptor, env, MAX_RENDER_BYTES)
    return to_png(png)
