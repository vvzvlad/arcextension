"""Process-memory counter for ``curator_auth_rejections_total`` (§12).

This is the ONE metric that is CORRECTLY held in process memory rather than read
from the DB: it is a ``_total`` counter of auth rejections, and the "read pass
facts from the DB, not process memory" rule (§12) applies only to the pass gauges —
those must survive a crash-loop restart, whereas a rejections counter legitimately
resets on restart like any Prometheus process counter.

A single module-level singleton is incremented at every auth-rejection site
(``require_ext_token`` / ``require_metrics_token`` 401s, the MCP Bearer reject, and
the ``/ext`` hello reject). Kept in its own leaf module (no project imports) so the
guards, the MCP ASGI wrapper, the ext channel and the metrics endpoint can all
import it without an import cycle through ``src.api.metrics``.
"""

from __future__ import annotations

import threading
from collections import Counter


class AuthRejections:
    """A thread-safe monotonic counter of auth rejections, keyed by a coarse reason.

    Only the unlabelled total is exported (§12 lists ``curator_auth_rejections_total``
    without a label); the per-reason breakdown is kept internally so a future phase
    can expose it cheaply without touching the increment sites.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_reason: Counter[str] = Counter()

    def incr(self, reason: str = "unknown") -> None:
        with self._lock:
            self._by_reason[reason] += 1

    def total(self) -> int:
        with self._lock:
            return int(sum(self._by_reason.values()))

    def by_reason(self) -> dict[str, int]:
        with self._lock:
            return dict(self._by_reason)


# The process-wide singleton every rejection site increments.
auth_rejections = AuthRejections()
