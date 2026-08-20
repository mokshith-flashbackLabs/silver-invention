"""Protection score engine (design 2026-08-19, migration 0022).

:mod:`imageshield.score.engine` is pure — no I/O, no boto3, no Postgres. It
takes a :class:`~imageshield.score.engine.ScoreState` snapshot the store
(Task 12) assembles from the database and returns
:class:`~imageshield.score.engine.Components`. Everything about *how* a score
moves lives here and is unit-testable without a database; everything about
*where the numbers came from* lives in the store.
"""
