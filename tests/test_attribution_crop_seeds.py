"""Face-crop seeds — ``POST /v1/attribute`` with ``crop_targets`` (spec 2026-08-31).

The rule these tests exist to hold down is the failure rule, not the happy
path. When a crop cannot be stored, the subject gets **no seed** — never a
full-photo seed. Falling back would mean a transient S3 hiccup silently
reinstates the transmission the whole design exists to prevent: a person in the
photo who never consented, shipped to Hive and Google every scan cycle, with a
200 response and nothing anywhere saying so.

The happy path is easy to re-derive if it breaks. The fallback is not — it is
one `except` away at all times, it looks like robustness, and nothing user
visible would ever reveal it.
"""

from __future__ import annotations

import io
from typing import Any
from uuid import UUID, uuid4

from PIL import Image

from imageshield.attribution.models import FaceMatch
from tests.test_attribution_routes import (
    PRESIGNED,
    _body,
    _face,
    _post,
    make_client,
)

PUT_URL = "https://proxy-s3.example/face-crops/{ref}?X-Amz-Signature=deadbeef"


def _big_photo() -> bytes:
    """A photo big enough that a 0.3x0.4 bbox clears crop.py's 40px floor.

    test_attribution_routes' default fake is 32x32, which crops to ~10px and is
    refused as CropTooSmall — correct behaviour, and it would make every test
    here pass through the skip path instead of the one under test.
    """
    buffer = io.BytesIO()
    Image.new("RGB", (640, 480), (10, 120, 60)).save(buffer, format="JPEG")
    return buffer.getvalue()


def _target(user_ref: UUID, ref: str) -> dict[str, str]:
    return {
        "user_ref": str(user_ref),
        "crop_ref": f"face-crops/{ref}.jpg",
        "crop_put_url": PUT_URL.format(ref=ref),
    }


def _two_face_client(*owners: UUID, **kwargs: Any) -> Any:
    """A photo with one face per owner plus one stranger, each owner matched."""
    faces = tuple(_face(i) for i in range(len(owners) + 1))
    matches = {
        i: (FaceMatch(external_image_id=str(owner), similarity=95.0),)
        for i, owner in enumerate(owners)
    }
    kwargs.setdefault("fetch_payload", _big_photo())
    return make_client(faces=faces, matches=matches, **kwargs)


# ── the seed becomes the crop ────────────────────────────────────────────────


def test_a_group_photo_seeds_the_crop_and_never_the_photo() -> None:
    owner = uuid4()
    client, _p, store, _f = _two_face_client(owner)

    response = _post(
        client, _body([owner], crop_targets=[_target(owner, "abc")])
    )

    assert response.status_code == 200
    planned = store.runs[0]["planned_seeds"]
    assert len(planned) == 1
    # THE ASSERTION THAT MATTERS. photo_ref must not appear as any seed's
    # source: the whole photo has a second person in it.
    assert planned[0].source_object_ref == "face-crops/abc.jpg"
    assert planned[0].seed_kind == "face_crop"
    assert planned[0].source_object_ref != "photos/2026/08/abc.jpg"


def test_the_crop_is_put_to_the_proxys_url_as_a_jpeg() -> None:
    owner = uuid4()
    client, _p, _s, _f = _two_face_client(owner)

    _post(client, _body([owner], crop_targets=[_target(owner, "abc")]))

    puts = client.app.state.object_uploader.puts
    assert len(puts) == 1
    url, data, content_type = puts[0]
    assert url == PUT_URL.format(ref="abc")
    assert content_type == "image/jpeg"
    # A real, decodable JPEG — not a placeholder and not the whole photo.
    assert Image.open(io.BytesIO(data)).format == "JPEG"


def test_the_response_echoes_the_crop_key_the_caller_minted() -> None:
    owner = uuid4()
    client, _p, _s, _f = _two_face_client(owner)

    response = _post(
        client, _body([owner], crop_targets=[_target(owner, "abc")])
    )

    seed = response.json()["seeds_registered"][0]
    # Echoed, never invented: it is the proxy's key in the proxy's bucket, and
    # they record it on their own row from this field.
    assert seed["crop_object_key"] == "face-crops/abc.jpg"


def test_two_household_members_get_their_own_crops() -> None:
    first, second = uuid4(), uuid4()
    client, _p, store, _f = _two_face_client(first, second)

    response = _post(
        client,
        _body(
            [first, second],
            crop_targets=[_target(first, "one"), _target(second, "two")],
        ),
    )

    assert response.status_code == 200
    planned = {plan.user_ref: plan for plan in store.runs[0]["planned_seeds"]}
    assert planned[first].source_object_ref == "face-crops/one.jpg"
    assert planned[second].source_object_ref == "face-crops/two.jpg"
    # Distinct objects, which also ends the double-spend the spec's §1.1 found:
    # two covered members in one photo used to dispatch the SAME object twice.
    assert len(client.app.state.object_uploader.puts) == 2


# ── when a crop cannot be stored ─────────────────────────────────────────────


def test_a_failed_crop_upload_registers_no_seed_at_all() -> None:
    owner = uuid4()
    client, _p, store, _f = _two_face_client(
        owner, upload_fails_for=frozenset({"abc"})
    )

    response = _post(
        client, _body([owner], crop_targets=[_target(owner, "abc")])
    )

    # The run still completes — one subject's crop failing is not a reason to
    # fail the attribution or lose the face rows.
    assert response.status_code == 200
    assert store.runs[0]["planned_seeds"] == ()
    assert response.json()["seeds_registered"] == []


def test_a_failed_crop_upload_does_not_fall_back_to_the_photo() -> None:
    """The one that must never regress.

    Stated separately from the test above even though they overlap, because
    "registers nothing" and "does not register the PHOTO" fail differently: a
    well-meaning `except UploadError: seed the photo instead` keeps the first
    green and turns the second red.
    """
    owner = uuid4()
    client, _p, store, _f = _two_face_client(
        owner, upload_fails_for=frozenset({"abc"})
    )

    _post(client, _body([owner], crop_targets=[_target(owner, "abc")]))

    sources = [plan.source_object_ref for plan in store.runs[0]["planned_seeds"]]
    assert "photos/2026/08/abc.jpg" not in sources


def test_a_failed_crop_is_recorded_as_skipped_so_it_is_never_silent() -> None:
    owner = uuid4()
    client, _p, store, _f = _two_face_client(
        owner, upload_fails_for=frozenset({"abc"})
    )

    _post(client, _body([owner], crop_targets=[_target(owner, "abc")]))

    skipped = store.runs[0]["skipped_seeds"]
    assert [(s.user_ref, s.reason) for s in skipped] == [
        (owner, "crop_upload_failed")
    ]


def test_one_members_failed_crop_does_not_cost_the_other_their_seed() -> None:
    first, second = uuid4(), uuid4()
    client, _p, store, _f = _two_face_client(
        first, second, upload_fails_for=frozenset({"one"})
    )

    _post(
        client,
        _body(
            [first, second],
            crop_targets=[_target(first, "one"), _target(second, "two")],
        ),
    )

    planned = store.runs[0]["planned_seeds"]
    assert [plan.user_ref for plan in planned] == [second]
    assert [s.user_ref for s in store.runs[0]["skipped_seeds"]] == [first]


def test_a_subject_with_no_target_is_skipped_rather_than_photo_seeded() -> None:
    """A caller bug — the proxy mints one target per candidate — and the safe
    answer to a caller bug here is still to register nothing."""
    first, second = uuid4(), uuid4()
    client, _p, store, _f = _two_face_client(first, second)

    _post(
        client,
        _body([first, second], crop_targets=[_target(first, "one")]),
    )

    planned = store.runs[0]["planned_seeds"]
    assert [plan.user_ref for plan in planned] == [first]
    assert [(s.user_ref, s.reason) for s in store.runs[0]["skipped_seeds"]] == [
        (second, "no_crop_target")
    ]


def test_a_skipped_subject_gets_no_score_recompute() -> None:
    """No seed was registered, so nothing about their protection changed."""
    owner = uuid4()
    client, _p, _s, _f = _two_face_client(
        owner, upload_fails_for=frozenset({"abc"})
    )

    _post(client, _body([owner], crop_targets=[_target(owner, "abc")]))

    assert client.app.state.score_store.calls == []


# ── when cropping is not what the run calls for ──────────────────────────────


def test_a_single_face_photo_still_seeds_the_whole_photo() -> None:
    """There is nobody else in it to remove, so a crop buys nothing."""
    owner = uuid4()
    client, _p, store, _f = make_client(
        faces=(_face(0),),
        matches={0: (FaceMatch(external_image_id=str(owner), similarity=95.0),)},
        fetch_payload=_big_photo(),
    )

    response = _post(
        client, _body([owner], crop_targets=[_target(owner, "abc")])
    )

    assert response.status_code == 200
    planned = store.runs[0]["planned_seeds"]
    assert planned[0].source_object_ref == "photos/2026/08/abc.jpg"
    assert planned[0].seed_kind == "user_supplied"
    assert client.app.state.object_uploader.puts == []
    assert response.json()["seeds_registered"][0]["crop_object_key"] is None


def test_a_caller_that_sends_no_targets_gets_the_old_behaviour() -> None:
    owner = uuid4()
    client, _p, store, _f = _two_face_client(owner)

    response = _post(client, _body([owner]))

    assert response.status_code == 200
    assert store.runs[0]["planned_seeds"][0].source_object_ref == "photos/2026/08/abc.jpg"
    assert client.app.state.object_uploader.puts == []


# ── validation at the boundary ───────────────────────────────────────────────


def test_two_targets_for_one_subject_is_422() -> None:
    """Picking one silently would leave the other object dangling in their
    bucket forever, with nothing on either side naming it."""
    owner = uuid4()
    client, _p, _s, _f = _two_face_client(owner)

    response = _post(
        client,
        _body([owner], crop_targets=[_target(owner, "one"), _target(owner, "two")]),
    )

    assert response.status_code == 422


def test_a_crop_ref_that_is_a_url_is_422() -> None:
    """0011's rule, applied at the boundary: a presigned URL in the seed's
    durable reference works for a week and 403s forever after."""
    owner = uuid4()
    client, _p, _s, _f = _two_face_client(owner)
    target = _target(owner, "abc") | {"crop_ref": PRESIGNED}

    assert _post(client, _body([owner], crop_targets=[target])).status_code == 422


def test_a_crop_ref_carrying_a_signature_is_422() -> None:
    owner = uuid4()
    client, _p, _s, _f = _two_face_client(owner)
    target = _target(owner, "abc") | {
        "crop_ref": "face-crops/abc.jpg?X-Amz-Signature=abc"
    }

    assert _post(client, _body([owner], crop_targets=[target])).status_code == 422


def test_a_non_https_put_url_is_422() -> None:
    owner = uuid4()
    client, _p, _s, _f = _two_face_client(owner)
    target = _target(owner, "abc") | {"crop_put_url": "http://proxy-s3.example/x"}

    assert _post(client, _body([owner], crop_targets=[target])).status_code == 422


def test_the_presigned_put_url_never_comes_back_in_the_response() -> None:
    """It is a credential. It goes out on one PUT and nowhere else."""
    owner = uuid4()
    client, _p, _s, _f = _two_face_client(owner)

    response = _post(
        client, _body([owner], crop_targets=[_target(owner, "abc")])
    )

    assert "X-Amz-Signature" not in response.text
    assert "proxy-s3.example" not in response.text
