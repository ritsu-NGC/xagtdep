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
  - record a node's "dependency" against the WRONG gate (whichever
    fragment happened to be under construction when a path revisited
    an already-owned node),
  - produce NON-CONVEX groups (a group containing nodes on both sides
    of nodes owned by a different group), which can create a cycle in
    the contracted gate-dependency graph EVEN THOUGH the original node
    DAG is fully acyclic.

The NEW approach (`build_gate_groups`, this file) instead:
  1. Assigns EVERY non-PI node to EXACTLY ONE gate group, by walking
     nodes in topological order (which `net.nodes` already is, or an
     alternative valid topological order -- see "TOPOLOGICAL ORDER
     CHOICE" below) and cutting group boundaries only at that point in
     the linear order. A group is therefore always a CONTIGUOUS RANGE
     of the topological order used.
  2. Proves (see accompanying .tex) that a contiguous range of a
     topological order is automatically CONVEX: if u, v are both in
     the range and w lies on some path u -> w -> v, then
     topo(u) < topo(w) < topo(v), and since u, v's topological indices
     already fall inside the range's [lo, hi) bounds, w's index must
     too -- so w is automatically a member of the same group. No
     separate convexity search is required. Crucially, this argument
     only uses the GENERIC topological-order property (for any edge
     u -> w, topo(u) < topo(w)) -- it never assumes any SPECIFIC
     topological order (such as creation order), so ANY valid
     topological order preserves this guarantee.
  3. Computes each group's boundary (inputs/outputs) AFTER ownership is
     fixed, by inspecting the ORIGINAL fanin/fanout edges: a node is an
     "output" (clean pebble) of its group if it is a primary output OR
     has at least one consumer owned by a different group; otherwise it
     is "internal" (dirty pebble).
  4. MERGES sibling fanout groups where beneficial (see "SIBLING
     FANOUT MERGING" below) -- a node with multiple non-XOR consumers
     scattered across different groups can be consolidated into one
     group when doing so stays convex and within budget.
  5. SPLITS any group whose `outputs` mix a genuine primary output (PO)
     with a non-PO output (see "MIXED PO GROUPS" below) -- this step is
     essential and was missing from an earlier revision.
  6. Derives gate-level dependencies strictly from boundary inputs
     (a group's external fanins), never from incidental path traversal.
  7. Validates the result: unique ownership (partition), and acyclicity
     of the induced gate-dependency graph (which is guaranteed by
     construction, but checked defensively).

This guarantees the original graph can be reconstructed exactly from
the union of all group-owned nodes (each retains its original
`fanins`), and that `GatePebbleSolver`'s dependency graph can never
contain a cycle that isn't already present in the original DAG (there
are none, since it's a DAG).

======================================================================
TOPOLOGICAL ORDER CHOICE (`topo_order` parameter on `build_gate_groups`)
======================================================================

Phase 1's greedy pebble-budget fill walks nodes in SOME topological
order and closes a group boundary once a fixed number of "pebble-
consuming" (non-XOR) nodes have accumulated. Which nodes end up
grouped together is therefore entirely a function of WHICH topological
order is used -- not of any semantic notion of "these nodes belong
together". With the network's default creation order, two nodes that
happen to be created back-to-back (e.g. two independent AND gates each
fed directly by primary inputs) can be grouped together purely by
coincidence, even though neither depends on the other, while a node's
own direct dependent ends up in a LATER group simply because it was
created later.

`topo_order` selects which topological order Phase 1 traverses:
  - "creation" (default): use `net.nodes` as-is (today's original
    behavior, unchanged, for full backward compatibility).
  - "dependency_chain": use `_dependency_chain_topo_order(net)`
    instead. This reorders nodes (still a VALID topological order --
    convexity is preserved regardless, per the argument in point 2
    above) using a "list scheduling with successor preference"
    heuristic (also used in compiler instruction scheduling to
    minimize register live-range overlap): after placing a node,
    immediately place one of its own direct consumers next, if that
    consumer just became "ready" (i.e. all of ITS non-PI fanins are
    now placed) as a result. This chains genuinely DEPENDENT nodes
    together in the traversal order, so Phase 1's greedy fill is far
    more likely to group a producer with its actual consumer, instead
    of an unrelated sibling that merely happens to sit nearby in
    creation order.

Neither option changes anything about Phases 2 through 4 (boundary
computation, mixed-PO splitting, dependency derivation, validation) --
they operate identically on whatever groups Phase 1 produces.

NOTE: switching the DEFAULT to "dependency_chain" would change group
membership (and therefore T-counts) for every existing DAG/benchmark
that has already been measured with "creation" order. Re-run any
existing T-count comparisons (e.g. run_caterpillar_experiments.py)
after switching if you want directly comparable numbers.

======================================================================
SIBLING FANOUT MERGING (`_merge_sibling_fanout_groups`)
======================================================================

Rule 6 of the gadget synthesis table (see `select_gadget_rule`, rule
4/"multiple fanouts" case) recognizes that when a single AND node `A`
feeds MULTIPLE downstream consumers (e.g. `B_n = A & something` and
`C_n = A & something_else`), those consumers can share a single copy
of `A`'s ancilla/ result rather than each recomputing or re-fanning-out
`A` independently. This is cheaper in both pebble usage and gate count
when done as ONE group instead of two separate groups that merely
happen to get scheduled in the same time step.

`_merge_sibling_fanout_groups`, run as a new phase between Phase 1
(partition) and Phase 2 (boundary computation), looks for exactly this
shape:
  - a node `A` (owned by some group `G_A`) with 2+ DIRECT non-XOR
    consumers,
  - where those consumers currently belong to DIFFERENT groups
    (`G_B`, `G_C`, ...),
  - and where the consumers' owning groups, together with everything
    between them in topological order, can be merged into ONE group
    without breaking CONVEXITY (a merged group must still be a
    CONTIGUOUS RANGE of the topological order -- see module docstring,
    point 2) and without exceeding a permissive pebble-budget slack
    factor (`merge_pebble_slack`).

This is intentionally CONSERVATIVE: it only merges groups that are
already topologically adjacent (i.e. no other group's nodes lie
strictly between them), since merging non-adjacent groups would force
absorbing every node in between too, which could pull in unrelated
gates and blow the budget for no benefit. In practice, when
`topo_order="dependency_chain"` is used, true sibling fanout consumers
are often already placed back-to-back by that heuristic, making them
adjacency-eligible for this merge pass.

Merging is applied greedily, in increasing order of group id, and is
NOT run to an exhaustive fixed point -- it makes one left-to-right
pass, which is sufficient for the common case of a single shared
ancilla feeding a small, localized cluster of consumers. Like
`_split_mixed_po_groups`, it always recomputes boundaries via
`_compute_boundaries` after any merge, so `outputs`/`internal`/
`inputs` stay consistent with the new ownership.

======================================================================
MIXED PO GROUPS
======================================================================

`GatePebbleSolver` requires any gate that owns at least one TRUE
primary output (a node in `net.pos`) to remain pebbled FOREVER (its
final state must be True). Consequently, that gate NEVER executes an
`uncompute_gate` event.

If a single group's `outputs` list contains BOTH a genuine PO node AND
some other, non-PO output node, then that non-PO output's qubit is
allocated when the group computes but can NEVER be freed -- since the
only place non-PO outputs are freed is the group's `uncompute_gate`
event, and a PO-owning group never uncomputes.

`_split_mixed_po_groups`, run as the phase after sibling-fanout
merging in `build_gate_groups`, detects any group whose `outputs` mix
a genuine PO with a non-PO output, and splits that group at the FIRST
point in topological order where the output type changes (PO ->
non-PO or non-PO -> PO). This is applied repeatedly (a fixed-point
loop) since a single split could, in principle, still leave a "mixed"
situation if a group contained multiple type transitions among its
outputs; the loop terminates because the total node count is fixed and
the group count only ever increases.

IMPORTANT BUG FIX: an earlier version of this function instead cut a
mixed group immediately after its LAST non-PO output, implicitly
assuming every non-PO output precedes every PO output in topological
order within the group. This assumption is FALSE in general -- on
dense, deeply interleaved arithmetic circuits (e.g. EPFL's `hyp.blif`,
a 128-bit integer hypotenuse/sqrt circuit), a group can have a non-PO
output that occurs, in topological order, AFTER its last PO output. In
that case the old "cut after last non-PO index" logic computed a cut
point equal to the group's own final node index, producing an EMPTY
second_part; the `if first_part and second_part` guard then silently
skipped the split entirely (treating it as a no-op), `changed` was
never set, the fixed-point loop believed it had converged, and
`_validate_no_mixed_po_groups` then correctly raised `AssertionError`
at the end of `build_gate_groups`. The fix instead finds the FIRST
index (in topological order) at which consecutive outputs switch
between PO and non-PO, and cuts there. This guarantees the SECOND part
is always non-empty (there is, by definition of "mixed", at least one
more output of the opposite type strictly after the transition point),
so the split always makes genuine progress on every iteration,
regardless of how PO and non-PO outputs happen to be interleaved in
topological order.

======================================================================
QUBIT RECLAMATION POLICY (`_replay_gate_pebbling`)
======================================================================

A group's `outputs` (clean pebbles) may be either:
  (a) a genuine primary output (PO) of the whole network, which must
      remain pebbled/live forever, or
  (b) a node that is merely consumed by a LATER group but is not
      itself a PO.
Case (b) values MUST eventually be freed once their owning gate's
uncompute event fires -- only case (a) (true PO membership, checked
against `net.pos`, NOT merely `group.outputs`) is exempt from being
freed. With the mixed-PO-group fix above, every group's outputs are
now either ALL genuine POs or contain NO genuine PO at all, so a group
with any non-PO output is now guaranteed to actually execute an
`uncompute_gate` event at some point, making case (b) freeing
reachable in practice.

Separately, a group's `internal` (dirty pebble) nodes are, by
definition, values that are NEVER read by anything outside their own
owning gate -- they exist purely as local ancilla for that gate's own
computation. They must therefore be freed EAGERLY, immediately after
their owning gate's compute step finishes, rather than deferred to
that gate's uncompute_gate event, for the same reason: a PO-owning
gate never uncomputes.

======================================================================
COMPOUND XOR CLUSTER DETECTION (`read_blif`)
======================================================================

`_is_xor_cover` only recognizes a genuine XOR/XNOR when a SINGLE
`.names` node's own cover has exactly the 2-row XOR pattern
({"01 1", "10 1"}) or XNOR pattern ({"00 1", "11 1"}). Many real BLIF
benchmarks (e.g. EPFL's `adder.blif`) instead decompose an XOR into
THREE separate `.names` nodes: two single-minterm "product" nodes over
the SAME two signals, combined by a third node via a NOR/OR/XNOR-style
multi-row cover. Each of these 3 nodes, in isolation, fails
`_is_xor_cover`, so all 3 were previously charged as full AND-cost
pebbles by `_pebble_cost`, and routed to AND-type synthesis rules by
`select_gadget_rule` -- even though the whole 3-node cluster is
functionally just one ancilla-free XOR gate.

`_try_mark_compound_xor_cluster`, called from `read_blif` right after
each node is created, detects this specific 3-node shape and
retroactively marks all 3 nodes' `is_xor = True`. This flows through
automatically to `_pebble_cost` (all 3 nodes now cost 0) and
`select_gadget_rule` (all 3 nodes now route to rule 5, ancilla-free
CNOT cascade).

This detector is intentionally NARROW: it only fires on the exact
"two complementary single-minterm products over the same 2 signals,
combined by a 2-row-negated-style combiner" shape.

======================================================================
MIN-STEP DIAGNOSTIC SEARCH (`find_min_gate_steps`)
======================================================================

`pebble_gates`'s own step-escalation loop only ever searches UPWARD
for the first step count at which Z3 becomes SAT at a given
`max_pebbles` -- it never checks whether FEWER steps would also work.
Because `GatePebbleSolver`'s encoding has no cost/objective function
(it only asks "does *some* valid schedule exist," never "what's the
*cheapest* one"), any schedule it returns may contain extra,
functionally unnecessary toggles that are pure unconstrained solver
slack rather than something the pebble budget or dependency structure
actually forces.

`find_min_gate_steps` (and its helper `_solve_gate_steps_at_fixed_
steps`) instead fixes `max_pebbles` and searches DOWNWARD from a
known-SAT step count until UNSAT is hit, to find the TRUE minimum step
count at that budget. This is a diagnostic/debug tool, not part of the
main solving hot path -- it performs a linear (not binary) downward
search, rebuilding a fresh solver at each step count.

======================================================================
PO-GATE MONOTONICITY (`GatePebbleSolver.add_step`)
======================================================================

`GatePebbleSolver` only checks, via `solve()`, that PO-owning gates are
pebbled at the MOST RECENT step examined -- it does not, on its own,
prevent a PO-owning gate from being uncomputed and recomputed at
EARLIER steps. `add_step` adds one explicit constraint per gate in
`po_gate_ids`: `Implies(s_cur, s_nxt)` -- i.e. once a PO-owning gate is
pebbled, it must REMAIN pebbled in every subsequent step. Empirically
confirmed (via `find_min_gate_steps`) to cost NOTHING in achievable
step count for `adder16.blif`.

======================================================================
JUSTIFIED-RECOMPUTE CONSTRAINT
======================================================================

PO-gate monotonicity alone does NOT stop a NON-PO gate from being
recomputed and re-uncomputed arbitrarily many times even after every
gate that ever depended on it has permanently finished needing it.
This was observed concretely in an `adder16.blif` schedule: a gate
whose `outputs` are consumed only by two dependents, both of which
compute once, early, and never again, nonetheless toggled on nearly
every one of the remaining steps, purely because the shared `PbLe`
pebble-budget constraint left room for Z3 to do so and nothing
forbade it.

The general principle missed by PO-monotonicity alone: a gate should
never be RECOMPUTED (transition False -> True after having already
been used and uncomputed at least once before) unless doing so is
actually justified by some real DEPENDENT of that gate itself
transitioning to True (i.e. becoming newly pebbled) at that same step.
If none of a gate's dependents are becoming newly pebbled at a given
step, there is no reason for that gate to come back to life.

Implementation (`GatePebbleSolver`):
  - `self.dependents[gid]`: the REVERSE of `depends_on`.
  - `ever_computed` state: one extra monotonic boolean per gate.
  - A recompute is only permitted if justified by some dependent
    itself newly pebbling at the same step; if a gate has no
    dependents at all, recompute is forbidden outright.

PO-owning gates are exempt from this (they already have full
monotonicity -- they never uncompute in the first place, so a
"recompute" can never occur for them).

======================================================================
ESCALATION ROUND LIMIT AND PER-ROUND RUNTIME MARKERS (`pebble_gates`)
======================================================================

`pebble_gates`'s escalation loop previously had only ONE stopping
condition besides success: `max_pebbles_cap`, which bounds how HIGH
the pebble budget is allowed to climb. This does not bound how much
WORK is done before giving up, because a SINGLE round at a given
pebble budget can itself run for a very long time -- the incremental
step-search inside one round climbs `num_steps` from 0 up to
`step_cap` one Z3 `add_step()` call at a time before that round gives
up and escalates. On a large, densely interleaved circuit (e.g. EPFL's
`hyp.blif`), a single round was observed to run for 2000+ incremental
steps before giving up.

Two independent additions address this:

  1. `max_escalations` (optional): caps the number of ESCALATION
     ROUNDS attempted, independent of `max_pebbles_cap`. If the limit
     is reached without finding a SAT schedule, `pebble_gates` raises
     a `RuntimeError`. If omitted (None, the default), no round limit
     is imposed.

  2. Per-round runtime markers: every escalation round's wall-clock
     duration, final step count reached, and SAT/UNSAT-exhausted
     outcome are recorded into an `escalation_log` (a list of dicts,
     one per round attempted) and printed to the console as each round
     completes. This log is also written into the debug JSON payload
     (under the key "escalation_log") via `dump_pebbling_debug_json`'s
     `extra` parameter whenever `dump_json` is provided -- including
     on failure paths (max_escalations reached, max_pebbles_cap
     reached, etc.), not just on success.

======================================================================
TOGGLE-COUNT BUDGET SEARCH (`pebble_gates_by_toggle_budget`)
======================================================================

`pebble_gates`'s step-count escalation is fundamentally expensive on
large, gate-dense circuits because EVERY escalation round rebuilds a
fresh `GatePebbleSolver` from scratch, and WITHIN a round, every
`add_step()` call re-issues a brand-new `PbLe` cardinality constraint
over all AND-type gates for that step alone. Since steps are never
discarded, the total problem size at step k is O(k * gates) -- by the
time a round gives up at (e.g.) 2212 steps, Z3 is carrying thousands
of independent copies of a cardinality constraint each ranging over
thousands of gate variables, which is the dominant cost driver on
circuits like EPFL's `hyp.blif`.

`pebble_gates_by_toggle_budget` restructures the search entirely:

  1. The full per-step structure (move clauses, PO monotonicity,
     justified-recompute, `ever_computed` tracking) is built via
     `GatePebbleSolver.build_all_steps(num_steps)` EXACTLY ONCE, using
     a single fixed, generous `num_steps` upper bound on the number of
     discrete time slots available (NOT a hard requirement that every
     slot be used -- multiple gates may toggle within the same slot,
     or none at all).

  2. Instead of escalating the NUMBER OF STEPS, search escalates a
     GLOBAL BUDGET on the TOTAL NUMBER OF COMPUTE/UNCOMPUTE (toggle)
     EVENTS allowed across the ENTIRE schedule -- summed over every
     gate and every step already built -- via
     `GatePebbleSolver.solve_with_toggle_budget(toggle_budget)`. This
     adds exactly ONE new `PbLe` constraint per round (over the
     already-built per-step activity variables `a_{gid,i}`), wrapped
     in a single `push()`/`pop()` pair, instead of rebuilding anything.

  3. This reframes the search axis from "how many time steps are
     needed" (a proxy that forces rebuilding expensive per-step
     structure to explore) to "how much total compute/uncompute work
     is needed" (a DIRECT cost measure that can be explored cheaply
     via incremental push/pop on an otherwise-static solver). Z3 can
     also reuse learned clauses across pushes within the same
     incremental context, which the old per-round full-rebuild
     approach could never benefit from.

CAVEAT: `num_steps` is still a fixed upper bound chosen up front. If
it is too small, NO toggle budget will ever be satisfiable (there
simply aren't enough time slots available for the required
dependency-respecting schedule to fit), and the search will exhaust
`max_toggle_budget` without ever finding SAT -- in that case,
`num_steps` itself needs to be increased, not the toggle budget. This
function does not currently auto-escalate `num_steps`; see
`find_min_gate_steps` for a separate utility that explores step-count
minimality directly, and consider widening `num_steps` manually (e.g.
based on the gate-dependency graph's longest path) if toggle-budget
escalation alone cannot find a schedule.

Everything else in this file (node-level Z3 pebbling, Reed-Muller/ESOP
network generators, gate-level Z3 pebbling, gadget synthesis,
Qiskit/QASM export) is unchanged in spirit from prior revisions.
"""

import random
import time
from collections import defaultdict

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
    """Simple DAG container: PIs + gates (non-PI nodes) + POs.

    `self.nodes` is maintained in TOPOLOGICAL ORDER (PIs first, then
    every gate in creation order, which is guaranteed topological since
    a gate's fanins must already exist -- and thus already appear
    earlier in `self.nodes` -- at the time the gate is created).
    """

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
    """
    Generates a random DAG. POs are set to EVERY "sink" node (a node
    never used as a fanin by any other node), so the PO count is
    data-dependent -- size `max_pebbles`/`pebbles` relative to
    `len(net.pos)` after generation.
    """
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
    """
    Random ESOP-style DAG: all internal gates are AND-type product
    terms; XOR gates appear ONLY directly beneath each of `num_outputs`
    fixed primary outputs, combining that output's product terms.
    Guarantees `len(net.pos) == num_outputs` exactly.
    """
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
    """
    If `rows` is a SINGLE-row cover asserting exactly one minterm over
    `fanin_order` (e.g. "10 1"), returns that minterm's bits remapped
    into `shared_order`'s indexing (a tuple of '0'/'1' chars). Returns
    None if `rows` isn't a single positive minterm row, contains a
    don't-care ('-'), or its fanins don't match `shared_order`'s node
    set exactly.
    """
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
    """
    Detects the 3-node XOR/XNOR decomposition pattern used by some BLIF
    benchmarks (e.g. EPFL's `adder.blif`) and marks all 3 nodes'
    `is_xor = True` if the shape matches. See module docstring
    ("COMPOUND XOR CLUSTER DETECTION").

    Returns True if a cluster was detected and marked, else False.
    """
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
        return False  # must both be products over the SAME 2 signals

    shared_order = list(p1_fanins)
    m1 = _minterm_bits(p1_fanins, p1_rows, shared_order)
    m2 = _minterm_bits(p2_fanins, p2_rows, shared_order)
    if m1 is None or m2 is None or m1 == m2:
        return False

    # A genuine XOR/XNOR minterm pair must be COMPLEMENTARY -- differ
    # in BOTH bit positions (e.g. "10" & "01", or "00" & "11").
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
    node_cover_info = {}   # Node -> (fanin_nodes, cover_rows)
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
    exclusively by this group (no other group may own any of them).

    After boundary computation:
      - `outputs` (== `clean_pebble`): nodes in this group that are
        either a primary output, or have at least one consumer owned
        by a DIFFERENT group. These are the values that must remain
        live (pebbled) after this gate's compute step.
      - `internal` (== `dirty_pebble`): nodes in this group whose every
        consumer is also owned by this same group (or has no
        consumer at all and isn't a PO). These are freed EAGERLY,
        right after this gate's own compute step (see
        `_replay_gate_pebbling`).
      - `inputs`: nodes NOT owned by this group (a PI, or a node owned
        by a different group) that at least one node in this group
        directly consumes as a fanin. This is the group's true external
        dependency surface.
      - `depends_on`: the set of OTHER gate ids that produce at least
        one of this group's `inputs`.

    INVARIANT (established by `_split_mixed_po_groups`): `outputs`
    never simultaneously contains a genuine primary output (member of
    `net.pos`) AND a non-PO output. Either every output in this group
    is a true PO, or none of them are. This is essential: a group
    owning a PO never uncomputes (its pebble state must stay True
    forever), so any non-PO output stranded in the same group would
    never be freed.
    """

    def __init__(self, gid):
        self.gid = gid
        self.nodes = []          # all nodes owned by this group, in topo order
        self.outputs = []        # == clean_pebble
        self.internal = []       # == dirty_pebble
        self.inputs = []         # external fanin nodes (PI or other-group)
        self.depends_on = set()  # other gate ids this gate depends on

    # Backward-compatible aliases used by GatePebbleSolver/printers.
    @property
    def clean_pebble(self):
        return self.outputs

    @property
    def dirty_pebble(self):
        return self.internal

    @property
    def xor_nodes(self):
        # No longer a separate bucket -- XOR nodes are just ordinary
        # members of `nodes`, classified into outputs/internal like any
        # other node. Kept as an empty list for old call sites that
        # still reference it (e.g. display/print helpers).
        return []

    @property
    def dependencies(self):
        # Backward-compat: node-level view of which owned nodes have an
        # external fanin (for display purposes only; the gate-id-level
        # dependency set is `depends_on`, computed directly).
        owned = set(self.nodes)
        return [n for n in self.nodes if any(fi not in owned for fi in n.fanins)]

    def all_nodes(self):
        return list(self.nodes)

    def __repr__(self):
        return f"GateGroup(id={self.gid}, nodes={[n.name for n in self.nodes]})"


def _compute_boundaries(net: PebblingNetwork, groups, pos_set, children=None):
    """
    Populates `inputs`/`outputs`/`internal` for every group in `groups`,
    based purely on each owned node's ORIGINAL fanin/fanout edges
    against the CURRENT ownership assignment. Can be called repeatedly
    (e.g. after `_merge_sibling_fanout_groups` or
    `_split_mixed_po_groups` change ownership) to recompute boundaries
    from scratch.
    """
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
    """Rebuilds a fresh list of `GateGroup` objects (gid 0..n-1) from
    a list of owned-node lists, preserving relative order."""
    groups = []
    for i, nodes in enumerate(node_lists):
        g = GateGroup(i)
        g.nodes = nodes
        groups.append(g)
    return groups


def _merge_sibling_fanout_groups(net: PebblingNetwork, groups, pos_set,
                                  max_pebbles, merge_pebble_slack=1.5):
    """
    Merges topologically ADJACENT sibling groups that both consume the
    same shared non-XOR node, so a shared ancilla/result (rule 6 of the
    gadget synthesis table -- "A is an AND node that has multiple
    fanouts") is computed and held ONCE by a single group, instead of
    being spread across separate groups that only coincidentally get
    scheduled together. See module docstring ("SIBLING FANOUT
    MERGING") for the full rationale.

    Algorithm (single left-to-right greedy pass over the CURRENT group
    list, in gid order):
      For each node `A` with 2+ direct non-XOR consumers:
        - Find the set of groups owning those consumers.
        - If that set has more than one group AND those groups are
          CONTIGUOUS in the current group list (i.e. their combined
          node ranges, plus any groups fully in between, form one
          unbroken run with no other group interleaved) -- merge all
          groups in that contiguous run into one.
        - The merge is only accepted if the merged group's Phase-1
          pebble cost (sum of `_pebble_cost` over its nodes) does not
          exceed `max_pebbles * merge_pebble_slack`.

    This is a single greedy pass, not run to a fixed point.

    Returns a new list of `GateGroup` objects (freshly renumbered) with
    boundaries recomputed via `_compute_boundaries`.
    """
    children = net.build_children()
    _compute_boundaries(net, groups, pos_set, children=children)

    node_owner_gid = {}
    for g in groups:
        for n in g.nodes:
            node_owner_gid[n] = g.gid

    # Map each gid to its index in the CURRENT group list, so we can
    # detect contiguity purely by index adjacency.
    gid_to_index = {g.gid: i for i, g in enumerate(groups)}

    merged_index_runs = []   # list of (start_idx, end_idx) index ranges to merge
    already_planned = set()  # indices already claimed by a planned merge run

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
            continue  # already all in one group

        consumer_indices = sorted(gid_to_index[gid] for gid in consumer_gids)
        lo, hi = consumer_indices[0], consumer_indices[-1]
        run_indices = list(range(lo, hi + 1))

        # Reject if this run overlaps a run already planned (keep it
        # simple: first-come-first-served in node topological order).
        if already_planned.intersection(run_indices):
            continue

        # Compute the merged group's Phase-1 pebble cost to check the
        # slack budget before committing to the merge.
        merged_nodes = []
        for idx in run_indices:
            merged_nodes.extend(groups[idx].nodes)
        merged_cost = sum(_pebble_cost(n) for n in merged_nodes)

        if merged_cost > max_pebbles * merge_pebble_slack:
            continue  # merge would blow the (slackened) pebble budget

        if len(run_indices) > 1:
            merged_index_runs.append((lo, hi))
            already_planned.update(run_indices)

    if not merged_index_runs:
        return groups

    merged_index_runs.sort()

    # Rebuild the group list, replacing each planned run with a single
    # merged group, and leaving all other groups untouched.
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
    """
    Repeatedly splits any group whose `outputs` mix a genuine primary
    output with a non-PO output, so that no group ever ends up in a
    state where a non-PO output can never be freed (because its
    owning group also owns a PO and therefore never uncomputes). See
    module docstring ("MIXED PO GROUPS") for the full rationale.

    Splitting is done by finding the FIRST point in topological order
    at which the output TYPE changes (PO -> non-PO or non-PO -> PO)
    among the group's own outputs, and cutting the group's owned-node
    list immediately after that point. This guarantees the second part
    is always non-empty.

    Runs to a fixed point: since each split strictly increases the
    number of groups while the total node count is fixed, this loop
    is guaranteed to terminate.
    """
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


def _pebble_cost(node):
    """
    Returns this node's Phase-1 contribution to the gate-group pebble
    budget.

    XOR nodes are hard-excluded (cost 0) because their synthesis rule is
    ancilla-free. Future cost-weighted grouping should change non-XOR
    costs here without ever letting XOR nodes contribute via a fallback.
    """
    return 0 if node.is_xor else 1


def _dependency_chain_topo_order(net: PebblingNetwork):
    """
    Computes an alternative valid topological order for `net.nodes`
    that greedily prefers scheduling a node's DIRECT CONSUMER
    immediately after it, whenever that consumer's other fanins are
    already placed too ("ready") -- rather than the network's original
    creation order.

    CORRECTNESS NOTE: this does not weaken any guarantee in
    `build_gate_groups`. The convexity proof (see module docstring) only
    uses the generic topological-order property -- for any edge
    u -> w, topo(u) < topo(w) -- and never anything specific to
    creation order. Any valid topological order preserves convexity of
    contiguous ranges. This function only changes WHICH nodes end up
    adjacent, and therefore which nodes Phase 1's greedy pebble-budget
    fill groups together.

    Falls back to the earliest-ready node by creation order (stable,
    deterministic) whenever no direct consumer of the just-placed node
    is yet ready.
    """
    children = net.build_children()
    creation_index = {n: i for i, n in enumerate(net.nodes)}

    # IMPORTANT: count only NON-PI fanins here. PIs are already
    # considered "placed" from the very start (see `placed` below) and
    # never go through the decrement step in the main loop (only nodes
    # actually popped from `ready` decrement their children's
    # counters). If we counted PI fanins too, a node whose fanins are
    # ALL PIs (e.g. a 2-input AND fed directly by two primary inputs)
    # would never have its counter reach 0, `ready` would start empty,
    # and the whole traversal would silently produce zero non-PI
    # nodes.
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


def build_gate_groups(net: PebblingNetwork, max_pebbles: int, topo_order="dependency_chain",
                       merge_sibling_fanouts=True, merge_pebble_slack=1.5):
    """
    Gate-group construction algorithm. See module docstring for the
    full rationale. Steps:

      1. Partition (contiguous topological-range fill, budget-limited)
      2. Boundary computation (outputs/internal/inputs)
      2.5. Sibling fanout merging (adjacency + slack-bounded)
      3. Mixed-PO group splitting (fixed point)
      4. Dependency derivation from boundary inputs
      5. Validation (partition uniqueness, acyclicity)

    Raises `ValueError` if `max_pebbles < len(net.pos)`, or if
    `topo_order` is not one of the recognized values.
    """
    if max_pebbles < len(net.pos):
        raise ValueError(
            f"max_pebbles={max_pebbles} is less than the number of primary "
            f"outputs ({len(net.pos)}). All POs must remain pebbled "
            f"simultaneously at the end, so max_pebbles must be >= "
            f"len(net.pos)."
        )

    pos_set = set(net.pos)

    if topo_order == "creation":
        traversal_order = net.nodes
    elif topo_order == "dependency_chain":
        traversal_order = _dependency_chain_topo_order(net)
    else:
        raise ValueError(
            f"Unknown topo_order: {topo_order!r} "
            f"(expected 'creation' or 'dependency_chain')"
        )

    # --- Phase 1: partition -------------------------------------------------
    groups = []
    current = GateGroup(0)
    cur_pebble = 0

    def finalize():
        nonlocal current
        if current.nodes:
            groups.append(current)
            current = GateGroup(len(groups))

    for node in traversal_order:
        if node.is_pi:
            continue

        current.nodes.append(node)
        node_cost = _pebble_cost(node)

        if node in pos_set:
            finalize()
            cur_pebble = 0
            continue

        cur_pebble += node_cost
        if node_cost == 0:
            continue

        if max_pebbles <= 1:
            # max_pebbles == 1 (only ever valid if len(net.pos) <= 1):
            # every non-PI, non-XOR node must be its own group.
            finalize()
            cur_pebble = 0
        elif cur_pebble >= max_pebbles - 1:
            finalize()
            cur_pebble = 0

    finalize()

    # --- Phase 2: boundary computation ---------------------------------------
    _compute_boundaries(net, groups, pos_set)

    # --- Phase 2.5: sibling fanout merging ------------------------------------
    if merge_sibling_fanouts:
        groups = _merge_sibling_fanout_groups(
            net, groups, pos_set, max_pebbles, merge_pebble_slack=merge_pebble_slack
        )

    # --- Phase 3: split mixed-PO groups ---------------------------------------
    groups = _split_mixed_po_groups(net, groups, pos_set)

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
    cycle = find_gate_dependency_cycle(groups)
    if cycle is not None:
        raise AssertionError(
            f"Internal error: gate-dependency cycle {cycle} detected despite "
            f"contiguous-topological-range construction, which should be "
            f"convex by construction. Please report this as a bug."
        )

    return groups


def _validate_gate_partition(net: PebblingNetwork, groups):
    """
    Confirms every non-PI node in `net` is owned by EXACTLY ONE group.
    Raises `ValueError` on violation (duplicate ownership or missing
    node).
    """
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
    """
    Defensive check: confirms `_split_mixed_po_groups` actually
    achieved its invariant -- no group's `outputs` mix a genuine PO
    with a non-PO output. Raises `AssertionError` if violated (would
    indicate a bug in the splitting logic).
    """
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


def find_gate_dependency_cycle(groups):
    """
    Static (non-Z3) DFS cycle check over `group.depends_on`. Returns a
    list of gate ids forming a cycle, or None if acyclic.
    """
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
    """
    Reconstruction utility: expands a solved gate-level schedule (list
    of (k, gid, op, tag) from `GatePebbleSolver.extract`) back into a
    node-level compute/uncompute sequence, using each gate's owned
    `nodes` list (in topological order).
    """
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
    """
    Reconstruction utility: since every non-PI node is owned by exactly
    one gate group, and every `Node` retains its ORIGINAL `fanins`, the
    full original graph (nodes + edges) can be recovered simply as the
    union of all group-owned nodes plus the original PI list, in
    topological order.
    """
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

class GatePebbleSolver:
    """
    Pebbles whole GateGroup objects. A gate can toggle only if every
    OTHER gate it depends on (`gate.depends_on`, derived from boundary
    inputs -- see `build_gate_groups`) is pebbled both before and after
    the transition.

    XOR-only gates (every owned node is an XOR node) are exempt from
    the simultaneous-pebble cap, since their gadget needs no ancilla.

    Gates that own at least one TRUE primary output (checked against
    `net.pos`) MUST remain pebbled at the final step, AND (see module
    docstring, "PO-GATE MONOTONICITY") are constrained to NEVER
    uncompute at any earlier step either, once pebbled.

    NON-PO gates are subject to a separate, more general constraint
    (see module docstring, "JUSTIFIED-RECOMPUTE CONSTRAINT"): once a
    non-PO gate has been computed and later uncomputed, it may only be
    RECOMPUTED again if doing so is justified by one of its actual
    dependents (gates that list it in their own `depends_on`)
    transitioning to True (newly pebbling) at that same step. A gate
    with no dependents at all may be computed once and uncomputed once,
    but never recomputed.
    """

    def __init__(self, gates, max_pebbles, net=None):
        self.gates = gates
        self.max_pebbles = max_pebbles
        self.slv = Solver()
        self.num_steps = 0
        self.current = {}       # gid -> (s, a)
        self.ever = {}          # gid -> ever_computed Bool
        self.history = []
        self.model = None

        def _is_xor_only(g):
            return len(g.nodes) > 0 and all(n.is_xor for n in g.nodes)

        # This is a GROUP-level flag used by the PbLe constraint over
        # whole-group state bits (`s_next[g.gid]`), not a per-node weight.
        # Mixed groups still count toward the limit because their AND
        # nodes do need ancilla; only groups composed entirely of XOR
        # nodes are exempt.
        self.counts_toward_limit = {g.gid: not _is_xor_only(g) for g in gates}
        self.gate_deps = {g.gid: set(g.depends_on) for g in gates}

        # Reverse of depends_on: dependents[gid] = gate ids that
        # depend ON gid (i.e. gid appears in their own depends_on).
        # Used by the justified-recompute constraint below.
        self.dependents = defaultdict(set)
        for g in gates:
            for dep_gid in g.depends_on:
                self.dependents[dep_gid].add(g.gid)

        pos_set = set(net.pos) if net is not None else set()
        self.po_gate_ids = {
            g.gid for g in gates if any(n in pos_set for n in g.outputs)
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

            # ever_computed is monotonic: once True, stays True. It
            # becomes True as soon as this gate is pebbled at all.
            self.slv.add(ever_nxt == Or(ever_cur, s_nxt))

            if gid in self.po_gate_ids:
                # PO-GATE MONOTONICITY: once pebbled, a PO-owning gate
                # may never be uncomputed again. Empirically confirmed
                # (via find_min_gate_steps) to cost nothing in
                # achievable step count for adder16.blif.
                self.slv.add(Implies(s_cur, s_nxt))
            else:
                # JUSTIFIED-RECOMPUTE CONSTRAINT (see module docstring):
                # a RECOMPUTE (transition False -> True, AFTER this
                # gate has already been used at least once before) is
                # only allowed if some actual dependent of this gate is
                # ITSELF newly pebbling (False -> True) at this same
                # step. If this gate has no dependents at all, the
                # recompute is simply forbidden outright (it may be
                # computed once and uncomputed once, but never again).
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
            self.slv.add(PbLe([(s_next[g.gid], 1) for g in limited_gates], self.max_pebbles))

        self.current = {gid: (s_next[gid], a_next[gid]) for gid in s_next}
        self.ever = dict(ever_next)
        self.history.append(dict(self.current))

    def build_all_steps(self, num_steps):
        """
        Convenience: calls add_step() exactly num_steps times up front,
        building the FULL step structure (move clauses, PO monotonicity,
        justified-recompute) ONCE. Used by the toggle-budget search
        (`pebble_gates_by_toggle_budget`) instead of incrementally
        growing steps per escalation round -- see module docstring,
        "TOGGLE-COUNT BUDGET SEARCH".
        """
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
        """
        Checks satisfiability with an ADDITIONAL global constraint: the
        TOTAL number of compute/uncompute events (activity variables
        `a_{gid,i}` summed across every gate and every step already
        built via `build_all_steps`) must not exceed `toggle_budget`.
        This replaces the step-count-based search: the step structure
        itself is already fixed, so this method only adds ONE new
        cardinality constraint via push/pop, instead of rebuilding or
        growing anything. Much cheaper per attempt than the old
        per-round full solver rebuild. See module docstring,
        "TOGGLE-COUNT BUDGET SEARCH".
        """
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
    """Serializes a node-level (node, action) step list to plain dicts."""
    return [{"node": n.name, "action": action} for n, action in steps]


def _gate_groups_to_json(gates):
    """Serializes GateGroup objects to plain dicts for JSON dumping."""
    out = []
    for g in gates:
        out.append({
            "gid": g.gid,
            "nodes": [n.name for n in g.nodes],
            "outputs": [n.name for n in g.outputs],
            "internal": [n.name for n in g.internal],
            "inputs": [n.name for n in g.inputs],
            "depends_on": sorted(g.depends_on),
        })
    return out


def dump_pebbling_debug_json(path, node_steps=None, gate_groups=None,
                              gate_steps=None, extra=None):
    """
    Writes a single JSON file containing the node-level pebbling
    sequence and/or gate groups and/or gate-level schedule, for
    external inspection. Purely a debug/dev toggle -- does not affect
    solving behavior.

    `extra`: optional dict merged directly into the top-level JSON
    payload. Used by `pebble_gates` / `pebble_gates_by_toggle_budget`
    to attach `escalation_log` (see module docstring, "ESCALATION
    ROUND LIMIT AND PER-ROUND RUNTIME MARKERS" /
    "TOGGLE-COUNT BUDGET SEARCH").
    """
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


def pebble_gates(gates, max_pebbles, max_steps="auto", verbose=True,
                  auto_increase_pebbles=True, max_pebbles_cap=None, net=None,
                  dump_json=None, max_escalations=None):
    """
    Solves for a valid gate-level pebbling schedule with an escalating
    max_pebbles search. See module docstring and `GatePebbleSolver` for
    the full solving semantics.

    max_escalations: optional hard cap on the number of ESCALATION
    ROUNDS attempted (i.e. distinct `current_pebbles` values tried),
    independent of `max_pebbles_cap`. See module docstring, "ESCALATION
    ROUND LIMIT AND PER-ROUND RUNTIME MARKERS", for the full rationale.
    If omitted (None), no round limit is imposed.

    Every escalation round's wall-clock duration, final step count
    reached, and outcome are printed to the console as the round
    completes, and recorded into a returned/dumped `escalation_log`.

    dump_json: optional path. If set, as soon as a solution is found
    (or the search fails for any reason), writes the solved gate-level
    schedule (if any), gate groups (if any), expanded node-level
    pebbling sequence (if any), AND the full `escalation_log` to this
    path as JSON via `dump_pebbling_debug_json`. Purely a debug/dev
    toggle -- does not affect solving behavior or the returned value.

    NOTE: for large, gate-dense circuits where a single round of this
    step-count-based search can itself run for a very long time (e.g.
    EPFL's `hyp.blif`), consider `pebble_gates_by_toggle_budget`
    instead -- see module docstring, "TOGGLE-COUNT BUDGET SEARCH".
    """
    if max_pebbles_cap is None:
        max_pebbles_cap = max(1, len(gates))
    max_pebbles_cap = max(max_pebbles_cap, max_pebbles)

    current_pebbles = max_pebbles
    escalations_tried = 0
    escalation_log = []

    while current_pebbles <= max_pebbles_cap:
        if max_escalations is not None and escalations_tried >= max_escalations:
            if dump_json:
                dump_pebbling_debug_json(
                    dump_json,
                    extra={"escalation_log": escalation_log, "outcome": "max_escalations_reached"},
                )
            raise RuntimeError(
                f"No gate-level pebbling solution found after "
                f"{escalations_tried} escalation round(s) (max_escalations="
                f"{max_escalations} reached), starting from max_pebbles="
                f"{max_pebbles} up to current_pebbles={current_pebbles - 1}. "
                f"Pass a larger max_escalations, a larger max_pebbles "
                f"starting point, or a smaller max_steps to fail faster "
                f"per round."
            )

        round_index = escalations_tried + 1
        round_start = time.time()

        step_cap = max_steps
        if step_cap == "auto":
            step_cap = max(4, len(gates) * 4)

        s = GatePebbleSolver(gates, current_pebbles, net=net)
        s.init()
        r = s.solve()
        while r == unsat and (step_cap is None or s.num_steps < step_cap):
            s.add_step()
            r = s.solve()

        round_elapsed = time.time() - round_start
        escalations_tried += 1

        round_record = {
            "round": round_index,
            "max_pebbles": current_pebbles,
            "steps_reached": s.num_steps,
            "step_cap": step_cap,
            "result": "sat" if r == sat else "unsat_exhausted",
            "elapsed_s": round_elapsed,
        }
        escalation_log.append(round_record)

        print(f"[round {round_index}] max_pebbles={current_pebbles} -> "
              f"{'SAT' if r == sat else 'UNSAT'} after {s.num_steps} step(s) "
              f"({round_elapsed:.2f}s)")

        if r == sat:
            s.save_model()
            if current_pebbles != max_pebbles:
                print(f"\nRequested max_pebbles={max_pebbles} was "
                      f"infeasible; escalated to {current_pebbles} "
                      f"(after {escalations_tried} round(s)).")
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

        if not auto_increase_pebbles:
            if dump_json:
                dump_pebbling_debug_json(
                    dump_json,
                    extra={"escalation_log": escalation_log, "outcome": "auto_increase_disabled"},
                )
            raise RuntimeError(
                f"No gate-level pebbling solution found with max_pebbles="
                f"{current_pebbles} within {step_cap} steps."
            )

        print(f"max_pebbles={current_pebbles} infeasible within "
              f"{step_cap} steps; increasing to {current_pebbles + 1}... "
              f"(escalation round {escalations_tried}"
              + (f"/{max_escalations}" if max_escalations is not None else "")
              + ")")
        current_pebbles += 1

    if dump_json:
        dump_pebbling_debug_json(
            dump_json,
            extra={"escalation_log": escalation_log, "outcome": "max_pebbles_cap_reached"},
        )
    raise RuntimeError(
        f"No gate-level pebbling solution found for any max_pebbles from "
        f"{max_pebbles} up to max_pebbles_cap={max_pebbles_cap}."
    )


def pebble_gates_by_toggle_budget(gates, max_pebbles, net, num_steps=None,
                                   start_toggle_budget=None, max_toggle_budget=None,
                                   toggle_budget_step=1, max_escalations=None,
                                   verbose=True, dump_json=None):
    """
    Alternative to `pebble_gates`. See module docstring, "TOGGLE-COUNT
    BUDGET SEARCH", for the full rationale.

    Instead of escalating the NUMBER OF TIME STEPS (which forces
    rebuilding/growing the incremental step structure, and re-issuing a
    fresh per-step PbLe cardinality constraint, at every escalation
    round), this fixes a single, generous `num_steps` ONCE (via
    `GatePebbleSolver.build_all_steps`) and instead escalates a GLOBAL
    BUDGET on the total number of compute/uncompute (toggle) events
    allowed across the whole schedule (via
    `GatePebbleSolver.solve_with_toggle_budget`).

    `num_steps` (if omitted): defaults to `max(4, 2 * len(gates))`, a
    generous fixed upper bound on how many discrete time slots are
    available (multiple gates may still toggle within the same slot,
    so this is not the same as a toggle-count bound).

    `start_toggle_budget` (if omitted): defaults to `len(gates)` (every
    gate must compute at least once).

    `max_toggle_budget` (if omitted): defaults to `2 * len(gates) *
    num_steps` purely as a generous ceiling; in practice the search
    usually succeeds long before this.

    `max_escalations`: caps the number of toggle-budget rounds
    attempted, same semantics as in `pebble_gates`.

    Prints the same per-round runtime marker style as `pebble_gates`
    and records an `escalation_log` (per round: budget tried, result,
    elapsed time), dumped to `dump_json` if provided.

    CAVEAT: if `num_steps` is too small, NO toggle budget will ever be
    satisfiable -- in that case, increase `num_steps` directly rather
    than `max_toggle_budget`.
    """
    if num_steps is None:
        num_steps = max(4, 2 * len(gates))
    if start_toggle_budget is None:
        start_toggle_budget = len(gates)
    if max_toggle_budget is None:
        max_toggle_budget = 2 * len(gates) * max(1, num_steps)

    build_start = time.time()
    s = GatePebbleSolver(gates, max_pebbles, net=net)
    s.init()
    s.build_all_steps(num_steps)
    build_elapsed = time.time() - build_start
    print(f"[toggle-budget setup] built {num_steps} step(s) once "
          f"({build_elapsed:.2f}s)")

    current_budget = start_toggle_budget
    escalations_tried = 0
    escalation_log = []

    while current_budget <= max_toggle_budget:
        if max_escalations is not None and escalations_tried >= max_escalations:
            if dump_json:
                dump_pebbling_debug_json(
                    dump_json,
                    extra={"escalation_log": escalation_log, "outcome": "max_escalations_reached"},
                )
            raise RuntimeError(
                f"No gate-level pebbling solution found after "
                f"{escalations_tried} toggle-budget round(s) "
                f"(max_escalations={max_escalations} reached), starting "
                f"from toggle_budget={start_toggle_budget} up to "
                f"current_budget={current_budget - toggle_budget_step} "
                f"(num_steps={num_steps} fixed). If no budget up to "
                f"max_toggle_budget={max_toggle_budget} succeeds, "
                f"num_steps itself may be too small -- consider "
                f"increasing it directly."
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
        f"at num_steps={num_steps}. Consider increasing num_steps directly "
        f"if you suspect the fixed step count itself is too small."
    )


def _solve_gate_steps_at_fixed_steps(gates, max_pebbles, net, num_steps):
    """
    Builds a fresh GatePebbleSolver, adds EXACTLY `num_steps` steps
    (regardless of intermediate SAT/UNSAT along the way -- only the
    FINAL check at exactly this step count is examined), and returns
    (result, solver) where result is z3's sat/unsat.
    """
    s = GatePebbleSolver(gates, max_pebbles, net=net)
    s.init()
    for _ in range(num_steps):
        s.add_step()
    r = s.solve()
    return r, s


def find_min_gate_steps(gates, max_pebbles, net, start_steps=None, verbose=True):
    """
    Finds the MINIMUM number of steps for which `GatePebbleSolver` is
    SAT at a FIXED `max_pebbles` budget, by decreasing the step count
    one at a time from `start_steps` until UNSAT is hit. See module
    docstring ("MIN-STEP DIAGNOSTIC SEARCH") for the full rationale.

    `start_steps`: an upper bound already known to be SAT (e.g. derived
    from a prior successful `pebble_gates(...)` call's returned
    `gate_steps`, via `max(k for k, *_ in gate_steps)`). If omitted,
    defaults to the same "auto" heuristic `pebble_gates` itself uses
    (`max(4, len(gates) * 4)`).

    Returns (min_steps, gate_steps_at_min_steps):
      - min_steps: the smallest step count that is still SAT.
      - gate_steps_at_min_steps: the extracted (k, gid, op, tag)
        schedule AT that minimal step count (from
        `GatePebbleSolver.extract`).

    Raises `RuntimeError` if `start_steps` itself is not SAT (i.e. your
    assumed upper bound was wrong -- try a larger `start_steps`, e.g.
    re-derived from a fresh `pebble_gates(...)` call).

    NOTE: this performs a LINEAR (not binary) downward search, since
    SAT/UNSAT is not asserted here to be monotonic in step count for
    every possible encoding subtlety (fewer steps is usually harder,
    so binary search would likely be safe, but a linear scan avoids
    relying on an unverified monotonicity property). Each step count
    rebuilds a fresh solver from scratch, so this is O(start_steps)
    solver constructions -- for large `gates` lists this may be slow;
    consider this a diagnostic/debug tool, not part of the main solving
    hot path.
    """
    if start_steps is None:
        start_steps = max(4, len(gates) * 4)

    r, s = _solve_gate_steps_at_fixed_steps(gates, max_pebbles, net, start_steps)
    if r != sat:
        raise RuntimeError(
            f"start_steps={start_steps} is not SAT at max_pebbles="
            f"{max_pebbles} -- pick a larger start_steps (e.g. re-derive "
            f"it from a successful pebble_gates(...) call first)."
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
    print(f"\n  Per-gate dependencies:")
    for g in gates:
        po_flag = " [MUST END PEBBLED]" if g.gid in s.po_gate_ids else ""
        dependents = sorted(s.dependents.get(g.gid, ()))
        print(f"    Gate {g.gid}{po_flag}: depends on {sorted(g.depends_on)}; "
              f"dependents: {dependents}")


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
    """
    Deterministically replays qubit assignment for a gate-level
    pebbling schedule.

    Qubit lifecycle rules:
      - Internal (dirty) nodes are allocated via `_alloc_dirty` when
        their owning gate computes, and freed back to `dirty_free`
        IMMEDIATELY after that same compute step -- they are pure
        ancilla local to the gate's own computation and are NEVER read
        by anything outside the gate (that's the definition of
        "internal").
      - Output (clean) nodes are allocated via `_alloc_clean` when
        their owning gate computes. On that gate's uncompute event:
          * if the node is a TRUE primary output (member of
            `net.pos`), it is NEVER freed -- it must remain live for
            the rest of the circuit.
          * otherwise, it IS freed back to `clean_free`. Thanks to
            `_split_mixed_po_groups` (run in `build_gate_groups`), a
            group can never contain both a genuine PO and a non-PO
            output, so any gate with a non-PO output is guaranteed to
            actually reach an `uncompute_gate` event where this
            freeing can happen.
      - XOR nodes never allocate a fresh qubit -- they alias one of
        their own fanins' qubits instead, so "freeing" an XOR node is
        purely a bookkeeping event (no qubit returned to any pool).
    """
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
                    # True primary output: remains live forever, never
                    # returned to any free pool.
                    continue

                q = alloc.assignment.pop(node, None)
                if q is None:
                    # Internal nodes were already freed eagerly at
                    # their own compute step; this is expected for
                    # them.
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
                        # Defensive fallback: should not normally be
                        # reached, since internal nodes are freed
                        # eagerly right after their compute step below.
                        alloc.dirty_free.append(q)
                        yield ("free_dirty", k, node, q)
            continue

        # compute_gate: process internal (dirty) nodes then outputs
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

        # Eagerly reclaim internal (dirty) qubits right after this
        # gate's own compute step -- nothing outside this gate ever
        # reads an internal node's value, so there is no reason to
        # wait for this gate's (possibly nonexistent, e.g. PO-owning)
        # uncompute_gate event to free them.
        for node in dirty_assigned:
            q = alloc.assignment.pop(node, None)
            if q is None:
                continue
            alloc.dirty_free.append(q)
            yield ("free_dirty", k, node, q)


def assign_qubits_to_pebbling(net, gates, gate_steps):
    """
    Maps a solved gate-level pebbling schedule onto concrete qubit
    indices. See `_replay_gate_pebbling` for the full reuse/liveness
    policy. A sanity-check assertion verifies no two simultaneously-live
    non-aliased nodes ever share a qubit index, and that at the end of
    the schedule ONLY true primary outputs remain live.
    """
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
                              "rounds attempted, independent of max_pebbles "
                              "growth itself. See module docstring, "
                              "'ESCALATION ROUND LIMIT AND PER-ROUND RUNTIME "
                              "MARKERS'.")
    parser.add_argument("--use-toggle-budget", action="store_true",
                         help="Use pebble_gates_by_toggle_budget instead of "
                              "pebble_gates -- escalates a global toggle-"
                              "count budget over a FIXED step count, instead "
                              "of escalating the step count itself. See "
                              "module docstring, 'TOGGLE-COUNT BUDGET "
                              "SEARCH'.")
    parser.add_argument("--toggle-num-steps", type=int, default=None,
                         help="Fixed number of time steps to build once, "
                              "when --use-toggle-budget is passed. Defaults "
                              "to max(4, 2*len(gates)).")
    parser.add_argument("--skip-qiskit", action="store_true")
    parser.add_argument("--topo-order", choices=["creation", "dependency_chain"],
                         default="creation",
                         help="Which topological order build_gate_groups uses "
                              "for its Phase 1 greedy pebble-budget fill. See "
                              "pebbling_solver.py module docstring, "
                              "'TOPOLOGICAL ORDER CHOICE'.")
    parser.add_argument("--no-merge-siblings", action="store_true",
                         help="Disable sibling fanout group merging (Phase "
                              "2.5 of build_gate_groups). See module "
                              "docstring, 'SIBLING FANOUT MERGING'.")
    parser.add_argument("--merge-pebble-slack", type=float, default=1.5,
                         help="Slack multiplier on max_pebbles allowed when "
                              "considering a sibling fanout merge. Default 1.5.")
    args = parser.parse_args()

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

    # --- Example: fixed two-output Boolean network via Reed-Muller --------
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
    )
    print_gate_node_groups(gates)
    display_gate_groups(gates)

    print_gate_pebbling_constraints(gates, gate_limit, net=net)
    cycle = find_gate_dependency_cycle(gates)
    print(f"\nGate dependency cycle check: {'CYCLE ' + str(cycle) if cycle else 'none (acyclic, as guaranteed)'}")

    if args.use_toggle_budget:
        gate_steps = pebble_gates_by_toggle_budget(
            gates, max_pebbles=gate_limit, net=net,
            num_steps=args.toggle_num_steps,
            max_escalations=args.max_escalations,
        )
    else:
        gate_steps = pebble_gates(
            gates, max_pebbles=gate_limit, max_steps=None, net=net,
            max_escalations=args.max_escalations,
        )

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
