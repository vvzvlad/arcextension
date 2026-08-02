"""Pure decision-engine tests (§7 steps 4-8) — the mutation-checkable core.

Each test builds a frozen :class:`Mirror` and asserts the decisions. Every guard is
written so that REMOVING it reddens the named test (step-7/8 non-conflation,
full-URL dedup, phase-A same-pass exclusion, the step-4 close guards, phase-B
source-first, deferred-when-target-not-ready).
"""

from __future__ import annotations

from src.curator.decide import (
    AbandonReloc,
    Close,
    PhaseAOpen,
    PhaseBVerify,
    decide,
)
from src.curator.mirror import InstanceRow, Mirror, RelocRow, TabRow

HOUR = 3_600_000
NOW = 10_000_000
IDLE = HOUR  # IDLE_MINUTES=60


def _tab(instance_id, tab_id, url, *, last_active_at=None, opened_at=0, window_id=1,
         pinned=0, active=0, audible=0, age_unknown=0, title="t"):
    # Default: idle for two hours (well past IDLE), so step-4 passes unless a flag says otherwise.
    if last_active_at is None:
        last_active_at = NOW - 2 * HOUR
    return TabRow(
        instance_id=instance_id, tab_id=tab_id, window_id=window_id, url=url,
        title=title, pinned=pinned, active=active, audible=audible,
        opened_at=opened_at, last_active_at=last_active_at, age_unknown=age_unknown,
    )


def _inst(instance_id, *, session="s", focused_window_id=None, connected=1, snapshot_at=NOW):
    return InstanceRow(
        id=instance_id, connected=connected, focused_window_id=focused_window_id,
        session_id=session, snapshot_at=snapshot_at, conn_epoch=1,
    )


def _rule(rid, pattern, instance_id, *, singleton=0, invalid=0):
    return {"id": rid, "pattern": pattern, "instance_id": instance_id,
            "singleton": singleton, "invalid": invalid}


def _mirror(tabs, instances, rules=(), windows=None, exemptions=(), quarantine=(), relocs=()):
    inst_map = {i.id: i for i in instances}
    win = windows if windows is not None else {
        (t.instance_id, t.window_id): ("normal", "normal") for t in tabs
    }
    return Mirror(
        tabs=list(tabs), instances=inst_map, windows=win, rules=list(rules),
        exemptions=list(exemptions), quarantine=list(quarantine),
        live_relocations=list(relocs),
    )


def _decide(mirror, ready):
    return decide(mirror, set(ready), now=NOW, idle_ms=IDLE, main_instance_id="main")


# --- HEADLINE: a singleton-ruled tab opened away-from-home is RELOCATED ------
def test_singleton_in_foreign_instance_relocated_not_closed():
    # rule grafana.lc -> prox, singleton. prox holds /d/abc; the human opened a
    # DIFFERENT dashboard /d/xyz in main and left it idle. It must be RELOCATED to
    # prox, never closed out of main as a "copy" of /d/abc (§7 non-conflation).
    rules = [_rule(1, "grafana.lc", "prox", singleton=1)]
    tabs = [
        _tab("prox", 10, "https://grafana.lc/d/abc"),
        _tab("main", 20, "https://grafana.lc/d/xyz"),
    ]
    res = _decide(_mirror(tabs, [_inst("prox"), _inst("main")], rules), {"prox", "main"})

    # RELOCATE (phase A), not a close.
    assert [(d.tab.tab_id, d.home) for d in res.phase_a] == [(20, "prox")]
    assert res.closes == []
    # And nothing closed /d/xyz anywhere.
    assert all(d.tab.tab_id != 20 for d in res.closes)


# --- step 7 q2: dedup is by the FULL url string, never the normalized one ----
def test_q2_dedup_full_url_not_normalized():
    rules = [_rule(1, "grafana.lc", "prox")]
    # prox already holds /d/abc?x=1. Two main tabs: an EXACT full-url match (dedupe)
    # and a query-only-different one (a genuinely different tab => relocate).
    tabs = [
        _tab("prox", 10, "https://grafana.lc/d/abc?x=1"),
        _tab("main", 20, "https://grafana.lc/d/abc?x=1"),   # exact => dedupe_close
        _tab("main", 21, "https://grafana.lc/d/abc?y=2"),   # different full => relocate
    ]
    res = _decide(_mirror(tabs, [_inst("prox"), _inst("main")], rules), {"prox", "main"})

    dedupes = [d for d in res.closes if d.decision == "dedupe"]
    assert [d.tab.tab_id for d in dedupes] == [20]
    assert [d.tab.tab_id for d in res.phase_a] == [21]
    # Survivor named is the pre-existing target tab.
    assert dedupes[0].survivor.tab_id == 10


# --- phase-A copies made THIS pass are excluded from step 7/8 (frozen mirror) -
def test_two_identical_sources_one_phase_a_other_deferred_no_same_pass_collapse():
    # Two identical tabs in main both route to an EMPTY prox. Exactly ONE opens; the
    # second is DEFERRED to the next pass — never a second phase A (that would be two
    # copies the curator itself created, which §7 forbids: for a `main` target the dupe
    # would survive forever, since `main` is a sink without dedup, §15).
    #
    # And the same-pass collapse ban still holds: neither tab may be dedupe- or
    # singleton-closed against the OTHER's not-yet-created copy — the copies do not
    # exist in the frozen mirror (§7 phase-A exclusion). The deferred tab WAITS; it is
    # not closed.
    rules = [_rule(1, "grafana.lc", "prox")]
    tabs = [
        _tab("main", 20, "https://grafana.lc/d/abc"),
        _tab("main", 21, "https://grafana.lc/d/abc"),
    ]
    res = _decide(_mirror(tabs, [_inst("prox"), _inst("main")], rules), {"prox", "main"})
    assert [d.tab.tab_id for d in res.phase_a] == [20]      # only the first opens
    assert res.closes == []                                  # nothing collapsed in-pass
    # The second is journalled as a deferral against the target, with the cause named.
    assert res.deferred == {"prox": 1}
    assert res.deferred_same_url == {"prox": 1}


def test_two_identical_sources_to_main_do_not_both_open():
    # The case that never self-heals: `main` is a sink WITHOUT dedup (§15), so a second
    # copy the curator opens there survives forever, and next pass's question (b) cannot
    # catch it either (phase B already closed both sources). Drop the per-pass
    # (target, url) set and this reddens with two phase-A opens into main.
    rules = [_rule(1, "grafana.lc", "main")]
    tabs = [
        _tab("prox", 20, "https://grafana.lc/d/abc"),
        _tab("prox", 21, "https://grafana.lc/d/abc"),
    ]
    res = _decide(_mirror(tabs, [_inst("prox"), _inst("main")], rules), {"prox", "main"})
    assert [d.tab.tab_id for d in res.phase_a] == [20]
    assert res.deferred_same_url == {"main": 1}


def test_two_DIFFERENT_urls_to_same_target_both_open():
    # The gate is per (target, FULL url), not per target: two different urls headed for
    # the same instance must BOTH open in one pass (else every relocation serializes).
    rules = [_rule(1, "grafana.lc", "prox")]
    tabs = [
        _tab("main", 20, "https://grafana.lc/d/abc"),
        _tab("main", 21, "https://grafana.lc/d/xyz"),
    ]
    res = _decide(_mirror(tabs, [_inst("prox"), _inst("main")], rules), {"prox", "main"})
    assert sorted(d.tab.tab_id for d in res.phase_a) == [20, 21]
    assert res.deferred == {}


# --- an orphaned rule wins the §8 ladder => deferred, never a foreign home ----
def test_orphan_beats_a_broader_valid_rule_on_the_specificity_ladder():
    """The specificity ladder (§8) must run ONCE, across valid and orphaned rules alike.

    ``*.borneo.lc -> prox`` is valid; the more specific ``www.borneo.lc -> ghost`` was
    orphaned (its instance retired). Before the flag the ladder picks the longer pattern,
    the home is unreachable, and the tab is DEFERRED — untouched. Asking the valid rules
    first and only then the orphans runs the ladder twice: ``*.borneo.lc`` wins by
    default and the tab is relocated into ``prox`` — a home the owner never chose for it,
    triggered by retiring an unrelated instance. Reddens under that two-step lookup:
    phase_a becomes [(20, 'prox')] and deferred is empty.
    """
    rules = [
        _rule(1, "*.borneo.lc", "prox"),
        _rule(2, "www.borneo.lc", "ghost", invalid=1),
    ]
    tabs = [_tab("main", 20, "https://www.borneo.lc/page")]
    res = _decide(
        _mirror(tabs, [_inst("main"), _inst("prox")], rules), {"main", "prox"}
    )
    assert res.phase_a == []
    assert res.closes == []
    assert res.deferred == {"ghost": 1}


def test_broader_orphan_does_not_shadow_a_more_specific_valid_rule():
    """The mirror image: when the VALID rule is the more specific one it still wins, so
    flagging a broad orphan does not freeze everything underneath it."""
    rules = [
        _rule(1, "*.borneo.lc", "ghost", invalid=1),
        _rule(2, "www.borneo.lc", "prox"),
    ]
    tabs = [_tab("main", 20, "https://www.borneo.lc/page")]
    res = _decide(
        _mirror(tabs, [_inst("main"), _inst("prox")], rules), {"main", "prox"}
    )
    assert [(d.tab.tab_id, d.home) for d in res.phase_a] == [(20, "prox")]
    assert res.deferred == {}


def test_orphaned_rule_defers_instead_of_draining_to_main():
    """A tab whose only matching rule is orphaned must not fall into the unruled ->
    main drain. A second, valid rule keeps the drain switched ON so the assertion is
    not vacuous. Reddens if orphans are excluded from matching: the tab routes to main.
    """
    rules = [
        _rule(1, "grafana.lc", "ghost", invalid=1),
        _rule(2, "other.lc", "prox"),   # keeps has_active_rules() true => drain on
    ]
    tabs = [_tab("prox", 20, "https://grafana.lc/d/x")]
    res = _decide(
        _mirror(tabs, [_inst("main"), _inst("prox")], rules), {"main", "prox"}
    )
    assert res.phase_a == []
    assert res.deferred == {"ghost": 1}


# --- unruled tab in main lives forever --------------------------------------
def test_unruled_in_main_lives():
    rules = [_rule(1, "grafana.lc", "prox")]  # rules exist (drain on)
    tabs = [_tab("main", 20, "https://random.example/page")]
    res = _decide(_mirror(tabs, [_inst("main"), _inst("prox")], rules), {"main", "prox"})
    assert res.is_empty()


# --- empty rules table disables the drain -----------------------------------
def test_empty_rules_drain_off():
    # No rules at all: an unruled tab in a THEMED instance must NOT drain to main.
    tabs = [_tab("prox", 20, "https://random.example/page")]
    res = _decide(_mirror(tabs, [_inst("prox"), _inst("main")], rules=[]), {"prox", "main"})
    assert res.is_empty()


def test_invalid_only_rules_drain_off():
    # A rule table with only an invalid rule is "empty" for the drain (§8).
    rules = [_rule(1, "grafana.lc", "prox", invalid=1)]
    tabs = [_tab("prox", 20, "https://random.example/page")]
    res = _decide(_mirror(tabs, [_inst("prox"), _inst("main")], rules), {"prox", "main"})
    assert res.is_empty()


# --- step-4 close guards apply to EVERY kind --------------------------------
def test_pinned_blocks_relocation():
    rules = [_rule(1, "grafana.lc", "prox")]
    tabs = [_tab("main", 20, "https://grafana.lc/d/x", pinned=1)]
    res = _decide(_mirror(tabs, [_inst("prox"), _inst("main")], rules), {"prox", "main"})
    assert res.is_empty()
    # Sanity: drop the pin and it DOES relocate (proves the guard is what blocked).
    tabs2 = [_tab("main", 20, "https://grafana.lc/d/x", pinned=0)]
    res2 = _decide(_mirror(tabs2, [_inst("prox"), _inst("main")], rules), {"prox", "main"})
    assert len(res2.phase_a) == 1


def test_audible_blocks_close():
    # An audible duplicate inside a non-main home must NOT be closed (§7 guard).
    rules = [_rule(1, "grafana.lc", "prox")]
    tabs = [
        _tab("prox", 10, "https://grafana.lc/d/abc"),
        _tab("prox", 11, "https://grafana.lc/d/abc", audible=1),  # dup, but audible
    ]
    res = _decide(_mirror(tabs, [_inst("prox"), _inst("main")], rules), {"prox", "main"})
    assert res.closes == []


def test_active_in_focus_blocks():
    rules = [_rule(1, "grafana.lc", "prox")]
    # active AND its window is the instance's focused window => on screen => skip.
    tabs = [_tab("main", 20, "https://grafana.lc/d/x", active=1, window_id=7)]
    inst = _inst("main", focused_window_id=7)
    res = _decide(_mirror(tabs, [inst, _inst("prox")], rules,
                          windows={("main", 7): ("normal", "normal")}), {"prox", "main"})
    assert res.is_empty()


def test_fullscreen_window_blocks():
    rules = [_rule(1, "grafana.lc", "prox")]
    tabs = [_tab("main", 20, "https://grafana.lc/d/x", window_id=9)]
    res = _decide(_mirror(tabs, [_inst("main"), _inst("prox")], rules,
                          windows={("main", 9): ("normal", "fullscreen")}), {"prox", "main"})
    assert res.is_empty()


def test_missing_window_skips_tab():
    # §6: a tab whose window is absent from the snapshot is skipped in the pass.
    rules = [_rule(1, "grafana.lc", "prox")]
    tabs = [_tab("main", 20, "https://grafana.lc/d/x", window_id=5)]
    res = _decide(_mirror(tabs, [_inst("main"), _inst("prox")], rules,
                          windows={}), {"prox", "main"})  # NO window rows
    assert res.is_empty()
    # With the window present it DOES relocate (proves the window presence is what gated).
    res2 = _decide(_mirror(tabs, [_inst("main"), _inst("prox")], rules,
                           windows={("main", 5): ("normal", "normal")}), {"prox", "main"})
    assert len(res2.phase_a) == 1


def test_not_idle_blocks():
    rules = [_rule(1, "grafana.lc", "prox")]
    tabs = [_tab("main", 20, "https://grafana.lc/d/x", last_active_at=NOW - 60_000)]  # 1 min
    res = _decide(_mirror(tabs, [_inst("main"), _inst("prox")], rules), {"prox", "main"})
    assert res.is_empty()


def test_active_quarantine_and_exemption_block():
    rules = [_rule(1, "grafana.lc", "prox")]
    tabs = [_tab("main", 20, "https://grafana.lc/d/x")]
    norm = "https://grafana.lc/d/x"
    q = _decide(_mirror(tabs, [_inst("main"), _inst("prox")], rules,
                        quarantine=[("main", norm, NOW + HOUR)]), {"prox", "main"})
    assert q.is_empty()
    e = _decide(_mirror(tabs, [_inst("main"), _inst("prox")], rules,
                        exemptions=[("main", norm, NOW + HOUR)]), {"prox", "main"})
    assert e.is_empty()
    # An EXPIRED quarantine does not block.
    live = _decide(_mirror(tabs, [_inst("main"), _inst("prox")], rules,
                           quarantine=[("main", norm, NOW - 1)]), {"prox", "main"})
    assert len(live.phase_a) == 1


# --- phase B: source verified FIRST -----------------------------------------
def test_phase_b_source_url_mismatch_abandons_and_redecides():
    # A live relocate row whose SOURCE now holds a DIFFERENT url: the human moved it.
    # => abandon the row (source_url_mismatch), and the moved tab re-decides THIS pass
    # (here it matches another rule and relocates). The target is NOT verified.
    rules = [_rule(1, "grafana.lc", "prox")]
    reloc = RelocRow(
        id=500, instance_from="main", session_id_from="s", tab_id=20,
        instance_to="prox", session_id_to="s", tab_id_to=99,
        url="https://grafana.lc/d/OLD", url_norm="https://grafana.lc/d/OLD",
        rule_id=1, rule_pattern="grafana.lc",
        src_opened_at=0, src_last_active_at=NOW - 2 * HOUR, src_age_unknown=0,
    )
    # tab 20 now shows a DIFFERENT (still grafana) url.
    tabs = [_tab("main", 20, "https://grafana.lc/d/NEW")]
    res = _decide(
        _mirror(tabs, [_inst("main"), _inst("prox")], rules, relocs=[reloc]),
        {"main", "prox"},
    )
    assert res.abandon == [AbandonReloc(500, "source_url_mismatch")]
    assert res.phase_b == []
    # The moved tab was NOT owned by the (now abandoned) row, so it re-decided.
    assert [d.tab.tab_id for d in res.phase_a] == [20]


def test_phase_b_source_matches_owns_and_verifies():
    rules = [_rule(1, "grafana.lc", "prox")]
    reloc = RelocRow(
        id=501, instance_from="main", session_id_from="s", tab_id=20,
        instance_to="prox", session_id_to="s", tab_id_to=99,
        url="https://grafana.lc/d/abc", url_norm="https://grafana.lc/d/abc",
        rule_id=1, rule_pattern="grafana.lc",
        src_opened_at=0, src_last_active_at=NOW - 2 * HOUR, src_age_unknown=0,
    )
    tabs = [_tab("main", 20, "https://grafana.lc/d/abc")]  # source still holds the url
    res = _decide(
        _mirror(tabs, [_inst("main"), _inst("prox")], rules, relocs=[reloc]),
        {"main", "prox"},
    )
    assert len(res.phase_b) == 1 and res.phase_b[0].reloc.id == 501
    # The source tab is OWNED by phase B => it is NOT re-routed (no phase-A re-open).
    assert res.phase_a == []
    assert res.abandon == []


def test_phase_b_source_gone_abandons():
    reloc = RelocRow(
        id=502, instance_from="main", session_id_from="s", tab_id=20,
        instance_to="prox", session_id_to="s", tab_id_to=99,
        url="https://grafana.lc/d/abc", url_norm="https://grafana.lc/d/abc",
        rule_id=None, rule_pattern=None,
        src_opened_at=0, src_last_active_at=NOW - 2 * HOUR, src_age_unknown=0,
    )
    res = _decide(  # source tab is gone entirely
        _mirror([], [_inst("main"), _inst("prox")], relocs=[reloc]),
        {"main", "prox"},
    )
    assert res.abandon == [AbandonReloc(502, "source_gone")]


# --- deferred when the target is not ready ----------------------------------
def test_deferred_when_target_not_ready():
    rules = [_rule(1, "grafana.lc", "prox")]
    tabs = [_tab("main", 20, "https://grafana.lc/d/x")]
    # prox is NOT in ready => the relocation is deferred, not opened.
    res = _decide(_mirror(tabs, [_inst("main"), _inst("prox")], rules), {"main"})
    assert res.phase_a == []
    assert res.deferred == {"prox": 1}


# --- intra-instance exact dedup only in NON-main ----------------------------
def test_intra_dedup_nonmain_only():
    rules = [_rule(1, "grafana.lc", "prox")]
    tabs = [
        _tab("prox", 10, "https://grafana.lc/d/abc", last_active_at=NOW - 2 * HOUR),
        _tab("prox", 11, "https://grafana.lc/d/abc", last_active_at=NOW - 3 * HOUR),  # older => loses
    ]
    res = _decide(_mirror(tabs, [_inst("prox"), _inst("main")], rules), {"prox", "main"})
    dedupes = [d for d in res.closes if d.decision == "dedupe"]
    assert [d.tab.tab_id for d in dedupes] == [11]      # older duplicate closes
    assert dedupes[0].survivor.tab_id == 10             # newer survives (ladder)


def test_main_is_a_sink_no_intra_dedup():
    # Two identical UNRULED tabs in main: main never dedups (§15).
    tabs = [
        _tab("main", 10, "https://x.example/p"),
        _tab("main", 11, "https://x.example/p"),
    ]
    rules = [_rule(1, "grafana.lc", "prox")]  # rules exist, but these are unruled
    res = _decide(_mirror(tabs, [_inst("main"), _inst("prox")], rules), {"main", "prox"})
    assert res.is_empty()


# --- singleton ladder keeps the winner --------------------------------------
def test_singleton_keeps_ladder_winner():
    rules = [_rule(1, "grafana.lc", "prox", singleton=1)]
    tabs = [
        _tab("prox", 10, "https://grafana.lc/d/a", last_active_at=NOW - 3 * HOUR),
        _tab("prox", 11, "https://grafana.lc/d/b", last_active_at=NOW - 2 * HOUR),  # newest => wins
        _tab("prox", 12, "https://grafana.lc/d/c", last_active_at=NOW - 4 * HOUR),
    ]
    res = _decide(_mirror(tabs, [_inst("prox"), _inst("main")], rules), {"prox", "main"})
    closed = sorted(d.tab.tab_id for d in res.closes if d.decision == "singleton")
    assert closed == [10, 12]
    assert all(d.survivor.tab_id == 11 for d in res.closes if d.decision == "singleton")


# --- unready source instance's tabs are not considered ----------------------
def test_unready_source_tabs_skipped():
    rules = [_rule(1, "grafana.lc", "prox")]
    tabs = [_tab("main", 20, "https://grafana.lc/d/x")]
    res = _decide(_mirror(tabs, [_inst("main"), _inst("prox")], rules), {"prox"})  # main NOT ready
    assert res.is_empty()
