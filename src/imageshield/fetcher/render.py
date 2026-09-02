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

# The sharp region is bbox plus its OWN margin, deliberately much smaller than
# attribution/crop.py's 0.25: a search seed wants context around the face,
# while every pixel sharpened here is a pixel of the hit image shown sharp.
# Enough that a tight Rekognition box does not clip a jaw or hairline, no more.
# Never import _MARGIN_FRACTION for this -- spec §1a.
_SHARP_MARGIN_FRACTION = 0.08

# Below this the sharp patch is too small to help identify anyone, so reveal
# degrades to the blurred frame rather than erroring -- spec §1a.
_MIN_SHARP_PIXELS = 24

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
            if reveal:
                rendered = _sharpen_face(rendered, source, bbox)
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
    return source.resize((round(width * scale), round(height * scale)), Image.Resampling.LANCZOS)


def _blur(source: Image.Image) -> Image.Image:
    radius = max(_BLUR_RADIUS_MIN, round(max(source.size) * _BLUR_FRACTION))
    return source.filter(ImageFilter.GaussianBlur(radius))


def _sharpen_face(blurred: Image.Image, source: Image.Image, bbox: BoundingBox) -> Image.Image:
    """Paste the sharp face box back over the blurred base.

    Both layers come from the SAME downscaled source, so they align exactly --
    which is why _downscale runs before this and not after.
    """
    box = _sharp_box(bbox, *source.size)
    if box is None:
        # Too small to identify anyone by. The blurred frame is still the
        # honest answer; erroring here would withhold it for nothing.
        return blurred
    composited = blurred.copy()
    composited.paste(source.crop(box), (box[0], box[1]))
    return composited


def _sharp_box(bbox: BoundingBox, width: int, height: int) -> tuple[int, int, int, int] | None:
    """Normalised box + the sharp margin -> pixel box, clamped to the frame.
    ``None`` when the result is too small to be worth sharpening."""
    margin_x = bbox.w * _SHARP_MARGIN_FRACTION
    margin_y = bbox.h * _SHARP_MARGIN_FRACTION

    left = _clamp(bbox.x - margin_x) * width
    top = _clamp(bbox.y - margin_y) * height
    right = _clamp(bbox.x + bbox.w + margin_x) * width
    bottom = _clamp(bbox.y + bbox.h + margin_y) * height

    box = (int(left), int(top), int(right), int(bottom))
    if box[2] - box[0] < _MIN_SHARP_PIXELS or box[3] - box[1] < _MIN_SHARP_PIXELS:
        return None
    return box


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))
