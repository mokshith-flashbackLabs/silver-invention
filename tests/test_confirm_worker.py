"""handle_message: the 9-step confirm orchestration, everything faked.

Dedup-before-spend and the two run_id guards are the load-bearing behaviours
(CLAUDE.md §7.6, INVARIANTS #37-41) — the cases below check them directly
rather than trusting that wiring the real gate/store/provider together would
happen to exercise them."""

from __future__ import annotations

import io
import json
import random
from uuid import UUID, uuid4

from PIL import Image

from imageshield.attribution.models import (
    AttributionUnavailable,
    BoundingBox,
    DetectedFace,
    FaceMatch,
)
from imageshield.confirm.models import (
    CONFIRM_REQUESTED_EVENT,
    REKOGNITION_CONFIRM_ID,
    ConfirmContext,
)
from imageshield.confirm.moderation import ConfirmUnavailable, ModerationLabel, ModerationSignal
from imageshield.confirm.phash import dhash
from imageshield.confirm.worker import ConfirmDeps, handle_message
from imageshield.types import UserRef
from tests.providers_fakes import FakeControlStore, runtime

USER_REF = UserRef(uuid4())
IMAGE_URL = "https://cdn.example/hit.jpg"


def _tiny_jpeg() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (16, 16), color=(200, 40, 40)).save(buffer, format="JPEG")
    return buffer.getvalue()


IMAGE_BYTES = _tiny_jpeg()
# A solid colour: every dHash gradient bit is 0 (no left/right difference
# anywhere), so IMAGE_BYTES's own hash is the degenerate low case by
# construction -- population 0. Fine for every test that does not care about
# the exact hash value; the dedup-specific tests below need control over
# degeneracy, so they use fixtures picked (and pinned by assertion) for that.


def _textured_jpeg() -> bytes:
    """A genuinely non-degenerate image -- fixed-seed noise, so the dHash has
    a mixed bit population rather than the near-uniform 0 or -1 a solid
    colour or a monotonic gradient produces (both tried and rejected while
    writing this fixture: a plain gradient's dHash is -1, all 64 bits set,
    which is itself the degenerate HIGH case)."""
    rng = random.Random(42)
    image = Image.new("L", (64, 64))
    image.putdata([rng.randint(0, 255) for _ in range(64 * 64)])
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()


def _degenerate_high_jpeg() -> bytes:
    """Every row strictly increasing left-to-right -> every gradient bit is
    1 -> dHash == -1 (all 64 bits set): the degenerate HIGH case, the mirror
    of IMAGE_BYTES's degenerate LOW (0)."""
    image = Image.new("L", (64, 64))
    image.putdata([(x * 4) % 256 for _y in range(64) for x in range(64)])
    buffer = io.BytesIO()
    image.convert("RGB").save(buffer, format="JPEG", quality=90)
    return buffer.getvalue()

_UNSET = object()


def _ctx(
    *,
    infringement_id: UUID | None = None,
    confirm_state: str = "unconfirmed",
    image_url: str | None = IMAGE_URL,
    run_id: object = _UNSET,
) -> ConfirmContext:
    return ConfirmContext(
        infringement_id=infringement_id if infringement_id is not None else uuid4(),
        user_ref=USER_REF,
        confirm_state=confirm_state,
        image_url=image_url,
        page_url="https://example.test/page",
        run_id=uuid4() if run_id is _UNSET else run_id,
    )


class FakeConfirmStore:
    def __init__(
        self,
        ctx: ConfirmContext | None,
        *,
        decided: tuple[tuple[UUID, int], ...] = (),
    ) -> None:
        self._ctx = ctx
        self._decided = decided
        self.unfetchable: list[tuple[UUID, str]] = []
        self.duplicates: list[tuple[UUID, UUID, int]] = []
        self.quarantines: list[tuple[UUID, int | None, list[dict[str, object]], float | None]] = []
        self.triages: list[dict[str, object]] = []
        self.skipped: list[tuple[UUID, str, str]] = []

    async def load_context(self, infringement_id: UUID) -> ConfirmContext | None:
        return self._ctx

    async def decided_phashes(self, user_ref: UserRef) -> tuple[tuple[UUID, int], ...]:
        return self._decided

    async def record_duplicate(
        self, infringement_id: UUID, *, duplicate_of: UUID, phash: int
    ) -> None:
        self.duplicates.append((infringement_id, duplicate_of, phash))

    async def record_quarantine(
        self,
        infringement_id: UUID,
        *,
        phash: int | None,
        moderation_labels: list[dict[str, object]],
        min_age_low: float | None,
    ) -> None:
        self.quarantines.append((infringement_id, phash, moderation_labels, min_age_low))

    async def record_triage(
        self,
        infringement_id: UUID,
        *,
        severity: str,
        phash: int | None,
        face_match_score: float | None,
        moderation_labels: list[dict[str, object]] | None,
        triage: dict[str, object],
    ) -> None:
        self.triages.append(
            {
                "infringement_id": infringement_id,
                "severity": severity,
                "phash": phash,
                "face_match_score": face_match_score,
                "moderation_labels": moderation_labels,
                "triage": triage,
            }
        )

    async def record_unfetchable(self, infringement_id: UUID, *, detail: str) -> None:
        self.unfetchable.append((infringement_id, detail))

    async def record_skipped(self, infringement_id: UUID, *, reason: str, detail: str) -> None:
        self.skipped.append((infringement_id, reason, detail))


class FakeAttributionProvider:
    def __init__(
        self,
        *,
        faces: tuple[DetectedFace, ...] = (),
        matches: tuple[FaceMatch, ...] = (),
        detect_error: Exception | None = None,
    ) -> None:
        self._faces = faces
        self._matches = matches
        self._detect_error = detect_error
        self.calls = 0
        self.detect_bytes: list[bytes] = []

    async def detect_faces(self, image: bytes) -> tuple[DetectedFace, ...]:
        self.calls += 1
        self.detect_bytes.append(image)
        if self._detect_error is not None:
            raise self._detect_error
        return self._faces

    async def search_face(
        self,
        image: bytes,
        face: DetectedFace,
        *,
        collection_id: str,
        match_threshold: float,
        max_candidates: int,
    ) -> tuple[FaceMatch, ...]:
        self.calls += 1
        return self._matches


class FakeModeration:
    def __init__(self, signal: ModerationSignal, *, error: Exception | None = None) -> None:
        self._signal = signal
        self._error = error
        self.calls = 0

    async def assess(self, image: bytes) -> ModerationSignal:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._signal


async def _fetch_ok(url: str) -> bytes | None:
    return IMAGE_BYTES


def _deps(
    *,
    store: FakeConfirmStore,
    control: FakeControlStore | None = None,
    provider: FakeAttributionProvider | None = None,
    moderation: FakeModeration | None = None,
    fetch: object = _fetch_ok,
) -> ConfirmDeps:
    default_control = FakeControlStore({REKOGNITION_CONFIRM_ID: runtime(REKOGNITION_CONFIRM_ID)})
    default_moderation = FakeModeration(ModerationSignal(labels=(), min_age_low=None))
    return ConfirmDeps(
        store=store,
        control=control if control is not None else default_control,
        provider=provider if provider is not None else FakeAttributionProvider(),
        moderation=moderation if moderation is not None else default_moderation,
        fetch=fetch,  # type: ignore[arg-type]
        face_match_threshold=92.0,
        max_faces=3,
        phash_hamming_max=8,
        csam_age_low_threshold=18,
        identity_collection="identity-v1",
        attribution_max_candidates=5,
    )


def _body(infringement_id: UUID, event: str = CONFIRM_REQUESTED_EVENT) -> str:
    return json.dumps({"event": event, "id": str(infringement_id)})


EXPLICIT_LABEL = ModerationLabel(
    name="Explicit Nudity", parent_name="Explicit Nudity", confidence=95.0
)


def _face() -> DetectedFace:
    return DetectedFace(
        face_index=0,
        bbox=BoundingBox(x=0.1, y=0.1, w=0.5, h=0.5),
        detect_confidence=99.0,
    )


def _matching_face_match() -> FaceMatch:
    return FaceMatch(external_image_id=str(USER_REF), similarity=97.0)


async def test_malformed_body_and_unknown_event_are_poison_pills() -> None:
    store = FakeConfirmStore(None)
    deps = _deps(store=store)

    assert await handle_message("not json", deps) is True
    assert await handle_message(_body(uuid4(), event="something.else"), deps) is True
    assert store.unfetchable == []
    assert store.triages == []


async def test_already_decided_context_deletes_with_zero_side_effects() -> None:
    ctx = _ctx(confirm_state="confirmed")
    store = FakeConfirmStore(ctx)
    provider = FakeAttributionProvider()
    deps = _deps(store=store, provider=provider)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is True
    assert provider.calls == 0
    assert store.unfetchable == []
    assert store.triages == []
    assert store.duplicates == []
    assert store.quarantines == []


async def test_missing_context_deletes_with_zero_side_effects() -> None:
    store = FakeConfirmStore(None)
    provider = FakeAttributionProvider()
    deps = _deps(store=store, provider=provider)

    handled = await handle_message(_body(uuid4()), deps)

    assert handled is True
    assert provider.calls == 0


async def test_no_image_url_records_unfetchable() -> None:
    ctx = _ctx(image_url=None)
    store = FakeConfirmStore(ctx)
    deps = _deps(store=store)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is True
    assert store.unfetchable == [(ctx.infringement_id, "no image_url recorded")]


async def test_fetch_failure_records_unfetchable() -> None:
    ctx = _ctx()
    store = FakeConfirmStore(ctx)

    async def _fetch_none(url: str) -> bytes | None:
        return None

    deps = _deps(store=store, fetch=_fetch_none)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is True
    assert len(store.unfetchable) == 1
    assert store.unfetchable[0][0] == ctx.infringement_id


async def test_duplicate_short_circuits_before_provider_is_touched() -> None:
    # Textured, not IMAGE_BYTES: a normal (non-degenerate) hash is the case
    # under test here -- the degenerate ones get their own tests below.
    textured = _textured_jpeg()
    existing_phash = dhash(textured)

    async def _fetch_textured(url: str) -> bytes | None:
        return textured

    ctx = _ctx()
    existing_id = uuid4()
    store = FakeConfirmStore(ctx, decided=((existing_id, existing_phash),))
    provider = FakeAttributionProvider()
    deps = _deps(store=store, provider=provider, fetch=_fetch_textured)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is True
    assert provider.calls == 0
    assert store.duplicates == [(ctx.infringement_id, existing_id, existing_phash)]
    assert store.triages == []


async def test_degenerate_low_hash_skips_dedup_even_with_a_decided_match() -> None:
    """IMAGE_BYTES's own hash is 0 (population 0) by construction -- a solid
    colour has no gradient anywhere. A decided entry at that same hash would
    dedup under a plain Hamming-distance check, but the low-texture guard
    must skip dedup entirely rather than machine-hide a real new hit against
    a coincidentally-identical degenerate hash (CLAUDE.md §7.3)."""
    ctx = _ctx()
    decided_id = uuid4()
    store = FakeConfirmStore(ctx, decided=((decided_id, 0),))
    provider = FakeAttributionProvider(faces=(_face(),), matches=(_matching_face_match(),))
    deps = _deps(store=store, provider=provider)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is True
    assert store.duplicates == []
    assert provider.calls > 0  # dedup was skipped; the bundle actually ran
    assert len(store.triages) == 1
    assert store.triages[0]["triage"]["phash_degenerate"] is True


async def test_degenerate_high_hash_skips_dedup_even_with_a_decided_match() -> None:
    """The mirror case: population 64 (hash -1), a monotonic gradient with no
    sign flips anywhere."""
    degenerate_bytes = _degenerate_high_jpeg()
    degenerate_hash = dhash(degenerate_bytes)
    assert degenerate_hash == -1, "fixture assumption: this image hashes to all-ones"

    async def _fetch_degenerate(url: str) -> bytes | None:
        return degenerate_bytes

    ctx = _ctx()
    decided_id = uuid4()
    store = FakeConfirmStore(ctx, decided=((decided_id, -1),))
    provider = FakeAttributionProvider(faces=(_face(),), matches=(_matching_face_match(),))
    deps = _deps(store=store, provider=provider, fetch=_fetch_degenerate)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is True
    assert store.duplicates == []
    assert provider.calls > 0
    assert len(store.triages) == 1
    assert store.triages[0]["triage"]["phash_degenerate"] is True


async def test_budget_skip_records_skip_and_leaves_state_untouched() -> None:
    ctx = _ctx()
    store = FakeConfirmStore(ctx)
    provider = FakeAttributionProvider()
    control = FakeControlStore(
        {REKOGNITION_CONFIRM_ID: runtime(REKOGNITION_CONFIRM_ID, cost="1.00", daily_budget="0.00")}
    )
    deps = _deps(store=store, provider=provider, control=control)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is True
    assert provider.calls == 0
    assert len(control.skips) == 1
    run_id, provider_id, reason, _detail = control.skips[0]
    assert run_id == ctx.run_id
    assert provider_id == REKOGNITION_CONFIRM_ID
    assert reason == "budget_exceeded"
    assert len(store.skipped) == 1
    assert store.skipped[0][:2] == (ctx.infringement_id, "budget_exceeded")
    assert control.outcomes == []
    assert store.triages == []


async def test_skip_with_no_run_id_skips_control_record_but_still_records_skipped() -> None:
    ctx = _ctx(run_id=None)
    store = FakeConfirmStore(ctx)
    control = FakeControlStore(
        {REKOGNITION_CONFIRM_ID: runtime(REKOGNITION_CONFIRM_ID, cost="1.00", daily_budget="0.00")}
    )
    deps = _deps(store=store, control=control)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is True
    assert control.skips == []  # no run_id -> no provider_calls write attempted
    assert len(store.skipped) == 1


async def test_happy_path_ncii_records_triage_with_ncii_suspected() -> None:
    ctx = _ctx()
    store = FakeConfirmStore(ctx)
    provider = FakeAttributionProvider(faces=(_face(),), matches=(_matching_face_match(),))
    moderation = FakeModeration(ModerationSignal(labels=(EXPLICIT_LABEL,), min_age_low=30.0))
    deps = _deps(store=store, provider=provider, moderation=moderation)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is True
    assert len(store.triages) == 1
    triage = store.triages[0]
    assert triage["severity"] == "ncii_suspected"
    assert triage["face_match_score"] == 97.0
    assert store.quarantines == []


async def test_csam_path_quarantines_and_never_triages() -> None:
    ctx = _ctx()
    store = FakeConfirmStore(ctx)
    provider = FakeAttributionProvider(faces=(_face(),), matches=(_matching_face_match(),))
    moderation = FakeModeration(ModerationSignal(labels=(EXPLICIT_LABEL,), min_age_low=10.0))
    deps = _deps(store=store, provider=provider, moderation=moderation)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is True
    assert len(store.quarantines) == 1
    assert store.quarantines[0][3] == 10.0
    assert store.triages == []


async def test_attribution_unavailable_records_error_outcome_and_returns_false() -> None:
    ctx = _ctx()
    store = FakeConfirmStore(ctx)
    provider = FakeAttributionProvider(detect_error=AttributionUnavailable("DetectFaces failed"))
    control = FakeControlStore({REKOGNITION_CONFIRM_ID: runtime(REKOGNITION_CONFIRM_ID)})
    deps = _deps(store=store, provider=provider, control=control)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is False
    assert len(control.outcomes) == 1
    _run_id, result, _cost = control.outcomes[0]
    assert result.status == "error"
    assert store.triages == []


async def test_confirm_unavailable_from_moderation_records_error_outcome() -> None:
    ctx = _ctx()
    store = FakeConfirmStore(ctx)
    provider = FakeAttributionProvider(faces=(_face(),), matches=(_matching_face_match(),))
    moderation = FakeModeration(
        ModerationSignal(labels=(), min_age_low=None),
        error=ConfirmUnavailable("DetectModerationLabels failed"),
    )
    control = FakeControlStore({REKOGNITION_CONFIRM_ID: runtime(REKOGNITION_CONFIRM_ID)})
    deps = _deps(store=store, provider=provider, moderation=moderation, control=control)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is False
    assert len(control.outcomes) == 1
    assert control.outcomes[0][1].status == "error"


async def test_unexpected_store_exception_is_caught_by_the_outer_net() -> None:
    """CRITICAL 1(c): none of steps 2-9's specific `except` clauses anticipate
    an arbitrary store failure. Without the outer net this raises out of
    ``handle_message`` and crash-loops the process on hostile or merely
    unlucky input; with it, the message is left for redelivery like any other
    crash-shaped failure (same contract as ``search/worker.py``)."""

    class ExplodingStore(FakeConfirmStore):
        async def record_triage(self, *args: object, **kwargs: object) -> None:
            raise RuntimeError("store exploded mid-flow")

    ctx = _ctx()
    store = ExplodingStore(ctx)
    provider = FakeAttributionProvider(faces=(_face(),), matches=(_matching_face_match(),))
    deps = _deps(store=store, provider=provider)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is False


async def test_undecodable_image_records_unfetchable() -> None:
    ctx = _ctx()
    store = FakeConfirmStore(ctx)

    async def _fetch_garbage(url: str) -> bytes | None:
        return b"not an image"

    provider = FakeAttributionProvider()
    deps = _deps(store=store, provider=provider, fetch=_fetch_garbage)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is True
    assert store.unfetchable == [(ctx.infringement_id, "undecodable")]
    assert provider.calls == 0  # never reaches Rekognition


def _webp_bytes() -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", (64, 64), (128, 40, 200)).save(buffer, format="WEBP")
    return buffer.getvalue()


async def test_rekognition_bundle_receives_jpeg_for_webp_hit() -> None:
    """The live 2026-08-20 failure: a .webp image_url reached DetectFaces raw
    and died with InvalidImageFormatException five times, then dead-lettered.
    The bundle must always receive JPEG/PNG."""
    webp = _webp_bytes()

    async def _fetch_webp(url: str) -> bytes | None:
        return webp

    ctx = _ctx()
    store = FakeConfirmStore(ctx)
    provider = FakeAttributionProvider(faces=(_face(),), matches=(_matching_face_match(),))
    deps = _deps(store=store, provider=provider, fetch=_fetch_webp)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is True
    assert len(store.triages) == 1
    with Image.open(io.BytesIO(provider.detect_bytes[0])) as opened:
        assert opened.format == "JPEG"


async def test_failed_match_persists_largest_detected_bbox() -> None:
    """Spec 2026-08-21 §3: a failed match still needs a crop target — the
    subject decides "similar photo — is this you?" from the largest DETECTED
    face. face_match_score stays None so matched-vs-similar is distinguishable."""
    small = DetectedFace(
        face_index=0,
        bbox=BoundingBox(x=0.6, y=0.6, w=0.2, h=0.2),
        detect_confidence=99.0,
    )
    big = DetectedFace(
        face_index=1,
        bbox=BoundingBox(x=0.1, y=0.1, w=0.3, h=0.3),
        detect_confidence=99.0,
    )
    ctx = _ctx()
    store = FakeConfirmStore(ctx)
    # No matches at all: resolve_face yields match_score=None for both faces.
    provider = FakeAttributionProvider(faces=(small, big), matches=())
    deps = _deps(store=store, provider=provider)

    handled = await handle_message(_body(ctx.infringement_id), deps)

    assert handled is True
    assert len(store.triages) == 1
    triage = store.triages[0]["triage"]
    assert isinstance(triage, dict)
    assert triage["best_face_bbox"] == {"x": 0.1, "y": 0.1, "w": 0.3, "h": 0.3}
    assert store.triages[0]["face_match_score"] is None
