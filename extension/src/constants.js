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
// old one ONLY after the server approves it (unknown_instance never wipes — see
// connection.js), which is why the two keys are distinct.
export const INSTANCE_SECRET_KEY = "instanceSecret"; // storage.local — the active secret (hex)
export const INSTANCE_SECRET_PENDING_KEY = "instanceSecretPending"; // storage.local — re-enroll secret
// The server-ASSIGNED instance id, learned from a successful hello_ack (the client no
// longer self-reports a trusted id, §2). Durable so the popup/startpage can name the
// rule target + filter own tabs even while the MV3 worker is cold.
export const INSTANCE_ID_KEY = "instanceId"; // storage.local — server-assigned id
// Operator-entered settings that USED to live in instance.json. The address and the
// browser name are per-profile now (a universal build has no generator to stamp them),
// and the shared token is gone entirely (§7). The enroll CODE is the ~10-min window
// code the operator reads off /admin and types once to submit an enrollment.
export const SERVICE_ADDRESS_KEY = "serviceAddress"; // storage.local — wss/ws service URL
export const BROWSER_NAME_KEY = "browserName"; // storage.local — suggested_title source
export const ENROLL_CODE_KEY = "enrollCode"; // storage.local — the window code (transient input)
// Durable enrollment facts, the SINGLE source getEnrollState reads (the in-memory
// helloAcked is useless at page open — the worker is cold). Shape:
//   { requestPending: bool, approved: bool, quarantined: bool, lastVerdict: str|null }
export const ENROLL_STATE_KEY = "enrollState"; // storage.local — durable enroll facts

// The five enroll states getEnrollState resolves to (§7). Only these are authoritative
// from the SW branch; actual connectivity stays with /api/state on the startpage.
export const ENROLL_NEEDS = "needs-enroll"; // no secret yet
export const ENROLL_PENDING = "pending"; // request submitted, awaiting approval
export const ENROLL_APPROVED = "approved"; // a hello has succeeded at least once
export const ENROLL_REVOKED = "revoked"; // server said `revoked` — secret wiped
export const ENROLL_QUARANTINED = "quarantined"; // server said `unknown_instance` post-approval

// hello_ack{ok:false}.error.code verdicts the client ACTS on (§7). Mirror the
// service-side strings in src/ext/protocol.py (REJECT_REVOKED / REJECT_UNKNOWN).
export const VERDICT_REVOKED = "revoked";
export const VERDICT_UNKNOWN = "unknown_instance";
// The execute_js opt-in checkbox (§12): per-copy, default OFF, lives in
// chrome.storage.local (survives a session, is set from the options page). It is
// the AUTHORITATIVE runtime state — read fresh on every execute_js and reported
// in `hello` — so a copied instance.json cannot smuggle the gate open.
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
export const CMD_NAVIGATE_TAB = "navigate_tab";
export const CMD_MERGE_WINDOWS = "merge_windows";
export const CMD_EXECUTE_JS = "execute_js";

// --- Command error codes (§6) -----------------------------------------------
// The `error.code` a failing `response` carries. Mirrors the ERR_* strings in
// src/ext/protocol.py.
export const ERR_STALE_SESSION = "stale_session";
export const ERR_PRECONDITION_FAILED = "precondition_failed";
export const ERR_NO_SUCH_TAB = "no_such_tab";
export const ERR_NO_WINDOW = "no_window";
export const ERR_JS_DISABLED = "js_disabled";
export const ERR_BUSY_DRAGGING = "busy_dragging";
export const ERR_INTERNAL = "internal";
