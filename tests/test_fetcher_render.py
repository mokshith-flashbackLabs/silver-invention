from __future__ import annotations

import io

import pytest
from PIL import Image

from imageshield.attribution.models import BoundingBox
from imageshield.fetcher.render import _LONG_EDGE_MAX, render_preview


def _texture(size: tuple[int, int] = (800, 800)) -> bytes:
    """A fine deterministic checkerboard — high local variance, so a blur is
    measurable. Built small and NEAREST-resized so cells land at ~8px: a
    coarse checkerboard would survive the blur and make every assertion here
    pass for the wrong reason."""
    cells = (max(2, size[0] // 8), max(2, size[1] // 8))
    base = Image.new("L", cells)
    base.putdata([255 if (x + y) % 2 == 0 else 0 for y in range(cells[1]) for x in range(cells[0])])
    buffer = io.BytesIO()
    base.resize(size, Image.NEAREST).convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def _variance(image: Image.Image, box: tuple[int, int, int, int] | None = None) -> float:
    """Grey-level variance over the whole image or one box. Our stand-in for
    'is this region sharp' — a blur flattens local contrast, so variance
    falls."""
    region = image.convert("L")
    if box is not None:
        region = region.crop(box)
    pixels = list(region.getdata())
    mean = sum(pixels) / len(pixels)
    return sum((value - mean) ** 2 for value in pixels) / len(pixels)


def _open(data: bytes) -> Image.Image:
    return Image.open(io.BytesIO(data))


BBOX = BoundingBox(x=0.4, y=0.4, w=0.2, h=0.2)


def test_default_render_blurs_the_whole_frame() -> None:
    source = _texture()
    rendered = render_preview(source, BBOX, reveal=False)

    before = _variance(_open(source))
    after = _variance(_open(rendered))
    assert after < before / 4


def test_default_render_has_no_sharp_region() -> None:
    """§0.4 as a test: not merely 'blurred on average' — no region is sharp.
    The face box is where a sharp patch would be if reveal leaked into the
    default."""
    rendered = _open(render_preview(_texture(), BBOX, reveal=False))
    width, height = rendered.size
    face = (
        int(0.4 * width),
        int(0.4 * height),
        int(0.6 * width),
        int(0.6 * height),
    )
    surround = (0, 0, int(0.2 * width), int(0.2 * height))

    assert _variance(rendered, face) < _variance(rendered, surround) * 3


def test_render_returns_jpeg_bytes() -> None:
    assert _open(render_preview(_texture(), BBOX, reveal=False)).format == "JPEG"


def test_long_edge_is_capped() -> None:
    rendered = _open(render_preview(_texture((3000, 1500)), BBOX, reveal=False))
    assert max(rendered.size) == _LONG_EDGE_MAX


def test_a_small_image_is_not_upscaled() -> None:
    rendered = _open(render_preview(_texture((300, 200)), BBOX, reveal=False))
    assert rendered.size == (300, 200)


def test_a_large_frame_is_still_visibly_blurred() -> None:
    """The hazard this whole change turns on (spec §2): a constant radius
    tuned for a face crop is nearly transparent on a full frame."""
    source = _texture((3000, 3000))
    rendered = render_preview(source, BBOX, reveal=False)

    assert _variance(_open(rendered)) < _variance(_open(source)) / 4


def test_a_small_frame_is_still_meaningfully_blurred() -> None:
    """The floor earns its keep here: 300 * 0.012 is 3.6, which would barely
    smudge an 8px checkerboard. _BLUR_RADIUS_MIN is what stops a small frame
    arriving effectively unblurred."""
    source = _texture((300, 300))
    rendered = render_preview(source, BBOX, reveal=False)

    assert _variance(_open(rendered)) < _variance(_open(source)) / 4


def test_undecodable_bytes_raise_undecodable_image() -> None:
    from imageshield.attribution.crop import UndecodableImage

    with pytest.raises(UndecodableImage):
        render_preview(b"not an image", BBOX, reveal=False)
