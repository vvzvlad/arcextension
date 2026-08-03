// The service address gate (§7). The address is an operator SETTING (a universal
// bundle has no generator to stamp it), and it decides the transport for every
// credential this extension owns.
//
// WHY THIS IS A GATE AND NOT A HINT. The credential model is option A: the client sends
// the RAW instance secret and the server hashes it. That raw secret goes on the wire in
// the enroll_request, in EVERY `hello` (once a minute, forever) and as the `/api` Bearer
// of the startpage and the popup — and the ONLY thing hiding it is TLS. A typo'd
// `ws://curator.lan:8000` therefore does not degrade the system, it publishes a
// permanent credential (plus the one-time window code) to every passive listener on the
// segment, while every screen keeps saying "connected". Nothing downstream can detect
// that, so the scheme is refused HERE, before the first frame.
//
// `ws://` stays legal for loopback ONLY, because a developer running the service on
// localhost has no certificate and the traffic never leaves the machine.
//
// SINCE THE ONLY ACCEPTABLE SCHEME IS DERIVABLE, THE OPERATOR DOES NOT TYPE IT.
// A scheme-less address is normalized here — `curator.example[:8443]` becomes
// `wss://curator.example[:8443]`, and a loopback host becomes `ws://…` (the same
// development exception the gate below already makes) — so the field can be filled in
// with just the address of the service. Normalization is part of THIS module on
// purpose: it decides the scheme, and the scheme is the security decision, so it must
// not be re-derived by any UI. An address that already carries a scheme is passed
// through untouched, which is what keeps every stored `wss://…` working unchanged and
// keeps an explicit `http://` / non-loopback `ws://` refused exactly as before.
//
// KEEP IN SYNC with the copy in extension/pages/options.js: that page is loaded raw
// under the extension_pages CSP and deliberately imports nothing (see its header), so
// the check is duplicated across the two build contexts. test/options.test.js pins the
// two implementations to the SAME tables of inputs — mirror any change into both.

// Hosts on which plaintext ws:// is tolerated. `new URL()` normalises an IPv6 host to
// its bracketed form, which is why "[::1]" is spelled with brackets here.
export const LOOPBACK_HOSTS = ["localhost", "127.0.0.1", "[::1]"];

// "<scheme>://" at the start. The `//` matters: without it `curator.example:8000`
// would look like the scheme "curator.example:" — which is exactly how a scheme-less
// host:port used to fall through to `malformed`.
const HAS_SCHEME = /^[a-z][a-z0-9+.-]*:\/\//i;

// Add the scheme the gate would accept for this host, when the operator left it out.
// Returns the address to store/dial; the gate below still has the final say (it is
// called on the result, and a normalized address that is still unacceptable — an
// explicit `http://`, a non-loopback `ws://`, an unparseable host — is refused with
// the same code as before).
export function normalizeServiceAddress(raw) {
  const value = typeof raw === "string" ? raw.trim() : "";
  if (!value) return "";
  if (HAS_SCHEME.test(value)) return value; // explicit scheme: never rewritten
  const bare = value.replace(/^\/+/, ""); // tolerate a protocol-relative "//host"
  let hostname;
  try {
    hostname = new URL("wss://" + bare).hostname;
  } catch {
    return value; // not an address even with a scheme — let the gate name it
  }
  if (!hostname) return value;
  return (LOOPBACK_HOSTS.includes(hostname) ? "ws://" : "wss://") + bare;
}

// Machine codes, mapped to human text by each UI (the same shape as the enroll_rejected
// reasons): the options page speaks English, the startpage Russian.
export const ADDR_EMPTY = "empty"; // nothing configured — not an error, just unset
export const ADDR_MALFORMED = "malformed"; // not a URL, or a scheme we do not speak
export const ADDR_HTTP_SCHEME = "http-scheme"; // a site URL where a socket URL is needed
export const ADDR_INSECURE = "insecure"; // ws:// to a non-loopback host: no TLS

// Returns null when `raw` is an address we are willing to send the raw secret over,
// else one of the ADDR_* codes above. A scheme-less address is normalized first, so
// what is judged here is always the address that would actually be dialled.
export function serviceAddressError(raw) {
  const value = normalizeServiceAddress(raw);
  if (!value) return ADDR_EMPTY;
  let url;
  try {
    url = new URL(value);
  } catch {
    return ADDR_MALFORMED;
  }
  // A leftover "curator.example:8000" (normalization declined it) parses with protocol
  // "curator.example:" and an EMPTY hostname, so the host check still catches it.
  if (!url.hostname) return ADDR_MALFORMED;
  if (url.protocol === "wss:") return null;
  if (url.protocol === "ws:") {
    return LOOPBACK_HOSTS.includes(url.hostname) ? null : ADDR_INSECURE;
  }
  if (url.protocol === "http:" || url.protocol === "https:") return ADDR_HTTP_SCHEME;
  return ADDR_MALFORMED;
}
