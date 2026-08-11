"""The recheck loop: does this infringement's page still exist?

``infringements.url_alive`` has existed since migration 0005 and nothing ever
set it false, so every infringement was permanently "live" and any count of
live exposure equalled the total.

A dead URL is also the only unambiguously good news v1 can deliver. Detection
without takedown is otherwise alerts-with-no-remedy, and *"this came down"* is
the one thing this system can tell someone that is purely positive.

The pieces, smallest first:

- ``policy.py`` — HTTP status → verdict. Only 404/410 mark a URL dead.
- ``ssrf.py`` — allowlist + post-DNS private-range guard, per redirect hop.
- ``pacer.py`` — minimum gap between two requests to the same domain.
- ``client.py`` — the prober. HEAD only; there is no ``get`` to call.
- ``store.py`` — the due queue and the two timestamps.
- ``loop.py`` — one pass over one batch.
- ``worker.py`` — the process.
"""
