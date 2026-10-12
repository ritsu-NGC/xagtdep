"""
Python port of caterpillar's z3_pebble_solver (see
include/caterpillar/solvers/z3_solver.hpp in gmeuli/caterpillar), with a
gate-grouping algorithm that guarantees reconstructability of the
original DAG and an acyclic gate-dependency graph.

======================================================================
GATE GROUPING: OLD vs NEW
======================================================================

The OLD approach (`build_gate_groups_by_rules` + `merge_gates_on_
shared_xor_nodes` in earlier revisions) walked every PI->PO path
independently and accumulated nodes into GateGroup fragments according
to per-path rules, then merged fragments that happened to share an XOR
node. This can:
  - assign the same node to multiple fragments,
  - record a node's "dependency" against the WRONG gate,
  - produce NON-CONVEX groups, which can create a cycle in the
    contracted gate-dependency graph EVEN THOUGH the original node DAG
    is fully acyclic.

The NEW approach (`build_gate_groups`, this file) instead:
  1. Assigns EVERY non-PI node to EXACTLY ONE gate group, by walking
     nodes in topological order and cutting group boundaries only at
     that point in the linear order. A group is therefore always a
     CONTIGUOUS RANGE of the topological order used.
  2. Proves (see accompanying .tex) that a contiguous range of a
     topological order is automatically CONVEX, so no separate
     convexity search is required.
  3. Computes each group's boundary (inputs/outputs) AFTER ownership is
     fixed, by inspecting the ORIGINAL fanin/fanout edges.
  4. MERGES sibling fanout groups where beneficial (see "SIBLING
     FANOUT MERGING" below).
  5. SPLITS any group whose `outputs` mix a genuine primary output (PO)
     with a non-PO output (see "MIXED PO GROUPS" below).
  5.5. RECOMBINES small adjacent non-PO fragments left behind by step 5
     (see "RECOMBINING SMALL NON-PO FRAGMENTS" below).
  6. Derives gate-level dependencies strictly from boundary inputs.
  7. Validates the result: unique ownership (partition), the hard
     per-group weight ceiling WITH dirty-ancilla-aware PO slack
     tracking (see "HARD PER-GROUP WEIGHT CEILING" and
     "DIRTY-ANCILLA-AWARE PO SLACK TRACKING" below), and acyclicity of
     the induced gate-dependency graph.

This guarantees the original graph can be reconstructed exactly from
the union of all group-owned nodes, and that `GatePebbleSolver`'s
dependency graph can never contain a cycle that isn't already present
in the original DAG.

======================================================================
TOPOLOGICAL ORDER CHOICE (`topo_order` parameter on `build_gate_groups`)
======================================================================

`topo_order` selects which topological order Phase 1 traverses:
  - "creation" (default): use `net.nodes` as-is.
  - "dependency_chain": use `_dependency_chain_topo_order(net)`
    instead -- a "list scheduling with successor preference"
    heuristic that chains genuinely DEPENDENT nodes together in the
    traversal order, so Phase 1's greedy fill is far more likely to
    group a producer with its actual consumer.
  - "breadth_first": use `_breadth_first_topo_order(net)` instead -- a
    plain FIFO (Kahn's algorithm) topological order. Unlike
    "dependency_chain" (which chains a producer directly to ONE of
    its own consumers, DFS-style), BFS processes the ENTIRE "ready"
    frontier in the order nodes became ready, so ALL siblings of a
    shared producer -- every direct consumer of the SAME node, i.e.
    the "A has multiple fanouts" shape (see "SHARED-FANOUT-CONSUMER
    COST EXEMPTION") -- end up adjacent in the traversal order. Phase
    1's greedy fill then naturally batches these siblings into one
    group simply by filling breadth-first until `max_pebbles` is hit,
    with no preference for depth over breadth. This directly supports
    the "CLEAN/DIRTY SPLIT RESOURCE MODEL" below: grouping fanout
    siblings together means their shared producer's dirty ancilla is
    consumed and released ONCE, within a single group's compute step,
    rather than needing cross-group sharing.

======================================================================
SHARED-FANOUT-CONSUMER COST EXEMPTION (rule 6 "multiple fanouts" shape)
======================================================================

`_pebble_cost` previously charged a flat cost of 1 to every non-XOR
node, regardless of structural role. This over-counts a specific,
common shape: a node `A` (an AND node) with 2+ direct non-XOR
consumers `B`, `C`, ... (rule 4/"A is an AND node that has multiple
fanouts" in `select_gadget_rule`). The gadget for this shape reuses
ONE shared ancilla holding `A` sequentially for BOTH `∧_B` and `∧_C`,
writing each result directly onto `B`/`C`'s OWN pre-existing output
wire, rather than allocating a fresh qubit per consumer.

Concretely: only `A` itself costs 1 (the shared ancilla). Each direct
non-XOR consumer of such an `A` (i.e. `B`, `C`, ...) costs 0. `B`/`C`'s
OWN further children are NOT exempted -- those genuinely need their
own distinct ancilla.

`_compute_shared_fanout_consumers(net, children=None)` computes the set
of all such exempted consumer nodes ONCE per network, and
`_pebble_cost(node, shared_fanout_consumers=None)` consults this set.
`build_gate_groups` computes this set once up front and threads it
through every phase that calls `_pebble_cost`.

======================================================================
HARD PER-GROUP WEIGHT CEILING (`_validate_max_group_weight`)
======================================================================

`max_pebbles` passed to `build_gate_groups` is a HARD CEILING: every
group's own weight (sum of `_pebble_cost`, WITH the shared-fanout-
consumer exemption applied), PLUS the cumulative unabsorbed PO weight
already reserved by earlier groups (see "DIRTY-ANCILLA-AWARE PO SLACK
TRACKING" below), must be `<= max_pebbles`. Any group violating this
can NEVER be scheduled.

Both `_merge_sibling_fanout_groups` and `_recombine_small_nonpo_groups`
clamp their effective per-merge cap to
`min(max_pebbles, max_pebbles * slack)` -- so `merge_pebble_slack`/
`recombine_slack` values > 1.0 are silently treated as 1.0.

`_validate_max_group_weight`, run as part of Phase 5 (validation) in
`build_gate_groups`, is a DEFENSIVE check that re-derives the SAME
dirty-pool/reserved-clean running totals Phase 1 used and re-verifies
the invariant after all phases have run, raising `AssertionError`
immediately if any group is found to violate it.

NOTE: this construction-time ceiling still uses FLAT `_pebble_cost`
per node (not yet the clean/dirty split used by the ACTUAL scheduling
engines -- see "CLEAN/DIRTY SPLIT RESOURCE MODEL" below). It remains a
conservative heuristic ceiling at construction time; the real
per-step budget enforcement happens in `GatePebbleSolver` and
`schedule_gates_greedy_pebble_limited`, which now use the more
accurate split model.

======================================================================
DIRTY-ANCILLA-AWARE PO SLACK TRACKING (Phase 1)
======================================================================

A group that owns a genuine primary output NEVER uncomputes, so its
PO-output qubit is permanently reserved from the moment it first
computes. However, a PO's value does NOT necessarily need a FRESH
clean qubit: it can be written onto an ALREADY-FREED dirty ancilla
left behind by an earlier group's internal computation. A PO only
consumes genuinely NEW, permanently-unavailable slack when there is
MORE pending PO weight than currently-freed dirty ancilla available to
absorb it.

Phase 1's greedy fill tracks two running counters, in topological
order, as each group finalizes:
  - `dirty_pool`: cumulative weight of INTERNAL (dirty) nodes from
    already-finalized groups.
  - `reserved_clean`: cumulative weight of PO-output nodes that could
    NOT be absorbed by `dirty_pool`, and therefore permanently reduce
    the budget available to every SUBSEQUENT group.

Each subsequent group's own fill threshold is
`max_pebbles - reserved_clean` instead of a flat `max_pebbles`.

If `reserved_clean` ever grows to meet or exceed `max_pebbles` itself,
`build_gate_groups` raises `ValueError` immediately at construction
time -- no further group of any size could ever be scheduled.

NOTE: this is a FIRST-ORDER APPROXIMATION used only for the
construction-time ceiling, not an exact reservation simulation of the
real scheduler (see "CLEAN/DIRTY SPLIT RESOURCE MODEL" for how the
real schedulers account for this more precisely, step by step).

======================================================================
SIBLING FANOUT MERGING (`_merge_sibling_fanout_groups`)
======================================================================

Rule 6 of the gadget synthesis table recognizes that when a single AND
node `A` feeds MULTIPLE downstream consumers, those consumers can
share a single copy of `A`'s ancilla/result rather than each
recomputing or re-fanning-out `A` independently.

`_merge_sibling_fanout_groups`, run as a new phase between Phase 1
(partition) and Phase 2 (boundary computation), looks for exactly this
shape and merges ADJACENT groups owning the shared node's consumers,
without breaking CONVEXITY and without exceeding the HARD per-group
weight ceiling, `min(max_pebbles, max_pebbles * merge_pebble_slack)`.

This is intentionally CONSERVATIVE: it only merges groups that are
already topologically adjacent. Merging is applied greedily, in one
left-to-right pass, not to an exhaustive fixed point. With
`topo_order="breadth_first"`, this merge pass should have far less
work to do, since Phase 1 already keeps fanout siblings adjacent.

======================================================================
MIXED PO GROUPS
======================================================================

`GatePebbleSolver` requires any gate that owns at least one TRUE
primary output to remain pebbled FOREVER -- it NEVER executes an
`uncompute_gate` event. If a single group's `outputs` mix a genuine PO
with a non-PO output, that non-PO output's qubit can never be freed.

`_split_mixed_po_groups`, run after sibling-fanout merging, detects any
group whose `outputs` mix a genuine PO with a non-PO output, and
splits that group at the FIRST point in topological order where the
output type changes. Applied repeatedly to a fixed point.

======================================================================
RECOMBINING SMALL NON-PO FRAGMENTS (`_recombine_small_nonpo_groups`)
======================================================================

`_split_mixed_po_groups` only ever SPLITS at PO/non-PO transition
points. `_recombine_small_nonpo_groups`, run as Phase 3.5, greedily
re-merges RUNS of topologically ADJACENT groups that are ALL entirely
non-PO back together, up to the HARD per-group weight ceiling,
`min(max_pebbles, max_pebbles * recombine_slack)`.

======================================================================
CLEAN/DIRTY SPLIT RESOURCE MODEL
======================================================================

Previously, `GatePebbleSolver`'s `PbLe` constraint (and the greedy
scheduler's `gate_weight`) charged a group's FULL weight (clean
outputs + internal/dirty nodes combined) for its ENTIRE live window,
from compute until uncompute. This is overly conservative for dirty
nodes: a dirty/internal node is never read by anything outside its
owning group (by definition -- see "QUBIT RECLAMATION POLICY"), so it
is only ACTUALLY needed transiently, during the group's own compute
step (to produce it) and again during its own uncompute step (to
restore it to |0>). Between those two moments, while the group
remains "live" purely because some OTHER group still depends on its
CLEAN output, the dirty ancilla is not needed at all and should not
be charged against the budget.

Each group's weight is now split into two parts:
  - `clean_weight` (via `_group_clean_weight`): sum of `_pebble_cost`
    over `g.outputs` (WITH the shared-fanout-consumer exemption
    applied). This is charged for the group's ENTIRE live window (its
    `s` state variable / the round(s) between compute and uncompute),
    since clean/boundary values may genuinely be read by other groups
    at any point during that window.
  - `dirty_weight` (via `_group_dirty_weight`): sum of `_pebble_cost`
    over `g.internal`. This is charged ONLY at the exact step/round
    where the group's own compute_gate or uncompute_gate event fires
    (its `a` "active/toggling" variable in `GatePebbleSolver`, or the
    round in which the greedy scheduler schedules that specific
    toggle) -- NOT for the group's entire live window.

The simultaneous-pebble budget constraint becomes:

    sum(live_group.clean_weight for live groups)
  + sum(toggling_group.dirty_weight for groups toggling THIS step)
  <= max_pebbles

This matches the real resource requirement more precisely: a group's
clean outputs are genuinely "stored values" with a lifetime spanning
potentially many steps (exactly the "count distinct live boundary
values" model), while a group's dirty scratch is only ever needed
for the single atomic operation (compute, or later uncompute) that
produces or restores it.

NOTE: this model treats ALL toggles occurring at the SAME step/round
as though they could be simultaneously in-flight, summing their dirty
weights together for that step's budget check. This is a slightly
CONSERVATIVE approximation (never UNSAFE) when multiple toggles
scheduled in the same round are actually applied sequentially
one-at-a-time in the real circuit, since the true peak usage can only
be lower than or equal to this sum.

Both `GatePebbleSolver`'s `PbLe` constraint and `schedule_gates_
greedy_pebble_limited`'s budget check now use this clean/dirty split
instead of a single flat per-group weight. `build_gate_groups`'s own
construction-time ceiling (`_validate_max_group_weight`, Phase 1's
PO-slack tracking) still uses the FLAT per-node cost model as a
conservative heuristic -- it is not yet updated to match this split
(a known follow-up).

======================================================================
NODE-LEVEL CLEAN WEIGHT RELEASE (`schedule_gates_greedy_pebble_
limited`, `node_level_clean_release`)
======================================================================

Previously, a group's ENTIRE `clean_weight` stayed charged against
the budget until ALL of that group's dependents had resolved (or,
for a PO-owning group, forever). This is overly conservative when a
group's `outputs` list contains MULTIPLE nodes consumed by DIFFERENT
downstream groups on different timelines: one output node might be
consumed by an already-resolved group, while another is consumed much
later -- yet the group's WHOLE clean_weight stayed locked up until the
slowest dependent resolved, even though the first output's weight
could safely have been released much sooner.

With `node_level_clean_release=True` (the default), the scheduler
tracks `node_pending_consumers[n]` for every non-PO output node `n` --
the number of OTHER groups that still need `n` as an input and have
not yet resolved (computed). Every time a group COMPUTES (not just
when it later uncomputes), it counts as having "consumed" its own
input dependencies; this decrements `node_pending_consumers` for each
of that group's input nodes. The instant a node's counter reaches
zero, its weight is released from `live_clean_weight` IMMEDIATELY --
independent of whatever state the REST of its owning group's other
outputs are in.

The group's own `uncompute_gate` event (when it eventually fires) only
subtracts whatever clean weight of that group's own outputs has NOT
already been released node-by-node, avoiding double-subtraction.

This directly targets the "6 giant non-PO groups" failure pattern
observed on EPFL's `bar` benchmark under `topo_order="breadth_first"`:
such groups' many outputs are typically consumed by many DIFFERENT
downstream groups on different timelines, so most of a giant group's
clean weight can now be released well before the group as a whole
is ever uncomputed (or without ever needing to uncompute it, if it
owns a PO).

Pass `node_level_clean_release=False` to restore the old, strictly
per-group (all-or-nothing) release behavior.

======================================================================
LOOP TERMINATION: PO-ONLY LIVE SET IS SUCCESS, NOT DEADLOCK
======================================================================

`schedule_gates_greedy_pebble_limited`'s main loop runs
`while pending or live:`. Once `pending` is empty and every remaining
`live` group is PO-owning, this is the CORRECT terminal state: every
PO-owning group stays live FOREVER by design (see "QUBIT RECLAMATION
POLICY"/"PO-GATE MONOTONICITY"), so there is nothing left to compute
and nothing left to uncompute. Without an explicit check for this
case, the loop would try another round, find that neither phase makes
any progress (correctly -- there is genuinely nothing left to do),
and incorrectly raise a deadlock `RuntimeError`, even though
scheduling had already fully succeeded.

The fix: when `not progressed` is detected, first check whether
`pending` is empty AND every group still in `live` is a PO-owning
group (`live.issubset(po_gate_ids)`). If so, this is SUCCESS -- break
out of the loop cleanly and return the (complete, correct) schedule
built so far. Only raise the deadlock `RuntimeError` if something is
genuinely still stuck (a non-empty `pending` with no path forward, or
a non-PO group stranded in `live` that can never be freed within
budget).

======================================================================
GREEDY SCHEDULER DEBUG JSON DUMP (`dump_json` on `schedule_gates_
greedy_pebble_limited` / `pebble_gates_greedy_first`)
======================================================================

Previously, only `pebble_gates` (the Z3-based path) ever called
`dump_pebbling_debug_json` -- the greedy scheduler, even on a full
SUCCESS, never wrote any debug JSON at all. Since greedy scheduling is
now the DEFAULT path (see "GREEDY-FIRST SCHEDULING" below), this meant
the common case (greedy succeeds, Z3 never invoked) produced NO
pebbling debug artifact whatsoever, even though callers like
`run_caterpillar_experiments.py` already pass a `dump_json_path` on
every run expecting one.

`schedule_gates_greedy_pebble_limited` now accepts an optional
`dump_json` path and writes a debug JSON (gate groups, gate-level
schedule, expanded node-level pebbling sequence) via
`dump_pebbling_debug_json`:
  - on SUCCESS, with `extra={"outcome": "greedy_success"}`;
  - on a GENUINE deadlock (not the PO-only-live success case above),
    with `extra={"outcome": "greedy_deadlock", ...}` including the
    step at which it deadlocked, the final `live_clean_weight`, and
    the stuck ready/pending gate ids -- so the partial schedule built
    so far remains inspectable post-mortem, same as `pebble_gates`'s
    own error-path dump.

`pebble_gates_greedy_first` forwards its own `dump_json` parameter
straight through to `schedule_gates_greedy_pebble_limited` on the
greedy attempt, AND to `pebble_gates` if the SAT fallback (or
`force_sat`) path is used instead -- so a debug JSON is written
regardless of which method ultimately scheduled the circuit.

======================================================================
QUBIT RECLAMATION POLICY (`_replay_gate_pebbling`)
======================================================================

A group's `outputs` may be a genuine PO (must remain live forever) or
a node merely consumed by a later group (must eventually be freed on
uncompute). A group's `internal` (dirty pebble) nodes are freed
EAGERLY, immediately after their owning gate's compute step finishes,
at ALLOCATION time.

======================================================================
COMPOUND XOR CLUSTER DETECTION (`read_blif`)
======================================================================

`_is_xor_cover` only recognizes a genuine XOR/XNOR when a SINGLE
`.names` node's own cover has exactly the 2-row XOR/XNOR pattern. Many
real BLIF benchmarks (e.g. EPFL's `adder.blif`) instead decompose an
XOR into THREE separate `.names` nodes. `_try_mark_compound_xor_
cluster`, called from `read_blif`, detects this specific 3-node shape
and retroactively marks all 3 nodes' `is_xor = True`.

======================================================================
MIN-STEP DIAGNOSTIC SEARCH (`find_min_gate_steps`)
======================================================================

Fixes `max_pebbles` and searches DOWNWARD from a known-SAT step count
until UNSAT is hit, to find the TRUE minimum step count at that
budget. Diagnostic/debug tool only.

======================================================================
BIDIRECTIONAL PEBBLE-COUNT SEARCH (`pebble_gates`)
======================================================================

`pebble_gates` makes exactly ONE "probe" attempt at `max_pebbles`,
then searches DOWNWARD (if the probe is SAT) or UPWARD (if UNSAT).
This search STOPS at the smallest pebble count that is still SAT --
it does NOT consider T-count. See "T-COUNT-GUIDED PEBBLE SEARCH".

======================================================================
PO-GATE MONOTONICITY / JUSTIFIED-RECOMPUTE CONSTRAINT
======================================================================

Once a PO-owning gate is pebbled, it must REMAIN pebbled forever. A
non-PO gate should never be RECOMPUTED unless justified by some real
dependent itself transitioning to True at that same step.

======================================================================
ESCALATION ROUND LIMIT, WALL-CLOCK TIMEOUT, STRUCTURAL FALLBACK
======================================================================

`max_escalations`/`timeout_s` bound the Z3-based search's cost.
`schedule_gates_structurally` is a non-SAT fallback with NO
pebble-budget concept at all, opt-in via `use_structural_fallback` on
`pebble_gates`.

======================================================================
GREEDY PEBBLE-LIMITED SCHEDULING (`schedule_gates_greedy_pebble_limited`)
======================================================================

A single-pass, deterministic, solver-free heuristic that ACTIVELY
ENFORCES the clean/dirty-split weighted `max_pebbles` budget (and an
optional narrower `clean_pebbles` budget on clean weight only). See
"NODE-LEVEL CLEAN WEIGHT RELEASE" above for the per-output-node
release refinement, "LOOP TERMINATION: PO-ONLY LIVE SET IS SUCCESS,
NOT DEADLOCK" above for the correct terminal-state check, and "GREEDY
SCHEDULER DEBUG JSON DUMP" above for the debug JSON behavior.

Raises `RuntimeError` on a GENUINE deadlock (no progress possible in
a round AND the remaining state is not simply "all POs, nothing
left to do").

======================================================================
GREEDY-FIRST SCHEDULING (`pebble_gates_greedy_first`)
======================================================================

`pebble_gates_greedy_first` makes GREEDY SCHEDULING THE DEFAULT PATH:
  1. Try `schedule_gates_greedy_pebble_limited` first. If it succeeds,
     return its result immediately -- no Z3 solving at all.
  2. If it DEADLOCKS (raises `RuntimeError`), the error PROPAGATES
     DIRECTLY to the caller by DEFAULT (`use_sat_fallback=False`).
  3. Pass `use_sat_fallback=True` explicitly to restore the OLD
     behavior of falling back to the Z3-based `pebble_gates`.

`force_sat` (default False): if True, SKIPS the greedy attempt
entirely and goes straight to `pebble_gates`.

======================================================================
TOGGLE-COUNT BUDGET SEARCH (`pebble_gates_by_toggle_budget`)
======================================================================

Builds the full per-step structure EXACTLY ONCE, and escalates a
GLOBAL BUDGET on the TOTAL NUMBER OF COMPUTE/UNCOMPUTE (toggle) events
via `solve_with_toggle_budget`, instead of rebuilding the solver every
round.

======================================================================
T-COUNT-GUIDED PEBBLE SEARCH (`find_best_gate_pebbling_by_tcount`)
======================================================================

Searches for the pebble budget that minimizes T-COUNT directly, using
a caller-supplied `t_count_fn(net, gates, gate_steps) -> int`.

======================================================================
WEIGHTED PBLE CONSTRAINT: GROUP COUNT -> QUBIT COUNT
======================================================================

`GatePebbleSolver`'s `PbLe` simultaneous-pebble constraint now weights
each gate's CLEAN contribution by its `s` (live) state and its DIRTY
contribution by its `a` (toggling) state -- see "CLEAN/DIRTY SPLIT
RESOURCE MODEL" above.

Everything else in this file (node-level Z3 pebbling, Reed-Muller/ESOP
network generators, gate-level Z3 pebbling, gadget synthesis,
Qiskit/QASM export) is unchanged in spirit from prior revisions.
"""

import random
import time
from collections import defaultdict, deque

from z3 import Bool, Solver, Implies, And, Or, Not, sat, unsat, PbLe


# ---------------------------------------------------------------------------
# Network primitives
# ---------------------------------------------------------------------------

class Node:
    def __init__(self, name, fanins=None, is_pi=False, is_xor=False):
        self.name = name
        self.fanins = fanins or []     # list of Node
        self.is_pi = is_pi
        self.is_xor = is_xor           # True for XOR gates, False for AND/other gates, PIs

    def __repr__(self):
        kind = "PI" if self.is_pi else ("XOR" if self.is_xor else "AND")
        return f"Node({self.name}, {kind})"


class PebblingNetwork:
    """Simple DAG container: PIs + gates (non-PI nodes) + POs."""

    def __init__(self):
        self.nodes = []          # topological order: PIs first, then gates
        self.pis = []
        self.pos = []

    def create_pi(self, name=None):
        n = Node(name or f"pi{len(self.pis)}", is_pi=True)
        self.nodes.append(n)
        self.pis.append(n)
        return n

    def create_gate(self, fanins, name=None, is_xor=False):
        n = Node(name or f"g{len(self.nodes)}", fanins=fanins, is_xor=is_xor)
        self.nodes.append(n)
        return n

    def create_and_gate(self, fanins, name=None):
        return self.create_gate(fanins, name=name, is_xor=False)

    def create_xor_gate(self, fanins, name=None):
        return self.create_gate(fanins, name=name, is_xor=True)

    def create_po(self, node):
        self.pos.append(node)

    def build_children(self):
        children = defaultdict(list)
        for n in self.nodes:
            for f in n.fanins:
                children[f].append(n)
        return children

    def print_summary(self):
        print(f"PIs: {[n.name for n in self.pis]}")
        print(f"POs: {[n.name for n in self.pos]}")
        print("Gates:")
        for n in self.nodes:
            if not n.is_pi:
                fanins = [f.name for f in n.fanins]
                kind = "XOR" if n.is_xor else "AND"
                print(f"  {n.name} ({kind}) = gate({fanins})")


# ---------------------------------------------------------------------------
# Random DAG generators
# ---------------------------------------------------------------------------

def random_dag(num_pis=4, num_gates=6, max_fanin=2, num_pos=1, seed=None, xor_prob=0.3):
    if seed is not None:
        random.seed(seed)

    net = PebblingNetwork()
    for i in range(num_pis):
        net.create_pi(f"pi{i}")

    for i in range(num_gates):
        pool = net.nodes
        fanin_count = random.randint(1, min(max_fanin, len(pool)))
        fanins = random.sample(pool, fanin_count)
        is_xor = random.random() < xor_prob
        net.create_gate(fanins, name=f"n{i}", is_xor=is_xor)

    children = net.build_children()
    sink_nodes = [n for n in net.nodes if not n.is_pi and not children.get(n)]
    if not sink_nodes:
        non_pi_nodes = [n for n in net.nodes if not n.is_pi]
        sink_nodes = non_pi_nodes[-1:] or net.nodes[-1:]
    for n in sink_nodes:
        net.create_po(n)

    return net


def random_esop_dag(num_pis=6, num_gates=16, max_fanin=2, num_outputs=2, seed=None):
    if seed is not None:
        random.seed(seed)

    net = PebblingNetwork()
    for i in range(num_pis):
        net.create_pi(f"pi{i}")

    for i in range(num_gates):
        pool = net.nodes
        fanin_count = random.randint(1, min(max_fanin, len(pool)))
        fanins = random.sample(pool, fanin_count)
        net.create_and_gate(fanins, name=f"and{i}")

    children = net.build_children()
    sinks = [n for n in net.nodes if not n.is_pi and not children.get(n)]

    groups = [[] for _ in range(num_outputs)]
    for idx, sink in enumerate(sinks):
        groups[idx % num_outputs].append(sink)

    const0 = None

    def get_const0():
        nonlocal const0
        if const0 is None:
            const0 = net.create_pi("const0")
        return const0

    xor_counter = [0]
    for g_idx, group in enumerate(groups):
        if not group:
            net.create_po(get_const0())
            continue
        node = group[0]
        for nxt in group[1:]:
            xor_counter[0] += 1
            node = net.create_xor_gate([node, nxt], name=f"out{g_idx}_xor{xor_counter[0]}")
        net.create_po(node)

    return net


# ---------------------------------------------------------------------------
# Reed-Muller-based DAG generation (Boolean bit-mapping -> ANF -> XAG)
# ---------------------------------------------------------------------------

def _mobius_transform(truth_table):
    anf = list(truth_table)
    n = len(anf)
    length = 1
    while length < n:
        for i in range(0, n, length * 2):
            for j in range(i, i + length):
                anf[j + length] ^= anf[j]
        length *= 2
    return anf


def random_boolean_bit_mapping(num_vars, num_outputs=1, seed=None, density=0.5):
    if seed is not None:
        random.seed(seed)
    size = 1 << num_vars
    return [[1 if random.random() < density else 0 for _ in range(size)]
            for _ in range(num_outputs)]


def truth_table_from_function(fn, num_vars):
    size = 1 << num_vars
    table = []
    for row in range(size):
        bits = [(row >> i) & 1 for i in range(num_vars)]
        table.append(int(bool(fn(*bits))) & 1)
    return table


def reed_muller_decompose(truth_table, num_vars):
    anf = _mobius_transform(truth_table)
    monomials = []
    for mask in range(len(anf)):
        if anf[mask]:
            term = tuple(i for i in range(num_vars) if (mask >> i) & 1)
            monomials.append(term)
    return monomials


def build_reed_muller_network(num_vars, truth_tables, var_names=None, share_cuts=True):
    net = PebblingNetwork()
    var_names = var_names or [f"x{i}" for i in range(num_vars)]
    pis = [net.create_pi(var_names[i]) for i in range(num_vars)]

    const_nodes = {}

    def get_const(name):
        if name not in const_nodes:
            const_nodes[name] = net.create_pi(name)
        return const_nodes[name]

    and_cache = {} if share_cuts else None
    and_counter = [0]

    def get_and_node(term):
        if not term:
            return get_const("const1")
        if len(term) == 1:
            return pis[term[0]]
        if share_cuts and term in and_cache:
            return and_cache[term]
        prefix_node = get_and_node(term[:-1])
        last_pi = pis[term[-1]]
        and_counter[0] += 1
        node = net.create_and_gate(
            [prefix_node, last_pi],
            name=f"and{and_counter[0]}_{'_'.join(map(str, term))}",
        )
        if share_cuts:
            and_cache[term] = node
        return node

    xor_cache = {} if share_cuts else None
    xor_counter = [0]

    def get_xor_of(node_a, node_b):
        key = frozenset((id(node_a), id(node_b)))
        if share_cuts and key in xor_cache:
            return xor_cache[key]
        xor_counter[0] += 1
        node = net.create_xor_gate([node_a, node_b], name=f"xor{xor_counter[0]}")
        if share_cuts:
            xor_cache[key] = node
        return node

    for tt in truth_tables:
        monomials = reed_muller_decompose(tt, num_vars)
        if not monomials:
            po_node = get_const("const0")
        else:
            monomials = sorted(monomials, key=lambda t: (len(t), t))
            term_nodes = [get_and_node(t) for t in monomials]
            po_node = term_nodes[0]
            for nxt in term_nodes[1:]:
                po_node = get_xor_of(po_node, nxt)
        net.create_po(po_node)

    return net


# ---------------------------------------------------------------------------
# BLIF reader
# ---------------------------------------------------------------------------

def _is_xor_cover(cover_rows, num_fanins):
    if num_fanins != 2:
        return False
    rows = set(r.strip() for r in cover_rows)
    xor_pattern = {"01 1", "10 1"}
    xnor_pattern = {"00 1", "11 1"}
    return rows == xor_pattern or rows == xnor_pattern


def _minterm_bits(fanin_order, rows, shared_order):
    if len(rows) != 1:
        return None
    parts = rows[0].strip().split()
    if len(parts) != 2:
        return None
    patt, outb = parts
    if outb != "1" or len(patt) != len(fanin_order):
        return None
    bits = {}
    for lit, n in zip(patt, fanin_order):
        if lit == "-":
            return None
        bits[n] = lit
    if set(bits.keys()) != set(shared_order):
        return None
    return tuple(bits[n] for n in shared_order)


def _try_mark_compound_xor_cluster(node, fanins, rows, node_cover_info):
    if len(fanins) != 2:
        return False
    p1, p2 = fanins
    info1 = node_cover_info.get(p1)
    info2 = node_cover_info.get(p2)
    if info1 is None or info2 is None:
        return False

    p1_fanins, p1_rows = info1
    p2_fanins, p2_rows = info2
    if len(p1_fanins) != 2 or len(p2_fanins) != 2:
        return False
    if set(p1_fanins) != set(p2_fanins):
        return False

    shared_order = list(p1_fanins)
    m1 = _minterm_bits(p1_fanins, p1_rows, shared_order)
    m2 = _minterm_bits(p2_fanins, p2_rows, shared_order)
    if m1 is None or m2 is None or m1 == m2:
        return False

    if not (m1[0] != m2[0] and m1[1] != m2[1]):
        return False

    combiner_rows = set(r.strip() for r in rows)
    if combiner_rows not in ({"00 0"}, {"11 1"}, {"00 1"}, {"11 0"}):
        return False

    node.is_xor = True
    p1.is_xor = True
    p2.is_xor = True
    return True


def read_blif(path):
    net = PebblingNetwork()
    name_to_node = {}
    node_cover_info = {}
    outputs = []
    pending = None
    pending_rows = []

    def get_or_create_pi(name):
        if name not in name_to_node:
            name_to_node[name] = net.create_pi(name)
        return name_to_node[name]

    def flush_pending():
        nonlocal pending, pending_rows
        if pending is None:
            return
        out_name, fanin_names = pending
        fanins = [name_to_node[f] for f in fanin_names]
        is_xor = _is_xor_cover(pending_rows, len(fanin_names))
        node = net.create_gate(fanins, name=out_name, is_xor=is_xor)
        name_to_node[out_name] = node
        node_cover_info[node] = (fanins, list(pending_rows))

        if not is_xor:
            _try_mark_compound_xor_cluster(node, fanins, pending_rows, node_cover_info)

        pending = None
        pending_rows = []

    with open(path, "r") as f:
        lines = f.readlines()

    joined_lines = []
    buf = ""
    for line in lines:
        line = line.rstrip("\n")
        if line.endswith("\\"):
            buf += line[:-1] + " "
        else:
            buf += line
            joined_lines.append(buf)
            buf = ""
    if buf:
        joined_lines.append(buf)

    for raw in joined_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith(".model"):
            continue
        if line.startswith(".inputs"):
            flush_pending()
            for name in line.split()[1:]:
                get_or_create_pi(name)
            continue
        if line.startswith(".outputs"):
            flush_pending()
            outputs.extend(line.split()[1:])
            continue
        if line.startswith(".names"):
            flush_pending()
            tokens = line.split()[1:]
            if len(tokens) < 1:
                continue
            *fanin_names, out_name = tokens
            for f in fanin_names:
                if f not in name_to_node:
                    get_or_create_pi(f)
            pending = (out_name, fanin_names)
            continue
        if line.startswith(".latch") or line.startswith(".gate") or line.startswith(".subckt"):
            raise NotImplementedError(
                "read_blif only supports combinational .names-based BLIF files"
            )
        if line.startswith(".end"):
            flush_pending()
            continue
        if pending is not None:
            pending_rows.append(line)
        continue

    flush_pending()
    for out_name in outputs:
        if out_name in name_to_node:
            net.create_po(name_to_node[out_name])

    return net


# ---------------------------------------------------------------------------
# Node-level Z3 pebbling solver
# ---------------------------------------------------------------------------

class Z3PebbleSolver:
    def __init__(self, net: PebblingNetwork, pebbles: int):
        self.net = net
        self.pebbles = pebbles
        self.slv = Solver()
        self.num_steps = 0
        self.current = {}
        self.model = None
        self.history = []

    def init(self):
        s0 = {n: Bool(f"s_0_{n.name}") for n in self.net.nodes}
        a0 = {n: Bool(f"a_0_{n.name}") for n in self.net.nodes}
        for n in self.net.nodes:
            self.slv.add(s0[n] == False)
            self.slv.add(a0[n] == False)
        self.current = {n: (s0[n], a0[n]) for n in self.net.nodes}
        self.history.append(dict(self.current))

    def add_step(self):
        self.num_steps += 1
        s_next = {n: Bool(f"s_{self.num_steps}_{n.name}") for n in self.net.nodes}
        a_next = {n: Bool(f"a_{self.num_steps}_{n.name}") for n in self.net.nodes}

        for n in self.net.nodes:
            s_cur, _ = self.current[n]
            s_nxt = s_next[n]
            a_nxt = a_next[n]

            if n.fanins:
                fanins_pebbled = And(
                    *[self.current[f][0] for f in n.fanins],
                    *[s_next[f] for f in n.fanins],
                )
                self.slv.add(Implies(s_cur != s_nxt, fanins_pebbled))

            self.slv.add(Implies(s_cur != s_nxt, a_nxt))
            self.slv.add(Implies(s_cur == s_nxt, a_nxt == False))

        if self.pebbles:
            self.slv.add(PbLe([(s_next[n], 1) for n in self.net.nodes], self.pebbles))

        self.current = {n: (s_next[n], a_next[n]) for n in self.net.nodes}
        self.history.append(dict(self.current))

    def solve(self):
        self.slv.push()
        po_set = set(self.net.pos)
        for n in self.net.nodes:
            s_cur, _ = self.current[n]
            if n in po_set:
                self.slv.add(s_cur)
            else:
                self.slv.add(s_cur == False)
        result = self.slv.check()
        if result == unsat:
            self.slv.pop()
        return result

    def save_model(self):
        self.model = self.slv.model()

    def extract_result(self, verbose=True):
        steps = []
        for k in range(1, self.num_steps + 1):
            comp, uncomp = [], []
            for n in self.net.nodes:
                s_prev, _ = self.history[k - 1][n]
                s_cur, a_cur = self.history[k][n]
                if self.model.eval(a_cur, model_completion=True):
                    s_prev_val = self.model.eval(s_prev, model_completion=True)
                    s_cur_val = self.model.eval(s_cur, model_completion=True)
                    assert bool(s_prev_val) != bool(s_cur_val)
                    if s_cur_val:
                        comp.append(n)
                    else:
                        uncomp.append(n)
            for n in uncomp:
                steps.append((n, "uncompute"))
                if verbose:
                    print(f"step {k}: uncompute {n.name}")
            for n in comp:
                steps.append((n, "compute"))
                if verbose:
                    print(f"step {k}: compute {n.name}")
        return steps


def pebble(net: PebblingNetwork, pebbles: int, max_steps="auto", verbose=True,
           auto_increase_pebbles=True, max_pebbles_cap=None):
    if pebbles < len(net.pos):
        raise ValueError(
            f"pebbles={pebbles} is less than the number of primary "
            f"outputs ({len(net.pos)})."
        )

    if max_pebbles_cap is None:
        max_pebbles_cap = max(1, len(net.nodes))
    max_pebbles_cap = max(max_pebbles_cap, pebbles)

    current_pebbles = pebbles
    while current_pebbles <= max_pebbles_cap:
        step_cap = max_steps
        if step_cap == "auto":
            step_cap = max(4, len(net.nodes) * 4)

        solver = Z3PebbleSolver(net, current_pebbles)
        solver.init()

        result = solver.solve()
        while result == unsat and (step_cap is None or solver.num_steps < step_cap):
            solver.add_step()
            result = solver.solve()

        if result == sat:
            solver.save_model()
            if current_pebbles != pebbles:
                print(f"\nRequested pebbles={pebbles} was infeasible; "
                      f"escalated to pebbles={current_pebbles}.")
            print(f"\nFound pebbling sequence using at most "
                  f"{current_pebbles} pebbles in {solver.num_steps} steps:\n")
            return solver.extract_result(verbose=verbose)

        if not auto_increase_pebbles:
            raise RuntimeError(
                f"No pebbling sequence found with pebbles={current_pebbles} "
                f"within {step_cap} steps."
            )

        print(f"pebbles={current_pebbles} infeasible within {step_cap} "
              f"steps; increasing to {current_pebbles + 1}...")
        current_pebbles += 1

    raise RuntimeError(
        f"No pebbling sequence found up to max_pebbles_cap={max_pebbles_cap}."
    )


def find_min_pebbles(net: PebblingNetwork, start=1, max_pebbles=None, max_steps="auto", verbose=False):
    if max_steps == "auto":
        max_steps = max(4, len(net.nodes) * 4)

    if max_pebbles is None:
        max_pebbles = max(1, len(net.nodes))
    max_pebbles = max(max_pebbles, start)
    max_pebbles = max(max_pebbles, len(net.pos))
    start = max(start, len(net.pos))

    p = start
    while p <= max_pebbles:
        solver = Z3PebbleSolver(net, p)
        solver.init()
        result = solver.solve()
        steps_taken = 0
        while result == unsat and (max_steps is None or steps_taken < max_steps):
            solver.add_step()
            result = solver.solve()
            steps_taken += 1

        if result == sat:
            solver.save_model()
            print(f"Minimum feasible pebble count: {p} "
                  f"(found in {solver.num_steps} steps)")
            return p, solver.extract_result(verbose=verbose)

        p += 1

    raise RuntimeError(f"No feasible pebbling sequence found up to max_pebbles={max_pebbles}.")


def group_pebbles(steps, pebble_limit):
    groups = []
    current_group = []
    cur_pebble = 0

    for node, action in steps:
        if action == "compute" and cur_pebble < pebble_limit:
            current_group.append(node)
            cur_pebble += 1
        elif action == "uncompute" and cur_pebble < pebble_limit:
            pass

        if cur_pebble == pebble_limit - 2:
            groups.append(current_group)
            current_group = []
            cur_pebble = 0

    if current_group:
        groups.append(current_group)

    return groups


def print_groups(groups):
    print(f"\n{len(groups)} pebble group(s):")
    for i, group in enumerate(groups):
        names = [n.name for n in group]
        print(f"  Group {i}: {names}")


# ---------------------------------------------------------------------------
# Gate structure: ownership-unique, boundary-derived
# ---------------------------------------------------------------------------

class GateGroup:
    """
    A gate group is a set of ORIGINAL DAG nodes (`self.nodes`), owned
    exclusively by this group.

    INVARIANT (established by `_split_mixed_po_groups`): `outputs`
    never simultaneously contains a genuine primary output AND a
    non-PO output.

    INVARIANT (established by `build_gate_groups`'s construction and
    re-verified defensively by `_validate_max_group_weight`): this
    group's weight, plus the cumulative unabsorbed PO weight already
    reserved by earlier groups, never exceeds the `max_pebbles`
    originally passed to `build_gate_groups` -- see module docstring,
    "HARD PER-GROUP WEIGHT CEILING" and "DIRTY-ANCILLA-AWARE PO SLACK
    TRACKING".
    """

    def __init__(self, gid):
        self.gid = gid
        self.nodes = []          # all nodes owned by this group, in topo order
        self.outputs = []        # == clean_pebble
        self.internal = []       # == dirty_pebble
        self.inputs = []         # external fanin nodes (PI or other-group)
        self.depends_on = set()  # other gate ids this gate depends on

    @property
    def clean_pebble(self):
        return self.outputs

    @property
    def dirty_pebble(self):
        return self.internal

    @property
    def xor_nodes(self):
        return []

    @property
    def dependencies(self):
        owned = set(self.nodes)
        return [n for n in self.nodes if any(fi not in owned for fi in n.fanins)]

    def all_nodes(self):
        return list(self.nodes)

    def __repr__(self):
        return f"GateGroup(id={self.gid}, nodes={[n.name for n in self.nodes]})"


def _compute_boundaries(net: PebblingNetwork, groups, pos_set, children=None):
    if children is None:
        children = net.build_children()

    owner_of = {}
    for g in groups:
        for n in g.nodes:
            owner_of[n] = g.gid

    for g in groups:
        owned = set(g.nodes)
        inputs = []
        seen_inputs = set()
        outputs = []
        internal = []

        for n in g.nodes:
            for fi in n.fanins:
                if fi not in owned and fi not in seen_inputs:
                    seen_inputs.add(fi)
                    inputs.append(fi)

            consumed_outside = any(ch not in owned for ch in children.get(n, []))
            if n in pos_set or consumed_outside:
                outputs.append(n)
            else:
                internal.append(n)

        g.inputs = inputs
        g.outputs = outputs
        g.internal = internal


def _renumber_groups(node_lists):
    groups = []
    for i, nodes in enumerate(node_lists):
        g = GateGroup(i)
        g.nodes = nodes
        groups.append(g)
    return groups


def _compute_shared_fanout_consumers(net: PebblingNetwork, children=None):
    if children is None:
        children = net.build_children()

    shared_consumers = set()
    for node in net.nodes:
        if node.is_pi or node.is_xor:
            continue
        non_xor_consumers = [ch for ch in children.get(node, []) if not ch.is_xor]
        if len(non_xor_consumers) >= 2:
            shared_consumers.update(non_xor_consumers)
    return shared_consumers


def _merge_sibling_fanout_groups(net: PebblingNetwork, groups, pos_set,
                                  max_pebbles, merge_pebble_slack=1.5,
                                  shared_fanout_consumers=None):
    """
    Merges topologically ADJACENT sibling groups that both consume the
    same shared non-XOR node. See module docstring ("SIBLING FANOUT
    MERGING"). The merge is only accepted if the merged group's weight
    does not exceed `min(max_pebbles, max_pebbles * merge_pebble_slack)`
    -- see module docstring, "HARD PER-GROUP WEIGHT CEILING".
    """
    children = net.build_children()
    if shared_fanout_consumers is None:
        shared_fanout_consumers = _compute_shared_fanout_consumers(net, children=children)

    _compute_boundaries(net, groups, pos_set, children=children)

    # HARD CEILING: merge_pebble_slack may only TIGHTEN the cap below
    # max_pebbles (values <= 1.0), never loosen it above max_pebbles.
    effective_cap = min(max_pebbles, max_pebbles * merge_pebble_slack)

    node_owner_gid = {}
    for g in groups:
        for n in g.nodes:
            node_owner_gid[n] = g.gid

    gid_to_index = {g.gid: i for i, g in enumerate(groups)}

    merged_index_runs = []
    already_planned = set()

    for node in net.nodes:
        if node.is_pi or node.is_xor:
            continue

        non_xor_consumers = [
            ch for ch in children.get(node, [])
            if not ch.is_xor
        ]
        if len(non_xor_consumers) < 2:
            continue

        consumer_gids = {node_owner_gid[ch] for ch in non_xor_consumers
                          if ch in node_owner_gid}
        if len(consumer_gids) < 2:
            continue

        consumer_indices = sorted(gid_to_index[gid] for gid in consumer_gids)
        lo, hi = consumer_indices[0], consumer_indices[-1]
        run_indices = list(range(lo, hi + 1))

        if already_planned.intersection(run_indices):
            continue

        merged_nodes = []
        for idx in run_indices:
            merged_nodes.extend(groups[idx].nodes)
        merged_cost = sum(
            _pebble_cost(n, shared_fanout_consumers) for n in merged_nodes
        )

        if merged_cost > effective_cap:
            continue  # merge would exceed the hard max_pebbles ceiling

        if len(run_indices) > 1:
            merged_index_runs.append((lo, hi))
            already_planned.update(run_indices)

    if not merged_index_runs:
        return groups

    merged_index_runs.sort()

    node_lists = []
    i = 0
    run_map = {lo: hi for lo, hi in merged_index_runs}
    while i < len(groups):
        if i in run_map:
            hi = run_map[i]
            combined_nodes = []
            for idx in range(i, hi + 1):
                combined_nodes.extend(groups[idx].nodes)
            node_lists.append(combined_nodes)
            i = hi + 1
        else:
            node_lists.append(groups[i].nodes)
            i += 1

    new_groups = _renumber_groups(node_lists)
    _compute_boundaries(net, new_groups, pos_set, children=children)
    return new_groups


def _split_mixed_po_groups(net: PebblingNetwork, groups, pos_set):
    children = net.build_children()
    _compute_boundaries(net, groups, pos_set, children=children)

    while True:
        node_lists = []
        changed = False

        for g in groups:
            po_outputs = [n for n in g.outputs if n in pos_set]
            non_po_outputs = [n for n in g.outputs if n not in pos_set]

            if po_outputs and non_po_outputs:
                sorted_outputs = sorted(g.outputs, key=lambda n: g.nodes.index(n))

                cut_idx = None
                prev_is_po = sorted_outputs[0] in pos_set
                prev_node_idx = g.nodes.index(sorted_outputs[0])
                for n in sorted_outputs[1:]:
                    is_po = n in pos_set
                    node_idx = g.nodes.index(n)
                    if is_po != prev_is_po:
                        cut_idx = prev_node_idx
                        break
                    prev_is_po = is_po
                    prev_node_idx = node_idx

                if cut_idx is not None:
                    first_part = g.nodes[:cut_idx + 1]
                    second_part = g.nodes[cut_idx + 1:]
                    if first_part and second_part:
                        node_lists.append(first_part)
                        node_lists.append(second_part)
                        changed = True
                        continue

            node_lists.append(g.nodes)

        if not changed:
            break

        groups = _renumber_groups(node_lists)
        _compute_boundaries(net, groups, pos_set, children=children)

    return groups


def _recombine_small_nonpo_groups(net: PebblingNetwork, groups, pos_set,
                                   max_pebbles, children=None,
                                   recombine_slack=1.0,
                                   shared_fanout_consumers=None):
    """
    Re-merges RUNS of topologically ADJACENT groups that are all
    entirely NON-PO back together, up to the HARD per-group weight
    ceiling, `min(max_pebbles, max_pebbles * recombine_slack)` -- see
    module docstring, "HARD PER-GROUP WEIGHT CEILING".
    """
    if children is None:
        children = net.build_children()
    if shared_fanout_consumers is None:
        shared_fanout_consumers = _compute_shared_fanout_consumers(net, children=children)

    def _is_po_group(g):
        return any(n in pos_set for n in g.outputs)

    node_lists = []
    run_nodes = []
    run_cost = 0

    def _flush_run():
        nonlocal run_nodes, run_cost
        if run_nodes:
            node_lists.append(run_nodes)
        run_nodes = []
        run_cost = 0

    # HARD CEILING: recombine_slack may only TIGHTEN the cap below
    # max_pebbles, never loosen it above max_pebbles.
    budget = min(max_pebbles, max_pebbles * recombine_slack)

    for g in groups:
        if _is_po_group(g):
            _flush_run()
            node_lists.append(g.nodes)
            continue

        group_cost = sum(
            _pebble_cost(n, shared_fanout_consumers) for n in g.nodes
        )

        if run_nodes and (run_cost + group_cost > budget):
            _flush_run()

        run_nodes.extend(g.nodes)
        run_cost += group_cost

    _flush_run()

    new_groups = _renumber_groups(node_lists)
    _compute_boundaries(net, new_groups, pos_set, children=children)
    return new_groups


def _pebble_cost(node, shared_fanout_consumers=None):
    if node.is_xor:
        return 0
    if shared_fanout_consumers is not None and node in shared_fanout_consumers:
        return 0
    return 1


def _group_clean_weight(g, shared_fanout_consumers=None):
    """
    Sum of `_pebble_cost` over `g.outputs` (clean/boundary values).
    See module docstring, "CLEAN/DIRTY SPLIT RESOURCE MODEL". Charged
    for a group's ENTIRE live window (compute until uncompute, or
    forever if PO).
    """
    return sum(_pebble_cost(n, shared_fanout_consumers) for n in g.outputs)


def _group_dirty_weight(g, shared_fanout_consumers=None):
    """
    Sum of `_pebble_cost` over `g.internal` (dirty/scratch values).
    See module docstring, "CLEAN/DIRTY SPLIT RESOURCE MODEL". Charged
    ONLY at the exact step/round the group's own compute_gate or
    uncompute_gate event fires -- not for its entire live window.
    """
    return sum(_pebble_cost(n, shared_fanout_consumers) for n in g.internal)


def summarize_po_lockin(gates, net):
    """
    Computes, for a solved `build_gate_groups` result, how many groups
    own at least one true primary output and how much CLEAN weight
    those PO-owning groups collectively lock in PERMANENTLY -- i.e.
    the hard floor on `live_clean_weight` that `schedule_gates_
    greedy_pebble_limited`'s uncompute phase can NEVER reclaim, since
    PO-owning groups are excluded from uncompute by construction.

    This is a FAST, SCHEDULING-FREE diagnostic: it only inspects the
    already-built `gates` list, so it can be computed in milliseconds
    even on a circuit where actual scheduling would deadlock or take
    hours. Use it to sanity-check whether a given `max_pebbles`/
    `solve_max_pebbles` budget could EVER succeed, before attempting
    to schedule at all -- if `total_po_clean_weight` alone already
    exceeds your intended budget, no schedule can succeed regardless
    of ordering, topo_order choice, or uncompute cleverness.

    Returns a dict:
        {
          "total_groups": int,
          "po_groups": int,
          "non_po_groups": int,
          "total_po_clean_weight": int,   # hard permanent floor
          "total_non_po_clean_weight": int,
          "largest_po_group": (gid, clean_weight) or None,
        }
    """
    children = net.build_children()
    shared_fanout_consumers = _compute_shared_fanout_consumers(net, children=children)
    pos_set = set(net.pos)

    po_groups = []
    non_po_groups = []
    for g in gates:
        if any(n in pos_set for n in g.outputs):
            po_groups.append(g)
        else:
            non_po_groups.append(g)

    po_weights = [
        (g.gid, _group_clean_weight(g, shared_fanout_consumers)) for g in po_groups
    ]
    non_po_weights = [
        _group_clean_weight(g, shared_fanout_consumers) for g in non_po_groups
    ]

    total_po_clean_weight = sum(w for _gid, w in po_weights)
    total_non_po_clean_weight = sum(non_po_weights)
    largest_po_group = max(po_weights, key=lambda t: t[1], default=None)

    return {
        "total_groups": len(gates),
        "po_groups": len(po_groups),
        "non_po_groups": len(non_po_groups),
        "total_po_clean_weight": total_po_clean_weight,
        "total_non_po_clean_weight": total_non_po_clean_weight,
        "largest_po_group": largest_po_group,
    }


def print_po_lockin_diagnostics(gates, net):
    """
    Prints `summarize_po_lockin`'s result in human-readable form.
    """
    info = summarize_po_lockin(gates, net)
    print(f"\n[PO lock-in diagnostic] {info['total_groups']} group(s) total: "
          f"{info['po_groups']} PO-owning, {info['non_po_groups']} non-PO.")
    print(f"[PO lock-in diagnostic] PERMANENT clean-weight floor from "
          f"PO-owning groups (never reclaimable by uncompute): "
          f"{info['total_po_clean_weight']}")
    if info["largest_po_group"] is not None:
        gid, w = info["largest_po_group"]
        print(f"[PO lock-in diagnostic] largest single PO-owning group: "
              f"gate {gid} (clean_weight={w})")
    print(f"[PO lock-in diagnostic] total clean weight across non-PO "
          f"groups (loose upper bound if ALL were simultaneously live, "
          f"NOT a real schedule): {info['total_non_po_clean_weight']}")
    if info["total_po_clean_weight"] > 0:
        print(f"[PO lock-in diagnostic] ANY max_pebbles/solve_max_pebbles "
              f"budget below {info['total_po_clean_weight']} can NEVER "
              f"succeed, regardless of scheduling order or topo_order "
              f"choice -- this is a hard floor.")
    return info


def _dependency_chain_topo_order(net: PebblingNetwork):
    children = net.build_children()
    creation_index = {n: i for i, n in enumerate(net.nodes)}

    remaining_fanins = {
        n: sum(1 for f in n.fanins if not f.is_pi)
        for n in net.nodes if not n.is_pi
    }
    placed = {n for n in net.nodes if n.is_pi}

    ready = sorted(
        (n for n, cnt in remaining_fanins.items() if cnt == 0),
        key=lambda n: creation_index[n],
    )

    order = [n for n in net.nodes if n.is_pi]
    last_placed = None

    while ready:
        chosen = None
        if last_placed is not None:
            candidates = sorted(
                (ch for ch in children.get(last_placed, [])
                 if ch not in placed and remaining_fanins.get(ch, -1) == 0),
                key=lambda n: creation_index[n],
            )
            if candidates:
                chosen = candidates[0]

        if chosen is None:
            chosen = ready[0]

        ready.remove(chosen)
        placed.add(chosen)
        order.append(chosen)

        for ch in children.get(chosen, []):
            if ch.is_pi or ch in placed:
                continue
            remaining_fanins[ch] -= 1
            if remaining_fanins[ch] == 0:
                ready.append(ch)
        ready.sort(key=lambda n: creation_index[n])

        last_placed = chosen

    return order


def _breadth_first_topo_order(net: PebblingNetwork):
    """
    Computes a BREADTH-FIRST topological order for `net.nodes` (a
    plain FIFO / Kahn's-algorithm topological sort), as an alternative
    to `_dependency_chain_topo_order`'s DFS-style successor-chaining.
    See module docstring, "TOPOLOGICAL ORDER CHOICE".

    Unlike the successor-preference heuristic, this does NOT try to
    place a node immediately before one of its own consumers. Instead,
    it processes the ENTIRE current "ready" frontier (every node whose
    fanins are all already placed) in FIFO order before advancing to
    the next frontier. This means ALL direct consumers of the SAME
    producer node -- i.e. true siblings in the "A has multiple
    fanouts" gadget shape -- become adjacent in the traversal order,
    since they all become ready at the same point (when `A` is
    placed) and are processed together, back-to-back, before any of
    THEIR own children are considered.

    Ties within a single ready frontier are broken by creation index,
    for determinism.

    As with `_dependency_chain_topo_order`, this is still a VALID
    topological order (every edge u -> w still satisfies
    topo(u) < topo(w)), so convexity of contiguous ranges (see module
    docstring, point 2) is preserved regardless of which order is
    used.
    """
    children = net.build_children()
    creation_index = {n: i for i, n in enumerate(net.nodes)}

    remaining_fanins = {
        n: sum(1 for f in n.fanins if not f.is_pi)
        for n in net.nodes if not n.is_pi
    }

    order = [n for n in net.nodes if n.is_pi]
    ready = deque(sorted(
        (n for n, cnt in remaining_fanins.items() if cnt == 0),
        key=lambda n: creation_index[n],
    ))

    while ready:
        node = ready.popleft()
        order.append(node)

        newly_ready = []
        for ch in children.get(node, []):
            if ch.is_pi:
                continue
            remaining_fanins[ch] -= 1
            if remaining_fanins[ch] == 0:
                newly_ready.append(ch)

        # Append newly-ready nodes in creation order for determinism,
        # preserving FIFO (breadth-first) processing of the frontier.
        for ch in sorted(newly_ready, key=lambda n: creation_index[n]):
            ready.append(ch)

    return order


def _print_group_size_histogram(groups, phase_label,
                                 bucket_edges=(1, 2, 3, 5, 10, 25, 50, 100, 200, 300)):
    if not groups:
        print(f"[build_gate_groups] [{phase_label}] no groups to measure.")
        return

    sizes = sorted(len(g.nodes) for g in groups)
    n = len(sizes)
    mean = sum(sizes) / n
    median = sizes[n // 2]

    print(f"[build_gate_groups] [{phase_label}] group size stats "
          f"(n={n}): min={sizes[0]} median={median} max={sizes[-1]} "
          f"mean={mean:.1f}")
    print(f"[build_gate_groups] [{phase_label}] smallest 10 sizes: {sizes[:10]}")
    print(f"[build_gate_groups] [{phase_label}] largest 10 sizes:  {sizes[-10:]}")

    edges = list(bucket_edges) + [float("inf")]
    counts = [0] * len(edges)
    for s in sizes:
        for i, edge in enumerate(edges):
            if s <= edge:
                counts[i] += 1
                break

    print(f"[build_gate_groups] [{phase_label}] size histogram:")
    for edge, count in zip(edges, counts):
        label = f"<= {edge}" if edge != float("inf") else f"> {bucket_edges[-1]}"
        bar = "#" * min(count, 60)
        print(f"    {label:>10s}: {count:>5d} {bar}")


def build_gate_groups(net: PebblingNetwork, max_pebbles: int, topo_order="dependency_chain",
                       merge_sibling_fanouts=True, merge_pebble_slack=1.5,
                       recombine_small_nonpo=True, recombine_slack=1.0,
                       debug_print=False):
    """
    Gate-group construction algorithm. `max_pebbles` is a HARD CEILING
    on every group's weight PLUS the cumulative unabsorbed PO weight
    already reserved by earlier groups -- see module docstring, "HARD
    PER-GROUP WEIGHT CEILING" and "DIRTY-ANCILLA-AWARE PO SLACK
    TRACKING". No group this function returns may ever exceed it,
    REGARDLESS of `merge_pebble_slack`/`recombine_slack` values.

    `topo_order` may be "creation", "dependency_chain", or
    "breadth_first" -- see module docstring, "TOPOLOGICAL ORDER
    CHOICE".

    Raises `ValueError` if the cumulative unabsorbed PO weight ever
    grows to meet or exceed `max_pebbles` partway through Phase 1
    partitioning -- this means NO further group of any size could
    ever be scheduled, and `max_pebbles` must be raised substantially.
    """
    if max_pebbles < len(net.pos):
        raise ValueError(
            f"max_pebbles={max_pebbles} is less than the number of primary "
            f"outputs ({len(net.pos)}). All POs must remain pebbled "
            f"simultaneously at the end, so max_pebbles must be >= "
            f"len(net.pos)."
        )

    pos_set = set(net.pos)
    children = net.build_children()
    shared_fanout_consumers = _compute_shared_fanout_consumers(net, children=children)

    if topo_order == "creation":
        traversal_order = net.nodes
    elif topo_order == "dependency_chain":
        traversal_order = _dependency_chain_topo_order(net)
    elif topo_order == "breadth_first":
        traversal_order = _breadth_first_topo_order(net)
    else:
        raise ValueError(
            f"Unknown topo_order: {topo_order!r} "
            f"(expected 'creation', 'dependency_chain', or "
            f"'breadth_first')"
        )

    # --- Phase 1: partition -------------------------------------------------
    groups = []
    current = GateGroup(0)
    cur_pebble = 0

    # DIRTY-ANCILLA-AWARE PO SLACK TRACKING (see module docstring):
    # `dirty_pool` is the cumulative freed-internal-node weight
    # available for PO reuse; `reserved_clean` is the cumulative PO
    # weight that could NOT be absorbed by dirty_pool and therefore
    # permanently reduces every SUBSEQUENT group's effective budget.
    dirty_pool = 0
    reserved_clean = 0

    def _finalize_po_slack_accounting(finished_group):
        """
        Updates dirty_pool/reserved_clean after `finished_group`
        closes. Internal (non-output) nodes replenish the dirty pool;
        PO outputs draw it down first, with only the shortfall
        reducing future groups' effective budget.
        """
        nonlocal dirty_pool, reserved_clean
        owned = set(finished_group.nodes)
        group_po_weight = 0
        group_internal_weight = 0
        for n in finished_group.nodes:
            cost = _pebble_cost(n, shared_fanout_consumers)
            if n in pos_set:
                group_po_weight += cost
            else:
                consumed_outside = any(
                    ch not in owned for ch in children.get(n, [])
                )
                if not consumed_outside:
                    group_internal_weight += cost
        dirty_pool += group_internal_weight
        covered = min(group_po_weight, dirty_pool)
        dirty_pool -= covered
        reserved_clean += (group_po_weight - covered)

    def finalize():
        nonlocal current
        if current.nodes:
            _finalize_po_slack_accounting(current)
            groups.append(current)
            current = GateGroup(len(groups))

    for node in traversal_order:
        if node.is_pi:
            continue

        current.nodes.append(node)
        node_cost = _pebble_cost(node, shared_fanout_consumers)

        # Effective per-group budget shrinks as earlier PO groups'
        # unabsorbed PO weight locks in permanently-reserved slack.
        effective_budget = max_pebbles - reserved_clean
        if effective_budget < 1:
            raise ValueError(
                f"build_gate_groups: cumulative unabsorbed PO weight "
                f"(reserved_clean={reserved_clean}) already meets or "
                f"exceeds max_pebbles={max_pebbles} partway through "
                f"partitioning -- no further group of any size could "
                f"ever be scheduled. This circuit's primary outputs "
                f"outpace the available dirty-ancilla pool for this "
                f"max_pebbles budget; raise max_pebbles substantially "
                f"(see module docstring, 'DIRTY-ANCILLA-AWARE PO SLACK "
                f"TRACKING')."
            )

        if node in pos_set:
            finalize()
            cur_pebble = 0
            continue

        cur_pebble += node_cost
        if node_cost == 0:
            continue

        if effective_budget <= 1:
            finalize()
            cur_pebble = 0
        elif cur_pebble >= effective_budget - 1:
            finalize()
            cur_pebble = 0

    finalize()

    if debug_print:
        print(f"[build_gate_groups] after Phase 1 (partition): {len(groups)} groups "
              f"(final reserved_clean={reserved_clean})")
        _print_group_size_histogram(groups, "Phase 1")

    # --- Phase 2: boundary computation ---------------------------------------
    _compute_boundaries(net, groups, pos_set, children=children)

    # --- Phase 2.5: sibling fanout merging ------------------------------------
    if merge_sibling_fanouts:
        groups = _merge_sibling_fanout_groups(
            net, groups, pos_set, max_pebbles, merge_pebble_slack=merge_pebble_slack,
            shared_fanout_consumers=shared_fanout_consumers,
        )
        if debug_print:
            print(f"[build_gate_groups] after Phase 2.5 (sibling merge): {len(groups)} groups")
            _print_group_size_histogram(groups, "Phase 2.5")

    # --- Phase 3: split mixed-PO groups ---------------------------------------
    groups = _split_mixed_po_groups(net, groups, pos_set)
    if debug_print:
        print(f"[build_gate_groups] after Phase 3 (mixed-PO split): {len(groups)} groups")
        _print_group_size_histogram(groups, "Phase 3")

    # --- Phase 3.5: recombine small adjacent non-PO fragments -----------------
    if recombine_small_nonpo:
        groups = _recombine_small_nonpo_groups(
            net, groups, pos_set, max_pebbles, recombine_slack=recombine_slack,
            shared_fanout_consumers=shared_fanout_consumers,
        )
        if debug_print:
            print(f"[build_gate_groups] after Phase 3.5 (recombine small "
                  f"non-PO fragments): {len(groups)} groups")
            _print_group_size_histogram(groups, "Phase 3.5")

    # --- Phase 4: dependency derivation ---------------------------------------
    node_owner = {}
    for g in groups:
        for n in g.nodes:
            node_owner[n] = g.gid

    for g in groups:
        deps = set()
        for inp in g.inputs:
            owner_gid = node_owner.get(inp)
            if owner_gid is not None and owner_gid != g.gid:
                deps.add(owner_gid)
        g.depends_on = deps

    # --- Phase 5: validation ---------------------------------------------------
    _validate_gate_partition(net, groups)
    _validate_no_mixed_po_groups(groups, pos_set)
    _validate_max_group_weight(net, groups, max_pebbles, pos_set, shared_fanout_consumers)
    cycle = find_gate_dependency_cycle(groups)
    if cycle is not None:
        raise AssertionError(
            f"Internal error: gate-dependency cycle {cycle} detected despite "
            f"contiguous-topological-range construction, which should be "
            f"convex by construction. Please report this as a bug."
        )

    return groups


def _validate_gate_partition(net: PebblingNetwork, groups):
    non_pi_nodes = [n for n in net.nodes if not n.is_pi]
    seen = {}
    for g in groups:
        for n in g.nodes:
            if n in seen:
                raise ValueError(
                    f"Node {n.name} appears in both gate {seen[n]} and "
                    f"gate {g.gid} -- gate partition is not unique."
                )
            seen[n] = g.gid

    missing = [n.name for n in non_pi_nodes if n not in seen]
    if missing:
        raise ValueError(f"Nodes missing from gate groups: {missing}")


def _validate_no_mixed_po_groups(groups, pos_set):
    for g in groups:
        po_outputs = [n for n in g.outputs if n in pos_set]
        non_po_outputs = [n for n in g.outputs if n not in pos_set]
        if po_outputs and non_po_outputs:
            raise AssertionError(
                f"Internal error: gate {g.gid} still has mixed PO/non-PO "
                f"outputs after splitting: PO outputs="
                f"{[n.name for n in po_outputs]}, non-PO outputs="
                f"{[n.name for n in non_po_outputs]}. Please report this "
                f"as a bug."
            )


def _validate_max_group_weight(net, groups, max_pebbles, pos_set, shared_fanout_consumers=None):
    """
    Defensive check: re-derives the SAME dirty-pool/reserved-clean
    running totals Phase 1 used (see module docstring, "DIRTY-ANCILLA-
    AWARE PO SLACK TRACKING") and confirms every group's own weight,
    plus the `reserved_clean` total already accumulated BEFORE it in
    topological order, does not exceed `max_pebbles`. Raises
    `AssertionError` on violation.
    """
    children = net.build_children()
    violations = []
    dirty_pool = 0
    reserved_clean = 0

    for g in groups:
        weight = sum(_pebble_cost(n, shared_fanout_consumers) for n in g.nodes)
        if weight + reserved_clean > max_pebbles:
            violations.append((g.gid, weight, reserved_clean, len(g.nodes)))

        owned = set(g.nodes)
        group_po_weight = 0
        group_internal_weight = 0
        for n in g.nodes:
            cost = _pebble_cost(n, shared_fanout_consumers)
            if n in pos_set:
                group_po_weight += cost
            else:
                consumed_outside = any(ch not in owned for ch in children.get(n, []))
                if not consumed_outside:
                    group_internal_weight += cost
        dirty_pool += group_internal_weight
        covered = min(group_po_weight, dirty_pool)
        dirty_pool -= covered
        reserved_clean += (group_po_weight - covered)

    if violations:
        details = ", ".join(
            f"gate {gid} (own_weight={w}, reserved_clean_before={rc}, "
            f"total={w + rc}, {n} node(s))"
            for gid, w, rc, n in violations
        )
        raise AssertionError(
            f"Internal error: {len(violations)} gate group(s) exceed the "
            f"hard max_pebbles={max_pebbles} ceiling (own weight + "
            f"unabsorbed reserved PO weight) after construction: "
            f"{details}. This group can NEVER be scheduled by "
            f"GatePebbleSolver or schedule_gates_greedy_pebble_limited -- "
            f"please report this as a bug in build_gate_groups."
        )


def find_gate_dependency_cycle(groups):
    deps = {g.gid: set(g.depends_on) for g in groups}

    WHITE, GRAY, BLACK = 0, 1, 2
    color = {g.gid: WHITE for g in groups}
    parent = {}

    def dfs(u):
        color[u] = GRAY
        for v in deps.get(u, ()):
            if color[v] == WHITE:
                parent[v] = u
                cyc = dfs(v)
                if cyc:
                    return cyc
            elif color[v] == GRAY:
                cyc = [v, u]
                cur = u
                while cur != v:
                    cur = parent[cur]
                    cyc.append(cur)
                return list(reversed(cyc))
        color[u] = BLACK
        return None

    for g in groups:
        if color[g.gid] == WHITE:
            cyc = dfs(g.gid)
            if cyc:
                return cyc
    return None


def expand_gate_schedule(gates, gate_steps):
    gate_by_id = {g.gid: g for g in gates}
    node_steps = []

    for k, gid, op, tag in gate_steps:
        g = gate_by_id[gid]
        if op == "compute_gate":
            for n in g.nodes:
                node_steps.append((n, "compute"))
        elif op == "uncompute_gate":
            for n in reversed(g.nodes):
                node_steps.append((n, "uncompute"))

    return node_steps


def reconstruct_original_graph(gates, pis, pos):
    net = PebblingNetwork()
    nodes = list(pis)
    for g in gates:
        nodes.extend(g.nodes)
    net.nodes = nodes
    net.pis = list(pis)
    net.pos = list(pos)
    return net


def display_gate_groups(gates):
    print("\nGate groups:")
    for g in gates:
        print(f"Gate {g.gid}")
        print(f"  nodes    : {[n.name for n in g.nodes]}")
        print(f"  outputs  : {[n.name for n in g.outputs]}")
        print(f"  internal : {[n.name for n in g.internal]}")
        print(f"  inputs   : {[n.name for n in g.inputs]}")
        print(f"  depends_on: {sorted(g.depends_on)}")


def print_gate_node_groups(gates):
    print(f"\n{len(gates)} gate group(s):")
    for g in gates:
        print(f"  Gate {g.gid}: {[n.name for n in g.nodes]}")


# ---------------------------------------------------------------------------
# Gate-level pebbling (max simultaneously pebbled AND-gates <= max_pebbles;
# pure-XOR gates are exempt from the cap)
# ---------------------------------------------------------------------------

class PebbleTimeoutError(RuntimeError):
    pass


class _WallClockBudget:
    def __init__(self, timeout_s):
        self.timeout_s = timeout_s
        self.start = time.time()

    def expired(self):
        if self.timeout_s is None:
            return False
        return (time.time() - self.start) >= self.timeout_s

    def elapsed_s(self):
        return time.time() - self.start

    def remaining_ms(self):
        if self.timeout_s is None:
            return None
        remaining = self.timeout_s - (time.time() - self.start)
        return max(1, int(remaining * 1000))


def schedule_gates_structurally(gates, net, free_policy="eager"):
    if free_policy not in ("eager", "lazy"):
        raise ValueError(f"Unknown free_policy: {free_policy!r}")

    pos_set = set(net.pos)

    po_gate_ids = {
        g.gid for g in gates if any(n in pos_set for n in g.outputs)
    }

    dependents = defaultdict(set)
    for g in gates:
        for dep_gid in g.depends_on:
            dependents[dep_gid].add(g.gid)

    remaining_dependents = {
        g.gid: len(dependents.get(g.gid, ())) for g in gates
    }

    def _is_xor_only(g):
        return len(g.nodes) > 0 and all(n.is_xor for n in g.nodes)

    tag_of = {g.gid: ("xor" if _is_xor_only(g) else "and") for g in gates}

    node_index = {n: i for i, n in enumerate(net.nodes)}
    topo_order = sorted(
        gates,
        key=lambda g: min((node_index[n] for n in g.nodes), default=0),
    )

    gate_steps = []
    step = 0
    live = set()

    for g in topo_order:
        step += 1
        gate_steps.append((step, g.gid, "compute_gate", tag_of[g.gid]))
        live.add(g.gid)

        if free_policy == "eager":
            for dep_gid in g.depends_on:
                remaining_dependents[dep_gid] -= 1
                if remaining_dependents[dep_gid] == 0 and dep_gid in live:
                    if dep_gid not in po_gate_ids:
                        gate_steps.append(
                            (step, dep_gid, "uncompute_gate", tag_of[dep_gid])
                        )
                        live.discard(dep_gid)
        else:  # "lazy"
            for dep_gid in g.depends_on:
                remaining_dependents[dep_gid] -= 1

    if free_policy == "lazy":
        step += 1
        for g in topo_order:
            if g.gid in live and g.gid not in po_gate_ids:
                gate_steps.append((step, g.gid, "uncompute_gate", tag_of[g.gid]))
                live.discard(g.gid)

    return gate_steps


def schedule_gates_greedy_pebble_limited(gates, net, max_pebbles, clean_pebbles=None,
                                          node_level_clean_release=True,
                                          dump_json=None):
    """
    Greedy, non-SAT heuristic scheduler. See module docstring, "GREEDY
    PEBBLE-LIMITED SCHEDULING", "CLEAN/DIRTY SPLIT RESOURCE MODEL",
    "NODE-LEVEL CLEAN WEIGHT RELEASE", "LOOP TERMINATION: PO-ONLY LIVE
    SET IS SUCCESS, NOT DEADLOCK", and "GREEDY SCHEDULER DEBUG JSON
    DUMP". Raises `RuntimeError` on a GENUINE deadlock only.

    Budget accounting splits each group's weight: `clean_weight` (sum
    over `g.outputs`) is charged for the group's ENTIRE live window
    (persists in `live_clean_weight` from compute until uncompute);
    `dirty_weight` (sum over `g.internal`) is charged ONLY for the
    round in which that SPECIFIC group's compute_gate or
    uncompute_gate event fires (accumulated into a per-round
    `round_dirty_weight` that resets every round).

    `node_level_clean_release` (default True): instead of releasing a
    group's ENTIRE clean_weight only when ALL of the group's
    dependents have resolved (or never, if it owns a PO), release
    each OUTPUT NODE's weight individually, the moment THAT SPECIFIC
    node's own remaining consumers have all resolved (i.e. computed).
    This matters when a group's outputs are consumed by DIFFERENT
    downstream groups on different timelines -- previously the
    group's whole clean_weight stayed charged against the budget
    until the SLOWEST dependent resolved, even if some of its other
    output nodes became safe to release much earlier. Pass False to
    restore the old all-or-nothing per-group release behavior.

    `dump_json` (optional path): if set, writes the solved gate-level
    schedule, gate groups, and expanded node-level pebbling sequence
    to this path as JSON via `dump_pebbling_debug_json`, on BOTH
    success (`extra={"outcome": "greedy_success"}`) and a genuine
    deadlock (`extra={"outcome": "greedy_deadlock", ...}`, including
    the step, live_clean_weight, and stuck gate ids). Purely a
    debug/dev toggle -- does not affect scheduling behavior.
    """
    if clean_pebbles is None:
        clean_pebbles = max_pebbles

    pos_set = set(net.pos)
    po_gate_ids = {g.gid for g in gates if any(n in pos_set for n in g.outputs)}

    children = net.build_children()
    shared_fanout_consumers = _compute_shared_fanout_consumers(net, children=children)

    def _is_xor_only(g):
        return len(g.nodes) > 0 and all(n.is_xor for n in g.nodes)

    tag_of = {g.gid: ("xor" if _is_xor_only(g) else "and") for g in gates}

    gate_by_id = {g.gid: g for g in gates}
    clean_weight = {
        g.gid: _group_clean_weight(g, shared_fanout_consumers) for g in gates
    }
    dirty_weight = {
        g.gid: _group_dirty_weight(g, shared_fanout_consumers) for g in gates
    }

    dependents = defaultdict(set)
    for g in gates:
        for dep_gid in g.depends_on:
            dependents[dep_gid].add(g.gid)

    remaining_dependents = {g.gid: len(dependents.get(g.gid, ())) for g in gates}
    remaining_deps_of = {g.gid: set(g.depends_on) for g in gates}

    node_index = {n: i for i, n in enumerate(net.nodes)}

    def _topo_key(g):
        return min((node_index[n] for n in g.nodes), default=0)

    # --- NODE-LEVEL CLEAN RELEASE SETUP -------------------------------------
    # node_pending_consumers[n]: number of OTHER groups that still need
    # node `n` as an input, and haven't resolved (computed) yet. When
    # this hits 0 for a non-PO output node, its weight is released
    # IMMEDIATELY, independent of the rest of its owning group's state.
    node_pending_consumers = defaultdict(int)
    node_consumer_gids = defaultdict(set)
    for g in gates:
        for inp in g.inputs:
            node_consumer_gids[inp].add(g.gid)
    for n, consumer_gids in node_consumer_gids.items():
        node_pending_consumers[n] = len(consumer_gids)

    node_weight = {}
    for g in gates:
        for n in g.outputs:
            if n not in pos_set:
                node_weight[n] = _pebble_cost(n, shared_fanout_consumers)

    def _resolve_consumer(gid):
        """
        Marks `gid` as having resolved (computed) its own input
        dependencies, releasing any of those input nodes whose last
        pending consumer was `gid`. Returns the total weight released.
        """
        if not node_level_clean_release:
            return 0
        released = 0
        for inp in gate_by_id[gid].inputs:
            if inp in pos_set or inp not in node_weight:
                continue
            node_pending_consumers[inp] -= 1
            if node_pending_consumers[inp] <= 0 and inp in node_weight:
                released += node_weight.pop(inp)
        return released

    pending = set(g.gid for g in gates)
    live = set()
    done = set()
    live_clean_weight = 0

    ready = sorted(
        (g.gid for g in gates if not remaining_deps_of[g.gid]),
        key=lambda gid: _topo_key(gate_by_id[gid]),
    )
    ready = list(dict.fromkeys(ready))

    gate_steps = []
    step = 0

    if not ready and pending:
        raise RuntimeError(
            "schedule_gates_greedy_pebble_limited: no gate is initially "
            "ready (every gate has an unmet dependency) -- this should "
            "be impossible for an acyclic gate-dependency graph; "
            "indicates a bug in build_gate_groups."
        )

    while pending or live:
        step += 1
        progressed = False
        round_dirty_weight = 0  # resets every round -- transient dirty
                                 # cost of toggles scheduled THIS round

        # --- 1. COMPUTE PHASE -------------------------------------------
        still_ready = []
        for gid in ready:
            cw = clean_weight[gid]
            dw = dirty_weight[gid]
            fits_total = (
                live_clean_weight + cw + round_dirty_weight + dw
            ) <= max_pebbles
            fits_clean = (cw == 0) or ((live_clean_weight + cw) <= clean_pebbles)

            if fits_total and fits_clean:
                gate_steps.append((step, gid, "compute_gate", tag_of[gid]))
                live.add(gid)
                pending.discard(gid)
                live_clean_weight += cw
                round_dirty_weight += dw
                progressed = True

                # This gid has now "consumed" its own dependencies --
                # release any producer output nodes whose last pending
                # consumer was gid (node-level release, independent of
                # the PRODUCER group's own uncompute state).
                released = _resolve_consumer(gid)
                live_clean_weight -= released

                for dependent_gid in dependents.get(gid, ()):
                    remaining_deps_of[dependent_gid].discard(gid)
            else:
                still_ready.append(gid)
        ready = still_ready

        # --- 2. UNCOMPUTE PHASE ------------------------------------------
        newly_freed = []
        for gid in sorted(live, key=lambda gid: _topo_key(gate_by_id[gid])):
            if gid in po_gate_ids:
                continue
            deps = dependents.get(gid, ())
            dw = dirty_weight[gid]
            fits_dirty = (
                live_clean_weight + round_dirty_weight + dw
            ) <= max_pebbles
            if all((d in live) or (d in done) for d in deps) and fits_dirty:
                gate_steps.append((step, gid, "uncompute_gate", tag_of[gid]))
                newly_freed.append(gid)
                round_dirty_weight += dw

        for gid in newly_freed:
            live.discard(gid)
            done.add(gid)
            # With node-level release, some (or all) of this group's
            # clean_weight may have ALREADY been subtracted earlier,
            # as individual output nodes were released one at a time.
            # Only subtract whatever weight is STILL outstanding for
            # this group's own output nodes to avoid double-counting.
            if node_level_clean_release:
                still_outstanding = sum(
                    node_weight.pop(n, 0) for n in gate_by_id[gid].outputs
                )
                live_clean_weight -= still_outstanding
            else:
                live_clean_weight -= clean_weight[gid]
            progressed = True

        # --- 3. Recompute ready set for next round -----------------------
        newly_ready = [
            gid for gid in pending
            if gid not in live and not remaining_deps_of[gid]
            and gid not in ready
        ]
        if newly_ready:
            ready.extend(
                sorted(newly_ready, key=lambda gid: _topo_key(gate_by_id[gid]))
            )

        if not progressed:
            # SUCCESS CASE (see module docstring, "LOOP TERMINATION:
            # PO-ONLY LIVE SET IS SUCCESS, NOT DEADLOCK"): if nothing
            # remains pending and every group still live is PO-owning,
            # this is the CORRECT terminal state -- PO-owning groups
            # stay live forever by design, so there is genuinely
            # nothing left to compute or uncompute. This is NOT a
            # deadlock; break cleanly and return the completed
            # schedule built so far.
            if not pending and live.issubset(po_gate_ids):
                break

            stuck_ready = sorted(ready)[:5]
            stuck_pending = sorted(
                gid for gid in pending if gid not in live
            )[:5]

            # Best-effort debug dump even on a genuine deadlock, so
            # the partial gate_steps built so far (and the gate
            # groups) remain inspectable post-mortem -- mirrors
            # pebble_gates's own error-path dump behavior.
            if dump_json:
                try:
                    node_steps = expand_gate_schedule(gates, gate_steps)
                    dump_pebbling_debug_json(
                        dump_json,
                        node_steps=node_steps,
                        gate_groups=gates,
                        gate_steps=gate_steps,
                        extra={
                            "outcome": "greedy_deadlock",
                            "deadlocked_at_step": step,
                            "live_clean_weight": live_clean_weight,
                            "stuck_ready": stuck_ready,
                            "stuck_pending": stuck_pending,
                        },
                    )
                except Exception as dump_e:  # noqa: BLE001 -- never let the debug dump itself crash the batch
                    print(f"    [pebbling debug json] failed to dump "
                          f"greedy deadlock state: "
                          f"{type(dump_e).__name__}: {dump_e}")

            raise RuntimeError(
                f"schedule_gates_greedy_pebble_limited: deadlocked at step "
                f"{step} -- no compute or uncompute could be scheduled. "
                f"max_pebbles={max_pebbles} (or clean_pebbles="
                f"{clean_pebbles}) is likely too small given current "
                f"live_clean_weight={live_clean_weight}. "
                f"ready-but-unfit gate ids: {stuck_ready} "
                f"(clean_weight={[clean_weight.get(g) for g in stuck_ready]}, "
                f"dirty_weight={[dirty_weight.get(g) for g in stuck_ready]}); "
                f"pending-but-blocked gate ids (deps not yet computed): "
                f"{stuck_pending} (unmet deps: "
                f"{[sorted(remaining_deps_of.get(g, set())) for g in stuck_pending]}, "
                f"clean_weight={[clean_weight.get(g) for g in stuck_pending]})."
            )

    # SUCCESS: dump the solved schedule, same artifact shape as
    # pebble_gates's success-path dump -- see module docstring,
    # "GREEDY SCHEDULER DEBUG JSON DUMP".
    if dump_json:
        node_steps = expand_gate_schedule(gates, gate_steps)
        dump_pebbling_debug_json(
            dump_json,
            node_steps=node_steps,
            gate_groups=gates,
            gate_steps=gate_steps,
            extra={"outcome": "greedy_success"},
        )

    return gate_steps


class GatePebbleSolver:
    """
    Pebbles whole GateGroup objects. See module docstring, "WEIGHTED
    PBLE CONSTRAINT: GROUP COUNT -> QUBIT COUNT" and "CLEAN/DIRTY
    SPLIT RESOURCE MODEL".
    """

    def __init__(self, gates, max_pebbles, net=None, z3_timeout_ms=None):
        self.gates = gates
        self.max_pebbles = max_pebbles
        self.slv = Solver()
        if z3_timeout_ms is not None:
            self.slv.set("timeout", int(z3_timeout_ms))
        self.num_steps = 0
        self.current = {}       # gid -> (s, a)
        self.ever = {}          # gid -> ever_computed Bool
        self.history = []
        self.model = None

        def _is_xor_only(g):
            return len(g.nodes) > 0 and all(n.is_xor for n in g.nodes)

        self.counts_toward_limit = {g.gid: not _is_xor_only(g) for g in gates}
        self.gate_deps = {g.gid: set(g.depends_on) for g in gates}

        self.dependents = defaultdict(set)
        for g in gates:
            for dep_gid in g.depends_on:
                self.dependents[dep_gid].add(g.gid)

        pos_set = set(net.pos) if net is not None else set()
        self.po_gate_ids = {
            g.gid for g in gates if any(n in pos_set for n in g.outputs)
        }

        shared_fanout_consumers = (
            _compute_shared_fanout_consumers(net) if net is not None else None
        )

        # CLEAN/DIRTY SPLIT RESOURCE MODEL (see module docstring):
        # clean_weight is charged for a group's ENTIRE live window
        # (its `s` state); dirty_weight is charged ONLY at the step
        # its own compute/uncompute event fires (its `a` variable).
        self.clean_weight = {
            g.gid: _group_clean_weight(g, shared_fanout_consumers) for g in gates
        }
        self.dirty_weight = {
            g.gid: _group_dirty_weight(g, shared_fanout_consumers) for g in gates
        }

    def init(self):
        s0 = {g.gid: Bool(f"Gs_0_{g.gid}") for g in self.gates}
        a0 = {g.gid: Bool(f"Ga_0_{g.gid}") for g in self.gates}
        ever0 = {g.gid: Bool(f"Gever_0_{g.gid}") for g in self.gates}
        for gid in s0:
            self.slv.add(s0[gid] == False)
            self.slv.add(a0[gid] == False)
            self.slv.add(ever0[gid] == False)
        self.current = {gid: (s0[gid], a0[gid]) for gid in s0}
        self.ever = dict(ever0)
        self.history.append(dict(self.current))

    def add_step(self):
        self.num_steps += 1
        s_next = {g.gid: Bool(f"Gs_{self.num_steps}_{g.gid}") for g in self.gates}
        a_next = {g.gid: Bool(f"Ga_{self.num_steps}_{g.gid}") for g in self.gates}
        ever_next = {g.gid: Bool(f"Gever_{self.num_steps}_{g.gid}") for g in self.gates}

        for g in self.gates:
            gid = g.gid
            s_cur, _ = self.current[gid]
            s_nxt = s_next[gid]
            a_nxt = a_next[gid]
            ever_cur = self.ever[gid]
            ever_nxt = ever_next[gid]

            if self.gate_deps[gid]:
                deps_now = [self.current[d][0] for d in self.gate_deps[gid]]
                deps_next = [s_next[d] for d in self.gate_deps[gid]]
                self.slv.add(Implies(s_cur != s_nxt, And(*(deps_now + deps_next))))

            self.slv.add(Implies(s_cur != s_nxt, a_nxt))
            self.slv.add(Implies(s_cur == s_nxt, a_nxt == False))

            self.slv.add(ever_nxt == Or(ever_cur, s_nxt))

            if gid in self.po_gate_ids:
                self.slv.add(Implies(s_cur, s_nxt))
            else:
                is_recompute = And(ever_cur, Not(s_cur), s_nxt)
                dep_gids = self.dependents.get(gid, ())
                if dep_gids:
                    justified = Or(*[
                        And(Not(self.current[d][0]), s_next[d])
                        for d in dep_gids
                    ])
                    self.slv.add(Implies(is_recompute, justified))
                else:
                    self.slv.add(Not(is_recompute))

        limited_gates = [g for g in self.gates if self.counts_toward_limit[g.gid]]
        if limited_gates:
            # CLEAN/DIRTY SPLIT RESOURCE MODEL: clean weight is charged
            # while the group's `s` state is live; dirty weight is
            # charged only while the group's `a` (toggling) variable
            # is true THIS step -- see module docstring.
            pble_terms = (
                [(s_next[g.gid], self.clean_weight[g.gid]) for g in limited_gates
                 if self.clean_weight[g.gid] > 0]
                + [(a_next[g.gid], self.dirty_weight[g.gid]) for g in limited_gates
                   if self.dirty_weight[g.gid] > 0]
            )
            if pble_terms:
                self.slv.add(PbLe(pble_terms, self.max_pebbles))

        self.current = {gid: (s_next[gid], a_next[gid]) for gid in s_next}
        self.ever = dict(ever_next)
        self.history.append(dict(self.current))

    def build_all_steps(self, num_steps):
        for _ in range(num_steps):
            self.add_step()

    def solve(self):
        self.slv.push()
        for g in self.gates:
            s_cur, _ = self.current[g.gid]
            if g.gid in self.po_gate_ids:
                self.slv.add(s_cur)
            else:
                self.slv.add(s_cur == False)
        r = self.slv.check()
        if r == unsat:
            self.slv.pop()
        return r

    def solve_with_toggle_budget(self, toggle_budget):
        self.slv.push()

        all_activity = []
        for k in range(1, self.num_steps + 1):
            for g in self.gates:
                _, a_k = self.history[k][g.gid]
                all_activity.append(a_k)
        if all_activity:
            self.slv.add(PbLe([(a, 1) for a in all_activity], toggle_budget))

        for g in self.gates:
            s_cur, _ = self.current[g.gid]
            if g.gid in self.po_gate_ids:
                self.slv.add(s_cur)
            else:
                self.slv.add(s_cur == False)

        r = self.slv.check()
        if r == unsat:
            self.slv.pop()
        return r

    def save_model(self):
        self.model = self.slv.model()

    def extract(self, verbose=True):
        seq = []
        for k in range(1, self.num_steps + 1):
            for g in self.gates:
                gid = g.gid
                s_prev, _ = self.history[k - 1][gid]
                s_cur, a_cur = self.history[k][gid]
                if self.model.eval(a_cur, model_completion=True):
                    is_on = bool(self.model.eval(s_cur, model_completion=True))
                    op = "compute_gate" if is_on else "uncompute_gate"
                    tag = "xor" if not self.counts_toward_limit[gid] else "and"
                    seq.append((k, gid, op, tag))
                    if verbose:
                        print(f"step {k}: {op} gate {gid} ({tag})")
        return seq


def _pebbling_steps_to_json(steps):
    return [{"node": n.name, "action": action} for n, action in steps]


def _gate_groups_to_json(gates):
    out = []
    for g in gates:
        out.append({
            "gid": g.gid,
            "nodes": [n.name for n in g.nodes],
            "node_is_xor": [n.is_xor for n in g.nodes],  # NEW: per-node XOR flag, aligned by index with "nodes"
            "outputs": [n.name for n in g.outputs],
            "internal": [n.name for n in g.internal],
            "inputs": [n.name for n in g.inputs],
            "depends_on": sorted(g.depends_on),
        })
    return out

def dump_pebbling_debug_json(path, node_steps=None, gate_groups=None,
                              gate_steps=None, extra=None):
    import json
    payload = {}
    if node_steps is not None:
        payload["node_pebbling_sequence"] = _pebbling_steps_to_json(node_steps)
    if gate_groups is not None:
        payload["gate_groups"] = _gate_groups_to_json(gate_groups)
    if gate_steps is not None:
        payload["gate_level_schedule"] = [
            {"step": k, "gate_id": gid, "op": op, "tag": tag}
            for (k, gid, op, tag) in gate_steps
        ]
    if extra:
        payload.update(extra)

    with open(path, "w") as f:
        json.dump(payload, f, indent=2)

    print(f"\nDumped pebbling debug info to {path}")


class _TimedOut(Exception):
    pass


def pebble_gates(gates, max_pebbles, max_steps="auto", verbose=True,
                  auto_increase_pebbles=True, max_pebbles_cap=None, net=None,
                  dump_json=None, max_escalations=None, timeout_s=None,
                  use_structural_fallback=False, structural_free_policy="eager"):
    """
    Solves for a valid gate-level pebbling schedule via bidirectional
    Z3 search. See module docstring, "BIDIRECTIONAL PEBBLE-COUNT
    SEARCH".
    """
    def _structural_fallback_result(reason):
        print(f"[pebble_gates] falling back to schedule_gates_structurally "
              f"({reason}); free_policy={structural_free_policy!r}. No "
              f"pebble-budget guarantee applies to this schedule.")
        gate_steps = schedule_gates_structurally(
            gates, net, free_policy=structural_free_policy
        )
        if dump_json:
            node_steps = expand_gate_schedule(gates, gate_steps) if gates else None
            dump_pebbling_debug_json(
                dump_json,
                node_steps=node_steps,
                gate_groups=gates,
                gate_steps=gate_steps,
                extra={"escalation_log": escalation_log,
                       "outcome": "structural_fallback",
                       "fallback_reason": reason,
                       "structural_free_policy": structural_free_policy},
            )
        return gate_steps

    if max_pebbles_cap is None:
        max_pebbles_cap = max(1, len(gates))
    max_pebbles_cap = max(max_pebbles_cap, max_pebbles)

    escalations_tried = 0
    escalation_log = []
    budget = _WallClockBudget(timeout_s)

    min_pebbles_floor = max(1, len(net.pos)) if net is not None else 1

    def _timeout_bail(partial_log):
        if use_structural_fallback:
            return _structural_fallback_result(
                f"overall timeout_s={timeout_s}s exceeded after "
                f"{len(partial_log)} escalation round(s)"
            )
        if dump_json:
            dump_pebbling_debug_json(
                dump_json,
                extra={"escalation_log": partial_log, "outcome": "timeout",
                       "elapsed_s": budget.elapsed_s(), "timeout_s": timeout_s},
            )
        raise PebbleTimeoutError(
            f"pebble_gates exceeded its overall timeout_s={timeout_s}s "
            f"after {len(partial_log)} escalation round(s) "
            f"({budget.elapsed_s():.2f}s elapsed)."
        )

    def _attempt(current_pebbles):
        nonlocal escalations_tried
        round_index = escalations_tried + 1
        round_start = time.time()

        step_cap = max_steps
        if step_cap == "auto":
            step_cap = max(4, len(gates) * 4)

        s = GatePebbleSolver(gates, current_pebbles, net=net,
                              z3_timeout_ms=budget.remaining_ms())
        s.init()
        r = s.solve()
        if budget.expired():
            round_elapsed = time.time() - round_start
            escalation_log.append({
                "round": round_index, "max_pebbles": current_pebbles,
                "steps_reached": s.num_steps, "step_cap": step_cap,
                "result": "timeout_mid_round", "elapsed_s": round_elapsed,
            })
            raise _TimedOut()
        while r == unsat and (step_cap is None or s.num_steps < step_cap):
            s.add_step()
            r = s.solve()
            if budget.expired():
                round_elapsed = time.time() - round_start
                escalation_log.append({
                    "round": round_index, "max_pebbles": current_pebbles,
                    "steps_reached": s.num_steps, "step_cap": step_cap,
                    "result": "timeout_mid_round", "elapsed_s": round_elapsed,
                })
                raise _TimedOut()

        round_elapsed = time.time() - round_start
        escalations_tried += 1

        escalation_log.append({
            "round": round_index,
            "max_pebbles": current_pebbles,
            "steps_reached": s.num_steps,
            "step_cap": step_cap,
            "result": "sat" if r == sat else "unsat_exhausted",
            "elapsed_s": round_elapsed,
        })

        print(f"[round {round_index}] max_pebbles={current_pebbles} -> "
              f"{'SAT' if r == sat else 'UNSAT'} after {s.num_steps} step(s) "
              f"({round_elapsed:.2f}s)")

        if r == sat:
            s.save_model()
            gate_steps = s.extract(verbose=verbose)
            return r, gate_steps
        return r, None

    def _finish_sat(final_pebbles, gate_steps, note=None):
        if note:
            print(note)
        if dump_json:
            node_steps = expand_gate_schedule(gates, gate_steps) if gates else None
            dump_pebbling_debug_json(
                dump_json,
                node_steps=node_steps,
                gate_groups=gates,
                gate_steps=gate_steps,
                extra={"escalation_log": escalation_log, "outcome": "sat",
                       "final_max_pebbles": final_pebbles},
            )
        return gate_steps

    if budget.expired():
        return _timeout_bail(escalation_log)

    try:
        r0, gate_steps0 = _attempt(max_pebbles)
    except _TimedOut:
        return _timeout_bail(escalation_log)

    if r0 == sat:
        best_pebbles, best_steps = max_pebbles, gate_steps0
        current_pebbles = max_pebbles - 1

        while current_pebbles >= min_pebbles_floor:
            if max_escalations is not None and escalations_tried >= max_escalations:
                break

            try:
                r, steps = _attempt(current_pebbles)
            except _TimedOut:
                break

            if r == sat:
                best_pebbles, best_steps = current_pebbles, steps
                current_pebbles -= 1
            else:
                print(f"max_pebbles={current_pebbles} is UNSAT; "
                      f"minimum feasible budget found is {best_pebbles}.")
                break

        note = None
        if best_pebbles != max_pebbles:
            note = (f"\nRequested max_pebbles={max_pebbles} was more than "
                    f"needed; tightened down to {best_pebbles} after "
                    f"{escalations_tried} total attempt(s).")

        return _finish_sat(best_pebbles, best_steps, note=note)

    current_pebbles = max_pebbles + 1
    while current_pebbles <= max_pebbles_cap:
        if max_escalations is not None and escalations_tried >= max_escalations:
            if use_structural_fallback:
                return _structural_fallback_result(
                    f"max_escalations={max_escalations} reached"
                )
            if dump_json:
                dump_pebbling_debug_json(
                    dump_json,
                    extra={"escalation_log": escalation_log, "outcome": "max_escalations_reached"},
                )
            raise RuntimeError(
                f"max_escalations={max_escalations} reached while searching "
                f"upward from max_pebbles={max_pebbles} (last tried: "
                f"{current_pebbles - 1})."
            )

        try:
            r, gate_steps = _attempt(current_pebbles)
        except _TimedOut:
            return _timeout_bail(escalation_log)

        if r == sat:
            note = (f"\nRequested max_pebbles={max_pebbles} was infeasible; "
                    f"escalated to {current_pebbles} (after "
                    f"{escalations_tried} total attempt(s)).")
            return _finish_sat(current_pebbles, gate_steps, note=note)

        if not auto_increase_pebbles:
            if use_structural_fallback:
                return _structural_fallback_result(
                    "auto_increase_pebbles is disabled and UNSAT at "
                    f"max_pebbles={current_pebbles}"
                )
            if dump_json:
                dump_pebbling_debug_json(
                    dump_json,
                    extra={"escalation_log": escalation_log, "outcome": "auto_increase_disabled"},
                )
            raise RuntimeError(
                f"No gate-level pebbling solution found with max_pebbles="
                f"{current_pebbles} within its step cap."
            )

        print(f"max_pebbles={current_pebbles} infeasible; increasing to "
              f"{current_pebbles + 1}... (escalation round "
              f"{escalations_tried}"
              + (f"/{max_escalations}" if max_escalations is not None else "")
              + ")")
        current_pebbles += 1

    if use_structural_fallback:
        return _structural_fallback_result(
            f"max_pebbles_cap={max_pebbles_cap} reached without finding a schedule"
        )
    if dump_json:
        dump_pebbling_debug_json(
            dump_json,
            extra={"escalation_log": escalation_log, "outcome": "max_pebbles_cap_reached"},
        )
    raise RuntimeError(
        f"No gate-level pebbling solution found for any max_pebbles from "
        f"{max_pebbles} up to max_pebbles_cap={max_pebbles_cap}."
    )


def pebble_gates_greedy_first(gates, max_pebbles, net, clean_pebbles=None,
                               use_sat_fallback=False, force_sat=False,
                               verbose=True, dump_json=None,
                               **pebble_gates_kwargs):
    """
    Greedy-first scheduling entry point. See module docstring,
    "GREEDY-FIRST SCHEDULING (`pebble_gates_greedy_first`)" and
    "GREEDY SCHEDULER DEBUG JSON DUMP".

    Tries `schedule_gates_greedy_pebble_limited` first (fast,
    deterministic, no Z3). By DEFAULT (`use_sat_fallback=False`), a
    deadlock (`RuntimeError`) is NOT caught -- it propagates straight
    to the caller.

    Pass `use_sat_fallback=True` to restore falling back to the
    Z3-based `pebble_gates` (bidirectional SAT search) on a greedy
    deadlock instead of raising.

    `force_sat=True` skips the greedy attempt entirely and calls
    `pebble_gates` directly.

    `dump_json` (optional path): forwarded to
    `schedule_gates_greedy_pebble_limited` on the greedy attempt (both
    success and genuine-deadlock), AND to `pebble_gates` if the SAT
    fallback or `force_sat` path is used instead -- so a debug JSON is
    always written regardless of which method ultimately scheduled
    the circuit.

    Returns:
        {
          "gate_steps": [...],
          "method": "greedy" or "sat",
          "greedy_error": str or None,
        }
    """
    greedy_error = None

    if not force_sat:
        try:
            start = time.time()
            gate_steps = schedule_gates_greedy_pebble_limited(
                gates, net, max_pebbles, clean_pebbles=clean_pebbles,
                dump_json=dump_json,
            )
            elapsed = time.time() - start
            if verbose:
                print(f"[pebble_gates_greedy_first] greedy scheduling "
                      f"succeeded in {elapsed:.3f}s "
                      f"({len(gate_steps)} gate-level event(s)) -- "
                      f"Z3 was never invoked.")
            return {
                "gate_steps": gate_steps,
                "method": "greedy",
                "greedy_error": None,
            }
        except RuntimeError as e:
            greedy_error = str(e)
            if not use_sat_fallback:
                raise
            if verbose:
                print(f"[pebble_gates_greedy_first] greedy scheduling "
                      f"deadlocked ({greedy_error}); falling back to "
                      f"Z3-based pebble_gates (use_sat_fallback=True)...")

    gate_steps = pebble_gates(
        gates, max_pebbles, net=net, verbose=verbose, dump_json=dump_json,
        **pebble_gates_kwargs
    )
    return {
        "gate_steps": gate_steps,
        "method": "sat",
        "greedy_error": greedy_error,
    }


def pebble_gates_by_toggle_budget(gates, max_pebbles, net, num_steps=None,
                                   start_toggle_budget=None, max_toggle_budget=None,
                                   toggle_budget_step=1, max_escalations=None,
                                   verbose=True, dump_json=None, timeout_s=None):
    """
    Alternative to `pebble_gates`. See module docstring, "TOGGLE-COUNT
    BUDGET SEARCH".
    """
    if num_steps is None:
        num_steps = max(4, 2 * len(gates))
    if start_toggle_budget is None:
        start_toggle_budget = len(gates)
    if max_toggle_budget is None:
        max_toggle_budget = 2 * len(gates) * max(1, num_steps)

    budget = _WallClockBudget(timeout_s)

    def _timeout_bail(partial_log):
        if dump_json:
            dump_pebbling_debug_json(
                dump_json,
                extra={"escalation_log": partial_log, "outcome": "timeout",
                       "elapsed_s": budget.elapsed_s(), "timeout_s": timeout_s},
            )
        raise PebbleTimeoutError(
            f"pebble_gates_by_toggle_budget exceeded its overall "
            f"timeout_s={timeout_s}s after {len(partial_log)} round(s) "
            f"({budget.elapsed_s():.2f}s elapsed)."
        )

    build_start = time.time()
    s = GatePebbleSolver(gates, max_pebbles, net=net,
                          z3_timeout_ms=budget.remaining_ms())
    s.init()
    s.build_all_steps(num_steps)
    build_elapsed = time.time() - build_start
    print(f"[toggle-budget setup] built {num_steps} step(s) once "
          f"({build_elapsed:.2f}s)")

    if budget.expired():
        _timeout_bail([])

    current_budget = start_toggle_budget
    escalations_tried = 0
    escalation_log = []

    while current_budget <= max_toggle_budget:
        if budget.expired():
            _timeout_bail(escalation_log)

        if max_escalations is not None and escalations_tried >= max_escalations:
            if dump_json:
                dump_pebbling_debug_json(
                    dump_json,
                    extra={"escalation_log": escalation_log, "outcome": "max_escalations_reached"},
                )
            raise RuntimeError(
                f"No gate-level pebbling solution found after "
                f"{escalations_tried} toggle-budget round(s) "
                f"(max_escalations={max_escalations} reached)."
            )

        round_index = escalations_tried + 1
        round_start = time.time()

        r = s.solve_with_toggle_budget(current_budget)

        round_elapsed = time.time() - round_start
        escalations_tried += 1

        round_record = {
            "round": round_index,
            "toggle_budget": current_budget,
            "num_steps": num_steps,
            "result": "sat" if r == sat else "unsat",
            "elapsed_s": round_elapsed,
        }
        escalation_log.append(round_record)

        print(f"[round {round_index}] toggle_budget={current_budget} "
              f"(num_steps={num_steps}) -> "
              f"{'SAT' if r == sat else 'UNSAT'} ({round_elapsed:.2f}s)")

        if r == sat:
            s.save_model()
            gate_steps = s.extract(verbose=verbose)

            if dump_json:
                node_steps = expand_gate_schedule(gates, gate_steps) if gates else None
                dump_pebbling_debug_json(
                    dump_json,
                    node_steps=node_steps,
                    gate_groups=gates,
                    gate_steps=gate_steps,
                    extra={"escalation_log": escalation_log, "outcome": "sat"},
                )

            return gate_steps

        print(f"toggle_budget={current_budget} infeasible; increasing to "
              f"{current_budget + toggle_budget_step}... "
              f"(round {escalations_tried}"
              + (f"/{max_escalations}" if max_escalations is not None else "")
              + ")")
        current_budget += toggle_budget_step

    if dump_json:
        dump_pebbling_debug_json(
            dump_json,
            extra={"escalation_log": escalation_log, "outcome": "max_toggle_budget_reached"},
        )
    raise RuntimeError(
        f"No gate-level pebbling solution found for any toggle_budget from "
        f"{start_toggle_budget} up to max_toggle_budget={max_toggle_budget} "
        f"at num_steps={num_steps}."
    )


_APPROXIMATE_RULE_T_COST = {
    1: 4,
    2: 4,
    3: 6,
    4: 7,
    5: 0,
}


def _approximate_t_count(net, gates, gate_steps):
    children = net.build_children()
    node_steps = expand_gate_schedule(gates, gate_steps)
    total = 0
    for node, _action in node_steps:
        if node.is_xor:
            continue
        rule = select_gadget_rule(node, net, children)
        total += _APPROXIMATE_RULE_T_COST.get(rule, 0)
    return total


def find_best_gate_pebbling_by_tcount(gates, max_pebbles, net, t_count_fn=None,
                                       max_steps="auto", verbose=False,
                                       max_escalations=None, timeout_s=None,
                                       dump_json=None):
    """
    Searches for the gate-level pebbling schedule with the LOWEST
    T-count. See module docstring, "T-COUNT-GUIDED PEBBLE SEARCH".
    """
    if t_count_fn is None:
        t_count_fn = _approximate_t_count

    budget = _WallClockBudget(timeout_s)
    escalation_log = []
    escalations_tried = 0
    tried = []

    min_pebbles_floor = max(1, len(net.pos)) if net is not None else 1

    def _attempt(current_pebbles):
        nonlocal escalations_tried
        round_index = escalations_tried + 1
        round_start = time.time()

        step_cap = max_steps
        if step_cap == "auto":
            step_cap = max(4, len(gates) * 4)

        s = GatePebbleSolver(gates, current_pebbles, net=net,
                              z3_timeout_ms=budget.remaining_ms())
        s.init()
        r = s.solve()
        if budget.expired():
            raise _TimedOut()
        while r == unsat and (step_cap is None or s.num_steps < step_cap):
            s.add_step()
            r = s.solve()
            if budget.expired():
                raise _TimedOut()

        round_elapsed = time.time() - round_start
        escalations_tried += 1
        escalation_log.append({
            "round": round_index, "max_pebbles": current_pebbles,
            "steps_reached": s.num_steps, "step_cap": step_cap,
            "result": "sat" if r == sat else "unsat_exhausted",
            "elapsed_s": round_elapsed,
        })
        print(f"[round {round_index}] max_pebbles={current_pebbles} -> "
              f"{'SAT' if r == sat else 'UNSAT'} after {s.num_steps} "
              f"step(s) ({round_elapsed:.2f}s)")

        if r == sat:
            s.save_model()
            return r, s.extract(verbose=verbose)
        return r, None

    def _timeout_bail_no_result():
        if dump_json:
            dump_pebbling_debug_json(
                dump_json,
                extra={"escalation_log": escalation_log, "tried": tried,
                       "outcome": "timeout"},
            )
        raise PebbleTimeoutError(
            f"find_best_gate_pebbling_by_tcount exceeded its overall "
            f"timeout_s={timeout_s}s before finding ANY SAT schedule "
            f"({budget.elapsed_s():.2f}s elapsed)."
        )

    def _finish(best_pebbles, best_steps, best_tcount):
        if dump_json:
            node_steps = expand_gate_schedule(gates, best_steps) if gates else None
            dump_pebbling_debug_json(
                dump_json,
                node_steps=node_steps,
                gate_groups=gates,
                gate_steps=best_steps,
                extra={"escalation_log": escalation_log, "tried": tried,
                       "outcome": "sat", "final_max_pebbles": best_pebbles,
                       "final_t_count": best_tcount},
            )
        return {
            "max_pebbles": best_pebbles,
            "gate_steps": best_steps,
            "t_count": best_tcount,
            "tried": tried,
        }

    if budget.expired():
        _timeout_bail_no_result()

    try:
        r0, gate_steps0 = _attempt(max_pebbles)
    except _TimedOut:
        _timeout_bail_no_result()
        return  # unreachable

    if r0 == unsat:
        tried.append({"max_pebbles": max_pebbles, "result": "unsat", "t_count": None})
        current_pebbles = max_pebbles + 1
        while True:
            if max_escalations is not None and escalations_tried >= max_escalations:
                raise RuntimeError(
                    f"find_best_gate_pebbling_by_tcount: max_escalations="
                    f"{max_escalations} reached while searching upward "
                    f"from max_pebbles={max_pebbles} without finding any "
                    f"SAT schedule."
                )
            try:
                r, steps = _attempt(current_pebbles)
            except _TimedOut:
                _timeout_bail_no_result()
                return  # unreachable

            if r == sat:
                tcount = t_count_fn(net, gates, steps)
                tried.append({"max_pebbles": current_pebbles, "result": "sat",
                               "t_count": tcount})
                print(f"\nmax_pebbles={max_pebbles} was infeasible; minimum "
                      f"feasible budget is {current_pebbles} "
                      f"(t_count={tcount}).")
                return _finish(current_pebbles, steps, tcount)

            tried.append({"max_pebbles": current_pebbles, "result": "unsat", "t_count": None})
            current_pebbles += 1

    best_pebbles = max_pebbles
    best_steps = gate_steps0
    best_tcount = t_count_fn(net, gates, gate_steps0)
    tried.append({"max_pebbles": max_pebbles, "result": "sat", "t_count": best_tcount})
    print(f"[probe] max_pebbles={max_pebbles}: t_count={best_tcount}")

    current_pebbles = max_pebbles - 1
    while current_pebbles >= min_pebbles_floor:
        if max_escalations is not None and escalations_tried >= max_escalations:
            print(f"max_escalations={max_escalations} reached during "
                  f"downward T-count search; stopping with best found so far.")
            break

        try:
            r, steps = _attempt(current_pebbles)
        except _TimedOut:
            print("Wall-clock budget expired during downward T-count "
                  "search; stopping with best found so far.")
            break

        if r != sat:
            tried.append({"max_pebbles": current_pebbles, "result": "unsat", "t_count": None})
            print(f"max_pebbles={current_pebbles} is UNSAT; stopping "
                  f"downward search. Best so far: max_pebbles={best_pebbles}, "
                  f"t_count={best_tcount}.")
            break

        tcount = t_count_fn(net, gates, steps)
        tried.append({"max_pebbles": current_pebbles, "result": "sat", "t_count": tcount})
        print(f"max_pebbles={current_pebbles}: t_count={tcount} "
              f"(previous best: {best_tcount} at max_pebbles={best_pebbles})")

        if tcount > best_tcount:
            print(f"T-count regressed ({tcount} > {best_tcount}); "
                  f"stopping. Returning max_pebbles={best_pebbles} "
                  f"(t_count={best_tcount}).")
            break

        best_pebbles, best_steps, best_tcount = current_pebbles, steps, tcount
        current_pebbles -= 1

    return _finish(best_pebbles, best_steps, best_tcount)


def _solve_gate_steps_at_fixed_steps(gates, max_pebbles, net, num_steps):
    s = GatePebbleSolver(gates, max_pebbles, net=net)
    s.init()
    for _ in range(num_steps):
        s.add_step()
    r = s.solve()
    return r, s


def find_min_gate_steps(gates, max_pebbles, net, start_steps=None, verbose=True):
    """
    Finds the MINIMUM number of steps for which `GatePebbleSolver` is
    SAT at a FIXED `max_pebbles` budget.
    """
    if start_steps is None:
        start_steps = max(4, len(gates) * 4)

    r, s = _solve_gate_steps_at_fixed_steps(gates, max_pebbles, net, start_steps)
    if r != sat:
        raise RuntimeError(
            f"start_steps={start_steps} is not SAT at max_pebbles="
            f"{max_pebbles} -- pick a larger start_steps."
        )

    best_steps = start_steps
    best_solver = s

    current_steps = start_steps - 1
    while current_steps >= 0:
        r, s = _solve_gate_steps_at_fixed_steps(gates, max_pebbles, net, current_steps)
        if r == sat:
            if verbose:
                print(f"  steps={current_steps}: SAT")
            best_steps = current_steps
            best_solver = s
            current_steps -= 1
        else:
            if verbose:
                print(f"  steps={current_steps}: UNSAT -- "
                      f"minimum is {best_steps}")
            break

    best_solver.save_model()
    gate_steps = best_solver.extract(verbose=False)
    return best_steps, gate_steps


def print_gate_pebbling_constraints(gates, max_pebbles, net=None):
    s = GatePebbleSolver(gates, max_pebbles, net=net)
    limited_gates = [g for g in gates if s.counts_toward_limit[g.gid]]
    xor_only_gates = [g for g in gates if not s.counts_toward_limit[g.gid]]

    print(f"\nGatePebbleSolver constraints (max_pebbles={max_pebbles}):")
    print(f"  Total gates: {len(gates)}")
    print(f"  AND-type gates: {len(limited_gates)} -> {[g.gid for g in limited_gates]}")
    print(f"  XOR-only gates (exempt): {len(xor_only_gates)} -> {[g.gid for g in xor_only_gates]}")
    print(f"  Gates required to remain pebbled at end: {sorted(s.po_gate_ids)}")
    print(f"\n  Per-gate dependencies and weights:")
    for g in gates:
        po_flag = " [MUST END PEBBLED]" if g.gid in s.po_gate_ids else ""
        dependents = sorted(s.dependents.get(g.gid, ()))
        cw = s.clean_weight.get(g.gid, 0)
        dw = s.dirty_weight.get(g.gid, 0)
        print(f"    Gate {g.gid}{po_flag}: clean_weight={cw} "
              f"(live-window), dirty_weight={dw} (toggle-only); "
              f"depends on {sorted(g.depends_on)}; dependents: {dependents}")


# ---------------------------------------------------------------------------
# Qubit assignment for gate-level pebbling
# ---------------------------------------------------------------------------

class QubitAllocation:
    def __init__(self):
        self.assignment = {}
        self.clean_free = []
        self.dirty_free = []
        self.total_qubits = 0
        self.timeline = []

    def _new_qubit(self):
        idx = self.total_qubits
        self.total_qubits += 1
        return idx

    def _alloc_clean(self):
        if self.clean_free:
            return self.clean_free.pop()
        return self._new_qubit()

    def _alloc_dirty(self):
        if self.dirty_free:
            return self.dirty_free.pop()
        if self.clean_free:
            return self.clean_free.pop()
        return self._new_qubit()


def _resolve_control_qubits(node, snapshot):
    if node in snapshot:
        return [snapshot[node]]
    if node.is_xor:
        qubits = []
        for fi in node.fanins:
            qubits.extend(_resolve_control_qubits(fi, snapshot))
        return qubits
    return []


def _replay_gate_pebbling(net, gates, gate_steps):
    alloc = QubitAllocation()

    for pi in net.pis:
        q = alloc._new_qubit()
        alloc.assignment[pi] = q
        yield ("pi_init", pi, q)

    gate_by_id = {g.gid: g for g in gates}
    pos_set = set(net.pos)

    for k, gid, op, tag in gate_steps:
        g = gate_by_id[gid]

        if op == "uncompute_gate":
            output_set = set(g.outputs)
            for node in g.nodes:
                if node in pos_set:
                    continue

                q = alloc.assignment.pop(node, None)
                if q is None:
                    continue

                is_output = node in output_set
                if node.is_xor:
                    yield (
                        "free_clean_xor_alias" if is_output else "free_dirty_xor_alias",
                        k, node, q,
                    )
                else:
                    if is_output:
                        alloc.clean_free.append(q)
                        yield ("free_clean", k, node, q)
                    else:
                        alloc.dirty_free.append(q)
                        yield ("free_dirty", k, node, q)
            continue

        dirty_assigned = []
        for node in g.internal:
            if node.is_xor and node.fanins:
                target_fanin = node.fanins[-1]
                resolved = _resolve_control_qubits(target_fanin, alloc.assignment)
                if resolved:
                    q = resolved[-1]
                    alloc.assignment[node] = q
                    yield ("assign_dirty_xor_alias", k, node, q, target_fanin)
                    continue
            q = alloc._alloc_dirty()
            alloc.assignment[node] = q
            dirty_assigned.append(node)
            yield ("assign_dirty", k, node, q)

        for node in g.outputs:
            if node.is_xor and node.fanins:
                target_fanin = node.fanins[-1]
                resolved = _resolve_control_qubits(target_fanin, alloc.assignment)
                if resolved:
                    q = resolved[-1]
                    alloc.assignment[node] = q
                    yield ("assign_clean_xor_alias", k, node, q, target_fanin)
                    continue
            q = alloc._alloc_clean()
            alloc.assignment[node] = q
            yield ("assign_clean", k, node, q)

        yield ("compute_ready", k, g, dict(alloc.assignment))

        for node in dirty_assigned:
            q = alloc.assignment.pop(node, None)
            if q is None:
                continue
            alloc.dirty_free.append(q)
            yield ("free_dirty", k, node, q)


def assign_qubits_to_pebbling(net, gates, gate_steps):
    alloc = QubitAllocation()
    live_qubits = {}

    def _check_and_mark_live(q, node_name):
        assert q not in live_qubits, (
            f"Qubit {q} assigned to '{node_name}' while still live for "
            f"'{live_qubits[q]}'."
        )
        live_qubits[q] = node_name

    def _mark_free(q, node_name):
        assert live_qubits.get(q) == node_name, (
            f"Attempted to free qubit {q} for '{node_name}', but it was "
            f"recorded as live for '{live_qubits.get(q)}' instead."
        )
        del live_qubits[q]

    for event in _replay_gate_pebbling(net, gates, gate_steps):
        kind = event[0]
        if kind == "pi_init":
            _, pi_node, q = event
            alloc.assignment[pi_node] = q
            alloc.total_qubits = max(alloc.total_qubits, q + 1)
            alloc.timeline.append((0, "pi_init", pi_node.name, q))
            _check_and_mark_live(q, pi_node.name)
        elif kind in ("assign_clean", "assign_dirty"):
            _, k, node, q = event
            alloc.total_qubits = max(alloc.total_qubits, q + 1)
            alloc.timeline.append((k, kind, node.name, q))
            _check_and_mark_live(q, node.name)
        elif kind in ("assign_clean_xor_alias", "assign_dirty_xor_alias"):
            _, k, node, q, target_fanin = event
            alloc.total_qubits = max(alloc.total_qubits, q + 1)
            alloc.timeline.append((k, kind, f"{node.name}~={target_fanin.name}", q))
        elif kind == "free_dirty":
            _, k, node, q = event
            alloc.timeline.append((k, "free_dirty", node.name, q))
            _mark_free(q, node.name)
        elif kind == "free_clean":
            _, k, node, q = event
            alloc.timeline.append((k, "free_clean", node.name, q))
            _mark_free(q, node.name)
        elif kind == "free_clean_xor_alias":
            _, k, node, q = event
            alloc.timeline.append((k, "free_clean_xor_alias", node.name, q))
        elif kind == "free_dirty_xor_alias":
            _, k, node, q = event
            alloc.timeline.append((k, "free_dirty_xor_alias", node.name, q))

    pi_names = {pi.name for pi in net.pis}
    remaining = {q: name for q, name in live_qubits.items() if name not in pi_names}
    assert not remaining, f"Non-PI qubits still live at end of schedule: {remaining}."

    return alloc


def print_qubit_allocation(alloc):
    print(f"\nTotal qubits used: {alloc.total_qubits}")
    print("Timeline:")
    for k, action, name, q in alloc.timeline:
        print(f"  step {k}: {action:24s} node={name:16s} qubit={q}")


# ---------------------------------------------------------------------------
# Gate/gadget synthesis (Toffoli/T-gate decomposition per synthesis table)
# ---------------------------------------------------------------------------

class QOp:
    def __init__(self, kind, targets, controls=None):
        self.kind = kind
        self.targets = targets
        self.controls = controls or []

    def __repr__(self):
        if self.controls:
            return f"{self.kind}(controls={self.controls}, targets={self.targets})"
        return f"{self.kind}({self.targets})"


class Gadget:
    def __init__(self, rule, node, ops, description):
        self.rule = rule
        self.node = node
        self.ops = ops
        self.description = description

    def print_ops(self):
        print(f"Node {self.node.name} -> Rule {self.rule}: {self.description}")
        for op in self.ops:
            print(f"    {op}")


def _is_xor_output_po(node, children, pos_set):
    for ch in children.get(node, []):
        if ch.is_xor and ch in pos_set:
            return True
    return False


def _fanin_kinds(node):
    if len(node.fanins) != 2:
        return None
    a, b = node.fanins
    return a.is_pi, b.is_pi


def select_gadget_rule(node, net, children):
    pos_set = set(net.pos)
    fanouts = children.get(node, [])

    if node.is_xor:
        return 5

    kinds = _fanin_kinds(node)
    multiple_fanouts = len(fanouts) > 1
    if multiple_fanouts:
        return 4

    is_po = node in pos_set
    xor_po_fanout = _is_xor_output_po(node, children, pos_set)
    if is_po or xor_po_fanout:
        return 4

    if kinds is not None:
        a_is_pi, b_is_pi = kinds
        if a_is_pi and b_is_pi:
            return 3
        if a_is_pi != b_is_pi:
            return 1
        if not a_is_pi and not b_is_pi:
            return 2

    return 1


def _gate_labels(node):
    a_label = node.fanins[0].name if len(node.fanins) > 0 else "A"
    b_label = node.fanins[1].name if len(node.fanins) > 1 else "B"
    f_label = f"f_{node.name}"
    anc_label = f"a_{node.name}"
    return a_label, b_label, f_label, anc_label


def generate_gate(node, net, children=None):
    if children is None:
        children = net.build_children()

    rule = select_gadget_rule(node, net, children)
    a, b, f, anc = _gate_labels(node)
    ops = []

    if rule == 1:
        ops = [
            QOp("CNOT", targets=[anc], controls=[a]),
            QOp("H", targets=[f]),
            QOp("T", targets=[anc]),
            QOp("CNOT", targets=[f], controls=[anc]),
            QOp("Tdg", targets=[f]),
            QOp("CNOT", targets=[f], controls=[b]),
            QOp("T", targets=[f]),
            QOp("CNOT", targets=[f], controls=[anc]),
            QOp("Tdg", targets=[f]),
            QOp("CNOT", targets=[f], controls=[b]),
            QOp("H", targets=[f]),
        ]
        description = "One fanin PI + one fanin from another node; F intermediate"
    elif rule == 2:
        ops = [
            QOp("CNOT", targets=[anc], controls=[a]),
            QOp("TOFFOLI", targets=[b], controls=[a, "|0>"]),
            QOp("H", targets=[f]),
            QOp("T", targets=[anc]),
            QOp("CNOT", targets=[f], controls=[anc]),
            QOp("Tdg", targets=[f]),
            QOp("CNOT", targets=[f], controls=[b]),
            QOp("T", targets=[f]),
            QOp("CNOT", targets=[f], controls=[anc]),
            QOp("Tdg", targets=[f]),
            QOp("CNOT", targets=[f], controls=[b]),
            QOp("H", targets=[f]),
        ]
        description = "Both A and B are fanins from other nodes"
    elif rule == 3:
        ops = [
            QOp("CNOT", targets=[f], controls=[b]),
            QOp("CNOT", targets=[f], controls=[a]),
            QOp("T", targets=[b]),
            QOp("Tdg", targets=[f]),
            QOp("T", targets=[a]),
            QOp("CNOT", targets=[f], controls=[b]),
            QOp("CNOT", targets=[b], controls=[a]),
            QOp("Tdg", targets=[f]),
            QOp("Tdg", targets=[b]),
            QOp("T", targets=[f]),
            QOp("CNOT", targets=[f], controls=[a]),
            QOp("H", targets=[f]),
        ]
        description = "A and B are primary inputs (standard Toffoli->Clifford+T)"
    elif rule == 4:
        ops = [
            QOp("TOFFOLI", targets=[f], controls=[a, b]),
            QOp("CNOT", targets=[anc], controls=[a]),
            QOp("CNOT", targets=[f], controls=[anc]),
        ]
        description = "F is a primary output / feeds PO XOR, or AND has multiple fanouts"
    elif rule == 5:
        ops = [
            QOp("CNOT", targets=[f], controls=[b]),
            QOp("CNOT", targets=[f], controls=[a]),
        ]
        description = "XOR node: CNOT cascade"
    else:
        raise ValueError(f"Unknown rule {rule} for node {node.name}")

    return Gadget(rule, node, ops, description)


def generate_all_gates(net):
    children = net.build_children()
    gadgets = []
    for n in net.nodes:
        if n.is_pi:
            continue
        gadgets.append(generate_gate(n, net, children))
    return gadgets


def print_all_gadgets(gadgets):
    print("\nGenerated gadgets:")
    for g in gadgets:
        g.print_ops()


# ---------------------------------------------------------------------------
# Qiskit circuit construction + QASM export
# ---------------------------------------------------------------------------

def _collect_qubit_labels(net, gadgets):
    labels = []
    seen = set()

    def add(label):
        if label not in seen:
            seen.add(label)
            labels.append(label)

    for n in net.nodes:
        add(n.name)
    for g in gadgets:
        for op in g.ops:
            for label in op.controls:
                add(label)
            for label in op.targets:
                add(label)
    return labels


def build_qiskit_circuit(net, gadgets=None, measure=False):
    from qiskit import QuantumCircuit, QuantumRegister, ClassicalRegister

    if gadgets is None:
        gadgets = generate_all_gates(net)

    labels = _collect_qubit_labels(net, gadgets)
    qubit_index = {label: i for i, label in enumerate(labels)}

    qreg = QuantumRegister(len(labels), "q")
    if measure:
        creg = ClassicalRegister(len(labels), "c")
        qc = QuantumCircuit(qreg, creg)
    else:
        qc = QuantumCircuit(qreg)

    def q(label):
        return qreg[qubit_index[label]]

    for g in gadgets:
        qc.barrier(label=g.node.name)
        for op in g.ops:
            if op.kind == "CNOT":
                qc.cx(q(op.controls[0]), q(op.targets[0]))
            elif op.kind == "TOFFOLI":
                qc.ccx(q(op.controls[0]), q(op.controls[1]), q(op.targets[0]))
            elif op.kind == "H":
                qc.h(q(op.targets[0]))
            elif op.kind == "T":
                qc.t(q(op.targets[0]))
            elif op.kind == "Tdg":
                qc.tdg(q(op.targets[0]))
            else:
                raise ValueError(f"Unsupported QOp kind: {op.kind}")

    if measure:
        qc.measure(qreg, creg)

    return qc, labels


def dump_qasm(qc, path=None):
    try:
        from qiskit import qasm2
        qasm_text = qasm2.dumps(qc)
    except ImportError:
        qasm_text = qc.qasm()

    if path is not None:
        with open(path, "w") as f:
            f.write(qasm_text)

    return qasm_text


# ---------------------------------------------------------------------------
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    def _parse_max_steps(value):
        v = value.strip().lower()
        if v == "auto":
            return "auto"
        if v == "none":
            return None
        try:
            return int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"invalid --max-steps value: {value!r} (expected 'auto', 'none', or an integer)"
            )

    parser = argparse.ArgumentParser(description="Z3-based reversible pebbling solver demo.")
    parser.add_argument("--max-pebbles", type=int, default=5)
    parser.add_argument("--max-steps", type=_parse_max_steps, default="auto")
    parser.add_argument("--max-escalations", type=int, default=None,
                         help="Cap the number of pebble-budget escalation "
                              "rounds attempted (in EITHER search direction).")
    parser.add_argument("--timeout-s", type=float, default=None,
                         help="Overall wall-clock timeout (seconds) for the "
                              "pebbling search.")
    parser.add_argument("--use-structural-fallback", action="store_true",
                         help="If the Z3-based search (used as a fallback "
                              "after a greedy deadlock, only when "
                              "--sat-fallback is also passed) times out or "
                              "exhausts its escalation budget during an "
                              "UPWARD search, fall back further to "
                              "schedule_gates_structurally instead of "
                              "raising. OFF by default.")
    parser.add_argument("--structural-free-policy", choices=["eager", "lazy"],
                         default="eager",
                         help="Freeing policy used by "
                              "schedule_gates_structurally if the "
                              "structural fallback is triggered.")
    parser.add_argument("--force-sat", action="store_true",
                         help="Skip greedy scheduling entirely and go "
                              "straight to the Z3-based pebble_gates "
                              "search (greedy scheduling is now the "
                              "DEFAULT path -- see "
                              "pebble_gates_greedy_first).")
    parser.add_argument("--sat-fallback", action="store_true",
                         help="If greedy scheduling deadlocks, fall back to "
                              "the Z3-based pebble_gates search instead of "
                              "raising immediately (the new default).")
    parser.add_argument("--clean-pebbles", type=int, default=None,
                         help="Optional narrower cap on sustained CLEAN "
                              "(boundary-output) weight, used by the "
                              "greedy scheduler.")
    parser.add_argument("--no-node-level-clean-release", action="store_true",
                         help="Disable NODE-LEVEL clean weight release in "
                              "the greedy scheduler, restoring the old "
                              "all-or-nothing per-group release behavior. "
                              "See module docstring, 'NODE-LEVEL CLEAN "
                              "WEIGHT RELEASE'.")
    parser.add_argument("--use-toggle-budget", action="store_true",
                         help="Use pebble_gates_by_toggle_budget instead of "
                              "the default greedy-first path.")
    parser.add_argument("--toggle-num-steps", type=int, default=None,
                         help="Fixed number of time steps to build once, "
                              "when --use-toggle-budget is passed.")
    parser.add_argument("--use-tcount-search", action="store_true",
                         help="Use find_best_gate_pebbling_by_tcount instead "
                              "of the default greedy-first path.")
    parser.add_argument("--dump-json", default=None,
                         help="Path to write a pebbling debug JSON file "
                              "(gate groups, gate-level schedule, expanded "
                              "node-level pebbling sequence). Works for "
                              "BOTH the default greedy-first path (success "
                              "and genuine deadlock) and the Z3-based "
                              "paths -- see module docstring, 'GREEDY "
                              "SCHEDULER DEBUG JSON DUMP'.")
    parser.add_argument("--skip-qiskit", action="store_true")
    parser.add_argument("--topo-order", choices=["creation", "dependency_chain", "breadth_first"],
                         default="creation",
                         help="Which topological order build_gate_groups uses "
                              "for its Phase 1 greedy pebble-budget fill. "
                              "'breadth_first' groups fanout siblings together "
                              "-- see module docstring, 'TOPOLOGICAL ORDER "
                              "CHOICE'.")
    parser.add_argument("--no-merge-siblings", action="store_true",
                         help="Disable sibling fanout group merging (Phase "
                              "2.5 of build_gate_groups).")
    parser.add_argument("--merge-pebble-slack", type=float, default=1.5,
                         help="Slack multiplier used when considering a "
                              "sibling fanout merge. Values > 1.0 are "
                              "clamped to never allow exceeding "
                              "max_pebbles itself -- see module docstring, "
                              "'HARD PER-GROUP WEIGHT CEILING'. Default 1.5.")
    parser.add_argument("--no-recombine-small-nonpo", action="store_true",
                         help="Disable Phase 3.5 recombination of small "
                              "adjacent non-PO fragments.")
    parser.add_argument("--recombine-slack", type=float, default=1.0,
                         help="Slack multiplier used when recombining small "
                              "non-PO fragments in Phase 3.5. Values > 1.0 "
                              "are clamped to never allow exceeding "
                              "max_pebbles itself. Default 1.0.")
    parser.add_argument("--debug-group-counts", action="store_true",
                         help="Print group count + size histogram after "
                              "every phase of build_gate_groups.")
    args = parser.parse_args()

    exclusive_flags = sum([args.use_tcount_search, args.use_toggle_budget])
    if exclusive_flags > 1:
        parser.error("--use-tcount-search and --use-toggle-budget are "
                      "mutually exclusive.")
    if args.use_tcount_search and args.use_structural_fallback:
        parser.error("--use-tcount-search does not support "
                      "--use-structural-fallback.")
    if (args.use_tcount_search or args.use_toggle_budget) and (
            args.force_sat or args.sat_fallback or args.clean_pebbles is not None
            or args.no_node_level_clean_release):
        parser.error("--force-sat/--sat-fallback/--clean-pebbles/"
                      "--no-node-level-clean-release only apply to the "
                      "default greedy-first path, not --use-tcount-search "
                      "or --use-toggle-budget.")

    pebble_limit = args.max_pebbles
    max_steps = args.max_steps

    def _try_build_and_dump_qiskit(net, gadgets, qasm_path):
        if args.skip_qiskit:
            print("\n(--skip-qiskit passed; skipping Qiskit circuit/QASM export)")
            return
        try:
            qc, qubit_labels = build_qiskit_circuit(net, gadgets)
        except ImportError:
            print("\n(qiskit is not installed; skipping Qiskit circuit/QASM export.)")
            return
        print(f"\nQubit layout ({len(qubit_labels)} qubits): {qubit_labels}")
        qasm_text = dump_qasm(qc, path=qasm_path)
        print(f"\nWrote QASM to {qasm_path} ({len(qasm_text)} chars)")

    print("=" * 60)
    print("Example: two fixed 4-variable Boolean functions, Reed-Muller "
          "decomposed with cut sharing, topological gate grouping")
    print("=" * 60)

    num_vars = 4
    var_names = ["x0", "x1", "x2", "x3"]

    def _f1(x0, x1, x2, x3):
        return (x0 & x1) ^ (x2 & x3)

    def _f2(x0, x1, x2, x3):
        return (x0 & x1) ^ (x0 & x2) ^ x3

    truth_tables = [
        truth_table_from_function(_f1, num_vars),
        truth_table_from_function(_f2, num_vars),
    ]

    net = build_reed_muller_network(num_vars, truth_tables, var_names=var_names, share_cuts=True)

    print("\nDAG input:")
    net.print_summary()

    steps = pebble(net, pebbles=pebble_limit, max_steps=max_steps)
    groups = group_pebbles(steps, pebble_limit)
    print_groups(groups)

    gadgets = generate_all_gates(net)
    print_all_gadgets(gadgets)

    _try_build_and_dump_qiskit(net, gadgets, "example_circuit.qasm")

    gate_limit = max(pebble_limit, len(net.pos))
    gates = build_gate_groups(
        net,
        max_pebbles=gate_limit,
        topo_order=args.topo_order,
        merge_sibling_fanouts=not args.no_merge_siblings,
        merge_pebble_slack=args.merge_pebble_slack,
        recombine_small_nonpo=not args.no_recombine_small_nonpo,
        recombine_slack=args.recombine_slack,
        debug_print=args.debug_group_counts,
    )
    print_gate_node_groups(gates)
    display_gate_groups(gates)

    print_gate_pebbling_constraints(gates, gate_limit, net=net)
    print_po_lockin_diagnostics(gates, net)
    cycle = find_gate_dependency_cycle(gates)
    print(f"\nGate dependency cycle check: {'CYCLE ' + str(cycle) if cycle else 'none (acyclic, as guaranteed)'}")

    if args.use_toggle_budget:
        gate_steps = pebble_gates_by_toggle_budget(
            gates, max_pebbles=gate_limit, net=net,
            num_steps=args.toggle_num_steps,
            max_escalations=args.max_escalations,
            timeout_s=args.timeout_s,
            dump_json=args.dump_json,
        )
    elif args.use_tcount_search:
        search_result = find_best_gate_pebbling_by_tcount(
            gates, max_pebbles=gate_limit, net=net,
            max_steps=None, verbose=False,
            max_escalations=args.max_escalations,
            timeout_s=args.timeout_s,
            dump_json=args.dump_json,
        )
        print(f"\n[tcount search] chosen max_pebbles={search_result['max_pebbles']} "
              f"t_count(approx)={search_result['t_count']}")
        print(f"[tcount search] attempts tried: {search_result['tried']}")
        gate_steps = search_result["gate_steps"]
    else:
        # GREEDY-FIRST (default): try the fast, solver-free heuristic
        # scheduler first. By default, a deadlock RAISES immediately
        # (use_sat_fallback=False) -- pass --sat-fallback to fall back
        # to the Z3-based pebble_gates search instead, or --force-sat
        # to skip greedy entirely.
        result = pebble_gates_greedy_first(
            gates, max_pebbles=gate_limit, net=net,
            clean_pebbles=args.clean_pebbles,
            use_sat_fallback=args.sat_fallback,
            force_sat=args.force_sat,
            max_steps=None,
            max_escalations=args.max_escalations,
            timeout_s=args.timeout_s,
            use_structural_fallback=args.use_structural_fallback,
            structural_free_policy=args.structural_free_policy,
            dump_json=args.dump_json,
        )
        gate_steps = result["gate_steps"]
        print(f"\n[scheduling] method used: {result['method']}"
              + (f" (greedy deadlock: {result['greedy_error']})"
                 if result["greedy_error"] else ""))

    print("\n" + "=" * 60)
    print("Reconstruction check")
    print("=" * 60)
    reconstructed = reconstruct_original_graph(gates, net.pis, net.pos)
    original_names = [n.name for n in net.nodes]
    reconstructed_names = [n.name for n in reconstructed.nodes]
    print(f"Original node count:      {len(original_names)}")
    print(f"Reconstructed node count: {len(reconstructed_names)}")
    print(f"Node sets match: {set(original_names) == set(reconstructed_names)}")

    node_level_steps = expand_gate_schedule(gates, gate_steps)
    print(f"\nExpanded {len(gate_steps)} gate-level steps into "
          f"{len(node_level_steps)} node-level compute/uncompute events.")

    if not args.skip_qiskit:
        alloc = assign_qubits_to_pebbling(net, gates, gate_steps)
        print_qubit_allocation(alloc)
