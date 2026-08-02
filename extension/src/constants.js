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

// The reconnect alarm name.
export const RECONNECT_ALARM = "ext-reconnect";
export const TICK_ALARM = "ext-tick";
