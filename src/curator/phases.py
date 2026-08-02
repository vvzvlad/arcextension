"""Execute pass decisions: phase A (open), phase B (verify-then-close), closes.

The pure engine (:mod:`decide`) says WHAT; this module does the I/O — ``open_tab`` /
``get_tab`` / ``close_tab`` (async, OUTSIDE any DB transaction, §4) — and the
resulting DB writes (each ONE lease-guarded ``Database.write``, §7). A lost lease
raises :class:`~src.curator.lease.LeaseLost` from the guarded write and propagates so
the runner stops the pass at once; a per-tab :class:`Exception` is isolated by the
runner (§7 "Каждая вкладка — в своём try/except") — but ``LeaseLost`` is re-raised.

Guard re-checks live at the edge: every ``close_tab`` carries the full ``expect``
(§6) so the extension re-verifies url / not-audible / not-pinned / min-idle / not
active-in-focus at ``tabs.remove`` time — the step-4 guards hold for EVERY close kind
(§7), phase B included.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field

from loguru import logger

from src.curator import convergence, lease
from src.curator.decide import step4_passes
from src.db.actions import (
    insert_action,
    mark_action_abandoned,
    normalize_url,
    set_action_status,
)
from src.ext import protocol
from src.ext.commands import CommandError, send_command


@dataclass
class PassCtx:
    db: object
    registry: object
    settings: object
    pass_id: str
    epoch: int
    now: int
    idle_ms: int
    mirror: object
    struck: set               # (instance_id, url_norm) already struck THIS pass (cap 1/pass)
    ready: dict               # instance_id -> Readiness captured at step 3 (§7)
    actions_count: int = 0    # incremented on every action row written
    closed: set = field(default_factory=set)  # (instance_id, tab_id) closed THIS pass


def _ready_unchanged(ctx: PassCtx, instance_id: str) -> bool:
    """§7 "готовность инстанса фиксируется на проход кортежем
    ``(instance_id, conn_epoch, session_id, snapshot_id)``, и его смена выводит инстанс
    из прохода целиком".

    Return ``True`` only when the instance's LIVE ``ConnState`` still carries the same
    ``conn_epoch`` AND ``session_id`` captured when readiness was fixed for this pass.
    A reconnect mid-pass installs a NEW ``ConnState`` (new epoch, possibly new session):
    ``send_command`` would then stamp the NEW session, so ``stale_session`` never fires,
    yet the decision's ``tab_id``/``expect`` came from the OLD snapshot — a ``close_tab``
    would ``precondition_failed`` and wrongly strike a healthy url. So a mismatch (or no
    captured readiness, or no live socket) means: SKIP the command and treat it as
    DEFERRED — no strike, no quarantine, no action row; the tab re-decides next pass.
    """
    captured = ctx.ready.get(instance_id)
    if captured is None:
        return False
    cs = ctx.registry.get(instance_id)
    if cs is None:
        return False
    return cs.conn_epoch == captured.conn_epoch and cs.session_id == captured.session_id


# --- small sync writers (run inside a guarded Database.write) ----------------
def _insert_copy_tab(conn: sqlite3.Connection, *, instance_id, tab_id, window_id, tab, now):
    """UPSERT the phase-A copy's ``tabs`` row, clocks INHERITED from the source (§7).

    UPSERT (never bare INSERT): a snapshot answered after ``open_tab`` may already
    hold this row (§6). The copy carries the source's ``opened_at`` / ``last_active_at``
    / ``age_unknown`` so it is not "younger" than the source for dedup, singleton or
    the idle guard (§7)."""
    conn.execute(
        "INSERT INTO tabs (instance_id, tab_id, window_id, url, title, fav_icon_url, "
        "pinned, active, opened_at, last_active_at, age_unknown, self_navigating, "
        "audible, updated_at) VALUES (?,?,?,?,?,NULL,0,0,?,?,?,0,0,?) "
        "ON CONFLICT(instance_id, tab_id) DO UPDATE SET "
        "window_id=excluded.window_id, url=excluded.url, title=excluded.title, "
        "opened_at=excluded.opened_at, last_active_at=excluded.last_active_at, "
        "age_unknown=excluded.age_unknown, updated_at=excluded.updated_at",
        (
            instance_id, tab_id, window_id, tab.url, tab.title,
            tab.opened_at, tab.last_active_at, tab.age_unknown, now,
        ),
    )


def _delete_tab(conn: sqlite3.Connection, instance_id: str, tab_id: int) -> None:
    """Drop a closed tab from the mirror so the pass's own DB view is consistent
    before the next snapshot (which would remove it anyway)."""
    conn.execute(
        "DELETE FROM tabs WHERE instance_id = ? AND tab_id = ?", (instance_id, tab_id)
    )


def _decision_name(rule) -> str:
    return "rule_home" if rule is not None else "unruled_drain"


def _rule_cols(rule):
    if rule is None:
        return None, None
    try:
        return rule["id"], rule["pattern"]
    except (KeyError, IndexError, TypeError):
        return getattr(rule, "id", None), getattr(rule, "pattern", None)


def _session_of(ctx: PassCtx, instance_id: str) -> str | None:
    cs = ctx.registry.get(instance_id)
    return cs.session_id if cs is not None else None


def _expect(tab, idle_ms: int) -> dict:
    # §6 volatile-guard re-check at the extension edge (applies to every close).
    return {
        "url": tab.url,
        "notAudible": True,
        "notPinned": True,
        "minIdleMs": idle_ms,
    }


async def _guard_before_command(ctx: PassCtx) -> None:
    """Raise :class:`~src.curator.lease.LeaseLost` if the fencing epoch already moved.

    Used ONLY by the two units that send their browser command before their first
    guarded write (phase A's ``open_tab``, step 9's ``merge_windows``); every other unit
    writes first and is fenced by that write. A read, not the :func:`lease.guard`
    write-UPDATE, so it costs no write-lock. See :func:`src.curator.lease.holds` for
    what this does and does not guarantee.
    """
    if not await ctx.db.read(lambda c: lease.holds(c, ctx.epoch)):
        raise lease.LeaseLost(f"lease epoch {ctx.epoch} no longer held")


def _strike_once(conn, ctx: PassCtx, instance_id, url_norm, reason) -> None:
    """Add at most ONE quarantine strike per (instance, url_norm) per pass (§7)."""
    pair = (instance_id, url_norm)
    if pair in ctx.struck:
        return
    ctx.struck.add(pair)
    convergence.add_strike(
        conn, instance_id, url_norm,
        now=ctx.now, ttl_ms=ctx.settings.quarantine_ttl_min * 60_000, reason=reason,
    )


# --- phase A: open a copy in the target (§7) ---------------------------------
async def run_phase_a(ctx: PassCtx, dec) -> None:
    tab = dec.tab
    home = dec.home
    url_norm = normalize_url(tab.url)
    # §7 mid-pass eject: skip (deferred) if the TARGET we open in — or the SOURCE the
    # copy inherits its clock from and whose pair the non-convergence strike lands on —
    # reconnected/changed session since readiness was captured. No open, no rows.
    if not _ready_unchanged(ctx, home) or not _ready_unchanged(ctx, tab.instance_id):
        logger.info("phase A deferred (instance readiness changed): {} -> {}", tab.url, home)
        return
    # Phase A is one of the two units whose browser command precedes its first guarded
    # write, so nothing else would notice a pause armed a moment ago until AFTER a tab
    # was opened. Check the fencing epoch first: a lost lease stops the whole pass (§7)
    # instead of opening one more tab in a curator the owner just switched off. This
    # narrows the window, it does not remove it — a pause landing between this check and
    # the send still gets one open through; the lease SLOT, held until this pass
    # finishes, is what keeps two passes from overlapping.
    await _guard_before_command(ctx)
    seed_age_ms = ctx.now - tab.last_active_at
    seed_opened_ago_ms = ctx.now - tab.opened_at
    try:
        result = await send_command(
            ctx.registry, ctx.db, home, protocol.CMD_OPEN_TAB,
            {
                "url": tab.url,
                "pinned": False,
                "active": False,
                "seed_age_ms": seed_age_ms,
                "seed_opened_ago_ms": seed_opened_ago_ms,
                "seed_age_unknown": bool(tab.age_unknown),
            },
            cmd_timeout_ms=ctx.settings.cmd_timeout_ms,
        )
    except CommandError as exc:
        # Open failure is connection-class (target dropped / epoch changed): defer,
        # never a strike (§7). The tab retries next pass. No copy, no relocate row.
        logger.info("phase A open deferred for {} -> {}: {}", tab.url, home, exc.code)
        return
    tab_id_to = result.get("tabId")
    window_id_to = result.get("windowId")
    # SUGGESTION 6 (§7): a malformed ``open_tab`` reply without an int ``tabId`` must
    # NOT be written. SQLite's INTEGER affinity would happily store a string/float as
    # ``tabs.tab_id`` / ``relocate.tab_id_to`` — a LIVE relocate row pointing at a
    # non-existent copy. Better no relocate row (the source is untouched this pass and
    # re-decides next pass) than a bogus one. Not counted as a completed phase-A open.
    if not isinstance(tab_id_to, int) or isinstance(tab_id_to, bool):
        logger.warning(
            "phase A open returned no int tabId for {} -> {}: {!r}; not writing rows",
            tab.url, home, tab_id_to,
        )
        return
    session_from = _session_of(ctx, tab.instance_id)
    if session_from is None:
        src_inst = ctx.mirror.instances.get(tab.instance_id)
        session_from = src_inst.session_id if src_inst is not None else None
    session_to = _session_of(ctx, home)
    rule_id, rule_pattern = _rule_cols(dec.rule)

    def _write(conn: sqlite3.Connection) -> None:
        _insert_copy_tab(
            conn, instance_id=home, tab_id=tab_id_to, window_id=window_id_to,
            tab=tab, now=ctx.now,
        )
        insert_action(
            conn,
            ts=ctx.now, kind="relocate", status="done", initiator="curator",
            pass_id=ctx.pass_id,
            instance_from=tab.instance_id, instance_to=home,
            tab_id=tab.tab_id, session_id_from=session_from,
            tab_id_to=tab_id_to, session_id_to=session_to,
            rule_id=rule_id, rule_pattern=rule_pattern,
            decision=_decision_name(dec.rule),
            src_opened_at=tab.opened_at, src_last_active_at=tab.last_active_at,
            src_age_unknown=tab.age_unknown,
            url=tab.url, url_norm=url_norm, title=tab.title, pinned=0,
        )
        # A phase-A open is an unproductive round UNTIL phase B completes it: strike
        # the SOURCE pair (§7 non-convergence latch). Phase B resets the counter on
        # completion, so a healthy relocation never reaches three.
        _strike_once(conn, ctx, tab.instance_id, url_norm, "nonconvergent")

    await ctx.db.write(lease.guarded(ctx.epoch, _write))
    ctx.actions_count += 1


# --- phase B: verify source (already), verify target, close source (§7) ------
async def run_phase_b(ctx: PassCtx, dec) -> None:
    reloc = dec.reloc
    source_tab = dec.source_tab
    url_norm = reloc.url_norm or normalize_url(reloc.url)

    # Close guards apply to phase B too (§7 step 4). The source is a >1h background
    # tab so it clears idle, but it may have become pinned/audible/active-in-focus.
    if not step4_passes(source_tab, ctx.mirror, ctx.now, ctx.idle_ms):
        return  # skip this pass; the relocate row stays live and retries.

    # §7 mid-pass eject: the target reconnected/changed session since readiness =>
    # its captured tab_id_to is stale; defer (leave the relocate row live, no strike).
    if not _ready_unchanged(ctx, reloc.instance_to):
        logger.info("phase B deferred (target readiness changed) for reloc {}", reloc.id)
        return

    # Verify the TARGET (source already verified by the URL match in decide).
    try:
        await send_command(
            ctx.registry, ctx.db, reloc.instance_to, protocol.CMD_GET_TAB,
            {"tabId": reloc.tab_id_to}, cmd_timeout_ms=ctx.settings.cmd_timeout_ms,
        )
    except CommandError as exc:
        if exc.code == protocol.ERR_NO_SUCH_TAB:
            # Copy vanished since the snapshot => abandon; next pass restarts phase A.
            await ctx.db.write(lease.guarded(ctx.epoch, lambda c: mark_action_abandoned(c, reloc.id)))
            return
        logger.info("phase B get_tab deferred for reloc {}: {}", reloc.id, exc.code)
        return  # connection-class: leave live, retry.

    # §7 mid-pass eject: the SOURCE reconnected/changed session between readiness and
    # now (e.g. during the get_tab above) => its captured tab_id is stale; a close on it
    # would precondition_failed and wrongly strike a healthy url. Defer (leave the row
    # live, no strike). This is exactly the WARNING-3 reconnect case.
    if not _ready_unchanged(ctx, reloc.instance_from):
        logger.info("phase B deferred (source readiness changed) for reloc {}", reloc.id)
        return

    # Copy is alive => complete the relocation at-least-once (§7, WARNING-1).
    # Record the DISTINCT relocate_close as `pending` UNDER the lease guard BEFORE the
    # browser close, so that a lease lost between a successful `close_tab` and its
    # completion write does not leave the source closed with NO record (a journal hole
    # + an un-undoable relocation). If THIS guarded pending write is fenced, LeaseLost
    # propagates and the pass stops — no close is attempted.
    def _pending(conn: sqlite3.Connection) -> int:
        return insert_action(
            conn, ts=ctx.now, kind="relocate_close", status="pending",
            initiator="curator", pass_id=ctx.pass_id, origin_action_id=reloc.id,
            instance_from=reloc.instance_from, instance_to=reloc.instance_to,
            tab_id=source_tab.tab_id, session_id_from=reloc.session_id_from,
            tab_id_to=reloc.tab_id_to, session_id_to=reloc.session_id_to,
            rule_id=reloc.rule_id, rule_pattern=reloc.rule_pattern,
            url=reloc.url, url_norm=url_norm,
        )

    pending_id = await ctx.db.write(lease.guarded(ctx.epoch, _pending))

    try:
        await send_command(
            ctx.registry, ctx.db, reloc.instance_from, protocol.CMD_CLOSE_TAB,
            {"tabId": source_tab.tab_id, "expect": _expect(source_tab, ctx.idle_ms)},
            cmd_timeout_ms=ctx.settings.cmd_timeout_ms,
        )
    except CommandError as exc:
        if exc.code == protocol.ERR_PRECONDITION_FAILED:
            # The source did NOT close (turned active/pinned/audible): fail the pending
            # row + strike, exactly as before (the row now exists, so UPDATE it).
            def _fail(conn: sqlite3.Connection) -> None:
                set_action_status(conn, pending_id, "failed", reason=protocol.ERR_PRECONDITION_FAILED)
                _strike_once(conn, ctx, reloc.instance_from, url_norm, protocol.ERR_PRECONDITION_FAILED)
            await ctx.db.write(lease.guarded(ctx.epoch, _fail))
            ctx.actions_count += 1
            return
        # Connection-class: the close is UNCERTAIN. LEAVE the pending row — next pass's
        # reconcile resolves it against the fresh mirror (→ done if the source is gone,
        # → abandoned if it is still present). Do NOT delete it.
        logger.info("phase B close deferred for reloc {}: {}", reloc.id, exc.code)
        return

    # Source closed => complete: pending → done, drop the source, reset strikes (§7).
    # If THIS guarded write is fenced (the WARNING-1 window), the pending row SURVIVES
    # and reconcile completes it next pass — that is the whole point.
    def _done(conn: sqlite3.Connection) -> None:
        set_action_status(conn, pending_id, "done")
        _delete_tab(conn, reloc.instance_from, source_tab.tab_id)
        # Completed phase B is a success on the pair => reset the strike counter (§7).
        convergence.reset_strikes(conn, reloc.instance_from, url_norm)

    await ctx.db.write(lease.guarded(ctx.epoch, _done))
    # Step 9 plans from the SAME frozen mirror, so it must not journal this tab as
    # "moved" — it no longer exists (§9).
    ctx.closed.add((reloc.instance_from, source_tab.tab_id))
    ctx.actions_count += 1


# --- dedupe / singleton closes (§7 step 7 q2, step 8) -----------------------
async def run_close(ctx: PassCtx, dec) -> None:
    tab = dec.tab
    url_norm = normalize_url(tab.url)
    survivor = dec.survivor
    detail = None
    instance_to = None
    if survivor is not None:
        detail = json.dumps(
            {
                "instance": survivor.instance_id,
                "tab_id": survivor.tab_id,
                "last_active_at": survivor.last_active_at,
            }
        )
        if dec.decision == "dedupe" and survivor.instance_id != tab.instance_id:
            instance_to = survivor.instance_id  # inter-instance dedup: survivor's home
    rule_id, rule_pattern = _rule_cols(dec.rule)

    # SUGGESTION 4 (§7): a dedupe/singleton close removes the SOURCE only because the
    # frozen mirror showed a survivor. If the human closed that survivor (or navigated
    # it away) between the snapshot and now, closing the source would delete the LAST
    # copy of the url. Mirror phase B's care: verify the survivor is still present with
    # the SAME url before closing the source; otherwise skip and re-decide next pass.
    if survivor is not None:
        # §7 mid-pass eject also covers the survivor's instance.
        if not _ready_unchanged(ctx, survivor.instance_id):
            logger.info("close deferred (survivor readiness changed) for {}", tab.url)
            return
        try:
            got = await send_command(
                ctx.registry, ctx.db, survivor.instance_id, protocol.CMD_GET_TAB,
                {"tabId": survivor.tab_id}, cmd_timeout_ms=ctx.settings.cmd_timeout_ms,
            )
        except CommandError as exc:
            # Survivor gone (no_such_tab) => do NOT close the source (it is the last
            # copy). Connection-class => defer. Either way skip; re-decide next pass.
            logger.info("close skipped (survivor verify {}) for {}", exc.code, tab.url)
            return
        survivor_url = (got.get("tab") or {}).get("url")
        if survivor_url != survivor.url:
            # Survivor navigated away => the url is no longer duplicated; do not close.
            logger.info("close skipped (survivor url changed) for {}", tab.url)
            return

    # §7 mid-pass eject: the source reconnected/changed session since readiness => its
    # captured tab_id is stale; a close would precondition_failed and wrongly strike a
    # healthy url. Defer (no strike, no action row).
    if not _ready_unchanged(ctx, tab.instance_id):
        logger.info("close deferred (source readiness changed) for {}", tab.url)
        return

    # At-least-once close journaling (§7, WARNING-1): record the close row as `pending`
    # UNDER the lease guard BEFORE the browser close, with every column the `done` row
    # carries, so a lease lost between a successful `close_tab` and its completion write
    # does not lose the close's record. A fenced pending write => LeaseLost, pass stops,
    # no close attempted.
    session_from = _session_of(ctx, tab.instance_id)

    def _pending(conn: sqlite3.Connection) -> int:
        return insert_action(
            conn, ts=ctx.now, kind=dec.kind, status="pending", initiator="curator",
            pass_id=ctx.pass_id, instance_from=tab.instance_id, instance_to=instance_to,
            tab_id=tab.tab_id, session_id_from=session_from,
            rule_id=rule_id, rule_pattern=rule_pattern, decision=dec.decision,
            url=tab.url, url_norm=url_norm, title=tab.title, detail=detail,
        )

    pending_id = await ctx.db.write(lease.guarded(ctx.epoch, _pending))

    try:
        await send_command(
            ctx.registry, ctx.db, tab.instance_id, protocol.CMD_CLOSE_TAB,
            {"tabId": tab.tab_id, "expect": _expect(tab, ctx.idle_ms)},
            cmd_timeout_ms=ctx.settings.cmd_timeout_ms,
        )
    except CommandError as exc:
        if exc.code == protocol.ERR_PRECONDITION_FAILED:
            # The source did NOT close: fail the pending row + strike (as before).
            def _fail(conn: sqlite3.Connection) -> None:
                set_action_status(conn, pending_id, "failed", reason=protocol.ERR_PRECONDITION_FAILED)
                _strike_once(conn, ctx, tab.instance_id, url_norm, protocol.ERR_PRECONDITION_FAILED)
            await ctx.db.write(lease.guarded(ctx.epoch, _fail))
            ctx.actions_count += 1
            return
        # Connection-class: the close is UNCERTAIN. LEAVE the pending row for reconcile.
        logger.info("close deferred for {} ({}): {}", tab.url, dec.kind, exc.code)
        return

    # Source closed => complete: pending → done, drop the tab, reset strikes (§7).
    # A fenced completion (the WARNING-1 window) leaves the pending row for reconcile.
    def _done(conn: sqlite3.Connection) -> None:
        set_action_status(conn, pending_id, "done")
        _delete_tab(conn, tab.instance_id, tab.tab_id)
        convergence.reset_strikes(conn, tab.instance_id, url_norm)  # success on the pair (§7)

    await ctx.db.write(lease.guarded(ctx.epoch, _done))
    # Step 9 plans from the SAME frozen mirror, so it must not journal this tab as
    # "moved" — it no longer exists (§9).
    ctx.closed.add((tab.instance_id, tab.tab_id))
    ctx.actions_count += 1


# --- reconcile: resolve prior-pass pending closes (§7, WARNING-1) -----------
def _source_present(mirror, row) -> bool:
    """Is the source of a PENDING close still present in this pass's frozen mirror?

    Present iff a tab with the recorded FULL url still sits in the source instance —
    keyed on URL ONLY, never on session or ``tab_id`` (both are discard-volatile, §5).
    A websocket reconnect wipes and repopulates the mirror ``tabs`` with new tab_ids
    and a NEW session, but a source that never actually closed (a connection-class
    failure whose ``pending`` row we left) still carries the same URL — so keying on
    URL, not session, avoids journaling a phantom ``done`` for a close that did not
    happen (the session short-circuit that once lived here did exactly that).

    ABSENT => the close took effect (or the url is gone anyway) → complete → done
    (at-least-once). PRESENT => the close never happened → abandon so ``decide``
    re-issues it; the re-issued close is still guarded by ``_expect``'s idle check at
    the extension edge, so an in-use tab is not force-closed and we never double-close
    (the original tab, if it truly closed, is gone — a live match is a different tab
    holding the same url, which the rule wants curated anyway).

    ⚠️ KNOWN LIMITATION, deliberately not fixed. The match is by URL only, so with
    THREE OR MORE tabs holding the same url in one instance, closing one of them still
    leaves a match and the (successful) close is journaled ``abandoned`` instead of
    ``done``. It cannot be tightened with what a pending row carries: ``tab_id`` is
    discard-volatile even inside one session and ``session_id`` is wiped by a
    reconnect (§5), which is exactly the case this function exists to survive.
    Counting instead of matching does not help either — the pre-close count is not
    recorded, and recording it would still be wrong the moment the human opens or
    closes a fourth copy mid-pass. The cost of the miss is bounded and self-healing:
    an ``abandoned`` close is re-issued by ``decide`` (dedup/singleton still sees the
    duplicates) or, for a relocate, one pass later — never a lost tab, only a journal
    row that under-reports. The readiness gate in :func:`run_reconcile` removes the
    far more damaging version of the same failure (a whole instance's stale mirror).
    """
    return any(t.url == row["url"] for t in mirror.tabs_of(row["instance_from"]))


async def run_reconcile(ctx: PassCtx, row) -> None:
    """Resolve ONE prior-pass ``pending`` close against the fresh frozen mirror (§7).

    Runs EARLY in the pass (before decide), per row isolated, every write lease-guarded.
    Idempotent and safe to run every pass: a pending row is resolved to exactly one
    terminal state and never revisited.

    **Gated on the source instance's readiness for THIS pass.** The verdict is read off
    a mirror, and a mirror is only as fresh as the snapshot that filled it. An instance
    that did not answer this pass's ``snapshot_request`` within ``SNAPSHOT_TIMEOUT_MS``
    (the laptop is asleep, the service worker has not woken) leaves the mirror showing
    the world as it was BEFORE the close — ``_source_present`` then says "still there"
    and a close that really happened is terminally journaled ``abandoned``. For a
    ``relocate_close`` that is permanent: ``abandoned`` does not retire the relocation,
    so a completed relocation looks abandoned forever. §7 fixes instance readiness for
    the whole pass by ``(instance_id, conn_epoch, session_id, snapshot_id)`` and takes an
    instance out of the pass ENTIRELY when it changes — that applies here too, so an
    unready source leaves the row ``pending`` for a pass with a fresh snapshot. That is
    precisely the at-least-once semantics ``pending`` was introduced for. (An instance
    retired for good therefore leaves its pending rows pending: an unresolved journal
    row that ``ACTIONS_RETENTION_DAYS`` eventually collects is a far cheaper wrong than
    a terminal verdict invented from a mirror nobody refreshed.)

    * Source ABSENT (the close happened, just wasn't journaled) → ``pending`` → ``done``
      and reset the pair's strikes; the source tab is already gone from the mirror, so
      no ``_delete_tab`` is needed (and doing it by the stale hint would risk a foreign
      row — see ``_complete``). For a relocate_close this retires the relocation
      (``live_relocations`` excludes a done/pending relocate_close).
    * Source PRESENT (the close never took effect — a connection-class failure or a
      lease lost before ``close_tab``) → ``pending`` → ``abandoned`` so nothing stale
      lingers. ``decide`` re-issues the close this pass (a plain close: its source tab
      is still in the mirror) or next pass (a relocate_close: the relocation re-enters
      ``live_relocations`` once the pending is no longer pending) — closed exactly once.
    """
    if not _ready_unchanged(ctx, row["instance_from"]):
        logger.info(
            "reconcile deferred (source instance not ready this pass) for action {}",
            row["id"],
        )
        return
    if _source_present(ctx.mirror, row):
        await ctx.db.write(
            lease.guarded(ctx.epoch, lambda c: mark_action_abandoned(c, row["id"]))
        )
        ctx.actions_count += 1
        return

    def _complete(conn: sqlite3.Connection) -> None:
        set_action_status(conn, row["id"], "done")
        # No `_delete_tab` here: the source is ABSENT by definition of this branch (no
        # tab holds the recorded url), and the row's ``tab_id`` is a discard-volatile
        # hint (§5) a reconnect may have REUSED for a foreign tab — deleting by it could
        # drop a legitimate mirror row. The DB tabs are refreshed by the next snapshot
        # regardless, and decide runs off the frozen mirror this pass, so nothing needs
        # the delete.
        if row["url_norm"] is not None:
            convergence.reset_strikes(conn, row["instance_from"], row["url_norm"])

    await ctx.db.write(lease.guarded(ctx.epoch, _complete))
    ctx.actions_count += 1


# --- step 9: window merge (§9) ----------------------------------------------
async def run_window_merge(ctx: PassCtx, merge) -> None:
    """Execute one §9 window merge: fold the source windows' UNPINNED tabs into the
    target, then journal a NON-UNDOABLE ``window_merge`` row.

    The server only names ``{windowIds, targetWindowId}``; the extension moves the
    unpinned tabs (pinned ones stay — a cross-window ``tabs.move`` resets ``pinned``,
    §9) and, BEFORE the move, marks the curator cause on BOTH windows so the neighbour
    activation the move triggers does not rejuvenate the merged window (§6). The
    server does NOT re-stamp any tab age here.

    ``busy_dragging`` (the human is holding a tab), a connection-class code, or
    ``no_window`` are NOT failures: no row is written and the merge re-decides against
    a fresh mirror next pass (§9). On success one ``window_merge`` row records the
    executed plan for the archive; NO pre-merge layout is stored (there is none in the
    schema), so ``undo._classify`` honestly reports the merge as not undone (§9)."""
    # §7 mid-pass eject: a reconnect / session change since readiness was captured
    # invalidates the snapshot's window & tab ids; defer (no command, no row).
    if not _ready_unchanged(ctx, merge.instance_id):
        logger.info("window merge deferred (instance readiness changed): {}", merge.instance_id)
        return
    # Like phase A, this command precedes the unit's first guarded write — check the
    # fencing epoch so a paused curator stops rearranging windows (see
    # ``_guard_before_command``).
    await _guard_before_command(ctx)

    try:
        result = await send_command(
            ctx.registry, ctx.db, merge.instance_id, protocol.CMD_MERGE_WINDOWS,
            {
                "windowIds": merge.source_window_ids,
                "targetWindowId": merge.target_window_id,
            },
            cmd_timeout_ms=ctx.settings.cmd_timeout_ms,
        )
    except CommandError as exc:
        # Transient (busy_dragging) or connection-class: retry next pass, no row (§9).
        logger.info("window merge deferred for {}: {}", merge.instance_id, exc.code)
        return

    # §9 wants the list of tabs that were actually MOVED. The plan came from the mirror
    # frozen BEFORE steps 4-8, so tab_ids this very pass closed would describe a move
    # that never happened; drop them. The extension answers ``merge_windows`` with a
    # ``{merged: N}`` COUNT and no ids, so the server's own bookkeeping is all there is:
    # if the command ever starts returning the moved ids, take them verbatim instead of
    # reconstructing — they are the only authoritative answer.
    #
    # What this list still cannot know, and deliberately does not pretend to:
    # a close that failed connection-class (its outcome is UNKNOWN, so the tab is not in
    # ``ctx.closed`` and stays listed), and tabs the human opened in a source window
    # after the snapshot (moved by the extension, absent from the plan). ``merged`` is
    # recorded beside the list precisely so the two can be compared when they disagree.
    moved_tab_ids = [
        tab_id for tab_id in merge.moved_tab_ids
        if (merge.instance_id, tab_id) not in ctx.closed
    ]
    detail = json.dumps(
        {
            "targetWindowId": merge.target_window_id,
            "windowIds": merge.source_window_ids,
            "moved_tab_ids": moved_tab_ids,
            "merged": result.get("merged"),
        }
    )

    def _write(conn: sqlite3.Connection) -> None:
        # Non-undoable (§9): a plain archival record; no pre-merge layout is kept.
        insert_action(
            conn, ts=ctx.now, kind="window_merge", status="done", initiator="curator",
            pass_id=ctx.pass_id, instance_from=merge.instance_id, detail=detail,
        )

    await ctx.db.write(lease.guarded(ctx.epoch, _write))
    ctx.actions_count += 1
