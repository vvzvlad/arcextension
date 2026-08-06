"""The canonical rule matcher — the SOLE implementation (§8).

§8 is the single canon of matcher semantics; the browser never duplicates this
(that is why preview is server-side too — a second implementation "would lie on
IDN and IPv6"). Everything here is deterministic and pure so it can be reused
verbatim by the (later) curator pass and by preview.

Semantics, verbatim from §8:

* Only ``http`` / ``https`` URLs ever match (any other scheme => no match).
* Normalize the URL's host+port explicitly: ``hostname`` lowercased, IDN → punycode
  via the stdlib ``idna`` codec, a trailing FQDN dot stripped, an IPv6 literal in
  bracketless form, and a missing port filled with the scheme default (80 / 443).
* A ``pattern`` is ``<hostPattern>[:<port>][/<prefix>]``. A missing port in the
  pattern matches ANY port; a missing path prefix matches ANY path.
* ``hostPattern`` is matched against the WHOLE hostname, anchored (full string):
  ``*`` → ``.*``, every other regex metachar escaped. There is NO partial match —
  ``borneo.lc`` matches neither ``evil-borneo.lc`` nor ``x.borneo.lc``. A path prefix,
  if present, restricts to a SEGMENT-boundary prefix of the URL path (case-sensitive):
  ``github.com/wirenboard`` matches ``/wirenboard`` and ``/wirenboard/repo`` but not
  ``/wirenboardXYZ``. Without a prefix the path is irrelevant to matching.
* Specificity ladder (pick the most specific matching rule): (a) an explicit port
  beats a missing port; (b) a longer path prefix; (c) more characters in
  ``hostPattern`` excluding ``*``; (d) smaller ``rules.id``.
* Validation happens at save, not as an exception during a pass: a pattern that
  does not parse (e.g. a full URL ``https://borneo.lc/path``) is rejected; a stored
  rule that stops compiling is excluded from matching (``invalid=1``) but never
  crashes a pass.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}


class InvalidPattern(ValueError):
    """A pattern that is not the ``hostPattern[:port]`` grammar (§8).

    Raised by :func:`compile_pattern` / :func:`validate_pattern` so a save can turn
    it into a human-readable 422, and so :func:`best_match` can defensively skip a
    stored pattern that no longer compiles.
    """


@dataclass(frozen=True)
class CompiledPattern:
    """A pattern compiled once (§8: "compile patterns once")."""

    regex: re.Pattern
    port: int | None          # None => matches any port
    explicit_port: bool       # a port was written in the pattern
    host_chars: int           # count of hostPattern chars EXCLUDING '*' (specificity)
    path_prefix: str | None   # None => matches any path; else a '/'-anchored prefix
    source: str               # the original pattern text (for diagnostics)


# --- URL normalization (§8) -------------------------------------------------
def normalize_target(url: str | None) -> tuple[str, int, str] | None:
    """Return the normalized ``(hostname, port, path)`` of ``url`` or ``None``.

    ``None`` means "this URL can never match any rule": a non-http(s) scheme, a
    missing host, an out-of-range port, or a hostname the ``idna`` codec refuses.
    We never raise — a malformed URL simply does not match (§8).

    The path is taken verbatim from ``urlsplit(url).path`` (an empty path becomes
    ``/``); it is NOT lowercased — the host is case-insensitive, the path is not.
    """
    if not url:
        return None
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    if parts.scheme not in _DEFAULT_PORTS:
        return None
    host = parts.hostname  # urlsplit already lowercases and strips IPv6 brackets
    if not host:
        return None
    # Strip a trailing FQDN dot ("borneo.lc." => "borneo.lc") BEFORE idna: an empty
    # trailing label would make the idna codec raise.
    if host.endswith("."):
        host = host[:-1]
    host = _to_ascii_host(host)
    if host is None:
        return None
    try:
        port = parts.port  # raises ValueError on an out-of-range port
    except ValueError:
        return None
    if port is None:
        port = _DEFAULT_PORTS[parts.scheme]
    # Path is case-sensitive and never lowercased; an empty path normalizes to '/'.
    path = parts.path or "/"
    return host, port, path


def _to_ascii_host(host: str) -> str | None:
    """Lowercase + IDN→punycode a hostname. ``None`` on an idna failure.

    An IPv6 literal (contains ':') is left as-is: it is already bracketless and
    lowercased by ``urlsplit``, and the idna codec is meaningless for it. A pure
    ASCII hostname passes through the codec unchanged, so we only pay for idna when
    there is actually a non-ASCII character to encode.
    """
    host = host.lower()
    if ":" in host:  # IPv6 literal — never run the idna codec on it
        return host
    if host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except Exception:
        # Any idna failure (empty label, disallowed codepoint, ...) => no match,
        # never a crash (§8: "handle failures → the rule/url simply doesn't match").
        return None


# --- pattern parsing / compilation (§8) -------------------------------------
def compile_pattern(pattern: str) -> CompiledPattern:
    """Compile a ``hostPattern[:port][/prefix]`` pattern; raise :class:`InvalidPattern`.

    Only this grammar is accepted. A scheme (``https://borneo.lc``), a query/fragment,
    whitespace, an empty host, or a non-numeric / out-of-range port are all rejected —
    that is the "validation at save" of §8. An optional path prefix is now allowed:
    ``github.com/wirenboard`` restricts the rule to URLs whose path starts, on a
    segment boundary, with ``/wirenboard``.
    """
    if not isinstance(pattern, str):
        raise InvalidPattern("pattern must be a string")
    if pattern == "":
        raise InvalidPattern("pattern must not be empty")
    if any(c.isspace() for c in pattern):
        raise InvalidPattern("pattern must not contain whitespace")
    # A scheme separator means a full URL was entered, not a host pattern — the exact
    # mistake §8 calls out ("введённый как полный URL"). Checked BEFORE the path split
    # so '://' is reported as a scheme, not mis-parsed as an empty-host path.
    if "://" in pattern:
        raise InvalidPattern(
            f"pattern looks like a full URL (contains a scheme '://'); "
            "enter only a host pattern like 'borneo.lc' or 'github.com/wirenboard'"
        )
    # A query/fragment still means a full URL was pasted; the path is now allowed.
    for bad in ("?", "#"):
        if bad in pattern:
            raise InvalidPattern(
                f"pattern looks like a URL (contains {bad!r}); "
                "enter only a host pattern like 'borneo.lc' or 'github.com/wirenboard'"
            )

    # Split the path prefix off FIRST, before host:port parsing — so a bracketed IPv6
    # authority ('[::1]:8080/path') is split at the FIRST '/', leaving '[::1]:8080' for
    # the unchanged _split_host_port (which understands the IPv6 authority form).
    head, slash, rest = pattern.partition("/")
    path_prefix = _normalize_path_prefix(rest) if slash else None

    host_part, port = _split_host_port(head)
    if host_part == "":
        raise InvalidPattern("pattern has an empty host")

    host_for_regex = _host_pattern_to_ascii(host_part)
    regex = _host_pattern_regex(host_for_regex)
    host_chars = sum(1 for c in host_part if c != "*")
    return CompiledPattern(
        regex=regex,
        port=port,
        explicit_port=port is not None,
        host_chars=host_chars,
        path_prefix=path_prefix,
        source=pattern,
    )


def _normalize_path_prefix(rest: str) -> str | None:
    """Normalize the text after the first '/' into a stored path prefix.

    ``rest`` is what followed the first '/' in the pattern. A trailing slash is
    cosmetic: ``github.com/wirenboard/`` is the same rule as ``github.com/wirenboard``,
    and ``github.com/`` (bare host) is the same as no prefix at all. So trailing
    slash(es) are stripped first; if nothing remains the prefix is ``None`` (matches
    any path, and must NOT win the ladder with a length-1 prefix). Otherwise the prefix
    is stored '/'-anchored (``/wirenboard``) and is NOT lowercased (paths are
    case-sensitive).
    """
    rest = rest.rstrip("/")
    if rest == "":
        return None
    return "/" + rest


def validate_pattern(pattern: str) -> None:
    """Raise :class:`InvalidPattern` if ``pattern`` is not valid (save-time gate)."""
    compile_pattern(pattern)


def _split_host_port(pattern: str) -> tuple[str, int | None]:
    """Split a pattern into ``(hostPattern, port|None)``.

    Handles the bracketed IPv6 authority form ``[::1]`` / ``[::1]:8080`` and a bare
    IPv6 literal (``::1`` — more than one colon, so it is NOT read as host:port).
    """
    if pattern.startswith("["):
        end = pattern.find("]")
        if end == -1:
            raise InvalidPattern("unbalanced '[' in IPv6 pattern")
        host = pattern[1:end]
        rest = pattern[end + 1:]
        if rest == "":
            return host, None
        if rest.startswith(":"):
            return host, _parse_port(rest[1:])
        raise InvalidPattern(f"unexpected text after ']': {rest!r}")

    colons = pattern.count(":")
    if colons == 0:
        return pattern, None
    if colons == 1:
        head, _, tail = pattern.partition(":")
        return head, _parse_port(tail)
    # More than one colon and no brackets => a bare IPv6 literal (e.g. '::1',
    # '2001:db8::1'); it carries no port and matches any port.
    return pattern, None


def _parse_port(text: str) -> int:
    if not text.isdigit():
        raise InvalidPattern(f"port must be numeric, got {text!r}")
    port = int(text)
    if not (1 <= port <= 65535):
        raise InvalidPattern(f"port out of range 1-65535: {port}")
    return port


def _host_pattern_to_ascii(host_part: str) -> str:
    """Lowercase + IDN-encode a hostPattern, preserving ``*``.

    A pattern with a wildcard or an IPv6 colon is left lowercased but not idna-
    encoded (the codec cannot process ``*`` or an IP). An idna failure is downgraded
    to the lowercased original so the pattern still compiles to a (never-matching-
    an-IDN) regex rather than being rejected here.
    """
    host = host_part.lower()
    if "*" in host or ":" in host or host.isascii():
        return host
    try:
        return host.encode("idna").decode("ascii")
    except Exception:
        return host


def _host_pattern_regex(host_for_regex: str) -> re.Pattern:
    # '*' → '.*', everything else escaped. NO '^'/'$' anchors: matching uses
    # `fullmatch` (below), which anchors BOTH ends against the whole string. `$`
    # would match just BEFORE a trailing '\n', so `re.compile('^borneo\\.lc$')`
    # accepts "borneo.lc\n" — a partial match §8 forbids. `fullmatch` (equivalently
    # `\Z`) has no such newline hole, so a hostname with a stray trailing newline
    # can never sneak past the anchor.
    parts = [re.escape(p) for p in host_for_regex.split("*")]
    body = ".*".join(parts)
    return re.compile(body)


def pattern_matches(compiled: CompiledPattern, host: str, port: int, path: str) -> bool:
    """True if a normalized ``(host, port, path)`` satisfies this compiled pattern."""
    if compiled.port is not None and compiled.port != port:
        return False
    # fullmatch anchors the WHOLE hostname (§8: "matched against the WHOLE
    # hostname"); unlike `$.match` it cannot be fooled by a trailing '\n'.
    if compiled.regex.fullmatch(host) is None:
        return False
    # A path prefix restricts to a SEGMENT-boundary prefix: '/wirenboard' matches
    # '/wirenboard' and '/wirenboard/repo' but not '/wirenboardXYZ'. Case-sensitive.
    if compiled.path_prefix is not None:
        prefix = compiled.path_prefix
        if path != prefix and not path.startswith(prefix + "/"):
            return False
    return True


# --- selection (§8 specificity ladder) --------------------------------------
def compile_rules(rules) -> list[tuple[object, CompiledPattern]]:
    """Compile each rule's pattern ONCE (§8: "compile patterns once").

    Returns ``[(rule, CompiledPattern), ...]`` for the rules eligible to match: a
    rule with a truthy ``invalid`` or a pattern that no longer compiles is dropped
    here (never matched, never fatal). Callers that match many URLs against the same
    rule set (the pass, preview) build this ONCE before the tab loop instead of
    recompiling every pattern for every tab.
    """
    out: list[tuple[object, CompiledPattern]] = []
    for rule in rules:
        if _rule_field(rule, "invalid"):
            continue
        try:
            compiled = compile_pattern(_rule_field(rule, "pattern"))
        except InvalidPattern:
            # A stored pattern that stopped compiling is excluded from matching
            # (§8) — defence in depth even if `invalid` was not yet flagged.
            continue
        out.append((rule, compiled))
    return out


def best_match_compiled(url: str | None, compiled_rules) -> object | None:
    """Most specific rule matching ``url`` from a pre-compiled list, or ``None``.

    ``compiled_rules`` is the output of :func:`compile_rules`. This is the hot path
    used per-tab: it does zero pattern compilation.
    """
    target = normalize_target(url)
    if target is None:
        return None
    host, port, path = target

    best = None
    best_key = None
    for rule, compiled in compiled_rules:
        if not pattern_matches(compiled, host, port, path):
            continue
        rule_id = _rule_field(rule, "id")
        prefix_len = len(compiled.path_prefix) if compiled.path_prefix else 0
        # Ladder as a min-key: explicit port first (0 < 1), then a LONGER path prefix
        # (negated), then MORE host chars (negated), then the SMALLER id.
        key = (
            0 if compiled.explicit_port else 1,
            -prefix_len,
            -compiled.host_chars,
            rule_id,
        )
        if best_key is None or key < best_key:
            best_key = key
            best = rule
    return best


def best_match(url: str | None, rules) -> object | None:
    """Return the most specific rule matching ``url``, or ``None`` (§8).

    ``rules`` is any iterable of mappings/rows exposing ``id``, ``pattern``,
    ``instance_id`` and (optionally) ``invalid``. Convenience wrapper that compiles
    the rule set then delegates to :func:`best_match_compiled`; matchers over many
    URLs should call :func:`compile_rules` once and reuse it.
    """
    return best_match_compiled(url, compile_rules(rules))


def _rule_field(rule, name):
    """Read a field from a dict, an sqlite3.Row, or any attribute-bearing object."""
    if isinstance(rule, dict):
        return rule.get(name)
    try:
        return rule[name]  # sqlite3.Row supports __getitem__ by column name
    except (KeyError, IndexError, TypeError):
        return getattr(rule, name, None)
