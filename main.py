"""Thin entry point: build settings, configure logging, run the ASGI app."""

import sys

import uvicorn
from loguru import logger

from src.app import create_app
from src.settings import settings


def main() -> None:
    logger.remove()
    logger.add(sys.stderr, level=settings.log_level)
    logger.info("Starting curator-service on {}:{}", settings.host, settings.port)
    app = create_app(settings)
    # SINGLE PROCESS ONLY (workers=1, the uvicorn default). The "one writer"
    # invariant of src/db/access.py is per-process: with >1 worker each process
    # opens its own writer connection and serialization would rely on
    # BEGIN IMMEDIATE + busy_timeout alone, not the design. Do not add workers>1.
    # DISABLE uvicorn's protocol-level websocket ping (ws_ping_interval=None) and
    # rely solely on the application heartbeat (§6). The app heartbeat is the
    # authoritative liveness mechanism the architecture specifies — it must
    # survive MV3 service-worker death and drives reconnect via chrome.alarms —
    # and its "two consecutive misses" semantics are what the /ext code and tests
    # assert. Leaving uvicorn's implicit 20/20s ping on would add a SECOND,
    # redundant liveness timer that can close the socket on its own schedule,
    # racing the app heartbeat and muddying the two-miss rule. One timer, one
    # source of truth. (ws_ping_timeout is moot once the ping is off; set to None
    # for symmetry.)
    uvicorn.run(
        app,
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        ws_ping_interval=None,
        ws_ping_timeout=None,
    )


if __name__ == "__main__":
    main()
