"""``simulate`` (preview) vs ``decide`` (the pass) on ONE fixture — the anti-drift guard.

§8 says the preview must model the WHOLE next pass and is the replacement for the removed
action-count limiter; it also says the preview is server-side precisely so it is not "a
second implementation of the matcher" that "would systematically lie". It drifted anyway:
the pass grew an orphaned-rule branch and a same-pass duplicate branch, and the preview —
carrying its own copy of the routing — kept reporting relocations the pass would never
perform, to the wrong destination.

So these tests do not assert hand-computed numbers. They build one mirror, run BOTH
engines over it, and compare. A future branch added to only one side reddens here.

KNOWN and deliberate divergence, excluded from the fixtures below: preview does not read
live ``relocate`` rows (phase-A/phase-B bookkeeping), so no fixture carries one. That is
documented in ``src/rules/preview.py``'s module docstring.
"""

from types import SimpleNamespace

from src.curator.decide import decide
from src.curator.mirror import InstanceRow, Mirror, TabRow
from src.rules.preview import PreviewInput, simulate

HOUR = 3_600_000
NOW = 10_000_000
IDLE = HOUR
MAIN = "main"


def _tab(instance_id, tab_id, url, *, last_active_at=None, opened_at=0, window_id=1,
         pinned=0, active=0, audible=0, age_unknown=0, title="t"):
    return TabRow(
        instance_id=instance_id, tab_id=tab_id, window_id=window_id, url=url, title=title,
        pinned=pinned, active=active, audible=audible, opened_at=opened_at,
        last_active_at=NOW - 2 * HOUR if last_active_at is None else last_active_at,
        age_unknown=age_unknown,
    )


def _inst(instance_id, *, focused_window_id=None, connected=1):
    return InstanceRow(id=instance_id, connected=connected,
                       focused_window_id=focused_window_id, session_id="s",
                       snapshot_at=NOW, conn_epoch=1)


def _rule(rid, pattern, instance_id, *, singleton=0, invalid=0):
    return {"id": rid, "pattern": pattern, "instance_id": instance_id,
            "singleton": singleton, "invalid": invalid}


def _both(tabs, instances, rules=(), ready=None, windows=None,
          exemptions=(), quarantine=()):
    """Run ``decide`` and ``simulate`` over the SAME inputs; return both results.

    ``ready_ids`` (the pass) and the preview's "counted" set are the same concept —
    instances whose mirror this run trusts — so one argument feeds both.
    """
    inst_list = list(instances)
    ready_ids = set(ready) if ready is not None else {i.id for i in inst_list}
    win = windows if windows is not None else {
        (t.instance_id, t.window_id): ("normal", "normal") for t in tabs
    }
    mirror = Mirror(
        tabs=list(tabs), instances={i.id: i for i in inst_list}, windows=win,
        rules=list(rules), exemptions=list(exemptions), quarantine=list(quarantine),
        live_relocations=[],
    )
    dec = decide(mirror, ready_ids, now=NOW, idle_ms=IDLE, main_instance_id=MAIN)

    inp = PreviewInput(
        rules=list(rules),
        tabs=[
            SimpleNamespace(**{f: getattr(t, f) for f in TabRow.__dataclass_fields__})
            for t in tabs
        ],
        instances=[
            SimpleNamespace(id=i.id, connected=i.connected, snapshot_at=i.snapshot_at,
                            focused_window_id=i.focused_window_id)
            for i in inst_list
        ],
        windows=win,
        exemptions=list(exemptions),
        quarantine=list(quarantine),
        now=NOW, idle_ms=IDLE, state_fresh_ms=10 ** 12, main_instance_id=MAIN,
        # Feed the preview the same trust set the pass got, so the comparison is about
        # ROUTING and nothing else.
        counted_override={i.id: ((i.id in ready_ids), "fresh") for i in inst_list},
    )
    prev = simulate(inp)
    return dec, prev


def _assert_agree(dec, prev):
    """The two engines must agree on every number a human is shown."""
    assert prev.relocations == len(dec.phase_a), (
        f"relocations: preview {prev.relocations} vs pass {len(dec.phase_a)}"
    )
    assert prev.closures == len(dec.closes), (
        f"closures: preview {prev.closures} vs pass {len(dec.closes)}"
    )
    assert prev.deferred_by_target == dict(dec.deferred), (
        f"deferred: preview {prev.deferred_by_target} vs pass {dict(dec.deferred)}"
    )
    # …and on WHERE each relocation goes, not merely how many there are.
    assert sorted((e["url"], e["from"], e["to"]) for e in prev.relocation_examples) == \
        sorted((d.tab.url, d.tab.instance_id, d.home) for d in dec.phase_a)


# --- THE regression: an orphaned rule defers, it does NOT drain to main -----
def test_orphaned_rule_defers_in_both_engines(tmp_path):
    """The reviewer's exact configuration.

    ``grafana.lc -> ghost`` is orphaned (its instance is retired, so the rule is flagged
    ``invalid``); a valid ``other.lc -> prox`` keeps the policy non-empty so the drain is
    on. The tab in ``prox`` matches the ORPHANED rule.

    The pass defers it (its home exists as policy, not as an instance) and leaves it
    alone. The preview used to call it unruled, drain it to ``main`` and report
    ``relocations: 1`` pointing at the wrong instance — an inflated count AND a false
    destination on the screen the owner confirms from.
    """
    tabs = [_tab("prox", 1, "https://grafana.lc/d/x")]
    rules = [_rule(1, "grafana.lc", "ghost", invalid=1), _rule(2, "other.lc", "prox")]
    dec, prev = _both(tabs, [_inst(MAIN), _inst("prox")], rules)

    _assert_agree(dec, prev)
    # Pinned explicitly so the test states the EXPECTED behaviour, not just agreement
    # (two engines could agree by both being wrong).
    assert prev.relocations == 0 and prev.relocation_examples == []
    assert prev.deferred_by_target == {"ghost": 1}
    assert dec.phase_a == [] and dict(dec.deferred) == {"ghost": 1}


def test_path_prefix_rule_agrees(tmp_path):
    """A path-prefix rule (#43) must route identically in preview and pass. One tab
    under /wirenboard relocates to 'work'; one under /personal has no rule and drains
    to main. Both engines must agree on the count AND the destinations."""
    tabs = [
        _tab("prox", 1, "https://github.com/wirenboard/wb-mqtt"),
        _tab("prox", 2, "https://github.com/personal/notes"),
    ]
    rules = [_rule(1, "github.com/wirenboard", "work")]
    dec, prev = _both(tabs, [_inst(MAIN), _inst("prox"), _inst("work")], rules)
    _assert_agree(dec, prev)
    assert prev.relocations == 2  # one to 'work', one drained to main
    dests = {e["url"]: e["to"] for e in prev.relocation_examples}
    assert dests["https://github.com/wirenboard/wb-mqtt"] == "work"
    assert dests["https://github.com/personal/notes"] == MAIN


def test_second_identical_tab_defers_instead_of_opening_twice(tmp_path):
    """Two tabs, same URL, same target, one pass: the pass opens ONE copy and defers the
    other (§7 forbids the curator creating duplicates of its own). The preview used to
    count two relocations."""
    tabs = [
        _tab("prox", 1, "https://grafana.lc/d/x"),
        _tab("prox", 2, "https://grafana.lc/d/x"),
    ]
    rules = [_rule(1, "grafana.lc", MAIN)]
    dec, prev = _both(tabs, [_inst(MAIN), _inst("prox")], rules)

    _assert_agree(dec, prev)
    assert prev.relocations == 1
    assert prev.deferred_by_target == {MAIN: 1}


# --- the branches that already agreed must KEEP agreeing --------------------
def test_plain_relocation_agrees(tmp_path):
    tabs = [_tab("prox", 1, "https://grafana.lc/d/x")]
    rules = [_rule(1, "grafana.lc", MAIN)]
    dec, prev = _both(tabs, [_inst(MAIN), _inst("prox")], rules)
    _assert_agree(dec, prev)
    assert prev.relocations == 1


def test_unruled_drain_to_main_agrees(tmp_path):
    # A themed tab with no rule drains to main — but ONLY while some rule exists (§8).
    tabs = [_tab("prox", 1, "https://random.io/x")]
    rules = [_rule(1, "grafana.lc", MAIN)]
    dec, prev = _both(tabs, [_inst(MAIN), _inst("prox")], rules)
    _assert_agree(dec, prev)
    assert prev.relocations == 1
    assert prev.relocation_examples[0]["to"] == MAIN


def test_empty_policy_touches_nothing_in_both(tmp_path):
    tabs = [_tab("prox", 1, "https://random.io/x")]
    dec, prev = _both(tabs, [_inst(MAIN), _inst("prox")], [])
    _assert_agree(dec, prev)
    assert prev.relocations == 0 and prev.closures == 0


def test_target_not_ready_defers_in_both(tmp_path):
    tabs = [_tab("prox", 1, "https://grafana.lc/d/x")]
    rules = [_rule(1, "grafana.lc", "media")]
    dec, prev = _both(
        tabs, [_inst(MAIN), _inst("prox"), _inst("media")], rules,
        ready={MAIN, "prox"},           # media is not ready this pass
    )
    _assert_agree(dec, prev)
    assert prev.deferred_by_target == {"media": 1}


def test_inter_instance_dedup_agrees(tmp_path):
    # The target already holds this exact URL => a close, never a relocation (§7 q2).
    tabs = [
        _tab("prox", 1, "https://grafana.lc/d/x"),
        _tab(MAIN, 9, "https://grafana.lc/d/x"),
    ]
    rules = [_rule(1, "grafana.lc", MAIN)]
    dec, prev = _both(tabs, [_inst(MAIN), _inst("prox")], rules)
    _assert_agree(dec, prev)
    assert prev.relocations == 0 and prev.closures == 1


def test_singleton_collapse_agrees(tmp_path):
    tabs = [
        _tab("prox", 1, "https://grafana.lc/a", last_active_at=NOW - 3 * HOUR),
        _tab("prox", 2, "https://grafana.lc/b", last_active_at=NOW - 2 * HOUR),
    ]
    rules = [_rule(1, "grafana.lc", "prox", singleton=1)]
    dec, prev = _both(tabs, [_inst(MAIN), _inst("prox")], rules)
    _assert_agree(dec, prev)
    assert prev.closures == 1


def test_intra_instance_dedup_agrees(tmp_path):
    tabs = [
        _tab("prox", 1, "https://grafana.lc/a"),
        _tab("prox", 2, "https://grafana.lc/a"),
    ]
    rules = [_rule(1, "grafana.lc", "prox")]
    dec, prev = _both(tabs, [_inst(MAIN), _inst("prox")], rules)
    _assert_agree(dec, prev)
    assert prev.closures == 1


def test_step4_guards_agree(tmp_path):
    # pinned / audible / active-in-focused-window / not-idle are all skipped by both.
    tabs = [
        _tab("prox", 1, "https://grafana.lc/a", pinned=1),
        _tab("prox", 2, "https://grafana.lc/b", audible=1),
        _tab("prox", 3, "https://grafana.lc/c", active=1, window_id=7),
        _tab("prox", 4, "https://grafana.lc/d", last_active_at=NOW - 60_000),
    ]
    rules = [_rule(1, "grafana.lc", MAIN)]
    dec, prev = _both(
        tabs, [_inst(MAIN), _inst("prox", focused_window_id=7)], rules,
        windows={("prox", 1): ("normal", "normal"), ("prox", 7): ("normal", "normal")},
    )
    _assert_agree(dec, prev)
    assert prev.relocations == 0 and prev.closures == 0


def test_exemption_and_quarantine_agree(tmp_path):
    tabs = [
        _tab("prox", 1, "https://grafana.lc/a"),
        _tab("prox", 2, "https://grafana.lc/b"),
    ]
    rules = [_rule(1, "grafana.lc", MAIN)]
    dec, prev = _both(
        tabs, [_inst(MAIN), _inst("prox")], rules,
        exemptions=[("prox", "https://grafana.lc/a", NOW + HOUR)],
        quarantine=[("prox", "https://grafana.lc/b", NOW + HOUR)],
    )
    _assert_agree(dec, prev)
    assert prev.relocations == 0
