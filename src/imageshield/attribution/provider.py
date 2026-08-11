"""The face port: detect faces in a photo, and search one of them.

Two methods, and the split between them is where a real API constraint lives.

``search_face`` takes the WHOLE image plus which face to search for, rather
than a pre-cropped face. That is deliberate: how a single face is isolated for
the search is the adapter's problem, not the caller's, and the answer is not
obvious. Verified against botocore's own service model rather than from
memory:

    Image members            ['Bytes', 'S3Object']       -- no URL
    SearchFacesByImage       "first detects the LARGEST face in the image,
                              and then searches the specified collection"

So a photo with three faces cannot be attributed by calling
``SearchFacesByImage`` three times on the same bytes — every call would search
the same largest face. Isolating face N requires cropping to its bounding box,
which needs an image codec this repo deliberately does not depend on
(``pyproject.toml`` excludes Pillow).

This seam is drawn so that decision can be made once, in one class, without
disturbing the resolution logic, the store, the route or the schema — all of
which are complete and tested against fakes.

**No S3 client, ever** (CLAUDE.md §3.3). The photo arrives as bytes read
through a proxy-minted presigned GET and is discarded; the same shape as the
enrolment path, where ``GetFaceLivenessSessionResults`` bytes go straight to
``IndexFaces``. Bytes in memory are fine and are how Rekognition is fed. Bytes
on disk or in a column are not (INVARIANTS #9).
"""

from __future__ import annotations

from typing import Protocol

from imageshield.attribution.models import DetectedFace, FaceMatch


class FaceAttributionProvider(Protocol):
    async def detect_faces(self, image: bytes) -> tuple[DetectedFace, ...]:
        """Every face in the photo, with a bbox and a detection confidence."""
        ...

    async def search_face(
        self,
        image: bytes,
        face: DetectedFace,
        *,
        collection_id: str,
        match_threshold: float,
        max_candidates: int,
    ) -> tuple[FaceMatch, ...]:
        """Candidates for ONE face, unfiltered and in provider order.

        The candidate filter is NOT applied here — it lives in
        ``resolve.py``, so that the discard rule is one pure function with one
        set of tests rather than a property of whichever adapter is wired in.
        """
        ...

    @property
    def model_id(self) -> str:
        """The face model version behind these scores.

        Recorded on every run and every face: a similarity produced by one
        model means nothing against one produced by another (INVARIANTS #4).
        """
        ...


class PhotoFetcher(Protocol):
    """Reads the photo through the proxy's presigned GET, and nothing else.

    A separate port from the provider so the thing that touches the network is
    swappable in tests without stubbing Rekognition, and so there is exactly
    one place that can hold image bytes.
    """

    async def fetch(self, presigned_get_url: str) -> bytes: ...
