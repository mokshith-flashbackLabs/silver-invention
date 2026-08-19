"""64-bit difference hash (dHash) for cross-URL duplicate detection.

A hash about the image, never the image: INVARIANTS #9 stands. Stored as a
signed Postgres BIGINT, so the unsigned 64-bit value is two's-complemented
here and masked back in :func:`hamming`.

Pillow is a dependency for exactly two jobs in this repo — attribution's face
crop and this hash (plus the fetcher's crop/blur, which reuses crop.py).
"""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

from imageshield.attribution.crop import UndecodableImage

_HASH_SIZE = 8
_MASK = (1 << 64) - 1


def dhash(image: bytes) -> int:
    """Hash of horizontal gradient signs over an 9x8 grayscale downscale."""
    try:
        with Image.open(io.BytesIO(image)) as opened:
            gray = opened.convert("L").resize(
                (_HASH_SIZE + 1, _HASH_SIZE), Image.Resampling.LANCZOS
            )
            pixels = list(gray.getdata())
    except (UnidentifiedImageError, OSError) as exc:
        raise UndecodableImage(str(exc)) from exc

    bits = 0
    for row in range(_HASH_SIZE):
        for col in range(_HASH_SIZE):
            left = pixels[row * (_HASH_SIZE + 1) + col]
            right = pixels[row * (_HASH_SIZE + 1) + col + 1]
            bits = (bits << 1) | (1 if right > left else 0)
    return bits - (1 << 64) if bits >= (1 << 63) else bits


def hamming(a: int, b: int) -> int:
    return ((a ^ b) & _MASK).bit_count()
