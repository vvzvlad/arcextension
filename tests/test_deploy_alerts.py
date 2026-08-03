"""deploy/alerts.yml must stay a loadable PROMETHEUS rule file (§12).

Prometheus and vmalert unmarshal this schema STRICTLY: one unknown field rejects the
WHOLE file, so the curator ends up with no alerts at all — including `curator-down`,
the one rule that reports the service is gone. A rule file that silently fails to load
is indistinguishable from a healthy system, which is why this is worth a test rather
than a review comment.

Validated for real with `promtool check rules deploy/alerts.yml` and
`promtool test rules deploy/alerts_test.yml` (Prometheus 2.53.3) during issue #37;
promtool is not a project dependency, so the schema check below is the CI-portable
stand-in. Keep both in mind when editing: this test encodes promtool's field sets, not a
guess at them.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import yaml

from src.settings import Settings

REPO_ROOT = Path(__file__).resolve().parents[1]
ALERTS_YML = REPO_ROOT / "deploy" / "alerts.yml"
SRC_DIR = REPO_ROOT / "src"

# Fields promtool accepts, from prometheus/model/rulefmt. Anything outside these sets
# fails the strict unmarshal — `noDataState` (a Grafana-MANAGED alert concept, whose
# provisioning format is a different schema entirely) is the concrete regression this
# guards: it was present on every rule and made the file load nowhere.
_GROUP_FIELDS = {"name", "interval", "query_offset", "limit", "rules", "labels"}
_RULE_FIELDS = {
    "record", "alert", "expr", "for", "keep_firing_for", "labels", "annotations",
}

# PASS_INTERVAL comes from the SHIPPING default, not a copy of it: changing
# `pass_interval_min` in src/settings.py makes every number in alerts.yml wrong, and a
# hardcoded 300 here would keep the test green while the alerts drifted.
_PASS_INTERVAL_S = Settings.model_fields["pass_interval_min"].default * 60

# §7 fixes the moment the alert FIRES at 3×PASS_INTERVAL, and firing time is
# (threshold crossing) + `for:`. `curator_pass_overdue_seconds` measures overdue BEYOND
# the interval (`elapsed − PASS_INTERVAL`, see `_pass_overdue_seconds`), so:
#     gauge > 300 crosses at elapsed 600s, + for:5m -> fires at 900s = 3×  ✔
_PASS_OVERDUE_FOR_S = 300
_PASS_OVERDUE_THRESHOLD = 3 * _PASS_INTERVAL_S - _PASS_INTERVAL_S - _PASS_OVERDUE_FOR_S
# The raw-age gauges take the plain multiple (no interval subtracted).
_SNAPSHOT_STALE_THRESHOLD = 6 * _PASS_INTERVAL_S  # 1800


def _rules() -> list[dict]:
    doc = yaml.safe_load(ALERTS_YML.read_text(encoding="utf-8"))
    return [rule for group in doc["groups"] for rule in group["rules"]]


def test_alerts_yml_uses_only_prometheus_rule_fields():
    doc = yaml.safe_load(ALERTS_YML.read_text(encoding="utf-8"))
    assert set(doc) == {"groups"}
    for group in doc["groups"]:
        unknown = set(group) - _GROUP_FIELDS
        assert not unknown, f"group {group.get('name')!r} has unknown fields: {unknown}"
        for rule in group["rules"]:
            unknown = set(rule) - _RULE_FIELDS
            # Redden: re-add `noDataState:` to any rule -> this fails, exactly as
            # promtool would ("field noDataState not found in type rulefmt.Rule").
            assert not unknown, f"rule {rule.get('alert')!r} has unknown fields: {unknown}"


def test_every_rule_is_a_complete_alerting_rule():
    rules = _rules()
    assert rules, "no rules parsed"
    for rule in rules:
        assert rule.get("alert"), f"rule without an `alert` name: {rule}"
        assert rule.get("expr"), f"{rule['alert']} has no `expr`"
        assert rule.get("annotations", {}).get("summary"), f"{rule['alert']} has no summary"
        assert rule.get("labels", {}).get("severity"), f"{rule['alert']} has no severity"


def test_no_rule_uses_the_forbidden_time_minus_metric_form():
    # `time() - <metric> > X` is a SERIES FILTER: on a healthy curator it matches
    # nothing, and an empty result is indistinguishable from a dead exporter. The
    # service exports ready-made gauges precisely so every rule is a plain
    # gauge-vs-threshold compare.
    for rule in _rules():
        assert "time(" not in rule["expr"], f"forbidden time() filter in {rule['alert']}"


def test_target_loss_also_fires_when_the_target_is_absent():
    # Two shapes of "down": scraped-and-failing (`up == 0`) and gone from service
    # discovery (no `up` series at all). A bare `up == 0` matches nothing in the
    # second case — an empty result never fires — so `absent()` must cover it.
    # This is the semantics the removed `noDataState: Alerting` used to carry.
    down = next(r for r in _rules() if r["alert"] == "curator-down")
    assert 'up{job="curator"} == 0' in down["expr"]
    assert 'absent(up{job="curator"})' in down["expr"]


def test_pass_overdue_threshold_and_for_are_the_expected_pair():
    # Both halves matter: the threshold alone does not determine when the alert fires.
    rule = next(r for r in _rules() if r["alert"] == "curator-pass-overdue")
    assert rule["expr"] == f"curator_pass_overdue_seconds > {_PASS_OVERDUE_THRESHOLD}"
    assert rule["for"] == f"{_PASS_OVERDUE_FOR_S // 60}m"


def test_pass_overdue_FIRES_at_exactly_three_pass_intervals():
    # Ties the alert to the SHIPPING formula rather than to a comment, and checks the
    # moment it FIRES (crossing + `for:`), which is what §7 actually promises —
    # measuring the crossing alone is what let the 4× overshoot through twice.
    from src.api.metrics import Snapshot, _pass_overdue_seconds

    now_ms = 1_000_000_000_000

    def gauge_after(elapsed_s: int) -> int:
        snap = Snapshot(finished_at=now_ms - elapsed_s * 1000)
        return _pass_overdue_seconds(snap, now_ms, _PASS_INTERVAL_S, paused=False)

    fires_at = 3 * _PASS_INTERVAL_S  # §7: «через 3×PASS_INTERVAL срабатывает алерт»
    crosses_at = fires_at - _PASS_OVERDUE_FOR_S

    # Not yet over the threshold just before the crossing...
    assert gauge_after(crosses_at - 1) <= _PASS_OVERDUE_THRESHOLD
    # ...over it from the crossing onward, and still over it when `for:` elapses, so
    # the alert actually fires at 3×PASS_INTERVAL rather than resolving in between.
    assert gauge_after(crosses_at + 1) > _PASS_OVERDUE_THRESHOLD
    assert gauge_after(fires_at) > _PASS_OVERDUE_THRESHOLD


def test_blind_restore_detection_is_alerted():
    # `1` means the marker is configured but unreadable, so restore-from-backup
    # detection is off — and nothing else reveals it: every other fingerprint component
    # travels inside the backup and matches, so a restore proceeds as ordinary work.
    # The summary must carry the fix, because the metric name does not tell an operator
    # that the cause is almost always file permissions (600 root:root vs uid 1000).
    rule = next(
        r for r in _rules() if r["alert"] == "curator-restore-marker-unreadable"
    )
    assert rule["expr"] == "curator_restore_marker_unreadable == 1"
    assert rule["for"] == "15m"
    summary = rule["annotations"]["summary"].lower()
    assert "unreadable" in summary
    assert "uid 1000" in summary, "the operator cannot diagnose this from the name alone"


def test_snapshot_stale_keeps_the_six_interval_threshold():
    # The 6× reserved by §12 for the half-open-socket case — asserted so the two
    # thresholds cannot be "unified" by a later edit. This gauge is a RAW age, so it
    # takes the plain multiple with nothing subtracted.
    rule = next(r for r in _rules() if r["alert"] == "curator-instance-snapshot-stale")
    assert (
        f"curator_instance_snapshot_age_seconds > {_SNAPSHOT_STALE_THRESHOLD}"
        in rule["expr"]
    )


def test_every_metric_named_in_a_rule_actually_exists():
    # promtool validates SYNTAX, not that a series exists: `curator_pass_overdue_second`
    # (no `s`) parses fine, passes `check rules`, matches nothing forever, and the rule
    # goes permanently silent — the exact failure class this file exists to catch.
    # Metrics are registered as `reg.metric("<name>", …)`, so require the quoted name.
    metrics_src = (SRC_DIR / "api" / "metrics.py").read_text(encoding="utf-8")
    # Strip label matchers first: `curator_instance_connected{id=~"curator_.*"}` must
    # contribute the SERIES name only — a label value that happens to start with
    # `curator_` is not a metric and would be a false failure.
    referenced = {
        name
        for rule in _rules()
        for name in re.findall(
            r"\bcurator_[a-z0-9_]+\b", re.sub(r"\{[^}]*\}", "", rule["expr"])
        )
    }
    assert referenced, "no curator_* metrics referenced — the regex stopped matching"
    unknown = sorted(n for n in referenced if f'"{n}"' not in metrics_src)
    assert not unknown, f"alert rules reference metrics /metrics never exports: {unknown}"


def test_enroll_window_gauge_is_covered_by_a_rule():
    # §37 acceptance 5, the direction the name-guard does NOT cover: #35 exports
    # curator_enroll_window_seconds_remaining, and a series with no rule is DEAD — scraped
    # forever, alerted on never. This test used to assert the OPPOSITE (no rule may
    # mention the gauge) because the pre-clamp gauge went negative forever after any
    # ordinary window, making the then-proposed `< 0` rule unable to resolve. The clamp
    # landed (56c073a: >0 open, 0 otherwise), so that prohibition now only froze the metric
    # dead. Redden: delete the rule.
    covering = [
        r for r in _rules() if "curator_enroll_window_seconds_remaining" in r["expr"]
    ]
    assert covering, "the enrollment-window gauge is exported but no rule reads it"
    # And it must not be the unreachable form the clamp retired: post-clamp the gauge is
    # never negative, so a `< 0` rule would be permanently silent.
    for rule in covering:
        assert "< 0" not in rule["expr"]


def test_window_held_open_rule_cannot_fire_on_one_ordinary_window():
    """The held-open rule must be blind to a NORMAL enrollment session and awake to a
    window kept open by repeated re-arming.

    Both halves are asserted against the SHIPPING numbers rather than a copy of them: the
    gauge is computed by the real ``_enroll_window_seconds_remaining`` from a deadline one
    window ahead, and the rule fires at (threshold crossing + ``for:``).

    "One armed window" is measured against ENROLL_WINDOW_MAX_MIN, the hard CEILING, not
    against ``enroll_window_min``'s default. ENROLL_WINDOW_MIN is operator config bounded
    by ``le=ENROLL_WINDOW_MAX_MIN``; pinning `for:` to the 10m default let a `for: 15m`
    ship that pages on ONE ordinary arm at a perfectly legal ENROLL_WINDOW_MIN=20 — and
    a test written against the default is green for exactly that bug. Asserting against
    the ceiling covers every value the settings field will accept, so the same mistake
    cannot return the next time the default moves.

    Redden either way: drop `for:` to at-or-below the ceiling and a legal single window
    starts paging; raise the gauge's clamp back to a negative and the second half stops
    meaning anything.
    """
    from src.api.metrics import Snapshot, _enroll_window_seconds_remaining
    from src.curator.enroll import ENROLL_WINDOW_MAX_MIN

    rule = next(r for r in _rules() if r["alert"] == "curator-enroll-window-held-open")
    assert rule["expr"] == "curator_enroll_window_seconds_remaining > 0"
    for_s = int(rule["for"].removesuffix("m")) * 60
    # The LONGEST window any legal ENROLL_WINDOW_MIN can arm — the value `for:` must beat.
    window_s = ENROLL_WINDOW_MAX_MIN * 60
    assert for_s > window_s, (
        "`for:` must outlast the LONGEST legal armed window (ENROLL_WINDOW_MAX_MIN), "
        "else a deployment that raises ENROLL_WINDOW_MIN pages on ordinary enrollments"
    )
    # The settings field really is bounded by that ceiling — otherwise `window_s` above is
    # not the worst case and the assertion proves nothing.
    field = Settings.model_fields["enroll_window_min"]
    assert any(
        getattr(m, "le", None) == ENROLL_WINDOW_MAX_MIN for m in field.metadata
    ), "enroll_window_min must stay clamped to ENROLL_WINDOW_MAX_MIN"
    assert field.default <= ENROLL_WINDOW_MAX_MIN

    now_ms = 1_000_000_000_000
    armed_at = now_ms - window_s * 1000  # a single window armed exactly one window ago

    def gauge_at(elapsed_s: int) -> int:
        snap = Snapshot(enroll_window_until=armed_at + window_s * 1000)
        return _enroll_window_seconds_remaining(snap, armed_at + elapsed_s * 1000)

    # A single window: positive while it runs, exactly 0 from its deadline on — so the
    # `for:` clock is reset long before it elapses and the rule cannot fire.
    assert gauge_at(window_s - 1) > 0
    assert gauge_at(window_s) == 0
    assert gauge_at(for_s) == 0

    # Re-armed at expiry (the operator holding the window open): the gauge is positive
    # again at the moment the previous one lapsed, so it never returns to 0 and the `for:`
    # clock runs to completion.
    rearmed = Snapshot(enroll_window_until=armed_at + 2 * window_s * 1000)
    assert _enroll_window_seconds_remaining(rearmed, armed_at + window_s * 1000) > 0


def test_admin_token_bruteforce_rule_scopes_to_the_login_reason():
    """The ADMIN_TOKEN rule must key on the FAILED-LOGIN reason, not the ambient
    "no session presented" one.

    ``admin_session`` ticks on every unauthenticated hit of the console — a browser that
    opens /admin before logging in produces it — so a rule on that label is noise at any
    threshold a brute force could reach. Redden: point the rule back at ``admin_session``,
    or drop the label matcher entirely (a burst of CORS/metrics rejections would then
    page).
    """
    from src.api.admin_page import ADMIN_BAD_TOKEN_REASON

    rule = next(r for r in _rules() if r["alert"] == "curator-admin-token-bruteforce")
    assert f'curator_auth_rejections_total{{reason="{ADMIN_BAD_TOKEN_REASON}"}}' in rule["expr"]
    assert "increase(" in rule["expr"]
    assert 'reason="admin_session"' not in rule["expr"]


def test_preauth_capacity_refusals_have_a_rule():
    """A refusal at the /ext pre-auth ceiling leaves NO other trace — no instances row, no
    reject_reason, no per-instance metric — so the counter is the only signal that the
    fleet is being turned away at the handshake. Redden: delete the rule."""
    rule = next(r for r in _rules() if r["alert"] == "curator-ext-preauth-capacity")
    assert 'curator_auth_rejections_total{reason="capacity"}' in rule["expr"]
    assert "increase(" in rule["expr"]


def test_enroll_bad_code_bruteforce_rule_scopes_to_the_reason():
    # §37: the brute-force alert must scope to the {reason="enroll_bad_code"} series (not
    # the whole auth-rejections family, which counts CORS/metrics/api rejections too) and
    # rate-limit via increase() so a healthy install's absent series stays silent. Redden:
    # drop the label matcher and a burst of unrelated rejections would page.
    rule = next(r for r in _rules() if r["alert"] == "curator-enroll-code-bruteforce")
    assert 'curator_auth_rejections_total{reason="enroll_bad_code"}' in rule["expr"]
    assert "increase(" in rule["expr"]


def test_every_enrollment_signal_has_an_alert_rule():
    # §37, the metric->rule direction the name-guard does NOT cover: a series with no rule
    # is DEAD (scraped forever, alerted on never), and CI is silent about it. These are the
    # signals with no constant behind them, so they are listed by hand; the per-reason
    # labels are enumerated FROM THE SOURCE by the test below instead.
    exprs = " ".join(r["expr"] for r in _rules())
    for needle in (
        'curator_auth_rejections_total{reason="admin_bad_token"}',   # ADMIN_TOKEN guessing
        'curator_auth_rejections_total{reason="capacity"}',          # /ext ceiling refusals
        "curator_enroll_window_seconds_remaining",                   # window held open
        "curator_main_instance_never_seen",                          # MAIN unusable
    ):
        assert needle in exprs, f"exported but never alerted on: {needle}"


# Reason labels that deliberately have NO rule. The default is the other way round — a
# reason label needs an alert unless it is listed here WITH a reason — because the
# thing this guards against is exactly what happened to `enroll_secret_conflict`: a new
# refusal reason was added, exported, and the hand-written needle list said nothing.
#
# Each entry is excluded because a rule on it would be noise, not because nobody got to
# it.
#
# Hello / enroll path (src/ext/protocol.py constants):
#   * `revoked` / `unknown_instance` — the NORMAL answers on the hello path. Every browser
#     waiting for approval ticks `unknown_instance` on every reconnect, and a revoked
#     instance that keeps retrying ticks `revoked` by design. Alerting on either would
#     page on ordinary operation.
#   * `enroll_closed` — likewise normal: any client that reconnects outside an open window
#     gets it, which is most of the time on a healthy install.
#   * `auth` — documented in `hello_reject_reason` as a last-line guard the channel is
#     supposed to have already handled with a more specific reason. It is a belt-and-
#     suspenders branch, not an operational signal.
#   * `instance` — REJECT_INSTANCE is not produced anywhere since the secret-based hello
#     landed (the id is server-assigned now); the constant is kept only for the wire
#     vocabulary. There is no series to alert on.
#   * `protocol` / `enroll_protocol` — a version skew during a rollout. It is per-instance
#     and self-describing: `reject_reason='protocol'` lands on the instances row and turns
#     the status red, and if the whole fleet is skewed the OUTCOME is already covered by
#     curator-instance-absent / curator-instance-snapshot-stale.
#   * `origin` — a deploy-time EXT_ALLOWED_ORIGINS mistake, with the same per-instance
#     trace on the row and the same absent-instance outcome alert above it.
#   * `duplicate_instance` — two browsers loading one profile's credential. It resolves as
#     soon as one of them goes away and it, too, is recorded on the row.
#
# HTTP auth paths (src/api/*, src/mcpiface/*) — these became visible to this guard only
# when the label source stopped being src/ext/protocol.py alone, so each needed the
# decision made for the first time:
#   * `admin_session` — the AMBIENT "no session presented" label: every browser that opens
#     /admin before logging in ticks it. This is the label the ADMIN_TOKEN brute-force rule
#     was deliberately split AWAY from (see the test below); a rule on it is noise at any
#     threshold a real brute force could reach.
#   * `api_token` — a 401 on /api/*. A REVOKED (or still-pending) instance is the designed
#     steady state here: its startpage keeps opening newtabs and its service worker keeps
#     flushing quick-links, each with a credential the service now refuses, so the counter
#     climbs for as long as the extension stays installed. That is ordinary post-revocation
#     operation — the same reasoning that excuses `revoked` on the hello path — so no
#     threshold separates it from token guessing.
#   * `metrics_token` — a 401 on /metrics, which is the endpoint the counter is read
#     THROUGH. If the scrape credential is wrong, Prometheus cannot scrape this series at
#     all, so a rule on it is unreachable by construction; the real outcome (no scrape) is
#     what `curator-down` fires on, `up == 0` covering exactly this.
#   * `mcp_token` — a bad Bearer on /mcp. Same credential as `admin_bad_token`, and the
#     residual risk is stated rather than hidden: a guesser who picks /mcp instead of the
#     login form is not paged. It is excluded anyway because the >5-in-10m threshold's
#     premise does not hold here — /mcp has no human form, only the owner's own agents,
#     which RETRY automatically, so one stale token in an agent config clears 5 attempts in
#     seconds. `admin_bad_token` keeps the rule because a human at a login form does not.
_REASONS_WITHOUT_A_RULE = frozenset({
    "auth",
    "instance",
    "origin",
    "duplicate_instance",
    "protocol",
    "revoked",
    "unknown_instance",
    "enroll_closed",
    "enroll_protocol",
    "admin_session",
    "api_token",
    "metrics_token",
    "mcp_token",
})


# --- Where the reason labels come from ---------------------------------------
#
# The needle list stopped being hand-written one round ago, but its SOURCE stayed
# hand-picked: the enumeration read the constants of `src/ext/protocol.py` and nothing
# else. `cors_preflight` (src/api/cors.py) never passes through that module, so it was
# exported, documented in deploy/DEPLOY.md, given no rule — and this guard said nothing.
# That is the same defect one floor up from the `enroll_secret_conflict` miss the guard was
# written for: the LIST was derived, the CHOICE OF SOURCE was not.
#
# So the source is now every WRITER of the counter, found by parsing all of `src/`: each
# `incr(<arg>)` call on the counter contributes the label(s) it can emit, wherever it lives
# and however the module reached the counter — by name (`auth_rejections.incr`) or through
# its module (`auth_metrics.auth_rejections.incr`). A new writer in a new module therefore
# needs no edit here. Three argument shapes are understood:
#
#   1. a string literal               -> that label;
#   2. a module-level string constant -> its value (e.g. ADMIN_BAD_TOKEN_REASON);
#   3. a runtime-computed argument    -> resolved by `_DYNAMIC_INCR_SITES` below.
#
# Anything else FAILS the test instead of contributing nothing, so an unreadable new call
# shape is a red test rather than a fresh silent hole.


def _src_modules() -> list[tuple[str, ast.Module]]:
    """Every ``src/**/*.py`` parsed once, keyed by repo-relative posix path."""
    return [
        (
            path.relative_to(REPO_ROOT).as_posix(),
            ast.parse(path.read_text(encoding="utf-8")),
        )
        for path in sorted(SRC_DIR.rglob("*.py"))
    ]


def _module_string_constants(tree: ast.Module) -> dict[str, str]:
    """Module-level ``NAME = "literal"`` bindings, so a constant argument resolves
    WITHOUT importing the module (importing every module under src/ just to read a label
    would drag half the app into this test)."""
    out: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Constant):
            if isinstance(node.value.value, str):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        out[target.id] = node.value.value
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            out[node.target.id] = node.value.value
    return out


def _src_tree(rel: str) -> ast.Module:
    """One parsed module from the src/ walk, by repo-relative posix path. Reddens loudly
    when the file moves, rather than letting a scanner keyed on it quietly find nothing."""
    trees = dict(_src_modules())
    assert rel in trees, f"{rel} is gone — a reason scanner below points at a moved file"
    return trees[rel]


def _literal_reason_args(rel: str, func_name: str, position: int) -> set[str]:
    """String LITERALS passed as the reason argument of ``func_name(...)`` inside ``rel``.

    The two resolvers below enumerate the protocol CONSTANTS, because that is where every
    reject reason is born today — but nothing enforces it. A reason handed to the helper
    as a bare literal (``_reject_enroll(ws, app, "new_thing")``) emits
    ``enroll_new_thing`` just the same, no ``ENROLL_*`` constant would ever name it, and
    the label would reach /metrics with no rule while this whole file stayed green.
    Reading the call sites costs one AST walk and closes that door: a literal becomes a
    label like any other and has to be alerted on or excused.
    """
    out: set[str] = set()
    for node in ast.walk(_src_tree(rel)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)):
            continue
        if node.func.id != func_name:
            continue
        candidates = list(node.args[position : position + 1])
        candidates += [kw.value for kw in node.keywords if kw.arg == "reason"]
        for arg in candidates:
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                out.add(arg.value)
    return out


def _literal_returns(rel: str, func_name: str) -> set[str]:
    """String literals RETURNED by ``func_name`` in ``rel``.

    The one indirection between a call site and a label: the reject call at
    src/ext/channel.py forwards whatever the pure gate decided, so a new reason can just
    as well be born as a bare ``return "new_thing"`` inside that gate without ever
    becoming a module constant. Same hole, one frame up.
    """
    for node in ast.walk(_src_tree(rel)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name != func_name:
            continue
        return {
            n.value.value
            for n in ast.walk(node)
            if isinstance(n, ast.Return)
            and isinstance(n.value, ast.Constant)
            and isinstance(n.value.value, str)
        }
    raise AssertionError(f"{func_name}() is gone from {rel} — this scanner is stale")


def _hello_reason_labels() -> set[str]:
    """Labels `_reject` (src/ext/channel.py) can pass to ``incr(reason)``: a hello reject
    reason, counted verbatim."""
    from src.ext import protocol

    labels = {
        v for k, v in vars(protocol).items()
        if k.startswith("REJECT_") and isinstance(v, str)
    }
    # `_reject(websocket, app, instance_id, reason)` — the reason is positional arg 3.
    return labels | _literal_reason_args("src/ext/channel.py", "_reject", 3)


def _enroll_reason_labels() -> set[str]:
    """Labels `_reject_enroll` can pass to ``incr("enroll_" + reason)``: every ENROLL_*
    constant, plus REJECT_PROTOCOL which the enroll path reuses — which is why
    ``enroll_protocol`` is a real label with no constant of its own."""
    from src.ext import protocol

    reasons = {
        v for k, v in vars(protocol).items()
        if k.startswith("ENROLL_") and isinstance(v, str)
    }
    reasons |= {protocol.REJECT_PROTOCOL}
    # `_reject_enroll(websocket, app, reason)` — the reason is positional arg 2 — plus the
    # single hop behind it: that call forwards `protocol.enroll_reject_reason`'s verdict.
    reasons |= _literal_reason_args("src/ext/channel.py", "_reject_enroll", 2)
    reasons |= _literal_returns("src/ext/protocol.py", "enroll_reject_reason")
    return {"enroll_" + v for v in reasons}


# The two increment sites whose argument is computed at runtime, so no static read can
# name the label. Keyed by (module, the argument as `ast.unparse` renders it): a site that
# CHANGES shape stops matching and reddens, rather than quietly resolving to a stale
# answer. Both live in the ext channel and both delegate to the protocol constants.
_DYNAMIC_INCR_SITES = {
    ("src/ext/channel.py", "reason"): _hello_reason_labels,
    ("src/ext/channel.py", "'enroll_' + reason"): _enroll_reason_labels,
}

# Modules that IMPORT the counter without incrementing it. Everything else importing it
# must contain a call this scan understands — which is what catches an aliased import
# (`... as ar`) or a wrapper the attribute match would not recognise.
_COUNTER_READERS = frozenset({"src/api/metrics.py"})

# The counter, as the scans below name it.
_COUNTER_MODULE = "src.api.auth_metrics"
_COUNTER_NAME = "auth_rejections"


def _dotted(node: ast.expr) -> str | None:
    """Render ``a`` / ``a.b.c`` as a dotted string; ``None`` for anything else."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        head = _dotted(node.value)
        return f"{head}.{node.attr}" if head else None
    return None


def _imports_counter(tree: ast.Module) -> bool:
    """Does this module reach the counter's module at all, by ANY import form?

    All three forms have to count, because the writer scan now recognises the call shapes
    all three produce::

        from src.api.auth_metrics import auth_rejections   # auth_rejections.incr(...)
        from src.api import auth_metrics                   # auth_metrics.auth_rejections.incr(...)
        import src.api.auth_metrics                        # src.api.auth_metrics.auth_rejections.incr(...)

    Matching only the first left the two module-import forms outside `importers`, so a
    writer using them was neither required to be a known writer nor reported as
    unaccounted — the label it emits went into /metrics with no rule and nothing reddened.
    """
    package, _, leaf = _COUNTER_MODULE.rpartition(".")
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module == _COUNTER_MODULE:
                return True
            if node.module == package and any(a.name == leaf for a in node.names):
                return True
        elif isinstance(node, ast.Import):
            if any(a.name == _COUNTER_MODULE for a in node.names):
                return True
    return False


def _scan_incr_sites() -> tuple[set[str], set[str], list[str]]:
    """Walk src/ and return (reason labels, modules that increment, unreadable sites)."""
    labels: set[str] = set()
    writers: set[str] = set()
    unreadable: list[str] = []
    for rel, tree in _src_modules():
        constants = _module_string_constants(tree)
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "incr"
            ):
                continue
            # The counter is reachable by NAME (`auth_rejections.incr`) or THROUGH ITS
            # MODULE (`auth_metrics.auth_rejections.incr`, or the fully qualified
            # `src.api.auth_metrics.auth_rejections.incr`). Requiring `func.value` to be a
            # bare Name saw only the first: a module-qualified writer contributed nothing to
            # `labels`, nothing to `unreadable` and nothing to `writers`, so its label was
            # exported with no rule and every assertion in this file still passed. Match the
            # dotted TAIL, which is `auth_rejections` in all three forms.
            target = _dotted(node.func.value)
            if not target or target.rpartition(".")[2] != _COUNTER_NAME:
                continue
            writers.add(rel)
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                labels.add(arg.value)
            elif isinstance(arg, ast.Name) and arg.id in constants:
                labels.add(constants[arg.id])
            else:
                rendered = "<no argument>" if arg is None else ast.unparse(arg)
                resolver = _DYNAMIC_INCR_SITES.get((rel, rendered))
                if resolver is None:
                    unreadable.append(f"{rel}:{node.lineno} incr({rendered})")
                else:
                    labels |= resolver()
    return labels, writers, unreadable


def _reason_labels() -> set[str]:
    """Every ``curator_auth_rejections_total{reason}`` label ANY module in src/ can emit."""
    labels, _, unreadable = _scan_incr_sites()
    assert not unreadable, (
        "auth_rejections.incr() called with an argument this guard cannot read: "
        f"{unreadable} — give the label a literal/module constant, or register the site "
        "in _DYNAMIC_INCR_SITES with a resolver naming every label it can emit"
    )
    return labels


def test_every_module_touching_the_counter_is_a_known_writer_or_reader():
    """A module that imports the rejections counter must either increment it in a shape
    `_scan_incr_sites` understands, or be listed as a reader.

    Without this, the one way a new label could still slip past the scan is an aliased
    import (`from src.api.auth_metrics import auth_rejections as ar`) — the attribute match
    keys on the name `auth_rejections`, so `ar.incr("new_reason")` would contribute nothing
    and raise nothing. Redden: alias the import in any module that increments.

    For that backstop to hold, `importers` has to see EVERY way of reaching the module —
    `from src.api import auth_metrics` and `import src.api.auth_metrics` included. It did
    not, and those two are not exotic: they produce a perfectly ordinary
    `auth_metrics.auth_rejections.incr(...)` writer that used to fall outside the writer
    scan AND outside this net at the same time, i.e. through both gates at once.
    """
    importers = {rel for rel, tree in _src_modules() if _imports_counter(tree)}
    assert importers, "nobody imports the counter — the import match stopped matching"
    _, writers, _ = _scan_incr_sites()
    unaccounted = sorted(importers - writers - _COUNTER_READERS)
    assert not unaccounted, (
        f"modules import the rejections counter but this guard sees no increment in them: "
        f"{unaccounted} — call it as `auth_rejections.incr(...)`, or add the module to "
        "_COUNTER_READERS if it only reads"
    )
    stale = sorted(_COUNTER_READERS - importers)
    assert not stale, f"_COUNTER_READERS lists modules that no longer import it: {stale}"


def test_a_module_qualified_writer_is_recognised_as_both_writer_and_importer():
    """The scan must see a writer that imports the MODULE, not the counter name.

    Asserted on synthetic source because no module under src/ is written this way today —
    which is exactly why the hole survived: both halves of the net keyed on the one form
    src/ happens to use. A writer spelled

        from src.api import auth_metrics
        auth_metrics.auth_rejections.incr("new_reason")

    used to miss the writer match (`func.value` is an Attribute, not a Name) AND the
    importer match (`node.module` is "src.api", not "src.api.auth_metrics"), so its label
    landed in /metrics with no rule while every test in this file stayed green. Both
    halves are checked here, and the fully qualified `import src.api.auth_metrics` form
    with it.
    """
    via_package = ast.parse(
        "from src.api import auth_metrics\n"
        "auth_metrics.auth_rejections.incr('probe_a')\n"
    )
    via_absolute = ast.parse(
        "import src.api.auth_metrics\n"
        "src.api.auth_metrics.auth_rejections.incr('probe_b')\n"
    )
    plain = ast.parse(
        "from src.api.auth_metrics import auth_rejections\n"
        "auth_rejections.incr('probe_c')\n"
    )
    for name, tree in (("package", via_package), ("absolute", via_absolute), ("plain", plain)):
        assert _imports_counter(tree), f"{name} import form is invisible to the importer scan"
        calls = [
            node
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "incr"
        ]
        assert calls, f"{name}: no incr call parsed"
        # The writer match in `_scan_incr_sites`: the dotted tail names the counter.
        tail = _dotted(calls[0].func.value)
        assert tail and tail.rpartition(".")[2] == _COUNTER_NAME, (
            f"{name} call form is invisible to the writer scan: {tail}"
        )
    # An unrelated `.incr()` on something else must NOT be mistaken for the counter.
    other = ast.parse("stats.requests.incr('x')\n")
    call = next(n for n in ast.walk(other) if isinstance(n, ast.Call))
    assert _dotted(call.func.value).rpartition(".")[2] != _COUNTER_NAME
    assert not _imports_counter(other)


def test_a_literal_reject_reason_still_becomes_a_label_needing_a_rule():
    """A reason passed to the reject helpers as a bare string must reach the label set.

    `_DYNAMIC_INCR_SITES` resolves the two computed `incr` sites from the protocol
    CONSTANTS, which is where every reason lives today — but the helpers take a plain
    `str`, so `_reject_enroll(ws, app, "new_thing")` would emit `enroll_new_thing` with no
    constant to enumerate it. The call sites (and the one gate whose verdict the enroll
    call forwards) are therefore read for literals too. Asserted on synthetic source: the
    real src/ passes constants only, which is precisely the state in which this could rot
    unnoticed.
    """
    tree = ast.parse(
        "async def f():\n"
        "    await _reject_enroll(websocket, app, 'brand_new')\n"
        "    await _reject(websocket, app, instance_id, 'hello_new')\n"
    )
    # Same extraction the scanners run, against a tree we control.
    got_enroll = {
        n.args[2].value
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_reject_enroll"
    }
    assert got_enroll == {"brand_new"}
    # And end to end on the real tree: every literal the live call sites carry is in the
    # label set the rule check consumes.
    labels = _reason_labels()
    for literal in _literal_reason_args("src/ext/channel.py", "_reject_enroll", 2):
        assert "enroll_" + literal in labels
    for literal in _literal_returns("src/ext/protocol.py", "enroll_reject_reason"):
        assert "enroll_" + literal in labels
    for literal in _literal_reason_args("src/ext/channel.py", "_reject", 3):
        assert literal in labels


def test_every_reject_reason_is_alerted_on_or_explicitly_excused():
    """Enumerate the reason labels FROM EVERY WRITER in src/ and require a rule for each.

    Two rounds of the same bug live behind this test. First the needle list was written by
    hand and `ENROLL_SECRET_CONFLICT` slipped through it. Then the list was derived — but
    only from `src/ext/protocol.py`, so `cors_preflight`, written in src/api/cors.py, was
    just as invisible: derived list, hand-picked source. The source is now every
    `auth_rejections.incr()` call under src/, so a label added in ANY module reddens this
    unless it gets a rule or a line in `_REASONS_WITHOUT_A_RULE` — which forces the "does
    this deserve an alert?" decision to be made and written down rather than defaulted to
    no.
    """
    exprs = " ".join(r["expr"] for r in _rules())
    labels = _reason_labels()
    # The exclusion list must not outlive the labels it excuses, or it silently starts
    # excusing nothing while looking authoritative.
    assert _REASONS_WITHOUT_A_RULE <= labels, (
        f"excused reasons that no longer exist: {_REASONS_WITHOUT_A_RULE - labels}"
    )
    for label in sorted(labels - _REASONS_WITHOUT_A_RULE):
        needle = f'curator_auth_rejections_total{{reason="{label}"}}'
        assert needle in exprs, (
            f"exported but never alerted on: {needle} — add a rule to deploy/alerts.yml, "
            "or list the reason in _REASONS_WITHOUT_A_RULE with why it needs none"
        )


def test_the_label_scan_sees_writers_outside_the_protocol_module():
    """The scan must cover the modules the old protocol-only source could not see.

    Asserted directly, because "the source is now every writer" is the whole fix and it
    would degrade silently: if the walk were narrowed back to src/ext, or the attribute
    match broke, every test above would still pass on the protocol labels alone. These
    three labels are born in three different packages and none of them exists in
    src/ext/protocol.py.
    """
    labels = _reason_labels()
    assert {"cors_preflight", "admin_bad_token", "mcp_token"} <= labels
    # And the protocol-derived half is still there — the scan ADDED writers, it did not
    # replace the dynamic channel sites.
    assert {"enroll_secret_conflict", "unknown_instance"} <= labels


def test_secret_conflict_rule_fires_on_a_single_occurrence():
    """A credential-substitution attempt is not a fat-finger, so its threshold is 0.

    `secret_conflict` means the window was open and the code was right, but the secret
    differed from the one frozen on the pending row — a healthy client always re-registers
    with the same durable secret, so this does not happen by accident. Reddens if the rule
    is given a brute-force-style threshold that would sit silent through a successful
    single-shot substitution."""
    rule = next(r for r in _rules() if r["alert"] == "curator-enroll-secret-conflict")
    assert 'curator_auth_rejections_total{reason="enroll_secret_conflict"}' in rule["expr"]
    assert "increase(" in rule["expr"]
    assert rule["expr"].strip().endswith("> 0")


def test_cors_preflight_rejections_have_a_rule():
    """The §12 «бесшумный отказ» must be alertable, not merely counted.

    An EMPTY `EXT_ALLOWED_ORIGINS` leaves /ext open and /api/* CORS closed (the deliberate
    asymmetry in `parse_origins`), so every instance stays connected and green while every
    startpage fetch dies at the preflight and the newtab re-renders a cache that never
    refreshes. The other mismatch — a non-empty list with the wrong id — is already covered:
    /ext rejects the same origin and `reject_reason='origin'` reddens the status bar. It is
    the empty case that leaves NO row and NO reject_reason, which is why this counter needs
    a rule of its own rather than a line in `_REASONS_WITHOUT_A_RULE`.

    Redden: delete the rule, and both `parse_origins`' claim that the counter makes the
    failure "audible" and deploy/DEPLOY.md §4's list of exposing signals become false.
    """
    rule = next(r for r in _rules() if r["alert"] == "curator-cors-preflight-rejected")
    assert 'curator_auth_rejections_total{reason="cors_preflight"}' in rule["expr"]
    # increase() over a window, so a healthy install's ABSENT series stays silent and one
    # historical rejection cannot page forever.
    assert "increase(" in rule["expr"]
    # The summary must name the variable to fix: the metric name says "CORS preflight",
    # which does not tell an operator that the cause is EXT_ALLOWED_ORIGINS.
    assert "EXT_ALLOWED_ORIGINS" in rule["annotations"]["summary"]


def test_scrape_job_name_matches_the_alert_selectors():
    # deploy/scrape.yml's job_name is what `up{job="curator"}` matches; a rename on
    # either side silently disables target-loss alerting.
    scrape = yaml.safe_load(
        (ALERTS_YML.parent / "scrape.yml").read_text(encoding="utf-8")
    )
    job_names = {job["job_name"] for job in scrape["scrape_configs"]}
    assert "curator" in job_names
