"""Server-clock guard (§7).

Every stored timestamp is absolute server-clock ms; nobody watches the server's own
clock. A forward jump (VM resumed from suspend, an NTP step, a container that started
before time sync) pushes EVERY tab of EVERY instance past the idle threshold at once —
guards protect at most one tab per instance (``focused_window_id``), and the absolute
``until`` of ``exemptions`` / ``quarantine`` is jumped by the same step. One pass would
then want to evict the whole fleet at once — which the per-pass action threshold
(``MAX_ACTIONS_PER_PASS``, see :mod:`src.curator.runner`) would defer behind a click,
but aborting on the step is still better: the mirror's ages are WRONG, so even the
deferred plan would be garbage. A backward jump makes ``serverNow - last_active_at``
negative, so nothing is ever idle — a silent permanent stall behind green alerts.

So the service keeps ``time.monotonic()`` beside the wall clock and, on pass entry,
compares the delta since the LAST check. Wall and monotonic advance together in
normal operation (their difference stays ~0); a clock STEP shows up as a large
difference because ``CLOCK_MONOTONIC`` does not jump (on Linux it does not even
advance across suspend, so a resume-from-suspend forward jump is caught). When the
step exceeds ``PASS_INTERVAL_MIN`` the pass is NOT executed, the delta is exported
(``curator_clock_step_seconds``, Фаза 12) and a ``snapshot_request`` goes to every
instance — the snapshot carries ``ageMs``, i.e. client deltas that never saw the
step, rebasing the whole mirror. The guard re-baselines every check, so the NEXT
pass runs normally instead of aborting forever.
"""

from __future__ import annotations

import time


class ClockGuard:
    """Stateful wall-vs-monotonic comparator. Injectable clocks make it testable.

    ``threshold_s`` is ``PASS_INTERVAL_MIN`` in seconds. :meth:`check` returns the
    skew in seconds since the previous check and re-baselines; the caller aborts the
    pass when ``abs(skew) > threshold_s``.
    """

    def __init__(self, threshold_s: float, *, wall=None, mono=None):
        self._threshold = threshold_s
        self._wall = wall or (lambda: time.time())
        self._mono = mono or (lambda: time.monotonic())
        self._last_wall = self._wall()
        self._last_mono = self._mono()

    def check(self) -> float:
        """Return the wall-vs-monotonic skew (seconds) since the last check, and
        re-baseline so the next pass measures from here."""
        w = self._wall()
        m = self._mono()
        skew = (w - self._last_wall) - (m - self._last_mono)
        self._last_wall = w
        self._last_mono = m
        return skew

    def exceeds(self, skew: float) -> bool:
        return abs(skew) > self._threshold
