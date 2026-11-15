#!/usr/bin/env python3
import argparse
import asyncio
import os
import sys
from pathlib import Path
from urllib.parse import quote
from uuid import uuid4


def configure_project(project: Path) -> None:
    app_dir = project.expanduser().resolve() / "app"
    if not (app_dir / "rag" / "ingestion" / "converter.py").is_file():
        raise FileNotFoundError(f"Не найден конвертер в {app_dir}")

    sys.path.insert(0, str(app_dir))

    # Путь должен быть доступен и дочернему процессу PDF-парсера.
    previous = os.environ.get("PYTHONPATH", "")
    os.environ["PYTHONPATH"] = (
        str(app_dir) + (os.pathsep + previous if previous else "")
    )


async def preview_pdf(source: Path, output_dir: Path):
    from pydantic import TypeAdapter

    from rag.ingestion.contracts import UploadInput, UploadSource
    from rag.ingestion.converter import convert_document
    from rag.model_gateway.contracts import ModelBinding, ModelRole
    from rag.model_gateway.entrypoints import close_models, start_models
    from rag.runtime._config import RuntimeSettings

    source = source.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()

    if not source.is_file():
        raise FileNotFoundError(f"Не найден входной файл: {source}")

    md_path = output_dir / f"{source.stem}.md"
    images_dir = output_dir / f"{source.stem}_images"

    if md_path.exists() or images_dir.exists():
        raise FileExistsError(
            "Результат с таким именем уже существует. "
            "Укажи другой каталог."
        )

    async def content():
        with source.open("rb") as stream:
            while block := stream.read(64 * 1024):
                yield block

    settings = RuntimeSettings()
    bindings = TypeAdapter(dict[ModelRole, ModelBinding]).validate_json(
        settings.model_bindings.get_secret_value()
    )
    selected = {
        role: binding
        for role, binding in bindings.items()
        if role in {"ocr", "vision"}
    }

    # Запускаются только модели. PostgreSQL, S3 и очереди не нужны.
    try:
        if selected:
            async with asyncio.timeout(settings.startup_timeout_s):
                await start_models(selected)

        result = await convert_document(
            UploadInput(
                content=content(),
                source=UploadSource(
                    filename=source.name,
                    access_level="employees",
                ),
                document_id=None,
            ),
            uuid4(),
        )
    finally:
        async with asyncio.timeout(settings.shutdown_timeout_s):
            await close_models()

    output_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir()

    markdown = result.markdown

    for image in result.images:
        filename = f"{image.ref.image_id}.png"
        (images_dir / filename).write_bytes(image.content.data)

        relative_url = quote(
            f"{images_dir.name}/{filename}",
            safe="/",
        )
        markdown = markdown.replace(
            f"(image://{image.ref.image_id})",
            f"({relative_url})",
        )

    with md_path.open("x", encoding="utf-8", newline="\n") as stream:
        stream.write(markdown)

    return md_path, images_dir, len(result.images)


def main():
    parser = argparse.ArgumentParser(
        description="PDF → Markdown и изображения через конвертер проекта"
    )
    parser.add_argument("input", type=Path, help="Путь к PDF")
    parser.add_argument(
        "output_dir", type=Path, help="Каталог для результата"
    )
    parser.add_argument(
        "--project",
        type=Path,
        default=Path("/home/randajam/Working/docs-bots"),
        help="Корень проекта docs-bots",
    )
    args = parser.parse_args()

    try:
        configure_project(args.project)

        from pydantic import ValidationError
        from rag.contracts import StepError

        try:
            md_path, images_dir, count = asyncio.run(
                preview_pdf(args.input, args.output_dir)
            )
        except StepError as error:
            parser.exit(
                1,
                f"Ошибка конвертации: {error.code}; "
                f"retryable={error.retryable}\n",
            )
        except ValidationError:
            parser.exit(
                1,
                "Некорректная конфигурация конвертера или моделей. "
                "Проверь .env.\n",
            )
        except TimeoutError:
            parser.exit(
                1, "Истёк срок запуска или остановки моделей.\n"
            )
    except OSError as error:
        parser.exit(1, f"Ошибка файловой системы: {error}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Прервано.\n")

    print(f"Markdown: {md_path}")
    print(f"Изображения: {images_dir} ({count} шт.)")


if __name__ == "__main__":
    main()