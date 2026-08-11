"""The candidate filter and the winner rule — pure, no provider, no database.

This is the load-bearing half of INVARIANTS #1a. Household scoping cannot be a
search parameter (``SearchFacesByImage`` takes no candidate set), so the filter
here is the only thing standing between "a stranger outranked the household
member" and "person A's photo became person B's monitored seed".
"""

from __future__ import annotations

from uuid import uuid4

from imageshield.attribution.models import BoundingBox, DetectedFace, FaceMatch
from imageshield.attribution.resolve import distinct_attributed, resolve_face
from imageshield.types import UserRef

BOX = BoundingBox(x=0.1, y=0.1, w=0.2, h=0.3)


def _face(index: int = 0, confidence: float = 99.8) -> DetectedFace:
    return DetectedFace(face_index=index, bbox=BOX, detect_confidence=confidence)


def _match(user_ref: UserRef | str, similarity: float) -> FaceMatch:
    return FaceMatch(external_image_id=str(user_ref), similarity=similarity)


def test_the_highest_scoring_candidate_wins() -> None:
    alice, bob = UserRef(uuid4()), UserRef(uuid4())

    resolved = resolve_face(
        _face(), [_match(alice, 94.0), _match(bob, 97.5)], (alice, bob)
    )

    assert resolved.resolved_user_ref == bob
    assert resolved.match_score == 97.5


def test_a_non_candidate_that_outranks_the_real_match_is_discarded() -> None:
    """Done-when: assert with a planted non-candidate that would otherwise
    outrank the real one.

    This is the whole reason the filter exists. Rekognition searches the WHOLE
    of identity-v1 — there is no way to restrict the search set — so a stranger
    enrolled by some other household will routinely appear in these results,
    sometimes above the person who is actually in the photo. If the winner were
    chosen before the filter, this photo would become that stranger's monitored
    seed.
    """
    owner = UserRef(uuid4())
    stranger = UserRef(uuid4())  # enrolled, but NOT named by the caller

    resolved = resolve_face(
        _face(), [_match(stranger, 99.9), _match(owner, 93.1)], (owner,)
    )

    assert resolved.resolved_user_ref == owner
    assert resolved.match_score == 93.1


def test_only_a_non_candidate_matching_leaves_the_face_unattributed() -> None:
    owner = UserRef(uuid4())
    stranger = UserRef(uuid4())

    resolved = resolve_face(_face(), [_match(stranger, 99.9)], (owner,))

    assert resolved.resolved_user_ref is None
    assert resolved.match_score is None


def test_no_matches_at_all_is_a_first_class_outcome() -> None:
    """Not an error, not a rejection. Most faces in most photos belong to
    people who are not enrolled — this is the common case."""
    resolved = resolve_face(_face(), [], (UserRef(uuid4()),))

    assert resolved.resolved_user_ref is None
    assert resolved.match_score is None
    # The bbox is kept regardless: it is provenance for the decision, and the
    # proxy renders boxes from it.
    assert resolved.bbox == BOX
    assert resolved.detect_confidence == 99.8


def test_an_unparseable_external_image_id_is_discarded_not_raised() -> None:
    """We set every ExternalImageId in this collection ourselves (INVARIANTS
    #6), so a non-UUID means the collection holds something we did not put
    there. That is a reason to ignore the match, not to fail a user's photo."""
    owner = UserRef(uuid4())

    resolved = resolve_face(
        _face(), [_match("not-a-uuid", 99.9), _match(owner, 91.0)], (owner,)
    )

    assert resolved.resolved_user_ref == owner


def test_detect_confidence_is_never_used_as_a_match_score() -> None:
    """Two different quantities. A confident detection of a stranger must not
    read as a confident identification."""
    owner = UserRef(uuid4())

    resolved = resolve_face(_face(confidence=99.99), [_match(owner, 88.0)], (owner,))

    assert resolved.detect_confidence == 99.99
    assert resolved.match_score == 88.0


# ── one seed per person, not per face ────────────────────────────────────────


def test_two_enrolled_people_produce_two_independent_pairs() -> None:
    alice, bob = UserRef(uuid4()), UserRef(uuid4())
    faces = (
        resolve_face(_face(0), [_match(alice, 95.0)], (alice, bob)),
        resolve_face(_face(1), [_match(bob, 96.0)], (alice, bob)),
    )

    pairs = distinct_attributed(faces)

    assert {ref for ref, _ in pairs} == {alice, bob}
    assert len(pairs) == 2


def test_one_person_appearing_twice_yields_one_pair_keeping_the_best_face() -> None:
    """A mirror, or a poster on the wall. The seed is the PHOTO, so registering
    it twice would double their scan cost for no extra coverage."""
    alice = UserRef(uuid4())
    faces = (
        resolve_face(_face(0), [_match(alice, 91.0)], (alice,)),
        resolve_face(_face(1), [_match(alice, 98.0)], (alice,)),
    )

    pairs = distinct_attributed(faces)

    assert len(pairs) == 1
    ref, face = pairs[0]
    assert ref == alice
    assert face.face_index == 1  # the stronger evidence is kept as provenance


def test_unattributed_faces_produce_no_pairs() -> None:
    faces = (
        resolve_face(_face(0), [], (UserRef(uuid4()),)),
        resolve_face(_face(1), [], (UserRef(uuid4()),)),
    )
    assert distinct_attributed(faces) == ()


def test_one_enrolled_face_among_strangers_still_produces_its_pair() -> None:
    """The face-level rule in one test: a photo with the owner and two
    strangers is a valid seed for the owner. The photo-level gate this
    replaces would have discarded it."""
    owner = UserRef(uuid4())
    faces = (
        resolve_face(_face(0), [], (owner,)),
        resolve_face(_face(1), [_match(owner, 94.0)], (owner,)),
        resolve_face(_face(2), [_match(UserRef(uuid4()), 99.0)], (owner,)),
    )

    pairs = distinct_attributed(faces)

    assert len(pairs) == 1
    assert pairs[0][0] == owner
    # ...and all three faces are still recorded, strangers included.
    assert [f.face_index for f in faces] == [0, 1, 2]
    assert sum(1 for f in faces if f.resolved_user_ref is None) == 2
