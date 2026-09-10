from hashlib import sha256
from io import BytesIO

import pytest
from PIL import Image

from app.imaging import InvalidReceiptImage, prepare_receipt_image


def make_png(size: tuple[int, int]) -> bytes:
    image = Image.new("RGBA", size, (40, 100, 180, 128))
    output = BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


def test_prepare_receipt_image_resizes_converts_and_hashes() -> None:
    original = make_png((3200, 800))

    prepared = prepare_receipt_image(original, max_side_px=1600)

    assert prepared.content_type == "image/jpeg"
    assert prepared.width == 1600
    assert prepared.height == 400
    assert prepared.sha256 == sha256(original).hexdigest()
    with Image.open(BytesIO(prepared.content)) as result:
        assert result.format == "JPEG"
        assert result.mode == "RGB"


def test_prepare_receipt_image_rejects_non_image_upload() -> None:
    with pytest.raises(InvalidReceiptImage, match="safe, readable"):
        prepare_receipt_image(b"this is not an image")


def test_prepare_receipt_image_rejects_oversized_upload_before_decode() -> None:
    with pytest.raises(InvalidReceiptImage, match="too large"):
        prepare_receipt_image(b"0" * 11, max_upload_bytes=10)


def test_prepare_receipt_image_rejects_unsupported_format() -> None:
    output = BytesIO()
    Image.new("RGB", (10, 10), "white").save(output, format="GIF")

    with pytest.raises(InvalidReceiptImage, match="JPEG, PNG, or WebP"):
        prepare_receipt_image(output.getvalue())
