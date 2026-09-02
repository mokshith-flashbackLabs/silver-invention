# Whole-Frame Blur Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** The subject's hit preview becomes the whole image blurred end to end, with only the face region sharpened on an explicit per-item tap — and no code path that returns a fully sharp frame.

**Architecture:** A new pure module `src/imageshield/fetcher/render.py` does all the pixel work over bytes: downscale, blur the whole frame at a radius proportional to its long edge, and — only when revealing — composite the sharp face region back over the blurred base. `fetcher/app.py`'s `/v1/crop` route calls it and stops calling `crop_to_face` entirely, so the search-seed and Rekognition-candidate crop paths are byte-identical afterwards.

**Tech Stack:** Python 3.11+, FastAPI, Pillow, pytest, structlog. No new dependencies.

**Spec:** `docs/superpowers/specs/2026-09-02-whole-frame-blur-design.md` (read §0 before starting — the `likely_not_subject` exposure was raised and reaffirmed by the owner, and §0.4 is the safety property every task must preserve)

## Global Constraints

- **No code path may return a fully sharp frame.** Spec §0.4. This is the one property to protect over any other consideration in this plan.
- **`src/imageshield/attribution/crop.py` IS NOT MODIFIED.** `crop_to_face` feeds the Hive/Google search seeds (`attribution/crop_upload.py:105`) and the `SearchFacesByImage` candidate crop (`attribution/rekognition.py:98`). Spec §5.
- **EXIF orientation is never applied.** Rekognition reports bboxes against the image *as stored*; rotating moves the face out from under its own box. `crop.py:36-38` documents this — the new module must do the same, and it matters more here because we composite by bbox.
- **No inline tunables.** Radius fraction, floor, long-edge cap, sharp margin and JPEG quality are all named module constants (INVARIANTS #1b's reasoning). Do not reuse or import `_MARGIN_FRACTION` from `attribution/crop.py` — spec §1a.
- **Pillow's fourth call site is `fetcher/render.py`**, named in `CLAUDE.md` §2 by Task 4. No fifth.
- **`ruff format` new files only** — do not reformat files this plan does not create.
- **Run pytest without an extra `-q`.**
- Nothing is written to disk, cache or S3 at any point (INVARIANTS #9, #10).

---

### Task 1: The blurred whole frame

**Files:**
- Create: `src/imageshield/fetcher/render.py`
- Test: `tests/test_fetcher_render.py`

**Interfaces:**
- Consumes: `BoundingBox` from `imageshield.attribution.models`; `UndecodableImage` from `imageshield.attribution.crop` (the exception type only — reused so `app.py`'s existing handler needs no change)
- Produces: `render_preview(image: bytes, bbox: BoundingBox, *, reveal: bool) -> bytes`, and the constants `_LONG_EDGE_MAX`, `_BLUR_FRACTION`, `_BLUR_RADIUS_MIN`. Task 2 extends the same function for `reveal=True`; Task 3 calls it from the route.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_fetcher_render.py`. The two helpers matter as much as the assertions — spec §8 records why: the existing `_png()` fixture is a **flat colour**, and blurring a flat image returns an identical image, so a sharp-vs-blurred assertion against it passes vacuously.

```python
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
    base.putdata(
        [255 if (x + y) % 2 == 0 else 0 for y in range(cells[1]) for x in range(cells[0])]
    )
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
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_fetcher_render.py -v`
Expected: collection error — `ModuleNotFoundError: No module named 'imageshield.fetcher.render'`.

- [ ] **Step 3: Write the minimal implementation**

Create `src/imageshield/fetcher/render.py`:

```python
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
    return source.resize((round(width * scale), round(height * scale)), Image.LANCZOS)


def _blur(source: Image.Image) -> Image.Image:
    radius = max(_BLUR_RADIUS_MIN, round(max(source.size) * _BLUR_FRACTION))
    return source.filter(ImageFilter.GaussianBlur(radius))
```

Note `bbox` and `reveal` are accepted but unused at this task's end — Task 2 is what consumes them. That is deliberate: the blurred base is independently testable and independently reviewable.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_fetcher_render.py -v`
Expected: PASS, 8 tests.

If `test_a_small_frame_is_still_meaningfully_blurred` fails, `_BLUR_RADIUS_MIN` is too low for the
fixture's 8px cells — raise the floor rather than coarsening the fixture, because the fixture is
standing in for real image detail.

- [ ] **Step 5: Format and typecheck**

Run: `ruff format src/imageshield/fetcher/render.py tests/test_fetcher_render.py`
Run: `ruff check src/imageshield/fetcher/render.py tests/test_fetcher_render.py`
Run: `mypy src/imageshield/fetcher/render.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/imageshield/fetcher/render.py tests/test_fetcher_render.py
git commit -m "feat(fetcher): the blurred whole frame, radius scaled to its long edge

A constant radius tuned for a face crop is nearly transparent over a full
frame, so a wider frame with the old constant would have shipped a WEAKER
blur than the crop it replaces. The radius is now a fraction of the long
edge with a floor, and the frame is capped at 1024 first so the blur does
not depend on source resolution.

The test fixture is a fine checkerboard, not the flat colour the existing
fetcher tests use: blurring a flat image returns an identical image, so
every sharp-vs-blurred assertion against it would pass vacuously.

bbox and reveal are accepted and unused until the next commit.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 2: The sharp face composite

**Files:**
- Modify: `src/imageshield/fetcher/render.py`
- Test: `tests/test_fetcher_render.py`

**Interfaces:**
- Consumes: `render_preview`, `_downscale`, `_blur`, `_SHARP_MARGIN_FRACTION`, `_MIN_SHARP_PIXELS` from Task 1
- Produces: `render_preview(..., reveal=True)` returning a frame sharp inside the face box and blurred everywhere else. No new public names.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_fetcher_render.py`:

```python
def _face_box(image: Image.Image) -> tuple[int, int, int, int]:
    width, height = image.size
    return (int(0.4 * width), int(0.4 * height), int(0.6 * width), int(0.6 * height))


def test_reveal_sharpens_the_face() -> None:
    rendered = _open(render_preview(_texture(), BBOX, reveal=True))

    assert _variance(rendered, _face_box(rendered)) > _variance(rendered) * 2


def test_reveal_leaves_everything_outside_the_face_blurred() -> None:
    """The point of the whole change: the surround must be as blurred as it is
    in the default render, not merely 'less sharp than the face'."""
    default = _open(render_preview(_texture(), BBOX, reveal=False))
    revealed = _open(render_preview(_texture(), BBOX, reveal=True))
    corner = (0, 0, int(0.2 * revealed.size[0]), int(0.2 * revealed.size[1]))

    assert _variance(revealed, corner) == pytest.approx(
        _variance(default, corner), rel=0.05
    )


def test_no_reveal_returns_a_fully_sharp_frame() -> None:
    """§0.4, the load-bearing safety property. The revealed frame must stay
    far from the source's detail overall -- a sharp face is a small patch, so
    whole-frame variance stays near the blurred render, nowhere near source."""
    source = _texture()
    revealed = render_preview(source, BBOX, reveal=True)

    assert _variance(_open(revealed)) < _variance(_open(source)) / 3


def test_reveal_sharpens_a_tighter_region_than_a_search_crop_would() -> None:
    """Spec §1a: the sharp margin must not be attribution/crop.py's 0.25. If
    someone 'simplifies' by importing _MARGIN_FRACTION, this fails."""
    from imageshield.fetcher.render import _SHARP_MARGIN_FRACTION

    assert _SHARP_MARGIN_FRACTION < 0.25


def test_a_degenerate_bbox_reveals_nothing_rather_than_erroring() -> None:
    """Spec §1a: a face too small for a useful sharp patch still gets a
    perfectly good blurred frame. Refusing the render would withhold the
    default view for no safety gain."""
    tiny = BoundingBox(x=0.5, y=0.5, w=0.001, h=0.001)
    revealed = render_preview(_texture(), tiny, reveal=True)
    default = render_preview(_texture(), tiny, reveal=False)

    assert revealed == default


def test_a_bbox_at_the_edge_is_clamped_not_wrapped() -> None:
    edge = BoundingBox(x=0.92, y=0.92, w=0.08, h=0.08)
    rendered = _open(render_preview(_texture(), edge, reveal=True))
    far_corner = (0, 0, int(0.2 * rendered.size[0]), int(0.2 * rendered.size[1]))
    default = _open(render_preview(_texture(), edge, reveal=False))

    # The opposite corner must be untouched by a box on the far edge.
    assert _variance(rendered, far_corner) == pytest.approx(
        _variance(default, far_corner), rel=0.05
    )
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_fetcher_render.py -v`
Expected: `test_reveal_sharpens_the_face` FAILS (reveal is ignored, so the face is as blurred as everything else); `test_a_degenerate_bbox_reveals_nothing_rather_than_erroring` PASSES already (reveal is a no-op) — that is fine, it is a guard for Task 2's implementation, not a driver.

- [ ] **Step 3: Write the minimal implementation**

In `render.py`, replace the body of `render_preview`'s `with` block and add two helpers:

```python
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
        raise UndecodableImage(str(exc)) from exc


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


def _sharp_box(
    bbox: BoundingBox, width: int, height: int
) -> tuple[int, int, int, int] | None:
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
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_fetcher_render.py -v`
Expected: PASS, 14 tests.

- [ ] **Step 5: Format, lint, typecheck**

Run: `ruff format src/imageshield/fetcher/render.py tests/test_fetcher_render.py`
Run: `ruff check src/imageshield/fetcher/render.py tests/test_fetcher_render.py`
Run: `mypy src/imageshield/fetcher/render.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/imageshield/fetcher/render.py tests/test_fetcher_render.py
git commit -m "feat(fetcher): reveal sharpens the face box and nothing else

Both layers come from the same downscaled source so they align exactly. The
sharp margin is 0.08, its own constant -- attribution/crop.py's 0.25 serves a
search seed, which wants context, while every pixel sharpened here is a pixel
of somebody's hit image shown sharp. A test fails if the two are ever shared.

A bbox too small for a useful patch returns the blurred frame rather than
erroring: refusing the render would withhold the default view for no gain.

Asserted: no argument returns a fully sharp frame (spec §0.4).

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 3: Wire the route, and retire its `crop_to_face` call

**Files:**
- Modify: `src/imageshield/fetcher/app.py:31` (imports), `:34` (imports), `:42-47` (the stale comment), `:208-228` (the route body)
- Modify: `tests/test_fetcher.py:94-117`

**Interfaces:**
- Consumes: `render_preview` from Task 2
- Produces: no new names. `POST /v1/crop`'s response contract changes from "a blurred face crop" to "a blurred whole frame, face sharp when `blur=false`".

- [ ] **Step 1: Update the two existing tests, and add the route-level ones**

`tests/test_fetcher.py:101` asserts `blurred.size[0] < 64  # cropped, not the whole frame` — a correct test of the contract this task reverses (spec §8). Replace lines 94-117 with:

```python
def _textured_png(size: tuple[int, int] = (256, 256)) -> bytes:
    """See tests/test_fetcher_render.py::_texture — a flat fill cannot show a
    blur, so the crop-route tests need texture too."""
    cells = (max(2, size[0] // 8), max(2, size[1] // 8))
    base = Image.new("L", cells)
    base.putdata(
        [255 if (x + y) % 2 == 0 else 0 for y in range(cells[1]) for x in range(cells[0])]
    )
    buffer = io.BytesIO()
    base.resize(size, Image.NEAREST).convert("RGB").save(buffer, format="PNG")
    return buffer.getvalue()


def _textured_handler(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200, content=_textured_png(), headers={"content-type": "image/png"}
    )


CROP_BODY = {
    "url": "https://x.example/a.png",
    "bbox": {"x": 0.4, "y": 0.4, "w": 0.2, "h": 0.2},
}


def test_crop_returns_the_whole_frame_blurred_by_default() -> None:
    """Changed 2026-09-02: this asserted `size[0] < 64  # cropped, not the
    whole frame`. The whole frame IS the contract now -- the subject needs to
    recognise the photo to answer honestly, and the blur is what makes showing
    it safe (spec §0.1)."""
    client = _client(_textured_handler)
    response = client.post("/v1/crop", json=CROP_BODY, headers=AUTH)

    assert response.status_code == 200
    rendered = Image.open(io.BytesIO(response.content))
    assert rendered.size == (256, 256)  # the whole frame, not a crop


def test_crop_with_blur_false_sharpens_only_the_face() -> None:
    """``blur=false`` is 'sharpen the face', NOT 'return it unblurred' -- the
    two responses must differ (a reveal button that changes nothing is a lie),
    and the revealed one must still be mostly blurred (spec §0.4)."""
    client = _client(_textured_handler)
    blurred = client.post("/v1/crop", json=CROP_BODY, headers=AUTH)
    revealed = client.post("/v1/crop", json={**CROP_BODY, "blur": False}, headers=AUTH)

    assert blurred.status_code == revealed.status_code == 200
    assert blurred.headers["content-type"] == revealed.headers["content-type"] == "image/jpeg"
    assert blurred.content != revealed.content

    source_detail = _grey_variance(_textured_png())
    assert _grey_variance(revealed.content) < source_detail / 3


def _grey_variance(data: bytes) -> float:
    pixels = list(Image.open(io.BytesIO(data)).convert("L").getdata())
    mean = sum(pixels) / len(pixels)
    return sum((value - mean) ** 2 for value in pixels) / len(pixels)


def test_crop_no_longer_refuses_a_tiny_face() -> None:
    """crop_too_small was raised by crop_to_face, which this route no longer
    calls. A tiny face now yields a blurred frame (spec §1a)."""
    client = _client(_textured_handler)
    body = {**CROP_BODY, "bbox": {"x": 0.5, "y": 0.5, "w": 0.001, "h": 0.001}}
    response = client.post("/v1/crop", json=body, headers=AUTH)

    assert response.status_code == 200


def test_crop_still_refuses_undecodable_bytes() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"nope", headers={"content-type": "image/png"})

    client = _client(handler)
    response = client.post("/v1/crop", json=CROP_BODY, headers=AUTH)

    assert response.status_code == 400
    assert response.json()["code"] == "not_an_image"
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `pytest tests/test_fetcher.py -v`
Expected: `test_crop_returns_the_whole_frame_blurred_by_default` FAILS (`size == (60, 60)` or similar — the route still crops); `test_crop_no_longer_refuses_a_tiny_face` FAILS with 400 `crop_too_small`.

- [ ] **Step 3: Rewrite the route**

In `src/imageshield/fetcher/app.py`:

Replace the import on line 31 (`Image`/`ImageFilter` are no longer used here — the pixel work moved):

```python
from imageshield.attribution.crop import UndecodableImage
from imageshield.attribution.models import BoundingBox
from imageshield.fetcher.render import render_preview
```

Delete the `from PIL import Image, ImageFilter` line, the `import io` line **only if nothing else in the file uses it** (check first — `/v1/fetch` may), the `CropTooSmall` import, and the `_BLUR_RADIUS` / `_BLUR_JPEG_QUALITY` constants with their comment block at lines 42-47 (both moved into `render.py` as scaled values).

Replace the route body from `bbox = BoundingBox(...)` to the end with:

```python
    bbox = BoundingBox(x=body.bbox.x, y=body.bbox.y, w=body.bbox.w, h=body.bbox.h)
    try:
        # `blur=false` means SHARPEN THE FACE, not "return it unblurred" --
        # see render.render_preview. No value of this flag returns a fully
        # sharp frame.
        rendered = render_preview(fetched.body, bbox, reveal=not body.blur)
    except UndecodableImage as exc:
        # The upstream content-type claimed image/*; the bytes disagreed.
        # Same user-facing meaning as the not_an_image refusal above.
        raise FetcherError(400, "not_an_image", str(exc)) from exc

    return Response(content=rendered, media_type="image/jpeg")
```

Also update the `CropRequest.blur` field docstring/comment so it says what the flag now means, and fix the stale module comment at lines 42-47 which describes a face crop.

- [ ] **Step 4: Run the tests to verify they pass**

Run: `pytest tests/test_fetcher.py tests/test_fetcher_render.py -v`
Expected: PASS.

- [ ] **Step 5: Prove the seed and attribution paths are untouched**

Run: `pytest tests/test_attribution_crop_seeds.py tests/test_attribution_rekognition.py tests/test_confirm_worker.py -v`
Expected: PASS, unchanged. `crop_to_face` was not modified, and this is the check that says so.

Run: `grep -rn "crop_to_face" src/imageshield/`
Expected: exactly three lines — the definition in `attribution/crop.py`, and the calls in `attribution/crop_upload.py` and `attribution/rekognition.py`. No fetcher line.

- [ ] **Step 6: Full suite, format, typecheck**

Run: `pytest tests/ -x`
Run: `ruff check src/imageshield/fetcher/`
Run: `mypy src/imageshield/fetcher/`
Expected: clean. Note the baseline: any failure unrelated to the fetcher should be checked against `git stash` before being attributed to this change.

- [ ] **Step 7: Commit**

```bash
git add src/imageshield/fetcher/app.py tests/test_fetcher.py
git commit -m "feat(fetcher): /v1/crop serves the whole frame, not a face crop

The route stops calling crop_to_face entirely. That function still feeds the
Hive/Google search seeds and the SearchFacesByImage candidate crop, and both
are byte-identical after this -- asserted by the attribution suites plus a
grep that leaves crop_to_face with exactly two callers, both in attribution/.

crop_too_small stops applying here: it came from crop_to_face, and a face too
small for a useful sharp patch still deserves the blurred frame. Undecodable
bytes still 400 as not_an_image.

test_fetcher.py:101 asserted 'cropped, not the whole frame' -- a correct test
of the contract this reverses, updated rather than deleted, and its flat-fill
fixture replaced with texture since a blur is invisible on a flat colour.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

### Task 4: Amend the rules this changes

**Files:**
- Modify: `CLAUDE.md` §1 (the "no full images" bullet), `CLAUDE.md` §2 (the Pillow call-site sentence)
- Modify: `INVARIANTS.md` #23 (reveal semantics), #10 (restate, unchanged)
- Modify: `ARCHITECTURE.md` §3.7 (the fetcher's crop route)
- Modify (other repo, separate commit): `../image_backend/docs/CLAUDE.md` §4 rule 4 and P17

**Interfaces:**
- Consumes: the behaviour shipped by Task 3
- Produces: nothing in code.

- [ ] **Step 1: Amend `CLAUDE.md` §1**

Find the bullet reading *"We do **not** show full images. Face crops, rendered live, blurred by default, revealed on tap."* Replace with:

```markdown
- We show a hit's full frame to **the subject only**, blurred end to end, and a
  per-item tap sharpens **only their face** — never the frame. There is no code
  path, and no request parameter, that returns a fully sharp image: the explicit
  content of a hit is never rendered sharp by this system. Staff never see hit
  imagery at all. *(Amended 2026-09-02, spec
  `docs/superpowers/specs/2026-09-02-whole-frame-blur-design.md` — previously
  "we do not show full images. Face crops, rendered live, blurred by default,
  revealed on tap". The frame widened because a face box strips the context a
  person needs to answer "is this you?" honestly; the blur, the tap and the
  subject-only access are what make showing it safe, and §0.2 of that spec
  records the `likely_not_subject` exposure the owner accepted.)*
```

- [ ] **Step 2: Amend `CLAUDE.md` §2 — the Pillow sentence**

Find *"**Pillow has three call sites, and no more.**"* and its list. Change "three" to "four" and add the new site to the enumeration:

```markdown
`fetcher/render.py` (the subject preview: downscale, whole-frame Gaussian, and
the sharp-face composite — 2026-09-02). All four operate on bytes already in
memory and never write a file.
```

Note in passing that this rule is a convention, not a test — `tests/test_boundaries.py` enforces the face-search and S3 greps only.

- [ ] **Step 3: Amend `INVARIANTS.md` #23**

State: the reveal tap sharpens the face region of an already-blurred frame; it does not un-blur the image. Add the assertion that no code path returns a fully sharp frame, and point at `tests/test_fetcher_render.py::test_no_reveal_returns_a_fully_sharp_frame` as the check. Leave #10 as it stands but note the render is still in-memory and still never cached — this change does not touch it.

- [ ] **Step 4: Amend `ARCHITECTURE.md` §3.7**

Update the crop-route description from "a face crop, blurred by default" to the whole-frame contract, and mention the 1024px cap so the payload size is documented where the deployable is.

- [ ] **Step 5: Commit the services-side docs**

```bash
git add CLAUDE.md INVARIANTS.md ARCHITECTURE.md
git commit -m "docs: the subject sees the whole frame, blurred, face sharp on tap

Amends CLAUDE.md §1 (which said we do not show full images), §2 (Pillow gains
a fourth call site, fetcher/render.py), INVARIANTS #23 (reveal sharpens the
face, never the frame) and ARCHITECTURE §3.7.

The rule changed because a face box strips the context a person needs to answer
'is this you?' honestly. What makes showing it safe is unchanged and now
structural: blurred by default, subject-only, per-render audited, ceilinged,
and no code path returns a fully sharp frame.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

- [ ] **Step 6: Amend the backend's docs — SEPARATE REPO, SEPARATE COMMIT**

In `../image_backend/docs/CLAUDE.md`, §4 rule 4 and P17 both justify the byte relay on the payload being a *crop* ("the subject's blurred face crop"). The relay argument survives intact in substance — `api` still decodes nothing and persists nothing — but the wording must stop calling it a face crop, and P17's *Check* line should still hold. Do not restate the safety argument; only correct what the payload is.

Commit in `image_backend`, never from the workspace root:

```bash
cd ../image_backend
git add docs/CLAUDE.md
git commit -m "docs(p17): the relayed preview is a blurred frame, not a face crop

Counterpart to image_flashbacklabs' whole-frame blur change. Nothing about the
relay's reasoning moves: api still decodes nothing, persists nothing, and the
buffer still lives for one response. Only the description of the payload was
wrong once services began sending the whole frame.

Co-Authored-By: 5mokshith <mokshithrao1481@gmail.com>"
```

---

## Spec §4 needs no task, deliberately

Spec §4 (a hit with no bbox stays a `404 preview_unavailable`) is **already the behaviour** and no
task changes it. `src/imageshield/http/routes/infringements.py:113-124` raises
`404 preview_unavailable` when `target.image_url is None or target.bbox is None`, before the fetcher
is called at all — so a bbox-less hit never reaches `render_preview`. The backend's
`preview_expected` rule in `src/reports/card.ts` is unchanged for the same reason (spec §6).

Do not "implement" §4. If you find yourself adding a bbox-less path to the fetcher, stop — the
refusal belongs in the route that already has it, and duplicating it there would give the render a
second opinion about who gets an image.

## Verification before calling this done

- [ ] `pytest tests/ -x` green in `image_flashbacklabs`, with any pre-existing failures confirmed against `git stash`
- [ ] `grep -rn "crop_to_face" src/imageshield/` returns exactly three lines, none in `fetcher/`
- [ ] `grep -rn "from PIL\|import PIL" src/imageshield/` returns exactly four files
- [ ] `mypy src/imageshield/fetcher/` clean
- [ ] No file outside `fetcher/`, the two test files, and the four docs was modified

## Deploy and the verification gap

Fetcher is the only deployable that changes; `confirm` imports the crop client but its behaviour is unaffected. No migration. Per `docs/deploy/DEPLOY-RUNBOOK.md`: build/push `imageshield/services:<sha>` (arm64, ~12 min, capture `buildx exit=$?` and never pipe to `tail`), `sed` the tag into `infra/ecs/*.json`, register the task defs, then `update-service --force-new-deployment` with `wait services-stable` between each.

**A visual check on dev is blocked by an unrelated live problem.** The preview render ceiling is exhausted for the test account — 200/200 in a rolling 24h, `preview_daily_render_ceiling` — so every preview 429s regardless of this change. Either wait for the window to roll off or raise the ceiling on dev first. Decide before implementing, so the work does not finish in a state that cannot be seen.
