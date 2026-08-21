"""The subject's preview surface — the only path by which a hit's pixels ever
reach a user, and the only caller of the fetcher besides the confirm worker.

Spec 2026-08-21: the subject — and only the subject — may see a blurred face
crop of their own hit, live-rendered per view, audited per render (INVARIANTS
#31), rate-ceilinged per user (#32), never persisted (#9/#10/#12).
"""
