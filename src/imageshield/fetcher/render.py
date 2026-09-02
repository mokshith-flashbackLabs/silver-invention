"""The subject preview render — the whole hit frame, blurred, with only the
face sharpened on an explicit tap.

Spec: docs/superpowers/specs/2026-09-02-whole-frame-blur-design.md.

THE PROPERTY THIS MODULE EXISTS TO HOLD (spec §0.4): no argument, and no
combination of arguments, returns a fully sharp frame. ``reveal=True``
sharpens the face box and nothing else, so the explicit content of a hit is
never rendered sharp by this system.

Pillow's FOURTH call site, named in CLAUDE.md §2. It lives here rather than in
app.py because app.py is a route module and this is pixel algebra — the same
separation attribution/crop.py already keeps.

EXIF ORIENTATION IS DELIBERATELY NOT APPLIED, exactly as in
attribution/crop.py: Rekognition reports bounding boxes against the image as
stored, so rotating would move the face out from under its own box. Here that
would also misplace the sharp composite, which is worse than a rotated crop --
it would sharpen the wrong region of somebody's abuse image.
"""

from __future__ import annotations

import io

from PIL import Image, ImageFilter, UnidentifiedImageError
from PIL.Image import DecompressionBombError

from imageshield.attribution.crop import UndecodableImage
from imageshield.attribution.models import BoundingBox

# The frame is capped before blurring, not after: the radius is a fraction of
# the long edge, so capping afterwards would make the blur depend on the
# source's resolution. Spec §3 -- also what keeps the proxy's BUFFERED relay
# honest, since it was reasoned on "a crop is tens of kilobytes".
_LONG_EDGE_MAX = 1024

# Radius as a fraction of the long edge, floored. A CONSTANT radius is the
# hazard spec §2 names: 12px tuned for a face crop is nearly transparent over
# a 3000px frame, which would pair a wider frame with a weaker blur. At the
# 1024 cap this lands at ~12, so a capped frame blurs about as hard as the old
# crop did.
_BLUR_FRACTION = 0.012
_BLUR_RADIUS_MIN = 6

_JPEG_QUALITY = 80


def render_preview(image: bytes, bbox: BoundingBox, *, reveal: bool) -> bytes:
    """JPEG bytes of the whole frame, blurred.

    ``reveal=False`` blurs everything, face included. ``reveal=True`` sharpens
    the face box only -- it does NOT return the image unblurred.
    """
    try:
        with Image.open(io.BytesIO(image)) as opened:
            source = _downscale(opened.convert("RGB"))
            rendered = _blur(source)
            buffer = io.BytesIO()
            rendered.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError, DecompressionBombError) as exc:
        # DecompressionBombError subclasses neither of the others -- Pillow's
        # guard against a small file that decodes into an enormous buffer.
        # Same handling as attribution/crop.py, for the same reason.
        raise UndecodableImage(str(exc)) from exc


def _downscale(source: Image.Image) -> Image.Image:
    """Cap the long edge. Never upscales -- a small frame stays its own size."""
    width, height = source.size
    longest = max(width, height)
    if longest <= _LONG_EDGE_MAX:
        return source
    scale = _LONG_EDGE_MAX / longest
    # Image.Resampling.LANCZOS, not the deprecated Image.LANCZOS alias --
    # matching confirm/phash.py:31, and the only form Pillow's stubs accept.
    return source.resize(
        (round(width * scale), round(height * scale)), Image.Resampling.LANCZOS
    )


def _blur(source: Image.Image) -> Image.Image:
    radius = max(_BLUR_RADIUS_MIN, round(max(source.size) * _BLUR_FRACTION))
    return source.filter(ImageFilter.GaussianBlur(radius))
