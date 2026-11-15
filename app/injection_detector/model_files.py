"""Закреплённые файлы модели детектора: загрузка и сверка SHA-256.

Отдельный модуль, чтобы слой образа с весами (1,2 ГБ) не пересобирался при правке сервера.

    python model_files.py   # скачать файлы в DETECTOR_MODEL_DIR; прерванная загрузка продолжается
"""

from __future__ import annotations

import hashlib
import os
import urllib.request
from pathlib import Path

# Модель и ревизия закреплены: пороги Guardrails подобраны именно на её оценках.
MODEL_ID = "Horizon-Labs/prompt-injection-guard-base"
MODEL_REVISION = "fc071c5754f8cf2edce9dc1d7fd3513ef9287007"
# имя файла -> (путь в репозитории модели, размер в байтах, SHA-256)
FILES = {
    "model.onnx": ("onnx/model.onnx", 1230730152,
                   "5ae9f527897b1ffcb2b13440875769d0c28a25fb976b8b42465f0ad5b462b61b"),
    "tokenizer.json": ("tokenizer.json", 34387555,
                       "47834a7dbbb0c4324fe9569fe47955241594a9ee539c9e478755d6fbc51e5f46"),
}
def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        while chunk := file.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def verify(directory: Path) -> None:
    """Проверить, что в каталоге лежат именно закреплённые файлы модели."""
    for name, (_, size, checksum) in FILES.items():
        path = directory / name
        if not path.is_file() or path.stat().st_size != size or _sha256(path) != checksum:
            raise RuntimeError(f"{name}: файл отсутствует или не совпадает с закреплённой ревизией модели")


def download(directory: Path) -> None:
    """Скачать закреплённые файлы; прерванная загрузка продолжается с места остановки."""
    directory.mkdir(parents=True, exist_ok=True)
    for name, (remote, size, checksum) in FILES.items():
        path, part = directory / name, directory / (name + ".part")
        if path.is_file() and path.stat().st_size == size and _sha256(path) == checksum:
            continue
        offset = part.stat().st_size if part.is_file() else 0
        if offset < size:
            request = urllib.request.Request(
                f"https://huggingface.co/{MODEL_ID}/resolve/{MODEL_REVISION}/{remote}",
                headers={"Range": f"bytes={offset}-"} if offset else {})
            with urllib.request.urlopen(request, timeout=60) as response:
                # сервер, не поддержавший Range, отдаёт файл целиком: начинаем заново
                mode = "ab" if offset and response.status == 206 else "wb"
                with part.open(mode) as file:
                    while chunk := response.read(1 << 20):
                        file.write(chunk)
        if part.stat().st_size != size or _sha256(part) != checksum:
            part.unlink()
            raise RuntimeError(f"{name}: загруженный файл не совпадает с закреплённой ревизией модели")
        part.replace(path)
        print(f"{name}: загружен и проверен", flush=True)


def model_directory() -> Path:
    return Path(os.environ.get("DETECTOR_MODEL_DIR", "/opt/detector/model"))


if __name__ == "__main__":
    download(model_directory())
