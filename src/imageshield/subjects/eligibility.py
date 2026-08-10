"""Who may be searched, and why the answer lives here.

Minors enrol in v1. Consent, guardianship and household seats all work for
them. **Discovery must not run for them**, and the reason is structural rather
than a policy preference: discovery finds images resembling the seed, and
nudify sites alter real photos, so for an enrolled minor a *successful* result
is CSAM inside this pipeline — fetched by the crop fetcher, rendered to a
parent, stored in ``infringements``, packaged by evidence export. There is no
version of the feature that works for a minor without that arising.

CSAM screening and reporting are deliberately deferred until the partner
corpus connects. Until they exist the correct behaviour is that **nothing
looks**, so nothing is found and no mandatory-reporting obligation starts.

The refusal happens at dispatch, never as a filter on results. A filter
implies a search ran.

Two things about where this decision lives:

- **The flag is ours, not the request's.** This service cannot check an age:
  it holds ``user_ref`` and no DOB, and that boundary is correct
  (CLAUDE.md §3.2). But if eligibility arrived as a per-request assertion,
  a proxy bug would silently scan a minor with nothing in our data to show it.
  So the proxy asserts once, at enrolment, and we store the answer.

- **Lowering ``MIN_DISCOVERY_AGE`` alone does not enable minor discovery.**
  The proxy sends a boolean, and :func:`eligibility_for` maps a minor to
  ineligible unconditionally — there is no configuration value that turns that
  branch off. v2 needs a config change *plus* the minor-specific handling
  code, which is precisely what the step-8 brief requires, and it is why
  :data:`MINOR_DISCOVERY_SUPPORTED` is a module constant rather than a setting.
"""

from __future__ import annotations

from imageshield.subjects.models import Eligibility

# v2 flips this, and flipping it alone is not enough: CSAM screening on fetched
# candidates and a mandatory-reporting path both have to exist first. It is
# here, in code, so enabling it costs a review and a `git blame` — the same
# reasoning as the calibration floor (CLAUDE.md §7.3).
MINOR_DISCOVERY_SUPPORTED = False

_ADULT = Eligibility(discovery_eligible=True, eligibility_reason="adult")
_MINOR = Eligibility(
    discovery_eligible=False, eligibility_reason="minor_discovery_deferred"
)


def eligibility_for(subject_is_adult: bool) -> Eligibility:
    """Map the proxy's one assertion onto the stored eligibility pair.

    ``subject_is_adult`` is computed by the proxy as ``age >=
    MIN_DISCOVERY_AGE`` against a DOB this service never sees. We do not
    second-guess the arithmetic; we refuse to accept its absence.
    """
    if subject_is_adult:
        return _ADULT
    if MINOR_DISCOVERY_SUPPORTED:  # pragma: no cover - v2 branch, see above
        raise NotImplementedError(
            "MINOR_DISCOVERY_SUPPORTED was set without the minor-specific"
            " handling code: CSAM screening on fetched candidates and a"
            " mandatory-reporting path must exist before a minor can be scanned"
        )
    return _MINOR
