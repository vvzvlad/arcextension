"""Wire an MCP server (streamable HTTP) into the EXISTING Starlette app (§11).

THE mount trap (§11), and exactly how this module avoids each half:

1. ``streamable_http_app()`` creates the ``session_manager`` LAZILY. Accessing
   ``mcp.session_manager`` before that call raises ``RuntimeError: Session manager
   can only be accessed after calling streamable_http_app()``. So :func:`build_mcp`
   calls it ONCE, at app-construction time, BEFORE the lifespan runs.
2. A mounted sub-app's lifespan is NOT run by Starlette, so the session manager's
   task group is never created -> ``RuntimeError: Task group is not initialized``
   on the first request. Fixed by entering ``mcp.session_manager.run()`` in the
   HOST app's lifespan (see :func:`src.app.create_app`). ``stateless_http=True`` does
   NOT fix this (the check precedes the branch).
3. ``streamable_http_app()`` already registers ``/mcp`` internally, so mounting that
   app under ``/mcp`` DOUBLES the path (``/mcp/mcp``). We do NOT mount the returned
   app: we take its ASGI handler (``StreamableHTTPASGIApp`` over the session manager)
   and expose it as a single ``Route('/mcp', …)`` on the host — the path is exactly
   ``/mcp``. A ``Route`` (not a ``Mount``) also avoids the trailing-slash 307 a Mount
   would add; the SDK follows redirects anyway, but an exact route is cleaner.

``mcp.run(transport="streamable-http")`` is unusable — it starts its own uvicorn and
takes the port; the service needs ``/ext``, ``/api/*``, ``/healthz``, ``/metrics`` on
the same port. The SDK version is NOT pinned (the 307-redirect concern that once
motivated a pin is false: the client hardcodes ``follow_redirects=True``).

Auth: ``/mcp`` sits behind the ``ADMIN_TOKEN`` Bearer (issue #35 §4: the agent equals
the human — both authenticate with ADMIN_TOKEN, which opens ``/mcp`` and ``/admin/*``).
The check runs in the ASGI wrapper before the request reaches the session
manager. DNS-rebinding Host/Origin validation is DISABLED: the endpoint is
Bearer-gated and reached server-to-server (no browser origin), behind Traefik — the
protection guards browser-driven localhost servers, which this is not, and enabling
it would couple the service to its deployment hostname.
"""

from __future__ import annotations

import contextvars
import secrets

from mcp.server import MCPServer
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp
from mcp.server.transport_security import TransportSecuritySettings
from starlette.exceptions import HTTPException
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from src.api.auth_metrics import auth_rejections
from src.mcpiface import tools

# The streamable-HTTP session id of the CURRENT request, set by the ASGI wrapper
# from the ``Mcp-Session-Id`` header. execute_js records it as ``auth_ctx`` so the
# js_audit trace names the MCP SESSION, not a token id (§12).
_mcp_session: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "mcp_session", default=None
)


def current_mcp_session() -> str | None:
    """The MCP session id bound to the in-flight request (or None before initialize)."""
    return _mcp_session.get()


# --- the MCP server + its tools ---------------------------------------------
def build_mcp(app_ref) -> MCPServer:
    """Build the MCPServer and register every §11 tool.

    ``app_ref`` is a holder whose ``.app`` is set to the host Starlette app once it
    exists; the tool wrappers read ``app_ref.app.state`` at CALL time (by which point
    the lifespan has populated ``db`` / ``ext_registry`` / ``settings``). The handler
    bodies live in :mod:`src.mcpiface.tools` and are unit-tested directly.
    """
    mcp = MCPServer("tabscurator")

    def _host():
        return app_ref.app

    async def _guarded(coro):
        """Run a handler coroutine; turn a ``ToolError`` into a structured result the
        agent can read (a refusal or a preview is data, not a transport failure)."""
        # §12 parity with /api/*: a degraded schema (a failed migration) is not trusted
        # for reads OR writes — every /api/* endpoint returns 503 via require_operational,
        # the curator driver does not start, and the MCP layer must refuse too so an
        # agent cannot mutate against an unverified schema or grab the lease.
        host = app_ref.app
        if host is not None and getattr(host.state, "degraded", False):
            coro.close()  # the handler body never ran; don't leave it un-awaited
            return {"ok": False, "error": "degraded",
                    "message": "service is in degraded mode (§12); mutations and reads refused"}
        try:
            return await coro
        except tools.ToolError as exc:
            return {"ok": False, "error": exc.code, "message": exc.message, **exc.payload}
        except HTTPException as exc:
            # A reused /api helper (e.g. list_actions' filter parser) may raise 422 —
            # surface it as structured data, not a transport failure.
            return {"ok": False, "error": f"http_{exc.status_code}", "message": exc.detail}

    # --- reads ---------------------------------------------------------------
    @mcp.tool()
    async def list_instances() -> dict:
        """List instances with a per-instance freshness envelope, stopped_at and
        pending_plan (§11). Awaits a fresh snapshot per instance before answering."""
        return await _guarded(tools.list_instances(_host()))

    @mcp.tool()
    async def list_tabs(
        instance: str | None = None, window_id: int | None = None,
        url_contains: str | None = None,
    ) -> dict:
        """List mirrored tabs plus a per-instance freshness envelope
        (snapshot_at/fresh/reason/session_id). Awaits a fresh snapshot before answering
        (§11/§6).

        Optional, intersecting filters: ``instance`` (exact), ``window_id`` (exact),
        ``url_contains`` (case-insensitive substring of the url). ``window_id`` alone
        filters across instances but is ambiguous — the key is (instance_id, window_id) —
        so pass ``instance`` alongside it to name one window.

        Each tab carries ``dup_group``: the normalized address (origin+path) shared by
        more than one tab in the POST-FILTER output, else null. It is a HINT, not a
        prediction of the curator's collapse — the curator dedups by the FULL url string
        and only OUTSIDE main (main is a sink without dedup). ``fav_icon_url`` is not
        returned."""
        return await _guarded(tools.list_tabs(
            _host(), instance=instance, window_id=window_id, url_contains=url_contains,
        ))

    @mcp.tool()
    async def list_windows() -> dict:
        """List a per-window summary (instance_id, window_id, type, state, tab_count,
        focused) plus the same per-instance freshness envelope as list_tabs (§11/§6).
        A compact per-window view, not the per-tab list."""
        return await _guarded(tools.list_windows(_host()))

    @mcp.tool()
    async def get_rules() -> dict:
        """List the curation rules (§8)."""
        return await _guarded(tools.get_rules(_host()))

    @mcp.tool()
    async def list_actions(
        kind: str | None = None, pass_id: str | None = None,
        instance: str | None = None, url: str | None = None, since: int | None = None,
        deferred: bool | None = None, limit: int | None = None, offset: int | None = None,
    ) -> dict:
        """List archive actions with the same filters as GET /api/actions (§10)."""
        return await _guarded(tools.list_actions(
            _host(), kind=kind, pass_id=pass_id, instance=instance, url=url,
            since=since, deferred=deferred, limit=limit, offset=offset,
        ))

    # --- rule writes (confirm_impact gate) -----------------------------------
    @mcp.tool()
    async def upsert_rule(rule: dict, confirm_impact: bool = False) -> dict:
        """Create/update a rule; momentous changes require confirm_impact=True (§8)."""
        return await _guarded(tools.upsert_rule(_host(), rule=rule, confirm_impact=confirm_impact))

    @mcp.tool()
    async def delete_rule(rule_id: int, confirm_impact: bool = False) -> dict:
        """Delete a rule; always requires confirm_impact=True after the preview (§8)."""
        return await _guarded(tools.delete_rule(_host(), rule_id=rule_id, confirm_impact=confirm_impact))

    @mcp.tool()
    async def reset_singleton(rule_id: int) -> dict:
        """Return a rule's canonical_url reset target (§8/§10)."""
        return await _guarded(tools.reset_singleton(_host(), rule_id=rule_id))

    # --- commands (initiator='mcp' + auth_ctx = MCP session) -----------------
    # #47 "session epoch": every mutating verb that targets an instance accepts an
    # optional ``expected_session`` — the ``session_id`` the agent just read off
    # list_instances/list_tabs. It is STAMPED into the command frame so the extension
    # refuses (``stale_session``) if the browser has restarted since; omit it to run
    # unprotected, exactly as before (fail-open by design).
    @mcp.tool()
    async def open_tab(instance: str, url: str, pinned: bool = False, active: bool = False,
                       window_id: int | None = None, lease_ttl_s: int | None = None,
                       expected_session: str | None = None) -> dict:
        """Open a tab in an instance (§6).

        ``window_id`` optionally names the destination window (#45): omit it and the
        extension auto-selects the §9 window (today's behaviour); give it and the tab is
        opened in THAT window, refused with ``no_window`` if it is not a normal,
        non-fullscreen window or has vanished. A server-side cross-check turns an OLD
        extension that ignores the key (dropping the tab in its own window) into the same
        loud ``no_window``.

        ``lease_ttl_s`` protects the opened url from the curator pass for that many seconds
        (an ``exemptions`` row — the "owned by the agent" lease). Clamped to the shared
        30-day ceiling; there is no "forever". A lease that could not be written is
        reported as ``lease: {ok:false, error}`` — the tab is open either way, so do NOT
        retry the open on that.

        The lease protects an ADDRESS, not a tab: it is written for the url you asked for
        and matched against the tab's live url. A REDIRECT therefore drops it —
        ``https://shop/checkout`` landing on ``/checkout/step-1`` leaves a row that matches
        nothing, even though ``lease: {ok:true}`` says the write succeeded. After any
        navigation you did not ask for, read the live url and re-arm with
        ``set_exemption``.

        ASKING FOR A LEASE ALSO CHANGES HOW AN UNKNOWN ``instance`` FAILS. With
        ``lease_ttl_s`` the instance is a lease ARGUMENT, judged exactly as ``set_exemption``
        judges it: an unknown one is a hard ``invalid_request`` BEFORE the tab is opened,
        nothing sent. WITHOUT it the same call reaches the socket and comes back
        ``no_connection`` instead. One door, two codes, decided by whether a lease was asked
        for — expected, and worth knowing before you read the code as a different fault.
        """
        return await _guarded(tools.open_tab(
            _host(), instance=instance, url=url, pinned=pinned, active=active,
            window_id=window_id, lease_ttl_s=lease_ttl_s, auth_ctx=current_mcp_session(),
            expected_session=expected_session,
        ))

    @mcp.tool()
    async def navigate_tab(instance: str, tab_id: int, url: str,
                           wait_until: str | None = None, selector: str | None = None,
                           timeout_ms: int | None = None,
                           expected_session: str | None = None) -> dict:
        """Point a tab at an http/https url (§6), optionally waiting for the page.

        ``wait_until`` defaults to ``'none'`` (return as soon as the navigation is issued —
        today's behaviour, answering the unchanged ``{ok, result}``). ``'load'`` waits for
        the tab to report ``complete``; ``'selector'`` waits for ``selector`` to match.
        BOTH wait for the navigation to COMMIT first, so neither ordinarily reports about
        the page the tab is leaving. THREE CASES ESCAPE THAT and can answer about the OLD
        document: navigating to the address the tab is ALREADY on (the reload is
        indistinguishable from the document it replaces); a navigation that never changes
        the document at all (a 204, a ``Content-Disposition: attachment`` download, a
        cancelled load); and a navigation that has produced NO observable trace by the third
        poll (~750 ms), where a bounded grace opens the gate rather than burn your whole
        deadline on a page that may already be loaded. The third one does NOT require the
        addresses to match: it can answer about the old document even when you asked for a
        different address. That is the deliberate trade — a rare wrong document instead of a
        certain wrong answer at the deadline — and the reason ``matched: true`` is worth
        confirming with ``list_tabs`` when it comes back suspiciously fast. A tab that was
        ALREADY loading when you called is deliberately NOT a fourth: for it a
        loading→complete round is the OLD document finishing, so neither that signal nor the
        grace is granted, and a navigation it cannot recognise any other way answers
        ``matched: false`` at the deadline instead of confidently about the page it was
        leaving.

        ``timeout_ms`` bounds the wait and is clamped to EXECUTE_JS_MAX_TIMEOUT_MS. It also
        removes that grace, and the boundary is 500 ms: the grace opens the gate on the THIRD
        poll and the first sits behind a 250 ms pre-pause, so a budget of 500 ms or less
        never reaches it — measured, ``timeout_ms=500`` answers ``matched: false`` where 501
        answers ``matched: true``. Above that but below about a second the gate can open and
        the CONDITION then gets one or two polls before the deadline (at 999 ms it is tested
        at 750 ms and again at 999 ms). A short budget is the pre-grace behaviour, not a
        faster version of the same one.

        WITH a wait the answer is ``wait_for``'s: ``{ok, matched, elapsed_ms}``. A wait that
        expires still answers ``ok`` with ``matched: false`` — the navigation WAS issued and
        the condition simply never became true, which is a verdict, not a failure.

        ``matched: false`` HAS TWO MEANINGS and your next move differs: either the condition
        never became true (the page is there, the selector is wrong or slower than
        ``timeout_ms``), or the COMMIT was never recognised, in which case the condition was
        never tested at all. The answer cannot tell them apart — read the tab's live url
        (``list_tabs``) before concluding the page is wrong. An extension too old to
        understand ``wait_until`` is NOT one of the two: it is refused with its OWN code,
        ``extension_too_old`` (never ``precondition_failed``, which on this verb always means
        "fix your argument", and never dressed up as ``matched: false``). Retrying or
        rewording the call cannot help — stop asking THIS copy to wait, or update its
        extension; the tab WAS navigated either way."""
        return await _guarded(tools.navigate_tab(
            _host(), instance=instance, tab_id=tab_id, url=url, wait_until=wait_until,
            selector=selector, timeout_ms=timeout_ms, auth_ctx=current_mcp_session(),
            expected_session=expected_session,
        ))

    @mcp.tool()
    async def close_tab(instance: str, tab_id: int | None = None,
                        tab_ids: list[int] | None = None,
                        expected_session: str | None = None) -> dict:
        """Close ONE tab (``tab_id``) or a LIST (``tab_ids``) in an instance (§6/#49).

        EXACTLY ONE of ``tab_id`` / ``tab_ids`` is required (both/neither, an empty
        ``tab_ids``, or duplicate ids => ``invalid_args``, nothing sent). The single form
        returns ``{result}`` unchanged; the list returns ``{results:[{index, ok, tabId?,
        error?, message?}]}`` matched by ``index``. Bulk applies the SAME guards as single
        (today none). ``timeout``/``no_connection`` on the LIST is UNKNOWN — the truth is
        the next ``list_tabs``; do NOT blindly retry the whole list."""
        return await _guarded(tools.close_tab(
            _host(), instance=instance, tab_id=tab_id, tab_ids=tab_ids,
            auth_ctx=current_mcp_session(), expected_session=expected_session,
        ))

    @mcp.tool()
    async def focus_tab(instance: str, tab_id: int,
                        expected_session: str | None = None) -> dict:
        """Focus a tab and raise its window (§6/§10)."""
        return await _guarded(tools.focus_tab(
            _host(), instance=instance, tab_id=tab_id, auth_ctx=current_mcp_session(),
            expected_session=expected_session,
        ))

    @mcp.tool()
    async def move_tab(
        instance: str, window_id: int | None, tab_id: int | None = None,
        tab_ids: list[int] | None = None, index: int | None = None,
        expected_session: str | None = None,
    ) -> dict:
        """Move ONE tab (``tab_id``) or a LIST (``tab_ids``) to ``window_id``/position
        inside one browser; omit index for the end (§6/§9/#49).

        EXACTLY ONE of ``tab_id`` / ``tab_ids`` is required (both/neither, empty, or
        duplicate ids => ``invalid_args``). The single form returns ``{result}`` unchanged;
        the list returns ``{results:[{index, ok, tabId?, windowId?, error?}]}`` — a pinned
        cross-window tab gets ``pinned_cross_window`` and stays, others move.

        Pass ``window_id=null`` to EXTRACT a SINGLE tab into a brand-new background window
        (#45); the response ``windowId`` is the created window's id. ``tab_ids`` WITH
        ``window_id=null`` is ``invalid_args`` — ``windows.create`` takes one tabId, so
        "one new window for all" is a different, unrequested op. Refuses with ``no_window``
        when a NAMED target is not a normal, non-fullscreen window. ``timeout``/
        ``no_connection`` on the LIST is UNKNOWN — re-read ``list_tabs``, do not blind-retry.
        """
        return await _guarded(tools.move_tab(
            _host(), instance=instance, tab_id=tab_id, tab_ids=tab_ids, window_id=window_id,
            index=index, auth_ctx=current_mcp_session(), expected_session=expected_session,
        ))

    @mcp.tool()
    async def merge_windows(instance: str, params: dict | None = None,
                            expected_session: str | None = None) -> dict:
        """Merge windows in an instance (§9)."""
        return await _guarded(tools.merge_windows(
            _host(), instance=instance, params=params, auth_ctx=current_mcp_session(),
            expected_session=expected_session,
        ))

    @mcp.tool()
    async def execute_js(
        instance: str, tab_id: int, code: str,
        world: str | None = None, url_at_exec: str | None = None,
        await_promise: bool = False, timeout_ms: int | None = None,
        max_bytes: int | None = None,
        expected_session: str | None = None,
    ) -> dict:
        """Run JS in a tab (§12: audited before send, gated by the extension-edge checkbox).

        A promise is awaited on EITHER path, with or without the flag: ``fetch(u).then(r =>
        r.json())`` resolves to the parsed body, not to a promise.

        ``await_promise=true`` is what you reach for to write the two KEYWORDS indirect eval
        cannot parse — a top-level ``await`` and a top-level ``return``. It is not ONLY
        that: the flag also re-parses the snippet as an EXPRESSION, so a source that is
        ambiguous between a block and an object literal changes meaning — ``{a:1};`` answers
        ``1`` without the flag (eval reads a labelled block) and ``{"a": 1}`` with it.
        Exotic, and in your favour, but do not read the flag as "the same result plus two
        keywords". A single EXPRESSION still returns its value on either path, with or
        without a trailing ``;`` (``document.title`` and ``document.title;`` both answer the
        title); MULTI-STATEMENT code must ``return`` explicitly, or the value is null. Reach
        for the flag when the snippet wants to write ``await``/``return``, not as a default.

        ``timeout_ms`` raises this one command's budget (clamped to
        EXECUTE_JS_MAX_TIMEOUT_MS) for code that legitimately takes longer than
        CMD_TIMEOUT_MS.

        The result is FLAT: ``value`` is the main frame's result. ``frames`` appears only
        when the injection genuinely produced more than one — its absence means one frame,
        and ``value`` is it. A value chrome could not structured-clone (a DOM node, a
        function, a circular object) arrives as ``{__unserializable, preview}`` instead of a
        silent null. Payloads are capped at ``max_bytes`` (default 40000) with ``truncated``
        + ``total_bytes`` — but that cut happens on the SERVICE, after the whole value has
        crossed the socket: it protects your context, not the wire. Unlike get_text, this
        cap does not reach the page, so a snippet that can return less should return less."""
        return await _guarded(tools.execute_js(
            _host(), instance=instance, tab_id=tab_id, code=code, world=world,
            url_at_exec=url_at_exec, await_promise=await_promise, timeout_ms=timeout_ms,
            max_bytes=max_bytes, auth_ctx=current_mcp_session(),
            expected_session=expected_session,
        ))

    @mcp.tool()
    async def get_text(instance: str, tab_id: int, selector: str | None = None,
                       max_bytes: int | None = None,
                       expected_session: str | None = None) -> dict:
        """Read a tab's visible text — ``innerText`` of ``selector`` (or of the whole body).

        A FIXED injected function, so — unlike execute_js — it needs NO execute_js checkbox
        and writes no js_audit row; the http/https target guard still applies. A
        ``selector`` matching nothing is ``precondition_failed``, not an empty string.
        Capped at ``max_bytes`` (default 40000), reporting ``truncated`` + ``total_bytes``."""
        return await _guarded(tools.get_text(
            _host(), instance=instance, tab_id=tab_id, selector=selector,
            max_bytes=max_bytes, auth_ctx=current_mcp_session(),
            expected_session=expected_session,
        ))

    @mcp.tool()
    async def set_input(instance: str, tab_id: int, selector: str, value: str,
                        expected_session: str | None = None) -> dict:
        """Set a controlled (React/Vue) field's value in ONE call; answers ``{ok, kind}``.

        A FIXED injected function, so — unlike execute_js — it needs NO execute_js checkbox
        and writes no js_audit row (``selector`` and ``value`` are DATA, not source). But it
        is a WRITE — the value lands in the field as if the user typed it — so it IS gated by
        the pause/stop switch, like the other mutating verbs; the http/https target guard
        applies too.

        The extension writes through the element's NATIVE prototype value setter and fires a
        bubbling ``input`` (plus ``change``), which is what makes a controlled React/Vue input
        actually see the value — a plain ``el.value = …`` is reverted by React's value-tracker.
        ``kind`` is ``"input"`` for an ``<input>`` / ``<textarea>`` and ``"contenteditable"``
        for a contenteditable element. HONEST LIMIT: set_input drives TEXT-LIKE fields — text-type
        ``<input>`` (text, email, password, search, url, number, date, hidden, …), ``<textarea>``,
        and ordinary contenteditable / textbox composers. It does NOT drive ``checkbox`` / ``radio``
        (their state is ``checked``, not ``value``), ``file`` (the native setter throws), or the
        button subtypes (submit / reset / button / image) — those are ``precondition_failed`` — and
        it does not drive rich editors (Slate / ProseMirror / Draft), which keep their model
        separate from the DOM and may discard a contenteditable write. A ``selector`` matching
        nothing, not parsing, matching a non-editable element, or matching an unsupported
        ``<input>`` subtype is ``precondition_failed``."""
        return await _guarded(tools.set_input(
            _host(), instance=instance, tab_id=tab_id, selector=selector, value=value,
            auth_ctx=current_mcp_session(), expected_session=expected_session,
        ))

    @mcp.tool()
    async def wait_for(instance: str, tab_id: int, url_matches: str | None = None,
                       selector: str | None = None, text_contains: str | None = None,
                       timeout_ms: int | None = None,
                       expected_session: str | None = None) -> dict:
        """Wait until a page condition holds; answers ``{ok, matched, elapsed_ms}``.

        A deadline that passes is ``matched: false`` — a SUCCESS carrying a negative
        verdict, not an error: the browser answered, the condition simply never became
        true. A ``timeout`` error here means the opposite and keeps its §11 meaning: no
        response arrived at all, so the state is UNKNOWN and must not be blindly retried.

        EXACTLY ONE of ``url_matches`` (substring of the live tab url — no injection at
        all), ``selector`` (matches in the page) or ``text_contains`` (substring of the
        body text). Zero or several is ``invalid_args`` and nothing is polled.

        ``timeout_ms`` defaults to EXECUTE_JS_MAX_TIMEOUT_MS and is clamped to it. Like
        get_text this injects a FIXED function, so no execute_js checkbox is needed."""
        return await _guarded(tools.wait_for(
            _host(), instance=instance, tab_id=tab_id, url_matches=url_matches,
            selector=selector, text_contains=text_contains, timeout_ms=timeout_ms,
            auth_ctx=current_mcp_session(), expected_session=expected_session,
        ))

    @mcp.tool()
    async def scroll_until(instance: str, tab_id: int, count_selector: str,
                           container_selector: str | None = None, direction: str = "down",
                           target_count: int | None = None, stable_rounds: int = 3,
                           interval_ms: int = 700, timeout_ms: int | None = None,
                           focus: bool = False,
                           expected_session: str | None = None) -> dict:
        """Scroll a tab until the ``count_selector`` match count stops growing.

        For loading an infinite feed / chat backlog before reading it. A FIXED injected
        function, so — unlike execute_js — NO execute_js checkbox and no js_audit row; the
        http/https target guard still applies. The scroll loop runs in the extension worker
        (a fresh inject every ``interval_ms``), so a background tab throttling its timers does
        not stall it.

        ``count_selector`` is the progress metric (how many items are loaded).
        ``container_selector`` is what to scroll (omit for the whole document/window).
        ``direction`` ``"down"`` (default, an ordinary feed growing off the bottom) or
        ``"up"`` (a chat/history that pulls OLDER items in at the top).

        Stops with ``stopped`` = ``"stable"`` (``stable_rounds`` steps with no growth, default
        3), ``"target"`` (``count >= target_count``), or ``"deadline"``. Answers ``{ok, count,
        rounds, stopped, elapsed_ms}``.

        ``focus`` (default false) activates the tab and raises its window first. Reach for it
        only when a background scroll stays flat: feeds built on IntersectionObserver do not
        load in a tab that is not on screen, so their count never grows — but focusing TAKES
        THE SCREEN from the human, which is why it is opt-in. ``timeout_ms`` defaults to
        EXECUTE_JS_MAX_TIMEOUT_MS and is clamped to it."""
        return await _guarded(tools.scroll_until(
            _host(), instance=instance, tab_id=tab_id, count_selector=count_selector,
            container_selector=container_selector, direction=direction,
            target_count=target_count, stable_rounds=stable_rounds, interval_ms=interval_ms,
            timeout_ms=timeout_ms, focus=focus, auth_ctx=current_mcp_session(),
            expected_session=expected_session,
        ))

    @mcp.tool()
    async def start_js(instance: str, tab_id: int, code: str,
                       world: str | None = None,
                       url_at_exec: str | None = None,
                       expected_session: str | None = None) -> dict:
        """Fire JS into a tab as a background JOB; answers ``{ok, job_id}`` at once.

        For code that legitimately outlives a single command (a long scrape, a slow fetch
        chain) without holding the socket open. ARBITRARY code, so gated EXACTLY like
        execute_js: audited before send, gated by the extension-edge checkbox (§12).
        Refused while paused.

        The code is wrapped fire-and-forget: it runs on in the page after this returns, and
        stashes its outcome under a page global keyed by ``job_id``. Read it with
        ``poll_job(instance, tab_id, job_id)`` — ``state`` walks ``running`` -> ``done`` (with
        ``value``) or ``error`` (with ``message``).

        The gate stops NEW starts, not a RUNNING job: the checkbox and pause refuse the
        next start_js/execute_js but cannot abort a job already firing in the page (§12) — a
        wider surface than execute_js. The job always runs as an awaited async body, so its
        audit row always carries ``awaitPromise:true`` (there is no non-await path here).

        HONEST LIMIT: the job state lives IN THE PAGE and dies with the tab — a reload,
        discard, or close loses it and ``poll_job`` then reports ``state:"unknown"``. This is
        an ergonomic pattern, not a durable job queue. Records also GROW: each job lingers
        under its id in ``window.__curatorJobs`` until that navigation/reload (poll_job does
        not consume them), so re-open a long-lived tab running many jobs. ``world`` (MAIN
        default) is the world the code and its job global live in; poll it in the same
        world."""
        return await _guarded(tools.start_js(
            _host(), instance=instance, tab_id=tab_id, code=code, world=world,
            url_at_exec=url_at_exec,
            auth_ctx=current_mcp_session(), expected_session=expected_session,
        ))

    @mcp.tool()
    async def poll_job(instance: str, tab_id: int, job_id: str,
                       world: str | None = None, max_bytes: int | None = None,
                       expected_session: str | None = None) -> dict:
        """Read a ``start_js`` job's state — ``{ok, state, value?, message?}``.

        A FIXED read, so — unlike execute_js — NO checkbox and no js_audit row; the
        http/https guard still applies. ``state`` is ``running`` | ``done`` | ``error`` |
        ``unknown``. ``unknown`` means the page-resident state is GONE (the tab
        reloaded/discarded/closed, or the id is wrong) — a DIFFERENT fact from ``running``, so
        do not read it as "still working". ``world`` (MAIN default) must match the world the
        job lives in: a job started with ``world="ISOLATED"`` must be polled with the same
        ``world``, or the MAIN-default read finds a different global and reports ``unknown``.
        ``value`` (on ``done``) is capped at ``max_bytes`` (default 40000) with ``truncated``
        + ``total_bytes``; ``message`` (on ``error``) is the error string."""
        return await _guarded(tools.poll_job(
            _host(), instance=instance, tab_id=tab_id, job_id=job_id, world=world,
            max_bytes=max_bytes,
            auth_ctx=current_mcp_session(), expected_session=expected_session,
        ))

    @mcp.tool()
    async def set_focus_emulation(instance: str, tab_id: int, enabled: bool,
                                  expected_session: str | None = None) -> dict:
        """Toggle focus emulation on a tab via chrome.debugger; answers ``{ok, enabled}``.

        Makes a BACKGROUND tab behave as focused — no timer throttling — WITHOUT taking the
        screen from the human. The emulation HOLDS ONLY WHILE the debugger is attached, so
        ``enabled=true`` attaches the debugger and keeps it attached, and ``enabled=false``
        turns it off and detaches. While it is on, the browser shows its «идёт отладка» bar
        (and the attached state is detectable by anti-bot systems — the cost of the debugger
        path).

        Gated by the SINGLE JS & Debugger checkbox (``allow_execute_js`` in list_instances),
        exactly like execute_js — but it runs no arbitrary code and writes no js_audit row
        (it only fakes focus). A tab with DevTools open, or already held by another debugger
        client, cannot be attached and answers ``debugger_attach`` (one debugger client per
        tab). MUTUALLY EXCLUSIVE with ws capture in BOTH directions: ``enabled=true`` on a tab
        under an active start_ws_capture answers ``debugger_attach``, and ``enabled=false`` never
        detaches a live capture. Refused while paused."""
        return await _guarded(tools.set_focus_emulation(
            _host(), instance=instance, tab_id=tab_id, enabled=enabled,
            auth_ctx=current_mcp_session(), expected_session=expected_session,
        ))

    # --- WebSocket-frame capture (§12, wave 21) ------------------------------
    @mcp.tool()
    async def start_ws_capture(instance: str, tab_id: int,
                               expected_session: str | None = None) -> dict:
        """Start capturing a tab's WebSocket frames via chrome.debugger; answers ``{ok}``.

        The first DATA-BEARING verb down the CDP path: it opens a read channel onto the tab's WS
        traffic (a messenger's live conversation) for ``read_ws_frames`` to drain. Gated by the
        SINGLE JS & Debugger checkbox (``allow_execute_js`` in list_instances) EXACTLY like
        execute_js, and — unlike set_focus_emulation — it writes a js_audit row before the send
        (the durable trace of WHO opened the channel, WHEN and on which tab). Refused while paused.

        MUTUALLY EXCLUSIVE with set_focus_emulation on the same tab — one debugger client per tab —
        so a tab already under focus emulation or an active capture answers ``debugger_attach``. The
        exclusion is SYMMETRIC: set_focus_emulation likewise refuses a tab this capture holds.
        ANTI-BOT COST: ``Network.enable`` is detectable and the «идёт отладка» bar shows for the
        WHOLE time the capture stays open, not just an instant — the exposure window is the entire
        read session, so stop it when done."""
        return await _guarded(tools.start_ws_capture(
            _host(), instance=instance, tab_id=tab_id,
            auth_ctx=current_mcp_session(), expected_session=expected_session,
        ))

    @mcp.tool()
    async def read_ws_frames(instance: str, tab_id: int, max_bytes: int | None = None,
                             expected_session: str | None = None) -> dict:
        """Drain a tab's captured WS frames — ``{ok, frames, dropped, url, remaining}``.

        A FIXED, DRAINING read of the buffer ``start_ws_capture`` already authorised: no second
        js_audit row, but DATA-BEARING — the frames carry personal data (phones, sums, addresses)
        into your context AND the session transcript, which outlives the task. Read only what you
        need. NOT refused while paused — unlike start_ws_capture it never touches the browser; it is
        a passive drain of the in-memory buffer (like list_exemptions), allowed under a stop so an
        already-captured conversation is not lost to ring eviction while the capture is still open.

        DRAINING: returned frames are REMOVED, so a repeat read yields only NEW frames (a stream).
        Capped at ``max_bytes`` (default 40000) of summed text payload; frames past the budget stay
        buffered as the tail — never dropped — and ``remaining`` counts them so you know to read
        again. ``dropped`` is how many frames the ring evicted on overflow since the last read
        (then reset). ``url`` is the socket URL (``None`` until the socket is seen). Each frame is
        ``{dir, opcode, ts, text}`` for text (opcode 1) or ``{dir, opcode, ts, size, binary}`` for
        binary/control frames (payload not captured). A tab with no active capture is
        ``precondition_failed``."""
        return await _guarded(tools.read_ws_frames(
            _host(), instance=instance, tab_id=tab_id, max_bytes=max_bytes,
            auth_ctx=current_mcp_session(), expected_session=expected_session,
        ))

    @mcp.tool()
    async def stop_ws_capture(instance: str, tab_id: int,
                              expected_session: str | None = None) -> dict:
        """Stop a tab's WS capture — best-effort ``Network.disable`` + detach; answers ``{ok}``.

        Pure IDEMPOTENT teardown: disables the Network domain, detaches the debugger, drops the
        buffer. A tab with no active capture is an idempotent ``{ok: true}``. NOT gated by the
        checkbox and NOT refused while paused — teardown must always be able to run so the «идёт
        отладка» bar and the anti-bot exposure can always be ended. The buffer lives in the MV3
        worker, so a worker death loses the capture on its own and a later stop is the no-op."""
        return await _guarded(tools.stop_ws_capture(
            _host(), instance=instance, tab_id=tab_id,
            auth_ctx=current_mcp_session(), expected_session=expected_session,
        ))

    # --- exemptions: the agent's «не трогать» lease (§10/§11) ----------------
    @mcp.tool()
    async def list_exemptions(instance: str | None = None,
                              include_expired: bool = False) -> dict:
        """List active «do not touch» exemptions the curator pass honours, optionally for
        one instance. ``include_expired`` also returns lapsed rows."""
        return await _guarded(tools.list_exemptions(
            _host(), instance=instance, include_expired=include_expired,
        ))

    @mcp.tool()
    async def set_exemption(instance: str, url: str, ttl_s: int,
                            reason: str | None = None) -> dict:
        """Protect ``instance`` + ``url`` from the curator pass for ``ttl_s`` seconds.

        Keyed by (instance, url), so a repeat call REFRESHES the deadline. Clamped to the
        30-day ceiling — an exemption is never infinite."""
        return await _guarded(tools.set_exemption(
            _host(), instance=instance, url=url, ttl_s=ttl_s, reason=reason,
        ))

    @mcp.tool()
    async def clear_exemption(instance: str, url: str) -> dict:
        """Lift an exemption. Idempotent: ``deleted: 0`` when it was already gone."""
        return await _guarded(tools.clear_exemption(_host(), instance=instance, url=url))

    @mcp.tool()
    async def relocate_tab(instance_from: str, instance_to: str,
                           tab_id: int | None = None, tab_ids: list[int] | None = None,
                           expected_session_from: str | None = None) -> dict:
        """Relocate ONE tab (``tab_id``) or a LIST (``tab_ids``) from ``instance_from`` to
        ``instance_to``, completed synchronously (#48/#49): open the copy in the target and
        close the source under §7's step-4 guards. Returns ``status:"done"`` on success, or
        ``status:"half"`` (with a ``reason``) when the source close cannot be completed —
        today's phase-A-only outcome, which the pass's phase B finishes later.

        EXACTLY ONE of ``tab_id`` / ``tab_ids`` is required (both/neither, empty, or
        duplicate ids => ``invalid_args``). The single form returns the #48 shape; the list
        returns ``{results:[{index, ok, status?, reason?, tab_id_to?, error?}]}`` and ONE
        shared ``undo_pass_id`` (``mcp-<uuid>``) that reverses the WHOLE batch as a unit
        (undo counts its units and requires ``confirm_impact`` like a pass). Duplicate
        addresses are DEDUPED within the batch and against what the target already holds —
        a dropped item gets ``error:"duplicate"`` (else N sources of one url would make N
        permanent copies). ``undo_pass_id`` (``mcp-<uuid>``) reverses the relocation.

        ``expected_session_from`` pins the SOURCE epoch (#47): a restarted source browser
        refuses the relocation before any copy is opened, and the same epoch is stamped on
        the synchronous source close. The target open/get_tab are never session-pinned — a
        copy in a restarted target is correct, not dangerous. ``timeout``/``no_connection``
        on the LIST is UNKNOWN — re-read ``list_tabs``, do not blind-retry the batch.
        """
        return await _guarded(tools.relocate_tab(
            _host(), instance_from=instance_from, tab_id=tab_id, tab_ids=tab_ids,
            instance_to=instance_to, auth_ctx=current_mcp_session(),
            expected_session_from=expected_session_from,
        ))

    # --- pass + pause --------------------------------------------------------
    @mcp.tool()
    async def run_pass(dry_run: bool = False) -> dict:
        """Trigger one curator pass; dry_run returns the plan without writing (§7)."""
        return await _guarded(tools.run_pass(_host(), dry_run=dry_run))

    @mcp.tool()
    async def pause(minutes: int | None = None) -> dict:
        """Stop the curator indefinitely (writes curator_stopped_at + bumps the epoch,
        §7/§12). `minutes` is accepted for backward compatibility and ignored — the
        stop lasts until `resume`."""
        return await _guarded(tools.pause(_host(), minutes=minutes))

    @mcp.tool()
    async def resume() -> dict:
        """Start the curator (clears curator_stopped_at, shifts TTL protections, then
        runs a confirming pass — which also executes an armed over-threshold plan,
        §7/§12)."""
        return await _guarded(tools.resume(_host()))

    # Create the session manager LAZILY, ONCE, BEFORE the lifespan (§11 trap #1).
    # DNS-rebinding protection off — see the module docstring.
    mcp.streamable_http_app(
        streamable_http_path="/mcp",
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return mcp


# --- the auth-gated ASGI handler + its Route --------------------------------
class _MCPAsgi:
    """ASGI app: enforce the ``ADMIN_TOKEN`` Bearer, bind the MCP session id, then hand
    off to the streamable-HTTP handler. A class instance (not a bare function) so a
    Starlette ``Route`` treats it as an ASGI app rather than a request endpoint."""

    def __init__(self, session_manager) -> None:
        # The SAME ASGI handler the SDK's own streamable_http_app wraps in its Route.
        self._asgi = StreamableHTTPASGIApp(session_manager)

    async def __call__(self, scope, receive, send) -> None:
        headers = dict(scope.get("headers") or [])
        settings = scope["app"].state.settings
        auth = headers.get(b"authorization", b"").decode("latin-1")
        scheme, _, token = auth.partition(" ")
        # Constant-time compare, as bytes, against ADMIN_TOKEN (issue #35 §4: the MCP
        # agent authenticates as the human/admin, not with a per-instance secret).
        if scheme.lower() != "bearer" or not secrets.compare_digest(
            token.encode("utf-8", "ignore"), settings.admin_token.encode("utf-8")
        ):
            # Count the rejection for curator_auth_rejections_total (§12).
            auth_rejections.incr("mcp_token")
            await PlainTextResponse("missing or invalid bearer token", status_code=401)(
                scope, receive, send
            )
            return
        # Bind the streamable-HTTP session id for this request so execute_js can record
        # it as auth_ctx (§12). Absent on the initialize request; present thereafter.
        session_id = headers.get(b"mcp-session-id", b"").decode("latin-1") or None
        reset = _mcp_session.set(session_id)
        try:
            await self._asgi(scope, receive, send)
        finally:
            _mcp_session.reset(reset)


def mcp_route(mcp: MCPServer) -> Route:
    """The single host-app route for the MCP endpoint — path exactly ``/mcp``."""
    return Route(
        "/mcp",
        _MCPAsgi(mcp.session_manager),
        methods=["GET", "POST", "DELETE"],
    )
