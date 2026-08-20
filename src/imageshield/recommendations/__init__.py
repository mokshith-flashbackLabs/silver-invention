"""Recommendation catalog (design 2026-08-19, migration 0022).

:mod:`imageshield.recommendations.catalog` is pure — it decides which
recommendations a person's current state *should* have open, and returns
that set. Reconciling it against what is actually open in the database
(inserting new ones, completing satisfied ones, expiring stale ones,
honouring a dismissal forever) is Task 12's job in the store; this package
only computes the desired set.
"""
