"""The review queue — the only place a hit becomes ``confirmed`` or
``rejected`` (migration 0021; INVARIANTS #19).

:mod:`.store` holds ``ReviewStore`` / ``PostgresReviewStore``: reading the
next pending task in queue order, the per-severity queue depth, and the
``decide`` transaction that is the human-only confirm gate. Nothing here ever
promotes a task out of ``pending`` except an explicit ``decide`` call — there
is no timeout and no auto-promotion.
"""
