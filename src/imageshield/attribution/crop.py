"""Crop a photo to one face's bounding box.

The only place Pillow is used, and the only reason it is a dependency.

``SearchFacesByImage`` "first detects the largest face in the image", so
searching for a *specific* face means handing Rekognition an image in which
that face is the only one — or at least the biggest. There is no stdlib JPEG
codec, and no Rekognition API that takes a region.

Two decisions worth stating:

**The margin.** A crop tight to the detected box loses the jaw, hairline and
ear context that face embeddings lean on, and recognition degrades. Rekognition
matches best on a face with some surrounding head. 25% of each dimension is a
middle setting: enough context to embed well, tight enough that a second face
standing beside the subject does not become the largest one in the crop, which
would silently attribute the wrong person.

**Clamping.** Rekognition bounding boxes can extend slightly beyond the image —
AWS documents this for faces at the frame edge, where the box is projected past
the boundary. Left unclamped, the margin arithmetic then produces negative
coordinates and Pillow silently pads with black, moving the face off-centre in
its own crop.
"""

from __future__ import annotations

import io

from PIL import Image, UnidentifiedImageError

from imageshield.attribution.models import BoundingBox

# See the module docstring. Not config: it is a property of how face embeddings
# behave, not an operational knob, and a per-environment value would make two
# deployments disagree about who is in a photo.
_MARGIN_FRACTION = 0.25

# Rekognition needs a face of at least ~40x40px to search reliably. Below that
# the crop is refused rather than sent: a search on a 12px face returns noise,
# and noise that clears the threshold attributes the wrong person.
_MIN_CROP_PIXELS = 40

# JPEG, because that is what the collection was built from and re-encoding to
# something else adds a difference between enrolment and query for no gain.
_JPEG_QUALITY = 95


class CropTooSmall(ValueError):
    """The face occupies too few pixels to search. The caller treats this as
    "no matches" — an unattributed face, which is a first-class outcome."""


class UndecodableImage(ValueError):
    """The bytes are not an image we can open."""


def crop_to_face(image: bytes, bbox: BoundingBox) -> bytes:
    """Return JPEG bytes of ``bbox`` plus a margin, clamped to the image."""
    try:
        with Image.open(io.BytesIO(image)) as opened:
            # EXIF orientation is deliberately NOT applied. Rekognition reports
            # bounding boxes against the image as stored, so rotating here would
            # move the face out from under its own box.
            source = opened.convert("RGB")
            width, height = source.size
            box = _pixel_box(bbox, width, height)
            if box is None:
                raise CropTooSmall(
                    f"face occupies fewer than {_MIN_CROP_PIXELS}px in a"
                    f" {width}x{height} image"
                )
            cropped = source.crop(box)
            buffer = io.BytesIO()
            cropped.save(buffer, format="JPEG", quality=_JPEG_QUALITY)
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise UndecodableImage(str(exc)) from exc


def _pixel_box(
    bbox: BoundingBox, width: int, height: int
) -> tuple[int, int, int, int] | None:
    """Normalised box + margin -> pixel box, clamped. ``None`` if too small."""
    margin_x = bbox.w * _MARGIN_FRACTION
    margin_y = bbox.h * _MARGIN_FRACTION

    left = _clamp(bbox.x - margin_x) * width
    top = _clamp(bbox.y - margin_y) * height
    right = _clamp(bbox.x + bbox.w + margin_x) * width
    bottom = _clamp(bbox.y + bbox.h + margin_y) * height

    box = (int(left), int(top), int(right), int(bottom))
    if box[2] - box[0] < _MIN_CROP_PIXELS or box[3] - box[1] < _MIN_CROP_PIXELS:
        return None
    return box


def _clamp(value: float) -> float:
    return min(1.0, max(0.0, value))
