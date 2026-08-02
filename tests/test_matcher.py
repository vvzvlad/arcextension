"""The canonical matcher (§8). Each test is written so it REDDENS if the guard it
names is removed (the specificity ladder, the anchored/no-partial match, the
scheme/port normalization, the invalid-excluded rule)."""

import pytest

from src.rules.matcher import (
    InvalidPattern,
    best_match,
    compile_pattern,
    normalize_target,
    validate_pattern,
)


def R(rid, pattern, instance="i", invalid=0):
    return {"id": rid, "pattern": pattern, "instance_id": instance, "invalid": invalid}


def match_instance(url, rules):
    r = best_match(url, rules)
    return None if r is None else r["instance_id"]


# --- normalization ----------------------------------------------------------
def test_normalize_host_lowercase_and_default_ports():
    assert normalize_target("http://Borneo.LC/a") == ("borneo.lc", 80)
    assert normalize_target("https://borneo.lc/a") == ("borneo.lc", 443)
    # explicit port overrides the default
    assert normalize_target("https://borneo.lc:8443/a") == ("borneo.lc", 8443)


def test_normalize_strips_trailing_fqdn_dot():
    assert normalize_target("https://borneo.lc./x")[0] == "borneo.lc"


def test_normalize_ipv6_bracketless():
    assert normalize_target("http://[::1]/") == ("::1", 80)
    assert normalize_target("https://[2001:DB8::1]:8080/x") == ("2001:db8::1", 8080)


def test_normalize_rejects_non_http_and_bad_port():
    assert normalize_target("ftp://borneo.lc/") is None
    assert normalize_target("file:///etc/passwd") is None
    assert normalize_target("javascript:alert(1)") is None
    assert normalize_target("http://borneo.lc:99999/") is None  # out of range
    assert normalize_target(None) is None


# --- anchored, whole-hostname, no partial (§8) ------------------------------
def test_anchored_no_partial():
    rules = [R(1, "borneo.lc", "home")]
    assert match_instance("https://borneo.lc/", rules) == "home"
    # Path is irrelevant to matching.
    assert match_instance("https://borneo.lc/a/b/c?q=1#f", rules) == "home"
    # NO partial match — these must all miss (reddens if the match is not anchored).
    assert match_instance("https://x.borneo.lc/", rules) is None
    assert match_instance("https://evil-borneo.lc/", rules) is None
    assert match_instance("https://borneo.lcx/", rules) is None


def test_wildcard():
    # '*' matches any host; '*.borneo.lc' matches sub-domains but NOT the bare host.
    assert match_instance("https://anything.example/", [R(1, "*")]) == "i"
    sub = [R(1, "*.borneo.lc", "home")]
    assert match_instance("https://a.borneo.lc/", sub) == "home"
    assert match_instance("https://a.b.borneo.lc/", sub) == "home"
    assert match_instance("https://borneo.lc/", sub) is None  # no dot before => no match


def test_non_http_never_matches():
    rules = [R(1, "borneo.lc", "home"), R(2, "*", "any")]
    assert match_instance("ftp://borneo.lc/", rules) is None
    assert match_instance("chrome://settings", rules) is None


# --- port specificity (§8a) -------------------------------------------------
def test_explicit_port_beats_any_port():
    # both match https (port 443); the explicit-port rule is more specific.
    rules = [R(1, "borneo.lc", "any"), R(2, "borneo.lc:443", "explicit")]
    assert match_instance("https://borneo.lc/", rules) == "explicit"
    # A wrong explicit port does not match at all; the any-port rule wins.
    rules2 = [R(1, "borneo.lc", "any"), R(2, "borneo.lc:8443", "explicit")]
    assert match_instance("https://borneo.lc/", rules2) == "any"


def test_default_port_fill_80_443():
    # `:80` matches http only; `:443` matches https only (default-port fill).
    rules = [R(1, "borneo.lc:80", "http80"), R(2, "borneo.lc:443", "https443")]
    assert match_instance("http://borneo.lc/", rules) == "http80"
    assert match_instance("https://borneo.lc/", rules) == "https443"


# --- host-char + id ladder (§8b, §8c) ---------------------------------------
def test_more_host_chars_wins():
    rules = [R(1, "*.borneo.lc", "wild"), R(2, "x.borneo.lc", "exact")]
    # exact has more non-'*' chars => more specific, regardless of id order.
    assert match_instance("https://x.borneo.lc/", rules) == "exact"
    # reversed ids: still the char count decides, not the id.
    rules_rev = [R(1, "x.borneo.lc", "exact"), R(2, "*.borneo.lc", "wild")]
    assert match_instance("https://x.borneo.lc/", rules_rev) == "exact"


def test_smaller_id_breaks_tie():
    rules = [R(5, "borneo.lc", "five"), R(2, "borneo.lc", "two")]
    assert match_instance("https://borneo.lc/", rules) == "two"


# --- IDN both ways (§8) -----------------------------------------------------
def test_idn_matches_both_forms():
    puny = "https://xn--e1afmkfd.xn--p1ai/"
    unicode_url = "https://пример.рф/"
    # a unicode pattern matches both the unicode and punycode URL forms
    rules_unicode = [R(1, "пример.рф", "home")]
    assert match_instance(unicode_url, rules_unicode) == "home"
    assert match_instance(puny, rules_unicode) == "home"
    # a punycode pattern likewise matches the unicode URL
    rules_puny = [R(1, "xn--e1afmkfd.xn--p1ai", "home")]
    assert match_instance(unicode_url, rules_puny) == "home"


# --- IPv6 patterns ----------------------------------------------------------
def test_ipv6_patterns_match():
    assert match_instance("http://[::1]/", [R(1, "[::1]", "loop")]) == "loop"
    assert match_instance("http://[::1]/", [R(1, "::1", "loop")]) == "loop"  # bare form
    url = "https://[2001:db8::1]:8080/x"
    assert match_instance(url, [R(1, "[2001:db8::1]:8080", "v6")]) == "v6"
    assert match_instance(url, [R(1, "[2001:db8::1]", "v6any")]) == "v6any"
    assert match_instance(url, [R(1, "[2001:db8::1]:9999", "v6")]) is None


# --- invalid rules excluded from matching (§8) ------------------------------
def test_invalid_flag_excluded_from_matching():
    rules = [R(1, "borneo.lc", "flagged", invalid=1), R(2, "borneo.lc", "ok")]
    # the invalid=1 rule (smaller id) is skipped; the valid one matches.
    assert match_instance("https://borneo.lc/", rules) == "ok"
    # a single flagged rule => no match at all (reddens if invalid is honored wrongly)
    assert match_instance("https://borneo.lc/", [R(1, "borneo.lc", "x", invalid=1)]) is None


def test_uncompilable_stored_pattern_excluded_not_fatal():
    # A stored pattern that no longer parses is skipped defensively, never raised.
    rules = [R(1, "https://borneo.lc/path", "bad"), R(2, "borneo.lc", "good")]
    assert match_instance("https://borneo.lc/", rules) == "good"


# --- validation grammar (§8) ------------------------------------------------
@pytest.mark.parametrize(
    "bad",
    [
        "https://borneo.lc/path",  # full URL
        "borneo.lc/path",           # path
        "borneo.lc?q=1",            # query
        "borneo.lc#f",              # fragment
        "borneo lc",                # whitespace
        "",                          # empty
        "borneo.lc:notaport",       # non-numeric port
        "borneo.lc:99999",          # out-of-range port
        "[::1",                      # unbalanced bracket
    ],
)
def test_validate_pattern_rejects_non_grammar(bad):
    with pytest.raises(InvalidPattern):
        validate_pattern(bad)


@pytest.mark.parametrize(
    "good", ["borneo.lc", "*.borneo.lc", "*", "borneo.lc:8443", "[::1]", "[2001:db8::1]:8080"]
)
def test_validate_pattern_accepts_grammar(good):
    compile_pattern(good)  # must not raise
