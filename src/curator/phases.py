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
from dataclasses import dataclass

from loguru import logger

from src.curator import convergence, lease
from src.curator.decide import step4_passes
from src.db.actions import insert_action, mark_action_abandoned, normalize_url
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

    # Copy is alive => close the SOURCE with the full expect (§6/§7).
    # TODO(Фаза 16): WARNING 1 — once ``lease.bump_epoch`` can shift the epoch mid-pass
    # (pause endpoints), a lease lost BETWEEN this ``close_tab`` and the guarded write
    # below leaves the source closed with no ``relocate_close`` row (unrecorded /
    # unrecoverable). Needs an at-least-once "pending-then-complete" write: record the
    # intent BEFORE the close, reconcile after. The Фаза-8 sub-path is closed by the
    # hardened renewal loop (runner ``_renew_loop``); this remains for the bump_epoch case.
    try:
        await send_command(
            ctx.registry, ctx.db, reloc.instance_from, protocol.CMD_CLOSE_TAB,
            {"tabId": source_tab.tab_id, "expect": _expect(source_tab, ctx.idle_ms)},
            cmd_timeout_ms=ctx.settings.cmd_timeout_ms,
        )
    except CommandError as exc:
        if exc.code == protocol.ERR_PRECONDITION_FAILED:
            def _fail(conn: sqlite3.Connection) -> None:
                insert_action(
                    conn, ts=ctx.now, kind="relocate_close", status="failed",
                    initiator="curator", pass_id=ctx.pass_id,
                    origin_action_id=reloc.id,
                    instance_from=reloc.instance_from, instance_to=reloc.instance_to,
                    tab_id=source_tab.tab_id, session_id_from=reloc.session_id_from,
                    url=reloc.url, url_norm=url_norm, reason=protocol.ERR_PRECONDITION_FAILED,
                )
                _strike_once(conn, ctx, reloc.instance_from, url_norm, protocol.ERR_PRECONDITION_FAILED)
            await ctx.db.write(lease.guarded(ctx.epoch, _fail))
            ctx.actions_count += 1
            return
        logger.info("phase B close deferred for reloc {}: {}", reloc.id, exc.code)
        return  # connection-class: leave live, retry.

    # Source closed => complete the relocation with a DISTINCT relocate_close (§7).
    def _done(conn: sqlite3.Connection) -> None:
        insert_action(
            conn, ts=ctx.now, kind="relocate_close", status="done",
            initiator="curator", pass_id=ctx.pass_id, origin_action_id=reloc.id,
            instance_from=reloc.instance_from, instance_to=reloc.instance_to,
            tab_id=source_tab.tab_id, session_id_from=reloc.session_id_from,
            tab_id_to=reloc.tab_id_to, session_id_to=reloc.session_id_to,
            rule_id=reloc.rule_id, rule_pattern=reloc.rule_pattern,
            url=reloc.url, url_norm=url_norm,
        )
        _delete_tab(conn, reloc.instance_from, source_tab.tab_id)
        # Completed phase B is a success on the pair => reset the strike counter (§7).
        convergence.reset_strikes(conn, reloc.instance_from, url_norm)

    await ctx.db.write(lease.guarded(ctx.epoch, _done))
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

    # TODO(Фаза 16): WARNING 1 — same at-least-once gap as phase B: once bump_epoch can
    # shift the epoch mid-pass, a lease lost between this close and the guarded write
    # loses the close's record. Needs a pending-then-complete write when pause lands.
    try:
        await send_command(
            ctx.registry, ctx.db, tab.instance_id, protocol.CMD_CLOSE_TAB,
            {"tabId": tab.tab_id, "expect": _expect(tab, ctx.idle_ms)},
            cmd_timeout_ms=ctx.settings.cmd_timeout_ms,
        )
    except CommandError as exc:
        if exc.code == protocol.ERR_PRECONDITION_FAILED:
            def _fail(conn: sqlite3.Connection) -> None:
                insert_action(
                    conn, ts=ctx.now, kind=dec.kind, status="failed",
                    initiator="curator", pass_id=ctx.pass_id,
                    instance_from=tab.instance_id, instance_to=instance_to,
                    tab_id=tab.tab_id, session_id_from=_session_of(ctx, tab.instance_id),
                    rule_id=rule_id, rule_pattern=rule_pattern, decision=dec.decision,
                    url=tab.url, url_norm=url_norm, title=tab.title,
                    reason=protocol.ERR_PRECONDITION_FAILED, detail=detail,
                )
                _strike_once(conn, ctx, tab.instance_id, url_norm, protocol.ERR_PRECONDITION_FAILED)
            await ctx.db.write(lease.guarded(ctx.epoch, _fail))
            ctx.actions_count += 1
            return
        logger.info("close deferred for {} ({}): {}", tab.url, dec.kind, exc.code)
        return

    def _done(conn: sqlite3.Connection) -> None:
        insert_action(
            conn, ts=ctx.now, kind=dec.kind, status="done", initiator="curator",
            pass_id=ctx.pass_id, instance_from=tab.instance_id, instance_to=instance_to,
            tab_id=tab.tab_id, session_id_from=_session_of(ctx, tab.instance_id),
            rule_id=rule_id, rule_pattern=rule_pattern, decision=dec.decision,
            url=tab.url, url_norm=url_norm, title=tab.title, detail=detail,
        )
        _delete_tab(conn, tab.instance_id, tab.tab_id)
        convergence.reset_strikes(conn, tab.instance_id, url_norm)  # success on the pair (§7)

    await ctx.db.write(lease.guarded(ctx.epoch, _done))
    ctx.actions_count += 1
