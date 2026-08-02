"""The curator pass (§7) — the core of the system.

The nine-step pass, two-phase relocation, inter-instance + intra-instance dedup,
singleton, quarantine, the non-convergence latch, the lease with a fencing epoch,
the ``passes`` table, the server-clock check and the continuity-break gate.

The package is called ``curator`` (not ``pass``) because ``pass`` is a Python
keyword and could never be imported as ``src.pass``. Semantics are §7's "проход
куратора".
"""
