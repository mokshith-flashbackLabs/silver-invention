"""The recheck loop: policy, egress guards, pacing, and one pass end to end.

No sockets and no database here — the guards, the redirect walk and the status
mapping are pure enough to prove without either, which is why they live apart
from ``recheck/http.py``. The SQL half is in ``tests/test_recheck_store.py``.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest

from imageshield.recheck.client import (
    HeadResponse,
    TransportError,
    UrlChecker,
)
from imageshield.recheck.loop import run_once
from imageshield.recheck.models import DueInfringement
from imageshield.recheck.pacer import DomainPacer
from imageshield.recheck.policy import verdict_for_status
from imageshield.recheck.ssrf import address_refusal, refusal_reason

ALLOWED = frozenset({"example.test", "other.test"})


class _Response:
    def __init__(self, status_code: int, headers: dict[str, str] | None = None) -> None:
        self._status = status_code
        self._headers = headers or {}

    @property
    def status_code(self) -> int:
        return self._status

    @property
    def headers(self) -> dict[str, str]:
        return self._headers


class RecordingTransport:
    """Records every request. Exposes ONLY ``head`` — the same shape as the
    production protocol, so a test cannot accidentally prove that a ``get``
    which does not exist was not called."""

    def __init__(self, *responses: HeadResponse | Exception) -> None:
        self.calls: list[tuple[str, str]] = []  # (method, url)
        self._responses = list(responses)

    async def head(self, url: str, *, timeout_seconds: float) -> HeadResponse:
        self.calls.append(("HEAD", url))
        response = (
            self._responses.pop(0) if self._responses else _Response(200)
        )
        if isinstance(response, Exception):
            raise response
        return response


def _item(url: str = "https://example.test/p", domain: str = "example.test") -> DueInfringement:
    return DueInfringement(
        infringement_id=uuid4(), page_url=url, source_domain=domain
    )


def _public_resolver(_host: str) -> list[str]:
    return ["93.184.216.34"]


def _checker(transport: RecordingTransport, **kwargs: Any) -> UrlChecker:
    return UrlChecker(
        transport,
        timeout_seconds=kwargs.pop("timeout_seconds", 5.0),
        max_redirects=kwargs.pop("max_redirects", 2),
        resolver=kwargs.pop("resolver", _public_resolver),
    )


# ── the status → verdict mapping ─────────────────────────────────────────────


@pytest.mark.parametrize("status", [404, 410])
def test_only_404_and_410_mean_dead(status: int) -> None:
    assert verdict_for_status(status) == "dead"


@pytest.mark.parametrize("status", [200, 204, 301, 302, 399])
def test_2xx_and_3xx_are_alive(status: int) -> None:
    assert verdict_for_status(status) == "alive"


@pytest.mark.parametrize("status", [401, 403])
def test_gated_is_alive_not_gone(status: int) -> None:
    """A host that has started requiring a login still has the material. Very
    often the opposite of removal."""
    assert verdict_for_status(status) == "alive"


@pytest.mark.parametrize("status", [500, 502, 503, 429, 418])
def test_server_trouble_and_anything_unexpected_change_nothing(status: int) -> None:
    """Marking a hit resolved because a site was briefly down would tell a
    victim their problem is fixed when it is not."""
    assert verdict_for_status(status) == "unchanged"


# ── the four done-when cases, through the checker ────────────────────────────


async def test_a_404_marks_the_url_dead() -> None:
    transport = RecordingTransport(_Response(404))
    result = await _checker(transport).check(_item(), ALLOWED)
    assert result.verdict == "dead"
    assert result.status_code == 404


async def test_a_timeout_leaves_the_row_unchanged() -> None:
    """Not evidence of removal — evidence that we could not reach the host."""
    transport = RecordingTransport(TransportError("read timeout"))
    result = await _checker(transport).check(_item(), ALLOWED)
    assert result.verdict == "unchanged"
    assert result.status_code is None


async def test_a_500_leaves_the_row_unchanged() -> None:
    transport = RecordingTransport(_Response(500))
    result = await _checker(transport).check(_item(), ALLOWED)
    assert result.verdict == "unchanged"


async def test_a_403_leaves_the_row_alive() -> None:
    transport = RecordingTransport(_Response(403))
    result = await _checker(transport).check(_item(), ALLOWED)
    assert result.verdict == "alive"


# ── HEAD only ────────────────────────────────────────────────────────────────


async def test_the_checker_only_ever_issues_head() -> None:
    """Done-when: the recheck worker never issues a GET — asserted on the
    mocked transport.

    A GET would pull the infringing page into this process, for material
    already classified as likely abuse, when all we need is liveness. The
    transport protocol has one method, so there is nothing to call.
    """
    transport = RecordingTransport(_Response(200), _Response(404))
    checker = _checker(transport)

    await checker.check(_item("https://example.test/a"), ALLOWED)
    await checker.check(_item("https://example.test/b"), ALLOWED)

    assert [method for method, _ in transport.calls] == ["HEAD", "HEAD"]
    assert not hasattr(transport, "get")


# ── egress guards ────────────────────────────────────────────────────────────


def test_a_host_resolving_to_link_local_is_refused() -> None:
    """Done-when: recheck refuses 169.254.169.254.

    The domain here IS allowlisted — the refusal comes from resolving it and
    finding a non-global address, which is the guard INVARIANTS #11 specifies:
    after DNS, never before. Checking the hostname would have passed this.
    """
    reason = refusal_reason(
        "https://example.test/p", ALLOWED, lambda _host: ["169.254.169.254"]
    )
    assert reason == "private_address"


@pytest.mark.parametrize(
    "address", ["127.0.0.1", "10.0.0.5", "192.168.1.1", "172.16.0.1", "::1", "fd00::1"]
)
def test_every_private_range_is_refused(address: str) -> None:
    assert (
        refusal_reason("https://example.test/p", ALLOWED, lambda _h: [address])
        == "private_address"
    )


def test_one_public_and_one_private_address_is_still_refused() -> None:
    """A name resolving to both is a rebinding attempt, not a lucky config."""
    reason = refusal_reason(
        "https://example.test/p", ALLOWED, lambda _h: ["93.184.216.34", "169.254.169.254"]
    )
    assert reason == "private_address"


def test_a_domain_absent_from_content_urls_is_refused() -> None:
    """Done-when. The allowlist is sourced from our own data — we only probe
    hosts a provider actually reported."""
    reason = refusal_reason("https://not-in-corpus.test/p", ALLOWED, _public_resolver)
    assert reason == "domain_not_allowlisted"


def test_a_literal_private_ip_url_is_refused_by_the_allowlist_first() -> None:
    reason = refusal_reason("http://169.254.169.254/latest/", ALLOWED, _public_resolver)
    assert reason == "domain_not_allowlisted"


@pytest.mark.parametrize("url", ["file:///etc/passwd", "gopher://example.test/", "not a url"])
def test_non_http_schemes_are_refused(url: str) -> None:
    assert refusal_reason(url, ALLOWED, _public_resolver) == "not_an_http_url"


# ── the ssrf split: address_refusal is the DNS + global-address half ────────
#
# recheck/ssrf.py's `refusal_reason` is now allowlist-check-then-delegate. The
# crop fetcher (`imageshield.fetcher`) has no domain allowlist — it fetches
# whatever URL a search provider or an infringement row names — so it calls
# `address_refusal` directly rather than `refusal_reason`. These tests pin the
# extracted function's own behaviour; the ordering test below pins that the
# delegation didn't change `refusal_reason`'s existing contract.


def test_address_refusal_is_none_for_a_global_address() -> None:
    assert address_refusal("https://ok.example/x", resolver=_public_resolver) is None


def test_address_refusal_refuses_a_private_address() -> None:
    reason = address_refusal("https://ok.example/x", resolver=lambda _h: ["169.254.169.254"])
    assert reason == "private_address"


def test_refusal_reason_never_resolves_a_non_allowlisted_domain() -> None:
    """Ordering, proven rather than assumed. The module docstring says the
    allowlist runs first because it is cheap and the resolve is a network
    round trip; a resolver that raises if it is ever called turns that claim
    into something this test would fail if the split got the order backwards.
    """

    def _must_not_be_called(_host: str) -> list[str]:
        raise AssertionError("resolver invoked for a non-allowlisted domain")

    reason = refusal_reason("https://not-in-corpus.test/p", ALLOWED, _must_not_be_called)
    assert reason == "domain_not_allowlisted"


async def test_a_refused_url_is_never_fetched_and_leaves_the_row_alone() -> None:
    transport = RecordingTransport(_Response(404))
    checker = _checker(transport)

    result = await checker.check(_item("https://elsewhere.test/p", "elsewhere.test"), ALLOWED)

    assert transport.calls == []  # never reached the network
    assert result.verdict == "unchanged"
    assert result.refused == "domain_not_allowlisted"


# ── redirects are guarded on every hop ───────────────────────────────────────


async def test_a_redirect_to_the_metadata_service_is_refused_at_the_second_hop() -> None:
    """The classic bypass: guards applied only to the first URL let
    `https://allowed/x -> 302 -> http://169.254.169.254/` straight through.
    The client walks redirects by hand so the guards apply to each one."""
    transport = RecordingTransport(
        _Response(302, {"location": "http://169.254.169.254/latest/meta-data/"}),
        _Response(200),  # would be returned if the second hop were fetched
    )
    checker = _checker(transport)

    result = await checker.check(_item(), ALLOWED)

    assert len(transport.calls) == 1  # the second hop never happened
    assert result.refused == "domain_not_allowlisted"
    assert result.verdict == "unchanged"


async def test_a_redirect_to_an_allowed_domain_is_followed_and_verdicted() -> None:
    transport = RecordingTransport(
        _Response(301, {"location": "https://other.test/moved"}),
        _Response(404),
    )
    result = await _checker(transport).check(_item(), ALLOWED)

    assert [url for _m, url in transport.calls] == [
        "https://example.test/p",
        "https://other.test/moved",
    ]
    assert result.verdict == "dead"
    assert result.redirects == 1


async def test_a_redirect_chain_longer_than_the_cap_changes_nothing() -> None:
    transport = RecordingTransport(
        _Response(302, {"location": "https://other.test/1"}),
        _Response(302, {"location": "https://example.test/2"}),
        _Response(302, {"location": "https://other.test/3"}),
        _Response(404),
    )
    result = await _checker(transport, max_redirects=2).check(_item(), ALLOWED)

    assert len(transport.calls) == 3  # original + 2 hops, then stop
    assert result.verdict == "unchanged"


# ── per-domain pacing ────────────────────────────────────────────────────────


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.sleeps.append(seconds)
        self.now += seconds


async def test_same_domain_requests_are_spaced_and_do_not_burst() -> None:
    """Done-when: 400 URLs on one domain do not burst.

    Probing one site's hits back to back gets the worker blocked and looks like
    an attack from the far end — it is the traffic shape a scanner makes.
    """
    clock = FakeClock()
    pacer = DomainPacer(2.0, sleep=clock.sleep, monotonic=clock.monotonic)

    for _ in range(400):
        await pacer.wait("example.test")

    # 399 gaps of 2s: the first request is free, every one after it waits.
    assert len(clock.sleeps) == 399
    assert set(clock.sleeps) == {2.0}
    assert clock.now == pytest.approx(798.0)


async def test_different_domains_are_not_paced_against_each_other() -> None:
    """Two hosts do not notice each other, so nothing is imposed across them."""
    clock = FakeClock()
    pacer = DomainPacer(2.0, sleep=clock.sleep, monotonic=clock.monotonic)

    for domain in ("a.test", "b.test", "c.test", "d.test"):
        await pacer.wait(domain)

    assert clock.sleeps == []


async def test_a_domain_probed_long_ago_waits_no_further() -> None:
    clock = FakeClock()
    pacer = DomainPacer(2.0, sleep=clock.sleep, monotonic=clock.monotonic)

    await pacer.wait("example.test")
    clock.now += 60.0
    waited = await pacer.wait("example.test")

    assert waited == 0.0
    assert clock.sleeps == []


# ── one pass ─────────────────────────────────────────────────────────────────


class FakeRecheckStore:
    def __init__(self, batch: tuple[DueInfringement, ...]) -> None:
        self._batch = batch
        self.verdicts: list[tuple[UUID, str]] = []
        self.domains_queries = 0

    async def due_batch(
        self, *, interval_days: int, limit: int
    ) -> tuple[DueInfringement, ...]:
        return self._batch[:limit]

    async def allowed_domains(self) -> frozenset[str]:
        self.domains_queries += 1
        return ALLOWED

    async def record_verdict(self, infringement_id: UUID, verdict: str) -> None:
        self.verdicts.append((infringement_id, verdict))


async def test_one_pass_paces_records_every_verdict_and_reads_the_allowlist_once() -> None:
    clock = FakeClock()
    batch = tuple(_item(f"https://example.test/{i}") for i in range(3))
    store = FakeRecheckStore(batch)
    transport = RecordingTransport(_Response(200), _Response(404), _Response(503))
    pacer = DomainPacer(2.0, sleep=clock.sleep, monotonic=clock.monotonic)

    results = await run_once(
        store, _checker(transport), pacer, interval_days=7, batch_size=10
    )

    assert [r.verdict for r in results] == ["alive", "dead", "unchanged"]
    assert store.verdicts == [
        (batch[0].infringement_id, "alive"),
        (batch[1].infringement_id, "dead"),
        (batch[2].infringement_id, "unchanged"),
    ]
    # Same domain throughout, so two gaps for three probes.
    assert clock.sleeps == [2.0, 2.0]
    # One allowlist read per PASS, not per URL.
    assert store.domains_queries == 1


async def test_an_empty_batch_does_no_work_at_all() -> None:
    store = FakeRecheckStore(())
    transport = RecordingTransport()
    clock = FakeClock()

    results = await run_once(
        store,
        _checker(transport),
        DomainPacer(2.0, sleep=clock.sleep, monotonic=clock.monotonic),
        interval_days=7,
        batch_size=10,
    )

    assert results == ()
    assert transport.calls == []
    assert store.domains_queries == 0  # not even the allowlist query
