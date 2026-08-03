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

import re
from pathlib import Path

import yaml

from src.settings import Settings

ALERTS_YML = Path(__file__).resolve().parents[1] / "deploy" / "alerts.yml"

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
    metrics_src = (
        Path(__file__).resolve().parents[1] / "src" / "api" / "metrics.py"
    ).read_text(encoding="utf-8")
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


def test_no_window_overdue_alert_until_the_gauge_is_clamped():
    # §37: there is deliberately NO alert on curator_enroll_window_seconds_remaining. The
    # gauge stays NEGATIVE forever after a NATURALLY-expired window (the settings row is
    # never cleared on expiry), so a `< 0` rule would fire once and never resolve. The
    # clamp-to-0 fix lives in the enrollment PR (#35); the window-overdue alert may be
    # added back only once it lands. Redden: reintroduce the rule before the clamp exists.
    alerts = {r["alert"] for r in _rules()}
    assert "curator-enroll-window-overdue" not in alerts
    assert not any(
        "curator_enroll_window_seconds_remaining" in r["expr"] for r in _rules()
    )


def test_enroll_bad_code_bruteforce_rule_scopes_to_the_reason():
    # §37: the brute-force alert must scope to the {reason="enroll_bad_code"} series (not
    # the whole auth-rejections family, which counts CORS/metrics/api rejections too) and
    # rate-limit via increase() so a healthy install's absent series stays silent. Redden:
    # drop the label matcher and a burst of unrelated rejections would page.
    rule = next(r for r in _rules() if r["alert"] == "curator-enroll-code-bruteforce")
    assert 'curator_auth_rejections_total{reason="enroll_bad_code"}' in rule["expr"]
    assert "increase(" in rule["expr"]


def test_enroll_bad_code_metric_has_an_alert_rule():
    # §37, the metric->rule direction the name-guard does NOT cover: #35 exports the
    # enroll_bad_code counter, but a series with no rule is DEAD (scraped, never alerted
    # on). Assert it is referenced by at least one rule here — the reason this rule was
    # added in #37. Redden: delete the bruteforce rule and this fails.
    #
    # (The other #35 gauge, curator_enroll_window_seconds_remaining, is intentionally NOT
    # alerted on yet — see test_no_window_overdue_alert_until_the_gauge_is_clamped — so it
    # is deliberately absent from this metric->rule check.)
    exprs = " ".join(r["expr"] for r in _rules())
    assert 'curator_auth_rejections_total{reason="enroll_bad_code"}' in exprs


def test_scrape_job_name_matches_the_alert_selectors():
    # deploy/scrape.yml's job_name is what `up{job="curator"}` matches; a rename on
    # either side silently disables target-loss alerting.
    scrape = yaml.safe_load(
        (ALERTS_YML.parent / "scrape.yml").read_text(encoding="utf-8")
    )
    job_names = {job["job_name"] for job in scrape["scrape_configs"]}
    assert "curator" in job_names
