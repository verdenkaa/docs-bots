"""One bounded image conversion in a disposable process. stdin/stdout are bytes."""

from io import BytesIO
import math
import sys
import warnings


def _convert(data, max_pixels):
    from PIL import Image, ImageOps

    Image.MAX_IMAGE_PIXELS = max_pixels
    with warnings.catch_warnings():
        warnings.simplefilter("error", Image.DecompressionBombWarning)
        with Image.open(BytesIO(data), formats=["PNG", "JPEG", "GIF", "WEBP"]) as source:
            if source.width * source.height > max_pixels:
                raise ValueError("Pixel limit exceeded")
            source.seek(0)  # The fixed photo profile takes the first animation frame.
            source.load()
            oriented = ImageOps.exif_transpose(source)
            source.close()
            width, height = oriented.size
            padded_width, padded_height = max(width, math.ceil(height / 20)), max(
                height, math.ceil(width / 20)
            )
            scale = min(1, 9998 / (padded_width + padded_height))
            size = (max(1, int(width * scale)), max(1, int(height * scale)))
            reduced = (
                oriented.resize(size, Image.Resampling.LANCZOS)
                if size != oriented.size
                else oriented
            )
            rgba = reduced.convert("RGBA")
            rgb = Image.new("RGB", rgba.size, "white")
            rgb.paste(rgba, mask=rgba.getchannel("A"))
            canvas = Image.new(
                "RGB",
                (
                    max(rgb.width, math.ceil(rgb.height / 20)),
                    max(rgb.height, math.ceil(rgb.width / 20)),
                ),
                "white",
            )
            canvas.paste(rgb, ((canvas.width - rgb.width) // 2, (canvas.height - rgb.height) // 2))
            del oriented, reduced, rgba, rgb
            for _ in range(32):
                for quality in (90, 80, 65, 50, 35):
                    output = BytesIO()
                    canvas.save(
                        output, format="JPEG", quality=quality, optimize=False, progressive=False
                    )
                    encoded = output.getvalue()
                    if len(encoded) <= 10_000_000:
                        with Image.open(BytesIO(encoded)) as check:
                            check.load()
                            if (
                                check.format != "JPEG"
                                or check.width + check.height > 10000
                                or max(check.size) > 20 * min(check.size)
                            ):
                                raise ValueError("Photo limits not satisfied")
                        return encoded
                width, height = max(1, int(canvas.width * 0.8)), max(1, int(canvas.height * 0.8))
                # Rounding must not turn an exactly 20:1 image into 21:1.
                width, height = max(width, math.ceil(height / 20)), max(
                    height, math.ceil(width / 20)
                )
                canvas = canvas.resize((width, height), Image.Resampling.LANCZOS)
    raise ValueError("Photo cannot fit byte limit")


def _main():
    try:
        pixels, memory, maximum = map(int, sys.argv[1:])
        if sys.platform == "linux":
            import resource

            resource.setrlimit(resource.RLIMIT_AS, (memory, memory))
        data = sys.stdin.buffer.read(maximum + 1)
        if len(data) > maximum:
            raise ValueError("Input limit exceeded")
        result = _convert(data, pixels)
    except ImportError:
        sys.exit(3)
    except Exception:
        sys.exit(2)
    sys.stdout.buffer.write(result)


if __name__ == "__main__":
    _main()
