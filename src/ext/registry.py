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
    heartbeat_task: asyncio.Task | None = None


@dataclass
class Registry:
    _by_id: dict[str, ConnState] = field(default_factory=dict)
    # Guards the hello evict-vs-reject critical section (see module docstring).
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def get(self, instance_id: str) -> ConnState | None:
        return self._by_id.get(instance_id)

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
