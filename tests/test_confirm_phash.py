from __future__ import annotations

import io

import pytest
from PIL import Image

from imageshield.attribution.crop import UndecodableImage
from imageshield.confirm.phash import bit_population, dhash, hamming


def _png(color: tuple[int, int, int], size: tuple[int, int] = (64, 64)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _gradient(size: tuple[int, int] = (64, 64)) -> bytes:
    image = Image.new("L", size)
    image.putdata([(x * 4) % 256 for y in range(size[1]) for x in range(size[0])])
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def test_same_image_same_hash_across_encodings() -> None:
    image = Image.open(io.BytesIO(_gradient()))
    as_png, as_jpeg = io.BytesIO(), io.BytesIO()
    image.save(as_png, format="PNG")
    image.convert("RGB").save(as_jpeg, format="JPEG", quality=90)
    assert hamming(dhash(as_png.getvalue()), dhash(as_jpeg.getvalue())) <= 4


def test_resized_copy_is_near() -> None:
    original = _gradient((128, 128))
    small = io.BytesIO()
    Image.open(io.BytesIO(original)).resize((40, 40)).save(small, format="PNG")
    assert hamming(dhash(original), dhash(small.getvalue())) <= 8


def test_different_content_is_far() -> None:
    assert hamming(dhash(_gradient()), dhash(_png((250, 10, 10)))) > 16


def test_fits_signed_bigint() -> None:
    value = dhash(_gradient())
    assert -(2**63) <= value < 2**63


def test_garbage_raises_undecodable() -> None:
    with pytest.raises(UndecodableImage):
        dhash(b"not an image")


def test_hamming_is_symmetric_and_zero_on_self() -> None:
    a, b = dhash(_gradient()), dhash(_png((0, 0, 0)))
    assert hamming(a, a) == 0
    assert hamming(a, b) == hamming(b, a)


def test_decompression_bomb_raises_undecodable_not_the_pillow_exception() -> None:
    """CRITICAL 1(a): ``PIL.Image.DecompressionBombError`` is NOT a subclass
    of ``UnidentifiedImageError`` or ``OSError`` -- before this fix it fell
    through ``dhash``'s except tuple entirely and crashed the confirm worker
    that called it. A small, highly-compressible solid-colour PNG decompresses
    into an enormous pixel buffer, so it is a tiny file on disk that still
    trips Pillow's guard; ``Image.MAX_IMAGE_PIXELS`` is lowered for the
    duration of this test so the fixture stays fast rather than needing a
    genuinely huge image.
    """
    original_limit = Image.MAX_IMAGE_PIXELS
    Image.MAX_IMAGE_PIXELS = 10_000
    try:
        buffer = io.BytesIO()
        Image.new("RGB", (2_000, 2_000), color=(10, 20, 30)).save(buffer, format="PNG")
        with pytest.raises(UndecodableImage):
            dhash(buffer.getvalue())
    finally:
        Image.MAX_IMAGE_PIXELS = original_limit


def test_bit_population_of_zero_and_all_ones() -> None:
    assert bit_population(0) == 0
    assert bit_population(-1) == 64


def test_bit_population_matches_hamming_against_zero() -> None:
    h = dhash(_gradient())
    assert bit_population(h) == hamming(h, 0)
