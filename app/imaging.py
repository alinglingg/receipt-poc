"""Safe, deterministic receipt image validation and compression."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from io import BytesIO
import warnings

from PIL import Image, ImageOps, UnidentifiedImageError


class InvalidReceiptImage(ValueError):
    """Raised when an upload is unsafe or unsuitable for receipt extraction."""


@dataclass(frozen=True)
class PreparedImage:
    content: bytes
    sha256: str
    width: int
    height: int
    content_type: str = "image/jpeg"


ALLOWED_FORMATS = {"JPEG", "PNG", "WEBP"}


def prepare_receipt_image(
    source: bytes,
    *,
    max_upload_bytes: int = 10 * 1024 * 1024,
    max_pixels: int = 40_000_000,
    max_side_px: int = 1600,
    jpeg_quality: int = 82,
) -> PreparedImage:
    """Validate and convert an uploaded receipt image to a compact JPEG.

    The input-size limit is checked before decoding. The pixel limit protects
    against decompression bombs, while a longest-edge cap controls vision cost.
    """
    if not source:
        raise InvalidReceiptImage("The image file is empty.")
    if len(source) > max_upload_bytes:
        raise InvalidReceiptImage("The image is too large. Please send a file under 10 MB.")
    if not 1 <= jpeg_quality <= 95:
        raise ValueError("jpeg_quality must be between 1 and 95.")

    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(BytesIO(source)) as probe:
                image_format = probe.format
                probe.verify()

            if image_format not in ALLOWED_FORMATS:
                raise InvalidReceiptImage("Please send a JPEG, PNG, or WebP image.")

            with Image.open(BytesIO(source)) as opened:
                if opened.width * opened.height > max_pixels:
                    raise InvalidReceiptImage("The image dimensions are too large. Please send a smaller image.")

                image = ImageOps.exif_transpose(opened)
                if image.mode in {"RGBA", "LA"} or (image.mode == "P" and "transparency" in image.info):
                    background = Image.new("RGB", image.size, "white")
                    background.paste(image.convert("RGBA"), mask=image.convert("RGBA").getchannel("A"))
                    image = background
                else:
                    image = image.convert("RGB")

                if max(image.size) > max_side_px:
                    image.thumbnail((max_side_px, max_side_px), Image.Resampling.LANCZOS)

                output = BytesIO()
                image.save(output, format="JPEG", quality=jpeg_quality, optimize=True, progressive=True)
    except (UnidentifiedImageError, Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
        raise InvalidReceiptImage("The upload is not a safe, readable image.") from exc
    except OSError as exc:
        raise InvalidReceiptImage("The image could not be decoded. Please send a clearer photo.") from exc

    compressed = output.getvalue()
    return PreparedImage(
        content=compressed,
        sha256=sha256(source).hexdigest(),
        width=image.width,
        height=image.height,
    )
