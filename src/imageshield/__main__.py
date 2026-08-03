"""Process entrypoint: ``python -m imageshield``.

Loads and validates configuration *before* handing off to uvicorn so a
missing or malformed key exits non-zero with a message naming the key —
the Phase 1 "Done when" contract.
"""

from __future__ import annotations

import asyncio
import sys

from imageshield.config import ConfigError, load_config


def main() -> int:
    try:
        cfg = load_config()
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 1

    import uvicorn

    config = uvicorn.Config(
        "imageshield.http.app:create_app",
        factory=True,
        host=cfg.http_host,
        port=cfg.http_port,
    )
    server = uvicorn.Server(config)

    if sys.platform == "win32":
        # uvicorn hard-codes the Proactor event loop on Windows, which
        # psycopg's async pool cannot run on — every connection attempt fails
        # and /health reports the DB degraded. Local dev only; the deployed
        # container is Linux and takes the plain path below.
        import selectors

        with asyncio.Runner(
            loop_factory=lambda: asyncio.SelectorEventLoop(selectors.SelectSelector())
        ) as runner:
            runner.run(server.serve())
        return 0

    server.run()
    return 0


if __name__ == "__main__":
    sys.exit(main())
