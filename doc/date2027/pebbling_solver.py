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
     nodes in topological order (which `net.nodes` already is) and
     cutting group boundaries only at that point in the linear order.
     A group is therefore always a CONTIGUOUS RANGE of the topological
     order.
  2. Proves (see accompanying .tex) that a contiguous range of a
     topological order is automatically CONVEX: if u, v are both in
     the range and w lies on some path u -> w -> v, then
     topo(u) < topo(w) < topo(v), and since u, v's topological indices
     already fall inside the range's [lo, hi) bounds, w's index must
     too -- so w is automatically a member of the same group. No
     separate convexity search is required.
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


def read_blif(path):
    net = PebblingNetwork()
    name_to_node = {}
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
        name_to_node[out_name] = net.create_gate(fanins, name=out_name, is_xor=is_xor)
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
    (e.g. after `_split_mixed_po_groups` changes ownership) to
    recompute boundaries from scratch.
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


def _split_mixed_po_groups(net: PebblingNetwork, groups, pos_set):
    """
    Repeatedly splits any group whose `outputs` mix a genuine primary
    output with a non-PO output, so that no group ever ends up in a
    state where a non-PO output can never be freed (because its
    owning group also owns a PO and therefore never uncomputes). See
    module docstring ("MIXED PO GROUPS") for the full rationale.

    Splitting is done by cutting a group's owned-node list immediately
    after the LAST non-PO output node in that group; everything up to
    and including that point becomes an earlier group (guaranteed to
    have no PO among its outputs, since the split point was chosen as
    the last non-PO output -- any PO in the original group must appear
    strictly after all non-PO outputs... this is not assumed, it's
    re-verified by recomputing boundaries and re-checking each
    iteration), and the remainder becomes a later group. Because every
    group is always a CONTIGUOUS RANGE of the overall topological
    order, splitting one range into two contiguous sub-ranges preserves
    convexity everywhere (a sub-range of a convex range is convex).

    Runs to a fixed point: since each split strictly increases the
    number of groups while the total node count stays fixed, this loop
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
                last_idx = max(g.nodes.index(n) for n in non_po_outputs)
                first_part = g.nodes[:last_idx + 1]
                second_part = g.nodes[last_idx + 1:]
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


def build_gate_groups(net: PebblingNetwork, max_pebbles: int):
    """
    Gate-group construction algorithm. See module docstring for the
    full rationale. Steps:

      1. Partition: walk `net.nodes` (already topological) in order,
         skipping PIs, and assign each non-PI node to the CURRENT group.
         Close the current group (start a fresh one) when:
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

    Raises `ValueError` if `max_pebbles < len(net.pos)`.
    """
    if max_pebbles < len(net.pos):
        raise ValueError(
            f"max_pebbles={max_pebbles} is less than the number of primary "
            f"outputs ({len(net.pos)}). All POs must remain pebbled "
            f"simultaneously at the end, so max_pebbles must be >= "
            f"len(net.pos)."
        )

    pos_set = set(net.pos)

    # --- Phase 1: partition -------------------------------------------------
    groups = []
    current = GateGroup(0)
    cur_pebble = 0

    def finalize():
        nonlocal current
        if current.nodes:
            groups.append(current)
            current = GateGroup(len(groups))

    for node in net.nodes:
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
    `net.pos`) MUST remain pebbled at the final step. Thanks to the
    Phase 2.5 split in `build_gate_groups`, such a gate's `outputs`
    NEVER also contain a non-PO output, so this "never uncomputes"
    requirement can no longer strand any non-PO value.
    """

    def __init__(self, gates, max_pebbles, net=None):
        self.gates = gates
        self.max_pebbles = max_pebbles
        self.slv = Solver()
        self.num_steps = 0
        self.current = {}
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

        pos_set = set(net.pos) if net is not None else set()
        self.po_gate_ids = {
            g.gid for g in gates if any(n in pos_set for n in g.outputs)
        }

    def init(self):
        s0 = {g.gid: Bool(f"Gs_0_{g.gid}") for g in self.gates}
        a0 = {g.gid: Bool(f"Ga_0_{g.gid}") for g in self.gates}
        for gid in s0:
            self.slv.add(s0[gid] == False)
            self.slv.add(a0[gid] == False)
        self.current = {gid: (s0[gid], a0[gid]) for gid in s0}
        self.history.append(dict(self.current))

    def add_step(self):
        self.num_steps += 1
        s_next = {g.gid: Bool(f"Gs_{self.num_steps}_{g.gid}") for g in self.gates}
        a_next = {g.gid: Bool(f"Ga_{self.num_steps}_{g.gid}") for g in self.gates}

        for g in self.gates:
            gid = g.gid
            s_cur, _ = self.current[gid]
            s_nxt = s_next[gid]
            a_nxt = a_next[gid]

            if self.gate_deps[gid]:
                deps_now = [self.current[d][0] for d in self.gate_deps[gid]]
                deps_next = [s_next[d] for d in self.gate_deps[gid]]
                self.slv.add(Implies(s_cur != s_nxt, And(*(deps_now + deps_next))))

            self.slv.add(Implies(s_cur != s_nxt, a_nxt))
            self.slv.add(Implies(s_cur == s_nxt, a_nxt == False))

        limited_gates = [g for g in self.gates if self.counts_toward_limit[g.gid]]
        if limited_gates:
            self.slv.add(PbLe([(s_next[g.gid], 1) for g in limited_gates], self.max_pebbles))

        self.current = {gid: (s_next[gid], a_next[gid]) for gid in s_next}
        self.history.append(dict(self.current))

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


def pebble_gates(gates, max_pebbles, max_steps="auto", verbose=True,
                  auto_increase_pebbles=True, max_pebbles_cap=None, net=None):
    if max_pebbles_cap is None:
        max_pebbles_cap = max(1, len(gates))
    max_pebbles_cap = max(max_pebbles_cap, max_pebbles)

    current_pebbles = max_pebbles
    while current_pebbles <= max_pebbles_cap:
        step_cap = max_steps
        if step_cap == "auto":
            step_cap = max(4, len(gates) * 4)

        s = GatePebbleSolver(gates, current_pebbles, net=net)
        s.init()
        r = s.solve()
        while r == unsat and (step_cap is None or s.num_steps < step_cap):
            s.add_step()
            r = s.solve()

        if r == sat:
            s.save_model()
            if current_pebbles != max_pebbles:
                print(f"\nRequested max_pebbles={max_pebbles} was "
                      f"infeasible; escalated to {current_pebbles}.")
            return s.extract(verbose=verbose)

        if not auto_increase_pebbles:
            raise RuntimeError(
                f"No gate-level pebbling solution found with max_pebbles="
                f"{current_pebbles} within {step_cap} steps."
            )

        print(f"max_pebbles={current_pebbles} infeasible within "
              f"{step_cap} steps; increasing to {current_pebbles + 1}...")
        current_pebbles += 1

    raise RuntimeError(
        f"No gate-level pebbling solution found for any max_pebbles from "
        f"{max_pebbles} up to max_pebbles_cap={max_pebbles_cap}."
    )


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
        print(f"    Gate {g.gid}{po_flag}: depends on {sorted(g.depends_on)}")


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
    parser.add_argument("--skip-qiskit", action="store_true")
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
    gates = build_gate_groups(net, max_pebbles=gate_limit)
    print_gate_node_groups(gates)
    display_gate_groups(gates)

    print_gate_pebbling_constraints(gates, gate_limit, net=net)
    cycle = find_gate_dependency_cycle(gates)
    print(f"\nGate dependency cycle check: {'CYCLE ' + str(cycle) if cycle else 'none (acyclic, as guaranteed)'}")

    gate_steps = pebble_gates(gates, max_pebbles=gate_limit, max_steps=None, net=net)

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
