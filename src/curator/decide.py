"""The PURE decision engine of the curator pass (§7 steps 4-8).

Given the frozen :class:`~src.curator.mirror.Mirror` and the set of instances that
answered THIS pass's snapshot (``ready_ids``), produce the decisions the runner then
executes. Kept pure (no I/O, no clock) so every guard is unit- and mutation-testable
and so phase-A/phase-B copies — which are created DURING execution and thus absent
from the frozen mirror — are excluded from steps 7 and 8 for free (§7).

**Step 7 answers three separate questions, and conflating them is the headline bug
(§7).** (a) "Does THIS relocation have a live copy?" — keyed ONLY by a live
``relocate`` row (→ phase B). (b) "Is an identical tab already in the target?" —
inter-instance dedup by the FULL URL string (→ ``dedupe_close``). (c) "Is the
singleton slot taken?" — a ``last_active_at`` comparison between tabs ALREADY in the
home, which lives in **step 8**, never here. Conflating (a) with (b)/(c) deletes a
singleton-ruled tab opened away-from-home out of a THIRD instance because of another
instance's content, without moving it — recorded as a ``dedupe_close`` that
dedupes nothing. So question (b) is the full URL, and question (c) is step 8 only.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.db.actions import normalize_url
from src.rules.matcher import (
    best_match_compiled,
    compile_rules,
    normalize_target,
)
from src.rules.preview import has_active_rules


# --- decision records --------------------------------------------------------
@dataclass
class AbandonReloc:
    """Mark a live ``relocate`` row ``abandoned`` (§7 phase B, source side).

    ``source_url_mismatch`` — the human moved the source to another address; the
    source tab (if still present) re-decides normally THIS pass. ``source_gone`` —
    the source tab vanished entirely.
    """

    reloc_id: int
    reason: str  # 'source_url_mismatch' | 'source_gone'


@dataclass
class PhaseBVerify:
    """A live ``relocate`` whose SOURCE still holds the recorded full URL (§7).

    The runner verifies the TARGET (``get_tab``) next, then closes the source
    (``relocate_close``) or abandons the row if the copy vanished. Source-first
    ordering is mandatory (§7): verifying the target first, on a source the human
    moved, feeds an un-quenchable ``close_tab → precondition_failed`` loop that
    quarantines the stale ``url_norm`` while the guard reads the current one.
    """

    reloc: object          # RelocRow
    source_tab: object     # TabRow


@dataclass
class PhaseAOpen:
    """Open a copy of ``tab`` in ``home`` (§7 phase A). Source is NOT touched."""

    tab: object            # TabRow
    home: str
    rule: object | None    # matched rule (None for the unruled → main drain)


@dataclass
class Close:
    """A close decision (§7 step 7 q2, or step 8). ``kind``/``decision`` per §4."""

    tab: object            # TabRow being closed (the source)
    kind: str              # 'dedupe_close' | 'singleton_close'
    decision: str          # 'dedupe' | 'singleton'
    survivor: object | None  # the surviving tab (TabRow), for detail/examples
    rule: object | None    # rule that decided (singleton), else None


@dataclass
class WindowMerge:
    """A step-9 window-merge decision for one instance (§9).

    Fold the ``source_window_ids`` **unpinned** tabs into ``target_window_id``.
    Pinned tabs are never moved (a cross-window ``tabs.move`` silently resets
    ``pinned`` — the §9 trap that would destroy the owner's only "do not touch"
    shield), so a source window that keeps pinned tabs simply does NOT vanish; the
    extension enforces the unpinned-only move at the edge. ``moved_tab_ids`` is the
    server's expectation from the frozen mirror (the sources' unpinned tabs) recorded
    in the NON-undoable ``window_merge`` journal (§9 "список перенесённых tab_id");
    the extension re-queries live and returns the actual moved count.
    """

    instance_id: str
    target_window_id: int
    source_window_ids: list        # source windows with >=1 unpinned tab, ascending
    moved_tab_ids: list            # the sources' unpinned tab_ids (journal only)


@dataclass
class Decisions:
    abandon: list = field(default_factory=list)          # AbandonReloc
    phase_b: list = field(default_factory=list)          # PhaseBVerify
    deferred: dict = field(default_factory=dict)         # instance_to -> count
    phase_a: list = field(default_factory=list)          # PhaseAOpen
    closes: list = field(default_factory=list)           # Close
    considered: int = 0                                  # tabs step 5-7 weighed

    def is_empty(self) -> bool:
        return not (self.abandon or self.phase_b or self.deferred or self.phase_a or self.closes)


# --- the §8 survivor ladder (three places, one criterion; §7 "Сходимость") ---
def survivor_key(tab):
    """MIN-key: the WINNER sorts first. Max ``last_active_at``; tie → min
    ``opened_at``; tie → min ``tab_id``; an ``age_unknown`` row loses to any
    observed one. A freshly-seeded copy inherits the source's clock, so it never
    out-ranks a worked-in original (§7)."""
    return (
        1 if tab.age_unknown else 0,
        -tab.last_active_at,
        tab.opened_at,
        tab.tab_id,
    )


# --- §7 step-4 guards (computable from the frozen mirror) --------------------
def step4_passes(tab, mirror, now: int, idle_ms: int) -> bool:
    """True if ``tab`` clears EVERY step-4 close guard (§7). False => skip it.

    These guards apply to relocation AND to every close kind (§7): phase B,
    ``dedupe_close`` and ``singleton_close`` alike. The volatile ones are re-checked
    at the extension edge via ``close_tab``'s ``expect`` (Фаза 6); this is the
    server-side gate against the frozen mirror.
    """
    if normalize_target(tab.url) is None:
        return False  # non-http(s) never matches / never moves
    if tab.pinned:
        return False  # pinned=1: "do not touch by hand" signal (§8)
    if tab.audible:
        return False  # audible background media (§6/§7)
    focused = mirror.instances.get(tab.instance_id)
    if (
        tab.active
        and tab.window_id is not None
        and focused is not None
        and tab.window_id == focused.focused_window_id
    ):
        return False  # active AND its window is on screen (§5/§6)
    if (now - tab.last_active_at) < idle_ms:
        return False  # not idle long enough (age_unknown reads as fresh → skipped)
    norm = normalize_url(tab.url)
    for (inst, url, until) in mirror.exemptions:
        if inst == tab.instance_id and until is not None and until > now and normalize_url(url) == norm:
            return False
    for (inst, url, until) in mirror.quarantine:
        if inst == tab.instance_id and until is not None and until > now and normalize_url(url) == norm:
            return False
    win = mirror.windows.get((tab.instance_id, tab.window_id))
    if win is None:
        return False  # §6: a tab whose window is absent from the snapshot is skipped
    wtype, wstate = win
    if wtype != "normal" or wstate == "fullscreen":
        return False  # not a normal window, or a fullscreen showcase (§7)
    return True


# --- routing (§7 step 5) -----------------------------------------------------
def _route(tab, compiled_rules, main_instance_id: str, drain_on: bool):
    """Return ``(home_instance | None, rule | None)`` (§7 step 5).

    Rule → its home (applies to tabs in ``main`` too — my design decision, §7).
    No rule + themed instance + rules exist → ``main`` (drain). No rule + ``main``,
    or an empty rules table (drain off, §8) → ``None`` (not touched)."""
    rule = best_match_compiled(tab.url, compiled_rules)
    if rule is not None:
        return _rule_instance(rule), rule
    if tab.instance_id != main_instance_id and drain_on:
        return main_instance_id, None
    return None, None


def _rule_instance(rule):
    try:
        return rule["instance_id"]
    except (KeyError, IndexError, TypeError):
        return getattr(rule, "instance_id", None)


def _rule_id(rule):
    try:
        return rule["id"]
    except (KeyError, IndexError, TypeError):
        return getattr(rule, "id", None)


def _rule_singleton(rule):
    try:
        return rule["singleton"]
    except (KeyError, IndexError, TypeError):
        return getattr(rule, "singleton", 0)


# --- the whole decision ------------------------------------------------------
def decide(mirror, ready_ids: set, *, now: int, idle_ms: int, main_instance_id: str) -> Decisions:
    """Compute all pass decisions against the frozen mirror (§7 steps 4-8)."""
    res = Decisions()
    compiled_rules = compile_rules(mirror.rules)
    drain_on = has_active_rules(mirror.rules)

    # Full-URL index of what each instance ALREADY holds (dedup is by the full
    # string, §7). Built from the frozen mirror => same-pass phase-A copies (which
    # do not exist yet) are excluded from question (b) automatically.
    urls_at: dict[str, set] = {}
    for t in mirror.tabs:
        urls_at.setdefault(t.instance_id, set()).add(t.url)

    # --- phase B: source-first per live relocate row (§7 question a) ---------
    owned: set = set()  # (instance_id, tab_id) handled by phase B => not re-routed
    for reloc in mirror.live_relocations:
        if reloc.instance_from not in ready_ids:
            continue  # source not ready this pass: leave live, retry later (not a strike)
        # Find the source by (instance_from, session_id_from, FULL URL); tab_id is
        # only a hint (discard changes it, §5). Session is per-instance, so every
        # tab of instance_from shares the instance's current session.
        candidates = [
            t for t in mirror.tabs
            if t.instance_id == reloc.instance_from and t.url == reloc.url
        ]
        if not candidates:
            # Source no longer holds the recorded URL. Is the hinted tab present
            # under a DIFFERENT url (human moved it) or gone entirely?
            moved = any(
                t.instance_id == reloc.instance_from and t.tab_id == reloc.tab_id
                for t in mirror.tabs
            )
            res.abandon.append(
                AbandonReloc(reloc.id, "source_url_mismatch" if moved else "source_gone")
            )
            continue  # do NOT own the tab: it re-decides normally this pass
        # Prefer the hinted tab_id, else the smallest tab_id (deterministic).
        source_tab = next(
            (t for t in candidates if t.tab_id == reloc.tab_id),
            min(candidates, key=lambda t: t.tab_id),
        )
        owned.add((reloc.instance_from, source_tab.tab_id))
        res.phase_b.append(PhaseBVerify(reloc, source_tab))

    # --- step 5-7: route every other guarded tab -----------------------------
    stayers: list = []  # (tab, rule) whose home == own instance => step 8
    for tab in mirror.tabs:
        if tab.instance_id not in ready_ids:
            continue
        if (tab.instance_id, tab.tab_id) in owned:
            continue  # a live relocation owns it (§7: blocks question a's re-open)
        if not step4_passes(tab, mirror, now, idle_ms):
            continue
        res.considered += 1
        home, rule = _route(tab, compiled_rules, main_instance_id, drain_on)
        if home is None:
            continue  # unruled main tab, or drain off => not touched
        if home == tab.instance_id:
            stayers.append((tab, rule))
            continue
        # Relocation candidate (home != own instance).
        if home not in ready_ids:
            res.deferred[home] = res.deferred.get(home, 0) + 1  # §7 step 6
            continue
        if tab.url in urls_at.get(home, set()):
            # Question (b): an identical tab (FULL url) already sits in the target
            # => inter-instance dedup, NOT a relocation (§7 q2, exists for §15).
            survivor = _pick_target(mirror, home, tab.url)
            res.closes.append(Close(tab, "dedupe_close", "dedupe", survivor, rule))
        else:
            res.phase_a.append(PhaseAOpen(tab, home, rule))

    # --- step 8: singleton, then exact intra-instance dedup in non-main ------
    consumed: set = set()
    _decide_singleton(res, mirror, stayers, consumed)
    _decide_intra_dedup(res, mirror, stayers, consumed, main_instance_id)
    return res


def _pick_target(mirror, home: str, full_url) -> object | None:
    """The pre-existing target tab that survives an inter-instance dedup (§7 q2:
    "выживает вкладка в цели"). Among target tabs with this exact URL, the §8 ladder
    winner names which tab really stays."""
    matches = [t for t in mirror.tabs if t.instance_id == home and t.url == full_url]
    if not matches:
        return None
    return sorted(matches, key=survivor_key)[0]


def _decide_singleton(res: Decisions, mirror, stayers, consumed) -> None:
    """§8/§7 step 8: a singleton rule keeps exactly ONE tab in its home; the ladder
    losers close. Singleton lives ONLY here — it compares tabs already in the home,
    never declares a foreign tab a copy (that is the non-conflation of §7)."""
    for rule in mirror.rules:
        if _rule_field(rule, "invalid") or not _rule_singleton(rule):
            continue
        rid = _rule_id(rule)
        group = [
            (tab, r)
            for (tab, r) in stayers
            if r is not None
            and _rule_id(r) == rid
            and (tab.instance_id, tab.tab_id) not in consumed
        ]
        if len(group) <= 1:
            continue
        group.sort(key=lambda pair: survivor_key(pair[0]))
        survivor = group[0][0]
        for tab, _r in group[1:]:
            res.closes.append(Close(tab, "singleton_close", "singleton", survivor, rule))
            consumed.add((tab.instance_id, tab.tab_id))


def _decide_intra_dedup(res: Decisions, mirror, stayers, consumed, main_instance_id) -> None:
    """§7 "Сходимость": exact (full-URL) duplicates within one NON-main instance
    collapse onto the ladder winner. ``main`` is a sink without dedup (§15)."""
    by_url: dict = {}
    for tab, _r in stayers:
        if tab.instance_id == main_instance_id:
            continue
        if (tab.instance_id, tab.tab_id) in consumed:
            continue
        by_url.setdefault((tab.instance_id, tab.url), []).append(tab)
    for (_inst, _url), tabs in by_url.items():
        if len(tabs) <= 1:
            continue
        tabs = sorted(tabs, key=survivor_key)
        survivor = tabs[0]
        for tab in tabs[1:]:
            res.closes.append(Close(tab, "dedupe_close", "dedupe", survivor, None))
            consumed.add((tab.instance_id, tab.tab_id))


def _rule_field(rule, name):
    try:
        return rule[name]
    except (KeyError, IndexError, TypeError):
        return getattr(rule, name, None)


# --- §9 step 9: window merge (pure planning; execution lives in phases.py) ----
def _window_mergeable(meta) -> bool:
    """True if a window may be a merge SOURCE or TARGET (§9).

    The SAME window predicate step 4 uses for tabs (§9 "гард — то же бездействие,
    что и везде"): type ``normal`` and state NOT ``fullscreen``. popup/app/devtools
    windows and a fullscreen showcase are neither folded nor merged into.

    FORK (maximized): a ``maximized`` state is "не fullscreen", so a maximized,
    hour-idle window IS mergeable here — deliberately matching ``step4_passes``
    (``wtype != "normal" or wstate == "fullscreen"``) rather than restricting to a
    plain ``normal`` state, so a tab that step 4 may relocate cannot live in a window
    step 9 refuses to collapse. Only ``fullscreen`` (the macOS dedicated-Space
    showcase, ledger 43) is exempt.
    """
    wtype, wstate = meta
    return wtype == "normal" and wstate != "fullscreen"


def _source_eligible(tabs, focused_window_id, now: int, idle_ms: int) -> bool:
    """True if a window is a merge SOURCE (§9): EVERY tab idle longer than
    ``IDLE_MINUTES``, NO on-screen tab (active in the instance's focused window),
    and NO audible tab. Guards apply to ALL tabs — pinned ones too — because the
    delay is the whole shield ("пока с окном работают, оно не трогается", §9).

    An empty tab list is NOT a source on its own; the caller additionally requires
    at least one unpinned tab (an all-pinned or empty window can never be emptied).

    Unlike step 4, this does NOT consult exemptions/quarantine: a merge is a COSMETIC
    cross-window move of unpinned tabs (it neither closes nor relocates), so the
    don't-destroy guards those tables provide do not apply — a recently-restored or
    quarantined tab is unharmed by being folded into another window."""
    if not tabs:
        return False
    for t in tabs:
        if t.audible:
            return False  # background media (§6): the copy would open silent
        if t.active and t.window_id is not None and t.window_id == focused_window_id:
            return False  # a tab on screen right now — the owner is here
        if (now - t.last_active_at) < idle_ms:
            return False  # not idle long enough (age_unknown reads as fresh => blocks)
    return True


def decide_window_merges(mirror, ready_ids: set, *, now: int, idle_ms: int) -> list:
    """Plan every instance's window merge for step 9 (§9). Pure over the frozen
    mirror, so the guards are unit- and mutation-testable without a socket.

    Per instance: among its mergeable windows pick the TARGET (most tabs; tie ->
    smallest ``window_id``, §9), then fold every OTHER mergeable window that clears
    the source guard AND still has an unpinned tab to move. An instance with fewer
    than two mergeable windows, or no eligible source, yields no decision."""
    merges: list = []
    for instance_id in ready_ids:
        inst = mirror.instances.get(instance_id)
        focused_window_id = inst.focused_window_id if inst is not None else None

        win_ids = [
            w
            for (i, w), meta in mirror.windows.items()
            if i == instance_id and _window_mergeable(meta)
        ]
        if len(win_ids) < 2:
            continue  # need a source AND a distinct target

        tabs_by_win: dict = {}
        for t in mirror.tabs:
            if t.instance_id == instance_id and t.window_id in win_ids:
                tabs_by_win.setdefault(t.window_id, []).append(t)

        # Target: most tabs, tie -> smallest window_id (§9). popup/app/devtools are
        # already excluded from win_ids, so they never win nor count.
        target = min(win_ids, key=lambda w: (-len(tabs_by_win.get(w, [])), w))

        source_windows: list = []
        moved: list = []
        for w in sorted(win_ids):
            if w == target:
                continue
            wtabs = tabs_by_win.get(w, [])
            if not _source_eligible(wtabs, focused_window_id, now, idle_ms):
                continue
            unpinned = [t.tab_id for t in wtabs if not t.pinned]
            if not unpinned:
                continue  # only pinned tabs => the window can't be emptied (§9)
            source_windows.append(w)
            moved.extend(unpinned)
        if not source_windows:
            continue
        merges.append(WindowMerge(instance_id, target, source_windows, moved))
    return merges
