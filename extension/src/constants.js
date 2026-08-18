// Extension-local constants (docs/architecture.md §5, §6).
//
// These are constants of the EXTENSION, not ENV of the service — none of their
// values is visible to the service and there is nothing to configure on its side
// (§6 "Heartbeat и переподключение", "Куратор не сбрасывает себе часы").

// The protocol version, compared for exact equality by the service (§6). A
// mismatch is a hard reject, never a silent downgrade.
export const PROTOCOL_VERSION = 1;

// Activity tick period. Also the reconnect alarm period: at 60 s the packed /
// unpacked service worker actually dies between ticks so the resurrection path
// is exercised; a 30 s alarm keeps the worker alive and leaves it untested (§6,
// row 42 of the measurements table). TICK_MS passes both the packed (30 s) and
// unpacked (1 s) alarm minimums.
export const TICK_MS = 60000;

// The idle threshold window (minutes). Doc-change marks older than this are
// dropped, both on write and during snapshot build; the self-navigation count is
// taken over this window (§5).
export const IDLE_MINUTES = 60;
export const IDLE_WINDOW_MS = IDLE_MINUTES * 60000;

// A tab that changes its document more than SELF_NAV_LIMIT times within
// IDLE_MINUTES is marked selfNavigating: for it only onActivated + the tick count
// as activity (§5 "Самонавигация активностью не считается"). The docChanges ring
// is SELF_NAV_LIMIT + 1 long so "more than the limit" is representable.
export const SELF_NAV_LIMIT = 10;
export const DOC_CHANGES_RING = SELF_NAV_LIMIT + 1;

// A curator close makes Chrome activate a neighbour; any onActivated in that
// window within this window after the curator wrote its cause is NOT counted as
// activity (§6 "Куратор не сбрасывает себе часы"). Extension constant, not ENV.
export const CURATOR_CAUSE_WINDOW_MS = 2000;

// Heartbeat interval the service uses; the "no ping for 2.5×" optimisation timer
// (deferred here) would build on it. Reconnect itself is alarm-driven (§6).
export const HEARTBEAT_MS = 15000;

// chrome.windows API sentinel for "no window focused".
export const WINDOW_ID_NONE = -1;

// Storage keys.
export const MAP_KEY = "activityMap"; // chrome.storage.session — the activity map
export const SESSION_ID_KEY = "sessionId"; // chrome.storage.session — the epoch
export const INSTALL_UUID_KEY = "installUuid"; // chrome.storage.local — survives sessions

// --- Enrollment (§7, issue #35) --------------------------------------------
// The per-install SECRET (32 random bytes, stored as hex) lives ONLY in this
// profile's chrome.storage.local — never in the bundle. The RAW secret hex goes on the
// wire over TLS (enroll_request + every hello, as `secret`) and as the /api Bearer; the
// SERVER hashes it into the stored sha256 (option A — a DB-only leak yields no usable
// credential). Absence of this key is the "needs-enroll" fact. When a quarantined instance
// re-enrolls, the
// FRESH secret is generated under INSTANCE_SECRET_PENDING_KEY and promoted over the
// old one ONLY after the server accepts it (unknown_instance never wipes — see
// connection.js), which is why the two keys are distinct.
export const INSTANCE_SECRET_KEY = "instanceSecret"; // storage.local — the active secret (hex)
export const INSTANCE_SECRET_PENDING_KEY = "instanceSecretPending"; // storage.local — re-enroll secret
// The server-ASSIGNED instance id, learned from a successful hello_ack (the client no
// longer self-reports a trusted id, §2). Durable so the popup/startpage can name the
// rule target + filter own tabs even while the MV3 worker is cold.
export const INSTANCE_ID_KEY = "instanceId"; // storage.local — server-assigned id
// Operator-entered settings that USED to live in instance.json. The address and the
// instance NAME are per-profile now (a universal build has no generator to stamp them),
// and the shared token is gone entirely (§7). The enroll CODE is the ~10-min window
// code the operator reads off /admin and types once to enrol.
export const SERVICE_ADDRESS_KEY = "serviceAddress"; // storage.local — wss/ws service URL
// The name this browser asks to be known by — and that is the WHOLE of it: the service
// has no separate display title anymore, so this value becomes the `instances` PRIMARY
// KEY verbatim on a successful enrol. Hence the charset below rather than free text: the
// id travels into URLs, metric labels and the operator console.
export const INSTANCE_NAME_KEY = "browserName"; // storage.local — the proposed instance_id
// The charset/length the service enforces (src/ext/protocol.py INSTANCE_ID_RE). Checked
// in the options page BEFORE sending so the refusal is immediate and local; the service
// re-checks it and answers `enroll_rejected{reason:"bad_id"}` for a client that skipped
// its own gate. Duplicated deliberately (the options page is loaded raw under the
// extension_pages CSP and imports nothing) — test/options.test.js runs both over the
// same table so they cannot drift.
export const INSTANCE_NAME_RE = /^[A-Za-z0-9._-]{1,64}$/;
export const ENROLL_CODE_KEY = "enrollCode"; // storage.local — the window code (transient input)

// There is deliberately no re-registration INTERVAL here, and no re-registration STATE
// either. An enroll_request is answered on the spot — `enroll_accepted` or
// `enroll_rejected` — so there is no window in which the client believes a request is
// waiting somewhere. It re-sends only while it still has a staged code and is not yet
// enrolled, which the alarm already covers; recovery from a closed window is an operator
// step (open a new one, type the new code), not a client retry.

// There is no INSTALL_UUID_PREFIX_LEN anymore. It sized the installUuid prefix the
// options page printed as "this install's identity", and that row is gone: /admin stopped
// printing the uuid when the pending-request list went away (§6), so the prefix had
// nothing left to be compared against and was one more line between the operator and the
// four fields that actually do something. installUuid itself is untouched — the SW still
// mints it (INSTALL_UUID_KEY above) and enroll_request still carries it.
// Durable enrollment facts, the SINGLE source getEnrollState reads (the in-memory
// helloAcked is useless at page open — the worker is cold). Shape:
//   { requestPending: bool, approved: bool, quarantined: bool, lastVerdict: str|null }
export const ENROLL_STATE_KEY = "enrollState"; // storage.local — durable enroll facts

// The four enroll states getEnrollState resolves to (§7). Only these are authoritative
// from the SW branch; actual connectivity stays with /api/state on the startpage.
//
// There is NO `pending` state. It meant "the service holds our request, an operator has
// yet to click Approve", and that step is gone: an enroll_request is accepted or refused
// on the spot, so the transition is needs-enroll → approved with nothing in between. A
// refusal leaves the state at needs-enroll and surfaces its REASON instead — which is the
// honest report, where "ожидает одобрения" over a refused attempt was not.
export const ENROLL_NEEDS = "needs-enroll"; // no secret yet, or the last attempt was refused
export const ENROLL_APPROVED = "approved"; // enrolled: the service assigned this id
export const ENROLL_REVOKED = "revoked"; // server said `revoked` — secret wiped
export const ENROLL_QUARANTINED = "quarantined"; // server said `unknown_instance` post-approval

// hello_ack{ok:false}.error.code verdicts the client ACTS on (§7). Mirror the
// service-side strings in src/ext/protocol.py (REJECT_REVOKED / REJECT_UNKNOWN).
export const VERDICT_REVOKED = "revoked";
export const VERDICT_UNKNOWN = "unknown_instance";
// The single JS & Debugger opt-in checkbox (§12): per-copy, default OFF, lives in
// chrome.storage.local (survives a session, is set from the options page). It is
// the AUTHORITATIVE runtime state — read fresh on every gated verb and reported in
// `hello` — so a copied instance.json cannot smuggle the gate open. ONE checkbox
// now gates BOTH execute_js/start_js AND the chrome.debugger (CDP) path (§12,
// set_focus_emulation and later slices): the storage KEY keeps its historical name
// `allowExecuteJs` on purpose — renaming it would reset every installed copy's
// checkbox to OFF, since the key is read per-copy from chrome.storage.local. The
// former separate `allowDebugger` key is gone (it gated nothing); migration v6 drops
// its mirror column service-side.
export const ALLOW_EXECUTE_JS_KEY = "allowExecuteJs";
// The last known /ext connection facts (§6 `get_connection_state`), kept in
// chrome.storage.session: an MV3 worker dies between events, so an in-memory-only
// `lastSeenAt` would read as "never" on every cold start — the startpage would show
// a healthy instance as never-connected. Session storage dies with the browser,
// which is exactly the lifetime of these facts.
export const CONNECTION_STATE_KEY = "connectionState";

// The reconnect alarm name.
export const RECONNECT_ALARM = "ext-reconnect";
export const TICK_ALARM = "ext-tick";

// --- Command verbs (§6 "Команды (сервис → расширение)") ---------------------
// The `command` field of a service->extension command frame. Mirrors the
// service-side CMD_* strings in src/ext/protocol.py — the two sides MUST agree.
export const CMD_OPEN_TAB = "open_tab";
export const CMD_CLOSE_TAB = "close_tab";
export const CMD_GET_TAB = "get_tab";
export const CMD_FOCUS_TAB = "focus_tab";
// Raise a window to the foreground by id, touching nothing inside it (§6).
export const CMD_FOCUS_WINDOW = "focus_window";
export const CMD_NAVIGATE_TAB = "navigate_tab";
export const CMD_MERGE_WINDOWS = "merge_windows";
export const CMD_EXECUTE_JS = "execute_js";
// Move ONE tab to a window/position inside THIS browser. Relocation BETWEEN
// browsers is an open+close pair (§7) — that works only because the instances are
// separate processes; between the windows of one instance there was nothing at
// all, though `chrome.tabs.move` has been in use here since merge_windows.
export const CMD_MOVE_TAB = "move_tab";
// Read a page's text, and wait for a page condition. Both inject a FIXED function that
// is committed into this bundle and known at build time — NOT arbitrary code — so
// neither is gated by the execute_js checkbox and neither writes a js_audit row (§12's
// argument is "усечённый код нереконструируем", and there is nothing to reconstruct
// here). Every OTHER gate still applies: session check, the service-side pause/stop and
// revoke checks, and the http/https edge guard on the target tab.
export const CMD_GET_TEXT = "get_text";
export const CMD_WAIT_FOR = "wait_for";
// Set the value of a controlled (React/Vue) input in ONE call: the NATIVE prototype value
// setter (which bypasses React's value-tracker — a plain `el.value = …` is reverted on the
// next render) followed by bubbling `input`/`change`, plus `contenteditable` coverage.
// FIXED-function like get_text/wait_for: `selector` and `value` are DATA — fed to
// querySelector and to the value assignment, never source spliced into a page eval — so it
// carries NO execute_js checkbox and writes NO js_audit row. But it is a MUTATION (it writes
// into the page AS THE USER), so the SERVICE side gates it behind the pause/stop switch, exactly
// like navigate_tab — as it gates every verb that reaches the browser, the read-only fixed ones
// (get_text/wait_for) included; the mutation is what sets set_input apart from those, not the gate.
export const CMD_SET_INPUT = "set_input";
// Scroll a tab until the element count for a selector stops growing (or a target /
// deadline is hit). FIXED-function like get_text/wait_for: its parameters are selectors
// and a direction — DATA fed to querySelector/scrollTop, never source spliced into a
// page eval — so it carries NO execute_js checkbox and writes NO js_audit row. The
// scheduler is the SERVICE WORKER's loop (a fresh chrome.scripting inject per step, like
// wait_for/pollUntil): pacing survives a page throttling its own timers.
export const CMD_SCROLL_UNTIL = "scroll_until";
// The Job-API pair (fire arbitrary code into a page global, then poll it):
//   - start_js carries ARBITRARY caller code, so it is behind the execute_js checkbox +
//     a js_audit row, EXACTLY like execute_js (§12). It wraps the code
//     in an async IIFE that stashes {state,value|message} under window.__curatorJobs[jobId]
//     and returns {jobId} WITHOUT awaiting the promise.
//   - poll_job is a FIXED read of that global (readJobInWorld takes only the jobId as
//     DATA), so — like get_text — no checkbox, no audit row. Job state lives IN THE PAGE
//     and dies with the tab (reload/discard/close): ergonomics, not durability.
export const CMD_START_JS = "start_js";
export const CMD_POLL_JOB = "poll_job";
// The first verb down the chrome.debugger (CDP) path (§12, wave 18). It toggles
// `Emulation.setFocusEmulationEnabled`, which makes a BACKGROUND tab behave as focused
// (no timer throttling) WITHOUT taking the screen from the human. STATEFUL by nature: the
// emulation holds ONLY while the debugger stays attached, so enable = attach + command +
// KEEP attached, and disable = command(false) + detach. Gated by the single JS & Debugger
// checkbox (ALLOW_EXECUTE_JS_KEY) at the edge. It carries NO arbitrary code and writes NO
// js_audit row — focus emulation exfiltrates nothing, it only fakes focus; a later
// data-bearing CDP verb (screenshot / network) needs its own audit.
export const CMD_SET_FOCUS_EMULATION = "set_focus_emulation";
// WebSocket-frame capture (§12, wave 21) — the FIRST data-bearing verb down the
// chrome.debugger path. `start_ws_capture` attaches the debugger and turns on `Network.*`
// event delivery so the extension buffers a tab's WS frames; `read_ws_frames` drains that
// buffer to the agent; `stop_ws_capture` disables the domain and detaches. Because the
// frames carry PAGE DATA out (a messenger's conversation — phones, sums, addresses), start
// is gated by the SAME single JS & Debugger checkbox as execute_js AND writes a js_audit
// row (unlike set_focus_emulation, which fakes focus and exfiltrates nothing). read is a
// FIXED, draining read of the already-authorised buffer (no second audit) but is size-capped
// for privacy; stop is pure idempotent teardown. One debugger client per tab — a tab held by
// focus emulation OR an active capture refuses a second `start_ws_capture` (`debugger_attach`).
export const CMD_START_WS_CAPTURE = "start_ws_capture";
export const CMD_READ_WS_FRAMES = "read_ws_frames";
export const CMD_STOP_WS_CAPTURE = "stop_ws_capture";
// Wake a DISCARDED tab (issue #68): the browser unloaded it from memory to save RAM, so
// every injecting/attaching verb answers the opaque "Cannot access contents of the page.
// Extension manifest must request permission…" — a message that blames the manifest for a
// tab that merely needs reloading. wake_tab reloads it (a reload re-materialises a discarded
// tab) and waits for `status:complete`. A FIXED action, not arbitrary code — no execute_js
// checkbox, no js_audit row (like navigate_tab). Answers {wasDiscarded} so the caller learns
// whether it was a real wake or a plain reload of an already-live tab — wake_tab ALWAYS reloads,
// so on a live tab it loses page state, it is not a no-op.
export const CMD_WAKE_TAB = "wake_tab";

// How often `wait_for` (and navigate_tab's waitUntil) re-tests its condition. 250 ms is
// the usual "fast enough to feel instant, cheap enough to run for 30 s" compromise: at
// the 30 s ceiling that is ~120 polls, each one `chrome.tabs.get` or one injected
// one-liner. The SERVICE budget for such a command must exceed the poll deadline, or the
// command times out on the wire before the page condition can resolve — see
// src/mcpiface/tools.py `_wait_budget_ms`.
export const WAIT_POLL_MS = 250;

// How many CONSECUTIVE polls of "nothing here suggests a navigation at all" end
// navigate_tab's commit gate anyway (see `navigationCommitted` in commands.js for the hole
// this closes). Expressed in polls, not milliseconds, because it is a count of OBSERVATIONS
// the browser failed to produce, not a duration. Three is deliberate: one `tabs.get` round
// trip is all a browser needs to expose `pendingUrl` or a `loading` status, so a navigation
// that has really started shows itself long before the third one — while three quiet polls
// (750 ms at WAIT_POLL_MS) stay a rounding error against the 30 s the caller may budget.
//
// ⚠️ THAT LAST CLAIM IS ABOUT THE DEFAULT, NOT ABOUT EVERY CALLER: a SHORT `timeoutMs`
// removes the grace silently. The gate opens on the THIRD poll and the first sits behind the
// 250 ms pre-pause, so a deadline of 2×WAIT_POLL_MS or less can never reach it — measured,
// 500 ms answers `matched:false` where 501 ms answers `matched:true` — and a deadline under
// about four intervals leaves the CONDITION one or two polls once the gate does open (at
// 999 ms it is tested at 750 and again at 999, the last sleep clamped to what remains). A
// caller passing 300 is therefore back to the pre-grace behaviour, which is why
// navigate_tab's MCP description says so too: the caller who picks the number is the one who
// needs to know.
export const WAIT_COMMIT_GRACE_POLLS = 3;

// The extension's OWN ceiling on how long a wait may occupy this worker, independent of
// whatever `timeoutMs` the service sends. A worker parked in a poll loop is a worker not
// running its tick, and a wait longer than a minute would straddle a TICK_MS period.
//
// This is the HARD ceiling of the pair: the operator's EXECUTE_JS_MAX_TIMEOUT_MS (30 s by
// default) is the knob, and `Settings` refuses a value above this number rather than
// accepting it and quietly delivering 60 s — a config that silently means something else
// than it says is worse than one that fails at startup. `tests/test_settings.py` pins the
// two together the way `tests/test_ext_protocol.py` pins the CMD_/ERR_ strings, since
// again there is no shared artifact the two languages could import.
export const WAIT_MAX_TIMEOUT_MS = 60000;

// --- Command error codes (§6) -----------------------------------------------
// The `error.code` a failing `response` carries. Mirrors the ERR_* strings in
// src/ext/protocol.py.
export const ERR_STALE_SESSION = "stale_session";
export const ERR_PRECONDITION_FAILED = "precondition_failed";
export const ERR_NO_SUCH_TAB = "no_such_tab";
// The target tab EXISTS but the browser has DISCARDED it — unloaded it from memory to save
// RAM (issue #68). It still answers `chrome.tabs.get` (with `discarded:true` and its url), so
// it is NOT `no_such_tab`, and its url is usually http/https, so it is NOT the `precondition_failed`
// "wrong scheme" either. Yet every injecting/attaching verb fails on it with the opaque "Extension
// manifest must request permission…" — a message that blames the manifest for a tab that only needs
// waking. Its OWN code so the agent stops fixing the manifest and calls wake_tab (or navigate_tab to
// its url) instead. Read BEFORE the injection from `chrome.tabs.get(tabId).discarded`.
export const ERR_TAB_DISCARDED = "tab_discarded";
export const ERR_NO_WINDOW = "no_window";
export const ERR_JS_DISABLED = "js_disabled";
export const ERR_BUSY_DRAGGING = "busy_dragging";
// move_tab refused because the tab is PINNED and the move would cross a window
// boundary (§9). Its own code rather than `precondition_failed`: the agent must be
// able to tell "the owner's do-not-touch shield stopped me, unpin it or move it
// inside its own window" apart from every other precondition, and act on it
// without parsing a message string.
export const ERR_PINNED_CROSS_WINDOW = "pinned_cross_window";
// set_focus_emulation could not attach the debugger to the target tab (§12): DevTools is
// open on it, or another debugger client is already attached — a tab takes ONE debugger
// client. Its own code so the agent can tell "unattachable tab" from every other
// precondition without parsing a message string.
export const ERR_DEBUGGER_ATTACH = "debugger_attach";
// There is deliberately NO extension-side `timeout` code. A waiting verb that reaches its
// deadline answers `ok:true` with `{matched:false, elapsedMs}` — a definite negative from a
// browser that is plainly alive. `timeout` (src/ext/protocol.py) stays what §11 defines it
// as: the SERVICE saying no frame arrived at all, i.e. the state is UNKNOWN and must not be
// blindly retried. One string, one meaning, one producer.
export const ERR_INTERNAL = "internal";
