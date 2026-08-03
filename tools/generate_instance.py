#!/usr/bin/env python3
"""Thin CLI entry point for the instance generator (§13).

Usage lives in the subcommands (``bundle``, ``generate``); see ``tools/README.md``.
Run via ``make bundle`` / ``make instance`` or directly with the project ``.venv``
python.
"""

import sys
from pathlib import Path

# Allow running as a bare script (`python tools/generate_instance.py …`), where
# only tools/ — not the repo root — is on sys.path. `make instance` uses `-m`,
# which does not need this, but the file is documented as a runnable CLI.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)

from tools.instancegen.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
