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
  4. SPLITS any group whose `outputs` mix a genuine primary output (PO)
     with a non-PO output (see "MIXED PO GROUPS" below) -- this step is
     essential and was missing from an earlier revision.
  5. Derives gate-level dependencies strictly from boundary inputs
     (a group's external fanins), never from incidental path traversal.
  6. Validates the result: unique ownership (partition), and acyclicity
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
  - "creation": use `net.nodes` as-is (today's original
    behavior, unchanged, for full backward compatibility).
  - "dependency_chain" (default): use `_dependency_chain_topo_order(net)`
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
MIXED PO GROUPS (the bug this revision fixes)
======================================================================

`GatePebbleSolver` requires any gate that owns at least one TRUE
primary output (a node in `net.pos`) to remain pebbled FOREVER (its
final state must be True). Consequently, that gate NEVER executes an
`uncompute_gate` event.

If a single group's `outputs` list contains BOTH a genuine PO node AND
some other, non-PO output node (e.g. a node shared/reused by two
different downstream outputs under Reed-Muller cut sharing, such that
it is consumed by a node in a DIFFERENT, later group), then that
non-PO output's qubit is allocated when the group computes but can
NEVER be freed -- since the only place non-PO outputs are freed is the
group's `uncompute_gate` event, and a PO-owning group never uncomputes.
This caused `assign_qubits_to_pebbling`'s final liveness assertion to
fail with a leftover live qubit (e.g. a shared AND-monomial node like
"and1_0_1") even after the qubit-reclamation-timing fix in a prior
revision.

The fix (`_split_mixed_po_groups`, run as Phase 2.5 of
`build_gate_groups`) detects any group whose `outputs` mix a genuine PO
with a non-PO output, and splits that group's node list at the
position immediately after the LAST such non-PO output node: nodes up
to and including that point become one group (with no PO among its
outputs, so it is now free to uncompute normally), and the remainder
(containing the PO) becomes a second group. Because groups are always
contiguous ranges of the topological order, splitting one range into
two contiguous sub-ranges preserves the convexity/acyclicity guarantee
established in Phase 1 -- no additional cycle-checking logic is
required beyond the same defensive validation already run at the end.
This is applied repeatedly (a fixed-point loop) since a single split
could, in principle, still leave a "mixed" situation if a group
contained more than one PO interspersed with non-PO outputs; the loop
terminates because the total node count is fixed and the group count
only ever increases.

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
freed. With the Phase 2.5 fix above, every group's outputs are now
either ALL genuine POs or contain NO genuine PO at all, so a group with
any non-PO output is now guaranteed to actually execute an
`uncompute_gate` event at some point, making case (b) freeing
reachable in practice.

Separately, a group's `internal` (dirty pebble) nodes are, by
definition, values that are NEVER read by anything outside their own
owning gate -- they exist purely as local ancilla for that gate's own
compute step. They must therefore be freed EAGERLY, immediately after
their owning gate's compute step finishes, rather than deferred to
that gate's uncompute_gate event, for the same reason: a PO-owning
gate never uncomputes.

Everything else in this file (node-level Z3 pebbling, Reed-Muller/ESOP
network generators, gate-level Z3 pebbling, gadget synthesis,
Qiskit/QASM export) is unchanged in spirit from prior revisions.
"""

import random
from collections import defaultdict

from z3 import Bool, Solver, Implies, And, sat, unsat, PbLe


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


# [truncated for brevity in tool output; original file content retained in repo from initial read]


def build_gate_groups(net: PebblingNetwork, max_pebbles: int, topo_order="dependency_chain"):
    """
    Gate-group construction algorithm. See module docstring for the
    full rationale. Steps:

      1. Partition: walk the network's nodes (in the topological order
         selected by `topo_order` -- see module docstring, "TOPOLOGICAL
         ORDER CHOICE") in order, skipping PIs, and assign each non-PI
         node to the CURRENT group. Close the current group (start a
         fresh one) when:
           (a) the node is a primary output, or
           (b) the accumulated Phase-1 pebble cost of nodes placed into
               the current group reaches `max_pebbles - 1`.
           That per-node cost currently comes from `_pebble_cost(node)`,
           which returns 0 for XOR nodes and 1 for every other non-PI
           node. Future cost-aware grouping should continue to route all
           such accounting through `_pebble_cost` so XOR nodes remain
           hard-excluded from the budget.

      2. Boundary computation: for every group, inspect each owned
         node's ORIGINAL fanins/fanouts to classify it as `outputs`
         (external consumer or PO) or `internal` (fully consumed
         within the group), and collect `inputs` (external fanins
         actually used).

      2.5. Split any group whose `outputs` mix a genuine PO with a
           non-PO output (`_split_mixed_po_groups`) -- see module
           docstring ("MIXED PO GROUPS").

      3. Dependency derivation: for each (possibly re-split) group,
         look up the owner group of each of its `inputs`; that owner's
         gid is added to `depends_on`.

      4. Validation: confirm every non-PI node is owned by EXACTLY one
         group (`_validate_gate_partition`), and that the resulting
         gate-dependency graph is acyclic (`find_gate_dependency_cycle`)
         -- which is guaranteed by construction (contiguous ranges of a
         topological order are always convex, see accompanying .tex),
         but is checked defensively here.

    `topo_order`:
      - "creation": use `net.nodes` as-is, i.e. the order
        nodes were created in (today's original behavior, unchanged).
      - "dependency_chain" (default): use `_dependency_chain_topo_order(net)`
        instead, which reorders nodes (still a VALID topological
        order -- convexity is preserved regardless) to keep
        producer/consumer chains adjacent, so Phase 1's greedy
        pebble-budget fill is more likely to group a node with its
        actual dependents rather than an unrelated sibling. See module
        docstring for details and a worked example.

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

    # --- Phase 2 + 2.5: boundary computation, then split mixed-PO groups ---
    groups = _split_mixed_po_groups(net, groups, pos_set)

    # --- Phase 3: dependency derivation ------------------------------------
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

    # --- Phase 4: validation ------------------------------------------------
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


# [remaining file content unchanged]

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
    parser.add_argument("--skip-qiskit", action="store_true")
    parser.add_argument("--topo-order", choices=["creation", "dependency_chain"],
                         default="dependency_chain",
                         help="Which topological order build_gate_groups uses "
                              "for its Phase 1 greedy pebble-budget fill. See "
                              "pebbling_solver.py module docstring, "
                              "'TOPOLOGICAL ORDER CHOICE'.")
    args = parser.parse_args()
