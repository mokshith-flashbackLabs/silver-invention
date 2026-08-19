"""Hit confirmation: Rekognition-based triage of review-band hits before a
human ever sees them (migration 0021; protection-score design doc §7).

The worker this package will hold fetches the image, perceptual-hashes it
(dedup against a prior human decision for the same user), face-matches
through :mod:`imageshield.attribution`, and runs moderation labels to assign
a severity. Machine ordering only — nothing is machine-dropped and nothing is
machine-confirmed (INVARIANTS #19); a human decision in the review queue is
the only thing that ever moves a hit to ``confirmed``.

This task (queue plumbing) adds only :mod:`.models` — the constants and
payload shapes later tasks import.
"""
