"""Server-side preview: model the WHOLE next pass against the current mirror (§8).

Preview is the replacement for the removed action-count limiter (§8, стр. 15), so
it must be honest about staleness and must model the whole pass, not just the one
edited rule:

* It applies the §8 matcher to the CURRENT ``tabs`` mirror and simulates the next
  pass's routing decision (§7 step 5) under the CANDIDATE rule set.
* It returns relocations and closures SEPARATELY, each with ``{url, title}``
  examples — a human recognizes "Draft: …" by title, not URL (§8).
* Freshness honesty: every instance carries ``snapshot_at`` + ``connected``; an
  instance that is disconnected or whose mirror is stale is marked **not counted**
  (never a silent zero), because its ``tabs`` rows are not deleted and would
  otherwise under-report the burst (§8).
* An empty→non-empty transition enables the "unruled → main" drain for ALL themed
  instances — the largest burst the system makes — which is a property of the pass,
  not of the one rule, so it is modeled here (§8).
* ``pinned=1`` tabs are never relocated or closed (§8). ``canonical_url`` is NOT
  applied by a pass (only a manual ``reset``), so it never appears here.

This is a faithful ESTIMATE the human confirms; it deliberately models the
determinable guards (§7 step 4) and the routing/dedup/singleton decisions, not the
in-flight phase-A/phase-B relocate bookkeeping (live ``relocate`` rows own their source
tab in the pass; preview does not read them).

**The routing decision itself is IMPORTED from the pass** (:func:`src.curator.decide._route`
plus :func:`~src.curator.decide.compile_orphan_rules`) rather than restated here, and
the same-pass ``planned_opens`` rule is mirrored. §8 is explicit that a second
implementation of the routing/matching is exactly what must not exist, and this module
drifted that way once: the pass grew an orphaned-rule branch (a tab whose rule points at
a retired instance is DEFERRED, not drained to ``main``) and a same-pass duplicate branch
(the second identical tab is deferred, not opened twice), while the preview kept its own
copy and reported relocations that would never happen — to the wrong destination.
``tests/test_rules_preview_parity.py`` runs both engines over one fixture and is the
guard against a third drift.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from types import SimpleNamespace

from src.db.actions import normalize_url
from src.rules.matcher import (
    InvalidPattern,
    compile_pattern,
    normalize_target,
)

# How many worked examples to carry per bucket (a preview, not a full dump).
_EXAMPLE_CAP = 20


def _field(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    try:
        val = obj[name]
        return default if val is None else val
    except (KeyError, IndexError, TypeError):
        return getattr(obj, name, default)


def _survivor_key(tab):
    """§8 survivor ladder as a MIN-key (the WINNER sorts first).

    Survivor = max ``last_active_at``; tie → min ``opened_at``; tie → min ``tab_id``;
    a row with ``age_unknown=1`` loses to any observed row. This is the SAME ladder
    the exact dedup (§7) and the later pass use, so the example names the tab that
    really survives — not merely the first DB row.
    """
    last = _field(tab, "last_active_at")
    opened = _field(tab, "opened_at")
    return (
        1 if _field(tab, "age_unknown") else 0,          # observed (0) beats unknown (1)
        -last if last is not None else float("inf"),      # larger last_active_at wins
        opened if opened is not None else float("inf"),   # smaller opened_at wins
        _field(tab, "tab_id"),                            # smaller tab_id wins
    )


@dataclass
class PreviewInput:
    """Everything the simulation needs, loaded once from the mirror."""

    rules: list          # candidate rule set (after the edit is applied)
    tabs: list           # all tabs rows
    instances: list      # instances rows: id, connected, snapshot_at, focused_window_id
    windows: dict        # (instance_id, window_id) -> (type, state)
    exemptions: list     # (instance_id, url, until)
    quarantine: list     # (instance_id, url, until)
    now: int
    idle_ms: int
    state_fresh_ms: int
    main_instance_id: str
    # Authoritative per-instance freshness computed by the ASYNC endpoint layer
    # AFTER it actively requested a snapshot from each connected instance (§8).
    # {instance_id: (counted, reason)}. When an id is present here it overrides the
    # window-based `_instance_counted` heuristic — an instance that JUST answered a
    # snapshot_request IS counted, one that timed out / is disconnected is not. Left
    # None only in pure-simulation unit tests that have no live sockets.
    counted_override: dict | None = None


@dataclass
class PreviewResult:
    relocations: int = 0
    closures: int = 0
    deferred: int = 0
    relocation_examples: list = field(default_factory=list)
    closure_examples: list = field(default_factory=list)
    instances: list = field(default_factory=list)  # per-instance freshness
    enables_drain: bool = False
    disables_curation: bool = False
    # Per-target deferral counts, the SAME shape ``decide`` produces
    # (``Decisions.deferred``). Kept next to the scalar ``deferred`` (which clients
    # already read) so the parity test can compare the two engines dict-to-dict
    # instead of on a total that could match by coincidence.
    deferred_by_target: dict = field(default_factory=dict)

    @property
    def impact(self) -> int:
        return self.relocations + self.closures

    def to_dict(self) -> dict:
        # Top-level integer `relocations`/`closures` + `examples` per §10's endpoint
        # table; the split example lists and freshness detail are additive.
        return {
            "relocations": self.relocations,
            "closures": self.closures,
            "deferred": self.deferred,
            "deferred_by_target": self.deferred_by_target,
            "impact": self.impact,
            "relocation_examples": self.relocation_examples,
            "closure_examples": self.closure_examples,
            "examples": self.relocation_examples + self.closure_examples,
            "instances": self.instances,
            "enables_drain": self.enables_drain,
            "disables_curation": self.disables_curation,
        }


def has_active_rules(rules) -> bool:
    """True if at least one rule is valid & compiles — the condition that enables
    the ``unruled → main`` drain (§8: an empty policy means "do nothing")."""
    for r in rules:
        if _field(r, "invalid"):
            continue
        try:
            compile_pattern(_field(r, "pattern"))
        except InvalidPattern:
            continue
        return True
    return False


def _instance_counted(inst, now: int, state_fresh_ms: int) -> tuple[bool, str]:
    """(counted, reason). Not counted => its tabs never contribute a silent zero."""
    if not _field(inst, "connected"):
        return False, "disconnected"
    snap = _field(inst, "snapshot_at")
    if snap is None:
        return False, "never_snapshotted"
    if (now - snap) >= state_fresh_ms:
        return False, "stale"
    return True, "fresh"


def _guarded(tab, inp: PreviewInput, exempt: set, quar: set, focused: dict) -> bool:
    """§7 step-4 guards computable from the mirror. False => the pass would skip it."""
    url = _field(tab, "url")
    if normalize_target(url) is None:
        return False  # non-http(s) never matches / never moves
    if _field(tab, "pinned"):
        return False  # pinned=1: never relocated or closed (§8)
    if _field(tab, "audible"):
        return False
    inst = _field(tab, "instance_id")
    win = _field(tab, "window_id")
    if _field(tab, "active") and win is not None and win == focused.get(inst):
        return False  # on-screen tab (§7 step 4)
    last = _field(tab, "last_active_at")
    if last is None or (inp.now - last) < inp.idle_ms:
        return False  # not idle long enough (or age unknown)
    norm = normalize_url(url)
    if (inst, norm) in exempt or (inst, norm) in quar:
        return False
    wtype, wstate = inp.windows.get((inst, win), ("normal", "normal"))
    if wtype != "normal" or wstate == "fullscreen":
        return False
    return True


def simulate(inp: PreviewInput) -> PreviewResult:
    """Run the whole-pass simulation and return the counts + examples (§8)."""
    # THE routing decision (§7 step 5) comes FROM the pass; it is never restated here.
    # Imported inside the function because ``src.curator.decide`` imports
    # ``has_active_rules`` from this module — the layering says preview is the lower
    # one, and a leaf import is how the rest of this codebase breaks that knot
    # (cf. ``src.api.guards.require_not_paused``). Hoisting ``_route`` into a neutral
    # module would be cleaner, but ``decide.py`` belongs to the pass.
    from src.curator.decide import _route, compile_match_candidates

    res = PreviewResult()
    res.enables_drain = has_active_rules(inp.rules)  # drain state UNDER the candidate
    res.disables_curation = not res.enables_drain

    # --- freshness table (every instance the mirror knows about) --------------
    seen_ids = {_field(i, "id") for i in inp.instances}
    for t in inp.tabs:
        seen_ids.add(_field(t, "instance_id"))
    inst_by_id = {_field(i, "id"): i for i in inp.instances}
    focused = {
        _field(i, "id"): _field(i, "focused_window_id") for i in inp.instances
    }
    override = inp.counted_override
    counted: set[str] = set()
    for iid in sorted(x for x in seen_ids if x is not None):
        inst = inst_by_id.get(iid, {"id": iid, "connected": 0, "snapshot_at": None})
        # Prefer the ACTIVE refresh outcome from the endpoint (it requested a fresh
        # snapshot and knows disconnected/fresh/timeout); fall back to the window
        # heuristic only in pure-simulation unit tests with no live sockets (§8).
        if override is not None and iid in override:
            ok, reason = override[iid]
        else:
            ok, reason = _instance_counted(inst, inp.now, inp.state_fresh_ms)
        if ok:
            counted.add(iid)
        res.instances.append(
            {
                "id": iid,
                "connected": bool(_field(inst, "connected")),
                "snapshot_at": _field(inst, "snapshot_at"),
                "counted": ok,
                "reason": reason,
            }
        )

    now = inp.now
    exempt = {
        (r[0], normalize_url(r[1]))
        for r in inp.exemptions
        if r[2] is not None and r[2] > now
    }
    quar = {
        (r[0], normalize_url(r[1]))
        for r in inp.quarantine
        if r[2] is not None and r[2] > now
    }

    # Full-URL index of what each instance's mirror already holds (dedup is by the
    # FULL string, §7: "по полной строке URL", never the normalized one).
    urls_at: dict[str, set] = {}
    for t in inp.tabs:
        urls_at.setdefault(_field(t, "instance_id"), set()).add(_field(t, "url"))

    consumed: set = set()  # (instance_id, tab_id) already accounted for

    # Compile every rule's pattern ONCE, before the tab loop (§8: "compile patterns
    # once") — `_route` then does zero compilation per tab. The candidate set includes
    # the ``invalid`` rules: the pass runs ONE specificity ladder over all of them and
    # asks the WINNER whether it was flagged, because a flagged rule still means "this
    # tab HAS a home and it is unreachable", not "this tab is unruled".
    compiled_candidates = compile_match_candidates(inp.rules)

    # (target, full url) pairs this simulated pass has already scheduled an open for.
    # The pass defers the SECOND identical tab rather than opening a copy of its own
    # (§7), so counting it as a relocation would over-report by one per duplicate.
    planned_opens: set = set()

    # --- routing: relocation vs inter-instance dedup vs deferred --------------
    stayers: list = []  # candidates whose home == their own instance
    for tab in inp.tabs:
        inst = _field(tab, "instance_id")
        if inst not in counted:
            continue  # stale/disconnected mirror: never a silent contribution
        if not _guarded(tab, inp, exempt, quar, focused):
            continue
        url = _field(tab, "url")
        # ONE routing implementation, shared with the pass. `_route` reads attributes,
        # while a preview tab is a sqlite3.Row/dict, so it is passed a thin view rather
        # than being re-implemented for the other access style.
        home, rule, orphan_home = _route(
            SimpleNamespace(url=url, instance_id=inst),
            compiled_candidates,
            inp.main_instance_id,
            res.enables_drain,
        )
        key = (inst, _field(tab, "tab_id"))
        if orphan_home is not None:
            # Home exists as policy but not as an instance (§12 invalid rule): the pass
            # defers and leaves the tab alone — it does NOT drain it to main.
            _add_deferred(res, orphan_home)
            consumed.add(key)
            continue
        if home is None:
            continue
        if home == inst:
            stayers.append((tab, rule))
            continue
        if home not in counted:
            _add_deferred(res, home)  # target not ready => deferred, not relocated (§7 step 6)
            consumed.add(key)
            continue
        if url in urls_at.get(home, set()):
            _add_closure(res, tab, inst, "dedupe_close")  # §7 step-7 q2
        elif (home, url) in planned_opens:
            # A copy of this exact URL is already being opened in this target THIS
            # pass; the second tab waits a pass instead of producing a second copy (§7).
            _add_deferred(res, home)
        else:
            planned_opens.add((home, url))
            _add_relocation(res, tab, inst, home)
        consumed.add(key)

    # --- singleton: multiple already-home tabs under one singleton rule -------
    for rule in inp.rules:
        if _field(rule, "invalid") or not _field(rule, "singleton"):
            continue
        try:
            rid = _field(rule, "id")
            compile_pattern(_field(rule, "pattern"))
        except InvalidPattern:
            continue
        group = [
            (tab, r)
            for (tab, r) in stayers
            if r is not None
            and _field(r, "id") == rid
            and (_field(tab, "instance_id"), _field(tab, "tab_id")) not in consumed
        ]
        if not group:
            continue
        # A singleton keeps exactly ONE tab in its home; the rest close (§8). The
        # keeper is the §8 ladder WINNER (max last_active_at, …), not the first DB
        # row — sort so group[0] is the survivor and the losers close.
        group.sort(key=lambda pair: _survivor_key(pair[0]))
        survivor = group[0][0]
        for tab, _r in group[1:]:
            _add_closure(
                res, tab, _field(tab, "instance_id"), "singleton_close", survivor
            )
            consumed.add((_field(tab, "instance_id"), _field(tab, "tab_id")))

    # --- intra-instance exact dedup in NON-main homes (§7 "Сходимость") -------
    by_home_url: dict = {}
    for tab, _r in stayers:
        inst = _field(tab, "instance_id")
        if inst == inp.main_instance_id:
            continue  # main is a sink WITHOUT dedup (§15)
        key = (inst, _field(tab, "tab_id"))
        if key in consumed:
            continue
        by_home_url.setdefault((inst, _field(tab, "url")), []).append(tab)
    for (inst, _url), tabs in by_home_url.items():
        # Keep the §8 ladder WINNER, collapse the exact duplicates onto it.
        tabs = sorted(tabs, key=_survivor_key)
        survivor = tabs[0]
        for tab in tabs[1:]:
            _add_closure(res, tab, inst, "dedupe_close", survivor)
            consumed.add((inst, _field(tab, "tab_id")))

    return res


def _add_deferred(res: PreviewResult, target: str) -> None:
    """Count one deferral, in both shapes: the scalar clients read and the per-target
    map that mirrors ``Decisions.deferred``."""
    res.deferred += 1
    res.deferred_by_target[target] = res.deferred_by_target.get(target, 0) + 1


def _add_relocation(res: PreviewResult, tab, frm: str, to: str) -> None:
    res.relocations += 1
    if len(res.relocation_examples) < _EXAMPLE_CAP:
        res.relocation_examples.append(
            {
                "url": _field(tab, "url"),
                "title": _field(tab, "title"),
                "from": frm,
                "to": to,
            }
        )


def _add_closure(res: PreviewResult, tab, inst: str, reason: str, survivor=None) -> None:
    res.closures += 1
    if len(res.closure_examples) < _EXAMPLE_CAP:
        ex = {
            "url": _field(tab, "url"),
            "title": _field(tab, "title"),
            "instance": inst,
            "reason": reason,
        }
        # For singleton/dedup closes, name the tab that SURVIVES (the §8 ladder
        # winner) so the human sees which tab really stays (§8). The inter-instance
        # dedupe_close survivor is a pre-existing home tab outside this candidate
        # set, so it carries no `survivor`.
        if survivor is not None:
            ex["survivor"] = {
                "url": _field(survivor, "url"),
                "title": _field(survivor, "title"),
                "tab_id": _field(survivor, "tab_id"),
            }
        res.closure_examples.append(ex)


# --- loading the mirror -----------------------------------------------------
def load_preview_input(
    conn: sqlite3.Connection,
    candidate_rules: list,
    *,
    now: int,
    idle_ms: int,
    state_fresh_ms: int,
    main_instance_id: str,
    counted_override: dict | None = None,
) -> PreviewInput:
    """Read the current mirror into a :class:`PreviewInput` (a reader ``fn(conn)``).

    ``candidate_rules`` is the rule set AFTER the edit is applied — the caller builds
    it so the same simulation serves create / update / delete and the empty↔non-empty
    transitions. ``counted_override`` is the endpoint's active-refresh freshness map
    ({instance_id: (counted, reason)}); see :class:`PreviewInput`.
    """
    conn.row_factory = sqlite3.Row
    tabs = conn.execute(
        "SELECT instance_id, tab_id, window_id, url, title, pinned, active, "
        "audible, opened_at, last_active_at, age_unknown FROM tabs"
    ).fetchall()
    instances = conn.execute(
        "SELECT id, connected, snapshot_at, focused_window_id FROM instances"
    ).fetchall()
    windows = {
        (r["instance_id"], r["window_id"]): (r["type"], r["state"])
        for r in conn.execute(
            "SELECT instance_id, window_id, type, state FROM windows"
        ).fetchall()
    }
    exemptions = [
        (r["instance_id"], r["url"], r["until"])
        for r in conn.execute("SELECT instance_id, url, until FROM exemptions").fetchall()
    ]
    quarantine = [
        (r["instance_id"], r["url"], r["until"])
        for r in conn.execute("SELECT instance_id, url, until FROM quarantine").fetchall()
    ]
    return PreviewInput(
        rules=candidate_rules,
        tabs=tabs,
        instances=instances,
        windows=windows,
        exemptions=exemptions,
        quarantine=quarantine,
        now=now,
        idle_ms=idle_ms,
        state_fresh_ms=state_fresh_ms,
        main_instance_id=main_instance_id,
        counted_override=counted_override,
    )
