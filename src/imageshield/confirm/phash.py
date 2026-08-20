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
from PIL.Image import DecompressionBombError

from imageshield.attribution.crop import UndecodableImage

_HASH_SIZE = 8
# Load-bearing: int.bit_count() counts bits of abs(x), not two's-complement representation,
# so without this mask hamming(-1, 0) would return 1 instead of 64.
_MASK = (1 << 64) - 1


def dhash(image: bytes) -> int:
    """Hash of horizontal gradient signs over a 9x8 grayscale downscale."""
    try:
        with Image.open(io.BytesIO(image)) as opened:
            gray = opened.convert("L").resize(
                (_HASH_SIZE + 1, _HASH_SIZE), Image.Resampling.LANCZOS
            )
            pixels = list(gray.getdata())
    except (UnidentifiedImageError, OSError, DecompressionBombError) as exc:
        # DecompressionBombError is NOT a subclass of UnidentifiedImageError or
        # OSError -- it is Pillow's own guard against a small file that
        # decompresses into an enormous pixel buffer (a zip-bomb-shaped PNG),
        # and it must land in the same "we could not hash this" outcome as any
        # other undecodable image, not crash the worker that called us.
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


def bit_population(h: int) -> int:
    """Count of set bits in the masked (unsigned two's-complement) 64-bit
    representation of ``h``. Used to detect a degenerate hash — near-0 or
    near-all-1s bits, which low-texture images (a solid background, a plain
    banner) produce regardless of what the image actually shows. See
    ``confirm/worker.py``'s dedup guard: two degenerate hashes collide by
    construction, not because the images resemble each other."""
    return (h & _MASK).bit_count()
