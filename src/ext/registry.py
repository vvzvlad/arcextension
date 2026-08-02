"""In-memory registry of live /ext connections (per process, on ``app.state``).

One ``ConnState`` per connected instance holds the live socket, the ``conn_epoch``
that socket owns, the ``installUuid`` (to tell a legitimate reconnect from a
duplicate), the heartbeat ``alive`` flag, the outstanding ``snapshot_request``
id + its ``sent_at``, and the heartbeat task handle.

The hello handler decides evict-vs-reject and swaps the map entry while holding
:attr:`Registry.lock`, so no second hello can interleave across the ``await
old.close()`` of an eviction (§6). The registry is process-local — the "one
writer" / single-process invariant of Фаза 2 already forbids >1 worker.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field

from starlette.websockets import WebSocket


@dataclass
class ConnState:
    ws: WebSocket
    conn_epoch: int
    install_uuid: str
    session_id: str | None
    # Heartbeat: set False when a ping is sent, back to True on pong.
    alive: bool = True
    # Most recent outstanding snapshot_request for this instance. A snapshot
    # whose id != pending_snapshot_id is stale and ignored (§6).
    pending_snapshot_id: str | None = None
    pending_sent_at: int | None = None
    # The id of the LAST snapshot that was actually applied (set by the channel
    # after apply_snapshot). The curator pass keys its per-instance readiness on
    # its OWN request id via this field, NOT on the ``snapshot_at`` column (§6/§7):
    # a human Cmd+T lands a foreign snapshot mid-pass and moves the column, which —
    # if readiness were column-keyed — would eject the whole fleet from the pass.
    last_applied_snapshot_id: str | None = None
    # Single-flight guard for GET /api/state's background refresh (§10): set while a
    # refresh snapshot_request is outstanding so a burst of newtab opens (every
    # Cmd+T hits /api/state) fires ONE request per instance, never a fan-out. The
    # single-threaded writer (§4) must not be hammered by duplicate snapshots.
    state_refresh_inflight: bool = False
    heartbeat_task: asyncio.Task | None = None
    # Outstanding service->extension commands keyed by request id (§6). Each
    # `send_command` stores a Future here and awaits it; the receive loop
    # resolves it when the matching `response {id}` frame arrives. A response
    # whose id is not present is a late/duplicate reply and is ignored.
    pending_commands: dict[str, asyncio.Future] = field(default_factory=dict)


@dataclass
class Registry:
    _by_id: dict[str, ConnState] = field(default_factory=dict)
    # Guards the hello evict-vs-reject critical section (see module docstring).
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def get(self, instance_id: str) -> ConnState | None:
        return self._by_id.get(instance_id)

    def items(self) -> list[tuple[str, ConnState]]:
        """Snapshot of the live (instance_id, ConnState) pairs.

        A COPY (list) so a caller can iterate while the map mutates underneath it
        (the curator pass sends snapshot_requests to every connected instance).
        """
        return list(self._by_id.items())

    def put(self, instance_id: str, state: ConnState) -> None:
        self._by_id[instance_id] = state

    def remove_if_current(self, instance_id: str, state: ConnState) -> bool:
        """Remove the entry ONLY if it is still exactly ``state``.

        Identity guard: an evicted old connection's finalizer must never delete
        the newer entry that replaced it.
        """
        if self._by_id.get(instance_id) is state:
            del self._by_id[instance_id]
            return True
        return False
