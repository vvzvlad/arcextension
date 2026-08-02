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
    uvicorn.run(app, host=settings.host, port=settings.port, log_level=settings.log_level.lower())


if __name__ == "__main__":
    main()
