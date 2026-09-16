"""
Python port of caterpillar's z3_pebble_solver (see
include/caterpillar/solvers/z3_solver.hpp in gmeuli/caterpillar).

Encodes the reversible pebbling game for a DAG as an incremental SAT
problem in Z3: at each step, a node's pebble state can flip only if all
its fanins are currently pebbled, and at most `pebbles` nodes may be
pebbled at once. Steps are added one at a time until the target nodes
(POs) are pebbled and all non-PI/PO nodes are unpebbled.

Also includes:
  - `group_pebbles`             : batches a compute/uncompute sequence
                                   into groups of nodes up to a pebble
                                   limit.
  - `random_dag`                : generates a random DAG (PebblingNetwork)
                                   for testing/benchmarking the solver.
  - `read_blif`                 : parses a (combinational) .blif file
                                   into a PebblingNetwork.
  - `build_gate_groups_by_rules` : traverses PI->PO paths and groups nodes
                                   into clean/dirty/xor "gates" per a set
                                   of custom rules.
  - `merge_gates_on_shared_xor_nodes`
                                 : merges gate subgraphs that share a common
                                   XOR node (post-processing; avoids duplicate
                                   gate definitions caused by multiple path
                                   convergences).
  - `build_gate_groups`          : wrapper that runs raw traversal then
                                   merges overlapping XOR-based subgraphs.
  - `print_gate_node_groups`     : prints each gate as a single combined
                                   list of its nodes.
  - `GatePebbleSolver`/`pebble_gates`
                                 : gate-level Z3 pebbling with a
                                   simultaneous-gate cap (XOR-only gates
                                   are exempt from the cap). Auto-escalates
                                   the pebble cap if infeasible.
  - `generate_gate`/`generate_all_gates`
                                 : synthesizes a symbolic quantum gadget
                                   (Toffoli/CNOT/H/T/T-dagger sequence)
                                   for each node according to a
                                   6-rule synthesis table.
  - `find_min_pebbles`           : auto-increments the pebble limit until
                                   a feasible pebbling sequence is found.

Note on minimum feasible pebble count: reversible pebbling of even a
simple 2-gate chain (e.g. n1 = XOR(p1,p2), n2 = AND(n1,p3), PO = n2) can
require MORE than "fanins + 1" pebbles, and can even exceed the number
of primary inputs. This is because after computing an intermediate node
and freeing its fanins, uncomputing that intermediate later requires
re-pebbling its fanins *while* the node it fed into (which must remain
pebbled, e.g. because it's a PO) is still alive. For that example, the
true minimum is 4 pebbles, even though there are only 3 PIs.

Because PI count is NOT a safe upper bound on the required pebble
count, `max_pebbles_cap` (for `pebble()`/`pebble_gates()`) and
`max_pebbles` (for `find_min_pebbles`) default instead to the total
number of nodes/gates in the network -- the only bound that is always
structurally safe, since you can never need more simultaneous pebbles
than there are nodes to pebble in the first place. The resolved cap is
also always widened (if needed) to be at least the requested starting
`pebbles`/`max_pebbles` value, so the solver is guaranteed to attempt
what you asked for at a minimum.

Note on the minimum required pebbles vs. number of POs: since every
primary output (PO) must remain pebbled simultaneously at the very end
of the pebbling game, `pebbles`/`max_pebbles` can never be less than
`len(net.pos)` -- anything smaller is guaranteed infeasible. `pebble()`,
`build_gate_groups_by_rules()`, and `find_min_pebbles()` all validate
this up front (raising `ValueError`, or in `find_min_pebbles`'s case,
silently widening the search range to start at `len(net.pos)`).

Because `random_dag` now makes every sink node a PO (see below), the
number of POs on a given random network is data-dependent and can vary
run to run. The demo below computes a per-network effective pebble
limit (`max(pebble_limit, len(rnd_net.pos))`) for the random-DAG
examples, so a user-requested `--max-pebbles` that happens to be
smaller than that network's PO count is automatically widened rather
than raising `ValueError`.

`pebble()` and `pebble_gates()` default to `auto_increase_pebbles=True`:
if the requested pebble count turns out to be infeasible (stays unsat
through the step cap), they automatically retry with one more pebble,
up to `max_pebbles_cap`, instead of raising immediately. Set
`auto_increase_pebbles=False` to get the old strict behavior (raise
`RuntimeError` right away if the requested count fails).

By default, `max_steps="auto"` resolves to a cap of 4 * (total number of
nodes/gates) per pebble-count attempt -- a reasonable bound that leaves
room for both compute and uncompute steps on every node, while avoiding
an unbounded runaway search. Pass `max_steps=None` for a fully unbounded
per-attempt search, or an explicit integer for a custom cap.

Gate-group construction design (`build_gate_groups_by_rules` /
`merge_gates_on_shared_xor_nodes` / `build_gate_groups`):

Raw traversal (`build_gate_groups_by_rules`) walks every PI->PO path and
builds gate fragments according to these rules:

  - Primary inputs (PIs) are always available and never need to be
    pebbled or uncomputed. They are skipped entirely -- no bookkeeping,
    no gate boundary change.
  - XOR nodes are recorded in `xor_nodes` (no ancilla/pebble bookkeeping
    needed) and do NOT finalize the gate.
  - Rule 1 (primary output): the node is a network output and therefore
    never needs to be uncomputed. Adds the node to `clean_pebble`,
    FINALIZES the gate, and stops traversal of this path.
  - Pebble-limit rule: fires when `cur_pebble == max_pebbles - 1`. Adds
    the node to `clean_pebble`, FINALIZES the gate, resets `cur_pebble`,
    and continues traversal of this path in a fresh gate.
  - Rule 3: fires when the node feeds a two-input AND gate whose OTHER
    fanin is itself an intermediate (non-PI) node, and this node has not
    already been visited. Adds the node to `clean_pebble` (it must still
    be uncomputed later -- enforced by the underlying Z3 solver, not the
    grouping itself), FINALIZES the gate, and stops traversal of this
    path. If the node HAS already been visited, this rule is skipped and
    traversal simply advances to the next node in the path.
  - Rule 4 (ordinary dirty pebble): the fallback case. Adds the node to
    `dirty_pebble` and increments `cur_pebble`, but does NOT finalize the
    gate -- traversal keeps accumulating into the same gate.

Because a DAG node can be reached by more than one PI->PO path (fanout
> 1), raw traversal can produce multiple gate fragments that describe
overlapping parts of the same underlying subgraph -- this is a normal
consequence of a converging DAG, not an error. Rather than trying to
prevent this during traversal, `merge_gates_on_shared_xor_nodes` is run
as a post-processing step: any two raw gate fragments that share a
common XOR node are merged (via union-find, transitively) into a single
combined gate. `build_gate_groups` is the recommended entry point that
runs both steps together.

Note on `random_dag` and primary output (PO) selection: POs are set to
EVERY "sink" node in the generated DAG -- i.e. every node that is not
used as a fanin by any other node -- rather than a random sample of
`num_pos` nodes. This guarantees that every node in the network is an
ancestor of at least one PO (since a node's forward chain in a DAG must
terminate at some sink), so path-based traversal (e.g.
`_enumerate_paths`/`build_gate_groups_by_rules`) is guaranteed to visit
every node in the network, not just the fanin cone of a single
arbitrarily-chosen output.

Command-line usage:
    python pebbling_solver.py [--max-pebbles N] [--max-steps auto|none|N]

  --max-pebbles N   Starting pebble count to try for the pebbling
                     examples (default: 5). Auto-escalates if infeasible.
                     For the random-DAG examples, this is automatically
                     widened (if needed) to at least the number of POs
                     in the generated network.
  --max-steps V      Step cap for the solver: an integer, 'auto'
                     (default, = 4 * number of nodes/gates), or 'none'
                     for unbounded search per pebble-count attempt.
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
        """
        Generic gate creation. Pass `is_xor=True` to mark this node as an
        XOR gate (so gadget synthesis picks Rule 5), otherwise it is
        treated as an AND-like node.
        """
        n = Node(name or f"g{len(self.nodes)}", fanins=fanins, is_xor=is_xor)
        self.nodes.append(n)
        return n

    def create_and_gate(self, fanins, name=None):
        """Convenience wrapper for an AND-like node."""
        return self.create_gate(fanins, name=name, is_xor=False)

    def create_xor_gate(self, fanins, name=None):
        """Convenience wrapper for an XOR node."""
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
# Random DAG generator
# ---------------------------------------------------------------------------

def random_dag(num_pis=4, num_gates=6, max_fanin=2, num_pos=1, seed=None, xor_prob=0.3):
    """
    Generates a random DAG as a PebblingNetwork.

    - `num_pis`: number of primary inputs.
    - `num_gates`: number of internal gates to create.
    - `max_fanin`: max number of fanins per gate (fanins are chosen from
                   any previously created node, PI or gate, guaranteeing
                   a valid topological / acyclic ordering).
    - `num_pos`: kept for backward compatibility but no longer used to
                 sample POs randomly. Instead, POs are set to EVERY
                 "sink" node -- i.e. every node that is not used as a
                 fanin by any other node. This guarantees that every
                 node in the network is an ancestor of at least one PO
                 (since a node's forward chain in a DAG must terminate
                 at some sink), so path-based traversal (e.g.
                 `build_gate_groups_by_rules`) will visit every node.
                 Because of this, the NUMBER of POs produced is
                 data-dependent -- callers should not assume it equals
                 `num_pos` and should size `max_pebbles`/`pebbles`
                 relative to `len(net.pos)` after generation.
    - `seed`: optional random seed for reproducibility.
    - `xor_prob`: probability that a given gate is created as an XOR node
                  instead of an AND-like node.
    """
    if seed is not None:
        random.seed(seed)

    net = PebblingNetwork()

    for i in range(num_pis):
        net.create_pi(f"pi{i}")

    for i in range(num_gates):
        pool = net.nodes  # any existing node can be a fanin -> stays acyclic
        fanin_count = random.randint(1, min(max_fanin, len(pool)))
        fanins = random.sample(pool, fanin_count)
        is_xor = random.random() < xor_prob
        net.create_gate(fanins, name=f"n{i}", is_xor=is_xor)

    # Every "sink" node (a node that is not consumed as a fanin by any
    # other node) becomes a PO. This guarantees every node in the
    # network is an ancestor of some PO -- otherwise its forward chain
    # would have to terminate at a node that isn't a sink, which is a
    # contradiction. PIs that happen to be sinks (unused entirely) are
    # excluded, since a bare, unconnected PI as a PO isn't meaningful
    # for the pebbling game.
    children = net.build_children()
    sink_nodes = [n for n in net.nodes if not n.is_pi and not children.get(n)]
    if not sink_nodes:
        # Degenerate case (e.g. num_gates == 0): fall back to the last
        # created node, if any, so the network still has a PO.
        non_pi_nodes = [n for n in net.nodes if not n.is_pi]
        sink_nodes = non_pi_nodes[-1:] or net.nodes[-1:]
    for n in sink_nodes:
        net.create_po(n)

    return net


# ---------------------------------------------------------------------------
# BLIF reader
# ---------------------------------------------------------------------------

def _is_xor_cover(cover_rows, num_fanins):
    """
    Detects a canonical XOR truth table from BLIF .names cover rows.
    e.g. for 2 inputs: rows {"01 1", "10 1"} (or their complement form)
    indicate XOR; anything else is treated as non-XOR (AND-like).
    """
    if num_fanins != 2:
        return False
    rows = set(r.strip() for r in cover_rows)
    xor_pattern = {"01 1", "10 1"}
    xnor_pattern = {"00 1", "11 1"}
    return rows == xor_pattern or rows == xnor_pattern


def read_blif(path):
    """
    Parses a (combinational) .blif file into a PebblingNetwork.

    Supports the standard subset of BLIF:
      .model <name>
      .inputs <names...>
      .outputs <names...>
      .names <fanin1> <fanin2> ... <output>
      <truth table rows>
      .end

    Each `.names` block becomes one gate node, with fanins being the
    listed input signal names (in order) and the node itself named after
    the block's output signal. The truth-table rows are inspected with a
    lightweight heuristic (`_is_xor_cover`) to detect canonical 2-input
    XOR/XNOR covers and tag the resulting node's `is_xor` flag
    accordingly; any other cover is treated as AND-like.

    Multi-output .names blocks (single line with no fanins, i.e. constant
    nodes) and `.latch`/`.subckt`/`.gate` lines are not supported, since
    the pebbling game here targets combinational logic.
    """
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

    # join continuation lines ending in backslash
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

        # truth-table row belonging to the current .names block
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
        self.current = {}   # node -> (s_var, a_var) at current step
        self.model = None
        self.history = []   # list of dict: node -> (s_var, a_var) per step

    def init(self):
        s0 = {n: Bool(f"s_0_{n.name}") for n in self.net.nodes}
        a0 = {n: Bool(f"a_0_{n.name}") for n in self.net.nodes}
        for n in self.net.nodes:
            # initial state: nothing pebbled, no activation at step 0
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

            # A gate can flip its pebble state only if all fanins are
            # (and remain) pebbled during the transition.
            if n.fanins:
                fanins_pebbled = And(
                    *[self.current[f][0] for f in n.fanins],
                    *[s_next[f] for f in n.fanins],
                )
                self.slv.add(Implies(s_cur != s_nxt, fanins_pebbled))

            # a_nxt marks that this node's state changed this step
            self.slv.add(Implies(s_cur != s_nxt, a_nxt))
            self.slv.add(Implies(s_cur == s_nxt, a_nxt == False))

        # pebble-count constraint: at most `pebbles` nodes pebbled at once
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
                self.slv.add(s_cur)       # target nodes must end pebbled
            else:
                self.slv.add(s_cur == False)  # everything else unpebbled
        result = self.slv.check()
        if result == unsat:
            self.slv.pop()
        return result

    def save_model(self):
        self.model = self.slv.model()

    def extract_result(self, verbose=True):
        """Reconstruct the compute/uncompute sequence from the model."""
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
            # uncompute first, then compute (matches original ordering)
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
    """
    Attempts to find a pebbling sequence using at most `pebbles` pebbles.

    `max_steps` controls the incremental step-search cap (per pebble
    count attempted):
      - "auto" (default): cap = 4 * (total number of nodes in `net`).
      - None: no cap -- keeps adding steps indefinitely until satisfiable.
      - int: an explicit step cap.

    `auto_increase_pebbles` (default True): if the requested `pebbles`
    count is exhausted (stays unsat through the step cap), automatically
    increments `pebbles` by 1 and retries, up to `max_pebbles_cap`.

    `max_pebbles_cap` (default: total number of nodes in `net`, i.e.
    `len(net.nodes)`): the ceiling for auto-escalation. This is the only
    bound that's always structurally safe -- PI count is NOT a safe
    upper bound, since reversible pebbling's compute/uncompute overlap
    can require more simultaneous pebbles than there are inputs (e.g. a
    2-gate chain with 3 PIs can genuinely need 4 pebbles). The resolved
    cap is always widened (if needed) to be at least the requested
    `pebbles`, so the solver is guaranteed to attempt what you asked for.

    Set `auto_increase_pebbles=False` to disable escalation and raise
    `RuntimeError` immediately if `pebbles` is infeasible within
    `max_steps`.

    Use `find_min_pebbles` if you want to explicitly search for the
    minimum feasible pebble count starting from some `start` value.

    Raises `ValueError` if `pebbles < len(net.pos)`, since all primary
    outputs must remain pebbled simultaneously at the end -- anything
    smaller is guaranteed infeasible.
    """
    if pebbles < len(net.pos):
        raise ValueError(
            f"pebbles={pebbles} is less than the number of primary "
            f"outputs ({len(net.pos)}). All POs must remain pebbled "
            f"simultaneously at the end, so pebbles must be >= len(net.pos)."
        )

    if max_pebbles_cap is None:
        max_pebbles_cap = max(1, len(net.nodes))
    # never let the cap be smaller than the requested starting point --
    # otherwise the search loop below would never even try `pebbles`.
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
                f"within {step_cap} steps. This network may structurally "
                f"require more pebbles. Try increasing `pebbles`, raising "
                f"`max_steps`, or setting auto_increase_pebbles=True."
            )

        print(f"pebbles={current_pebbles} infeasible within {step_cap} "
              f"steps; increasing to {current_pebbles + 1}...")
        current_pebbles += 1

    raise RuntimeError(
        f"No pebbling sequence found for any pebble count from {pebbles} "
        f"up to max_pebbles_cap={max_pebbles_cap}. Pass an explicit larger "
        f"max_pebbles_cap if this network genuinely needs more pebbles."
    )


def find_min_pebbles(net: PebblingNetwork, start=1, max_pebbles=None, max_steps="auto", verbose=False):
    """
    Auto-increments the pebble limit starting from `start` until a
    feasible pebbling sequence is found (or `max_pebbles` is exceeded).

    `max_pebbles` (default: total number of nodes in `net`, i.e.
    `len(net.nodes)`): the ceiling to search up to. Always widened (if
    needed) to be at least `start`.

    `max_steps` controls the per-pebble-count incremental search cap:
      - "auto" (default): cap = 4 * (total number of nodes in `net`).
      - None: no cap -- keeps adding steps indefinitely until satisfiable.
      - int: an explicit step cap.

    Since all primary outputs must remain pebbled simultaneously at the
    end, any pebble count below `len(net.pos)` is guaranteed infeasible.
    Both `start` and `max_pebbles` are silently widened (if needed) to be
    at least `len(net.pos)`, so the search never wastes time on
    guaranteed-infeasible pebble counts.

    Returns (pebbles_used, steps). Raises `RuntimeError` if nothing up to
    `max_pebbles` works.
    """
    if max_steps == "auto":
        max_steps = max(4, len(net.nodes) * 4)

    if max_pebbles is None:
        max_pebbles = max(1, len(net.nodes))
    # never let the cap be smaller than the requested starting point
    max_pebbles = max(max_pebbles, start)
    # never let the cap be smaller than the number of POs either -- all
    # POs must remain pebbled simultaneously at the end
    max_pebbles = max(max_pebbles, len(net.pos))
    # and never let the starting point search below the PO count, since
    # anything less is guaranteed infeasible
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

    raise RuntimeError(
        f"No feasible pebbling sequence found up to max_pebbles={max_pebbles}."
    )


# ---------------------------------------------------------------------------
# Grouping compute/uncompute sequence into batches
# ---------------------------------------------------------------------------

def group_pebbles(steps, pebble_limit):
    """
    Groups a pebbling sequence (list of (node, action) tuples, action in
    {"compute", "uncompute"}) into batches of up to `pebble_limit` nodes.

    Rules:
      - If action == compute and cur_pebble < pebble_limit:
            add the node to the current group, cur_pebble += 1
      - If action == uncompute and cur_pebble < pebble_limit:
            ignore the step (does not affect the group)
      - If cur_pebble == pebble_limit - 2:
            close/save the current group and start a new one
    """
    groups = []
    current_group = []
    cur_pebble = 0

    for node, action in steps:
        if action == "compute" and cur_pebble < pebble_limit:
            current_group.append(node)
            cur_pebble += 1
        elif action == "uncompute" and cur_pebble < pebble_limit:
            # ignored per the rules
            pass

        if cur_pebble == pebble_limit - 2:
            groups.append(current_group)
            current_group = []
            cur_pebble = 0

    # capture any leftover partial group
    if current_group:
        groups.append(current_group)

    return groups


def print_groups(groups):
    print(f"\n{len(groups)} pebble group(s):")
    for i, group in enumerate(groups):
        names = [n.name for n in group]
        print(f"  Group {i}: {names}")


# ---------------------------------------------------------------------------
# Gate structure for clean/dirty/xor grouping (path-traversal rules)
# ---------------------------------------------------------------------------

class GateGroup:
    def __init__(self, gid):
        self.gid = gid
        self.clean_pebble = []     # list[Node]
        self.dirty_pebble = []     # list[Node]
        self.xor_nodes = []        # list[Node] - XOR nodes, no ancilla needed
        self.dependencies = set()  # nodes in this gate whose fanins come from other nodes

    def all_nodes(self):
        """
        Returns every node in this gate as a single combined list, in the
        order they were added: dirty pebbles first, then xor nodes, then
        clean pebbles (matching the order nodes are typically consumed
        during traversal).
        """
        return self.dirty_pebble + self.xor_nodes + self.clean_pebble

    def __repr__(self):
        return (f"GateGroup(id={self.gid}, "
                f"nodes={[n.name for n in self.all_nodes()]})")


def _enumerate_paths(net: PebblingNetwork):
    """Enumerate PI->PO paths (DFS)."""
    paths = []

    def backtrack(node, suffix):
        suffix.append(node)
        if node.is_pi or not node.fanins:
            paths.append(list(reversed(suffix)))  # PI -> ... -> PO
            suffix.pop()
            return
        for f in node.fanins:
            backtrack(f, suffix)
        suffix.pop()

    for po in net.pos:
        backtrack(po, [])
    return paths


def build_gate_groups_by_rules(net: PebblingNetwork, max_pebbles: int):
    """
    Builds raw gate fragments by traversing every PI->PO path.

    Gate boundaries (finalize the current gate and start a new one):
      - Rule 1: node is a primary output.
      - Rule 3: node feeds a two-input AND whose OTHER fanin is itself
        an intermediate (non-PI) node, and this node has not yet been
        visited.
      - Pebble-limit rule: cur_pebble == max_pebbles - 1.

    Rule 4 (ordinary dirty pebble) does NOT finalize the gate -- it just
    appends the node to dirty_pebble and increments cur_pebble; traversal
    continues accumulating into the same gate.

    Primary inputs are always available and are never included in a gate.
    XOR nodes are recorded but do not consume pebble capacity and do not
    finalize the gate. The raw fragments are later merged by
    `merge_gates_on_shared_xor_nodes()`.

    Raises `ValueError` if `max_pebbles < len(net.pos)`, since all
    primary outputs must remain pebbled simultaneously at the end --
    anything smaller is guaranteed infeasible.
    """
    if max_pebbles < len(net.pos):
        raise ValueError(
            f"max_pebbles={max_pebbles} is less than the number of primary "
            f"outputs ({len(net.pos)}). All POs must remain pebbled "
            f"simultaneously at the end, so max_pebbles must be >= "
            f"len(net.pos)."
        )

    children = net.build_children()
    paths = _enumerate_paths(net)
    visited = set()

    gates = []
    gate = GateGroup(0)
    cur_pebble = 0

    def finalize_gate():
        nonlocal gate
        if gate.clean_pebble or gate.dirty_pebble or gate.xor_nodes:
            gates.append(gate)
            gate = GateGroup(len(gates))

    def _feeds_two_fanin_and_with_intermediate_fanin(node):
        """
        True if `node` feeds a non-XOR child with exactly two fanins,
        where at least one of that child's OTHER fanins is itself an
        intermediate (non-PI) node.
        """
        for child in children.get(node, []):
            if child.is_xor or len(child.fanins) != 2:
                continue
            other_fanins = [f for f in child.fanins if f is not node]
            if any(not f.is_pi for f in other_fanins):
                return True
        return False

    for path in paths:
        i = 0

        while i < len(path):
            node = path[i]

            # Dependencies belong to the gate containing the node.
            if any(fanin in net.nodes for fanin in node.fanins):
                gate.dependencies.add(node)

            # Primary inputs are always available. They do not belong to
            # any gate and do not consume pebble capacity.
            if node.is_pi:
                visited.add(node)
                i += 1
                continue

            # XOR nodes are included in the subgraph but do not consume
            # pebble capacity and do not finalize the gate.
            if node.is_xor and node not in net.pos:
                gate.xor_nodes.append(node)
                visited.add(node)
                i += 1
                continue

            # Rule 1: primary output.
            #
            # A primary output remains pebbled as the final result and
            # therefore does not need to be uncomputed.
            if node in net.pos:
                gate.clean_pebble.append(node)
                visited.add(node)
                if node.is_xor:
                    gate.xor_nodes.append(node)
                finalize_gate()
                cur_pebble = 0
                break

            # Pebble-limit rule: cur_pebble reached max_pebbles - 1.
            if cur_pebble == max_pebbles - 1:
                gate.clean_pebble.append(node)
                visited.add(node)
                finalize_gate()
                cur_pebble = 0
                i += 1
                continue

            # Rule 3: node feeds a two-input AND whose OTHER fanin is
            # itself an intermediate (non-PI) node, and this node hasn't
            # been visited yet.
            #
            # The node is an intermediate value, so it must eventually be
            # uncomputed, but it terminates construction of this gate.
            if _feeds_two_fanin_and_with_intermediate_fanin(node):
                if node not in visited:
                    gate.clean_pebble.append(node)
                    visited.add(node)
                    finalize_gate()
                    cur_pebble = 0
                    break
                else:
                    i += 1
                    continue

            # Rule 4: ordinary dirty pebble. Does NOT finalize the gate --
            # traversal continues accumulating into the same gate.
            gate.dirty_pebble.append(node)
            visited.add(node)
            cur_pebble += 1
            i += 1

    finalize_gate()
    return gates


def merge_gates_on_shared_xor_nodes(gates):
    """
    Post-processing step: merges gates whose node-subgraphs share one or
    more common XOR nodes into a single combined gate.

    Rationale: `build_gate_groups_by_rules` builds one gate fragment per
    PI->PO path traversal segment. Since a DAG node can be reachable from
    multiple paths (fanout > 1), two different path traversals can each
    independently build a gate fragment around the same underlying XOR
    node(s) -- producing separate GateGroup objects that are really
    describing the same combined subgraph. Rather than special-casing
    this during traversal, we instead let traversal proceed freely and
    merge the resulting gates afterward: any two gates that share at
    least one XOR node are considered part of the same combined gate.

    Uses union-find over gate indices to merge transitively (if gate A
    shares an XOR node with gate B, and gate B shares a different XOR
    node with gate C, then A/B/C are all merged into one).

    Returns a new list of GateGroup objects (renumbered by gid), each
    with deduplicated clean_pebble/dirty_pebble/xor_nodes lists (node
    order preserved, first-seen order wins) and a unioned dependencies
    set.
    """
    n = len(gates)
    if n == 0:
        return []

    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[ry] = rx

    # union any two gates that share a common XOR node
    xor_node_to_gate_idx = {}
    for idx, g in enumerate(gates):
        for xn in g.xor_nodes:
            if xn in xor_node_to_gate_idx:
                union(idx, xor_node_to_gate_idx[xn])
            else:
                xor_node_to_gate_idx[xn] = idx

    # bucket gate indices by their union-find root, preserving first-seen
    # root order so gid assignment is stable/deterministic
    root_order = []
    buckets = defaultdict(list)
    for idx in range(n):
        root = find(idx)
        if root not in buckets:
            root_order.append(root)
        buckets[root].append(idx)

    merged_gates = []
    for new_gid, root in enumerate(root_order):
        merged = GateGroup(new_gid)
        seen_clean, seen_dirty, seen_xor = set(), set(), set()
        for idx in buckets[root]:
            g = gates[idx]
            for node in g.clean_pebble:
                if node not in seen_clean:
                    merged.clean_pebble.append(node)
                    seen_clean.add(node)
            for node in g.dirty_pebble:
                if node not in seen_dirty:
                    merged.dirty_pebble.append(node)
                    seen_dirty.add(node)
            for node in g.xor_nodes:
                if node not in seen_xor:
                    merged.xor_nodes.append(node)
                    seen_xor.add(node)
            merged.dependencies |= g.dependencies
        merged_gates.append(merged)

    return merged_gates


def build_gate_groups(net: PebblingNetwork, max_pebbles: int):
    """
    Convenience wrapper: runs `build_gate_groups_by_rules` (raw per-path
    traversal) followed by `merge_gates_on_shared_xor_nodes`
    (post-processing merge of gate fragments that share XOR nodes). This
    is the recommended entry point for gate-group construction -- use
    `build_gate_groups_by_rules` directly only if you specifically want
    the raw, unmerged per-path fragments.
    """
    raw_gates = build_gate_groups_by_rules(net, max_pebbles)
    return merge_gates_on_shared_xor_nodes(raw_gates)


def display_gate_groups(gates):
    print("\nGate groups:")
    for g in gates:
        print(f"Gate {g.gid}")
        print(f"  nodes        : {[n.name for n in g.all_nodes()]}")
        print(f"  clean_pebble : {[n.name for n in g.clean_pebble]}")
        print(f"  dirty_pebble : {[n.name for n in g.dirty_pebble]}")
        print(f"  xor_nodes    : {[n.name for n in g.xor_nodes]}")
        print(f"  dependencies : {[n.name for n in g.dependencies]}")


def print_gate_node_groups(gates):
    """
    Prints each gate as a single group containing all of its nodes
    (dirty + xor + clean combined), mirroring the earlier `print_groups`
    style for compute/uncompute sequences.
    """
    print(f"\n{len(gates)} gate group(s):")
    for g in gates:
        names = [n.name for n in g.all_nodes()]
        print(f"  Gate {g.gid}: {names}")


# ---------------------------------------------------------------------------
# Gate-level pebbling (max simultaneously pebbled AND-gates <= max_pebbles;
# pure-XOR gates are exempt from the cap)
# ---------------------------------------------------------------------------

class GatePebbleSolver:
    """
    Pebbles whole GateGroup objects under dependency constraints.
    A gate can toggle only if all producer gates for its dependencies are pebbled.

    XOR-only gates (gates whose clean_pebble and dirty_pebble lists are both
    empty, i.e. they contain only xor_nodes) do NOT count against
    `max_pebbles`, since Rule 5's CNOT-cascade gadget needs no ancilla.
    They are still tracked/toggled for dependency-ordering purposes.
    """

    def __init__(self, gates, max_pebbles):
        self.gates = gates
        self.max_pebbles = max_pebbles
        self.slv = Solver()
        self.num_steps = 0
        self.current = {}
        self.history = []
        self.model = None

        # a gate counts toward the pebble cap only if it holds AND-type
        # (clean/dirty) nodes; pure XOR gates are exempt
        self.counts_toward_limit = {
            g.gid: bool(g.clean_pebble or g.dirty_pebble) for g in gates
        }

        # map node -> producer gate (first gate that contains it)
        self.node_owner = {}
        for g in gates:
            for n in g.clean_pebble + g.dirty_pebble + g.xor_nodes:
                self.node_owner.setdefault(n, g.gid)

        # gate dependencies by gid
        self.gate_deps = defaultdict(set)
        for g in gates:
            for n in g.dependencies:
                for fi in n.fanins:
                    if fi in self.node_owner:
                        src_gid = self.node_owner[fi]
                        if src_gid != g.gid:
                            self.gate_deps[g.gid].add(src_gid)

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

        # only gates that actually hold clean/dirty (AND) pebbles count
        # toward the simultaneous-pebble cap
        limited_gates = [g for g in self.gates if self.counts_toward_limit[g.gid]]
        if limited_gates:
            self.slv.add(
                PbLe([(s_next[g.gid], 1) for g in limited_gates], self.max_pebbles)
            )

        self.current = {gid: (s_next[gid], a_next[gid]) for gid in s_next}
        self.history.append(dict(self.current))

    def solve(self):
        self.slv.push()
        # final condition: all gates off (cleaned)
        for g in self.gates:
            s_cur, _ = self.current[g.gid]
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
                  auto_increase_pebbles=True, max_pebbles_cap=None):
    """
    Attempts gate-level pebbling with at most `max_pebbles` AND-gates
    pebbled simultaneously (XOR-only gates are exempt).

    `max_pebbles_cap` (default: total number of gate groups in `gates`,
    i.e. `len(gates)`): the ceiling for auto-escalation. Always widened
    (if needed) to be at least the requested `max_pebbles`.

    `max_steps` controls the incremental search cap:
      - "auto" (default): cap = 4 * (number of gates in `gates`).
      - None: no cap.
      - int: explicit cap.

    `auto_increase_pebbles` (default True): if `max_pebbles` is
    infeasible, increments it by 1 and retries, up to `max_pebbles_cap`.

    Raises `RuntimeError` if no feasible solution is found for any
    pebble count tried.
    """
    if max_pebbles_cap is None:
        max_pebbles_cap = max(1, len(gates))
    # never let the cap be smaller than the requested starting point --
    # otherwise the search loop below would never even try `max_pebbles`.
    max_pebbles_cap = max(max_pebbles_cap, max_pebbles)

    current_pebbles = max_pebbles
    while current_pebbles <= max_pebbles_cap:
        step_cap = max_steps
        if step_cap == "auto":
            step_cap = max(4, len(gates) * 4)

        s = GatePebbleSolver(gates, current_pebbles)
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
        f"{max_pebbles} up to max_pebbles_cap={max_pebbles_cap}. Pass an "
        f"explicit larger max_pebbles_cap if this network genuinely needs "
        f"more pebbles."
    )


# ---------------------------------------------------------------------------
# Gate/gadget synthesis (Toffoli/T-gate decomposition per synthesis table)
# ---------------------------------------------------------------------------
#
# Implements the mapping described in the reversible-pebbling synthesis
# table (Meuli et al.), which maps each AND/XOR node in the logic network
# to a quantum gadget depending on:
#   - whether its fanins are primary inputs or fanins from other nodes
#   - whether its output(s) are primary outputs or intermediate
#   - whether it fans out into an XOR node that is itself a primary output
#   - whether it has multiple fanouts
#
# Each generated gadget is returned as an ordered list of `QOp` operations
# (Toffoli, CNOT, Hadamard, T, T-dagger), operating on named qubit labels
# derived from the node's own signals (A, B, F, ancilla a, target f, etc).
# This does not perform actual circuit simulation - it produces a
# structural description of which gadget applies and what gate sequence
# it expands to, mirroring the table's "Gadget" column.


class QOp:
    """A single symbolic quantum operation."""

    def __init__(self, kind, targets, controls=None):
        self.kind = kind            # "H", "T", "Tdg", "CNOT", "TOFFOLI"
        self.targets = targets      # list[str] target qubit label(s)
        self.controls = controls or []  # list[str] control qubit label(s)

    def __repr__(self):
        if self.controls:
            return f"{self.kind}(controls={self.controls}, targets={self.targets})"
        return f"{self.kind}({self.targets})"


class Gadget:
    """The gadget selected for a node: rule number + op sequence."""

    def __init__(self, rule, node, ops, description):
        self.rule = rule
        self.node = node
        self.ops = ops
        self.description = description

    def __repr__(self):
        return f"Gadget(rule={self.rule}, node={self.node.name}, ops={len(self.ops)})"

    def print_ops(self):
        print(f"Node {self.node.name} -> Rule {self.rule}: {self.description}")
        for op in self.ops:
            print(f"    {op}")


def _is_xor_output_po(node, children, pos_set):
    """True if node fans out into an XOR node that is itself a primary output."""
    for ch in children.get(node, []):
        if ch.is_xor and ch in pos_set:
            return True
    return False


def _fanin_kinds(node):
    """Returns (a_is_pi, b_is_pi) for a 2-input node's two fanins."""
    if len(node.fanins) != 2:
        return None
    a, b = node.fanins
    return a.is_pi, b.is_pi


def select_gadget_rule(node, net, children):
    """
    Classifies `node` against the six table rules and returns the rule
    number (int) that applies.
    """
    pos_set = set(net.pos)
    fanouts = children.get(node, [])

    if node.is_xor:
        return 5  # Rule 5: any XOR node

    kinds = _fanin_kinds(node)  # (a_is_pi, b_is_pi) or None if not 2-input
    multiple_fanouts = len(fanouts) > 1

    # Rule 4 (AND node with multiple fanouts) takes priority when applicable
    if multiple_fanouts:
        return 4  # duplicated "#4" row: AND node with multiple fanouts

    is_po = node in pos_set
    xor_po_fanout = _is_xor_output_po(node, children, pos_set)
    if is_po or xor_po_fanout:
        return 4  # F is a primary output, or fans into an XOR primary output

    if kinds is not None:
        a_is_pi, b_is_pi = kinds
        if a_is_pi and b_is_pi:
            return 3  # both A and B are primary inputs
        if a_is_pi != b_is_pi:
            return 1  # one of A, B is a PI, the other is a fanin
        if not a_is_pi and not b_is_pi:
            return 2  # both A and B are fanins from other nodes

    # fallback: treat as rule 1 if ambiguous / single-fanin node
    return 1


def _gate_labels(node):
    """Derives symbolic qubit labels for a node's A/B/F/a/f wires."""
    a_label = node.fanins[0].name if len(node.fanins) > 0 else "A"
    b_label = node.fanins[1].name if len(node.fanins) > 1 else "B"
    f_label = f"f_{node.name}"
    anc_label = f"a_{node.name}"
    return a_label, b_label, f_label, anc_label


def generate_gate(node, net, children=None):
    """
    Generates the gadget (rule + op sequence) for `node` according to the
    synthesis table. Returns a `Gadget` instance.
    """
    if children is None:
        children = net.build_children()

    rule = select_gadget_rule(node, net, children)
    a, b, f, anc = _gate_labels(node)
    ops = []

    if rule == 1:
        # One of A/B is a PI, the other a fanin; F are intermediate fanouts.
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
        # Both A and B are fanins from other nodes.
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
        # A and B are primary inputs: canonical Toffoli-to-T-gate decomposition.
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
        # F is a primary output (or feeds an XOR that is a PO), or AND node
        # has multiple fanouts.
        ops = [
            QOp("TOFFOLI", targets=[f], controls=[a, b]),
            QOp("CNOT", targets=[anc], controls=[a]),
            QOp("CNOT", targets=[f], controls=[anc]),
        ]
        description = "F is a primary output / feeds PO XOR, or AND has multiple fanouts"

    elif rule == 5:
        # Any XOR node: simple CNOT cascade B -> F, A -> F.
        ops = [
            QOp("CNOT", targets=[f], controls=[b]),
            QOp("CNOT", targets=[f], controls=[a]),
        ]
        description = "XOR node: CNOT cascade"

    else:
        raise ValueError(f"Unknown rule {rule} for node {node.name}")

    return Gadget(rule, node, ops, description)


def generate_all_gates(net):
    """
    Runs `generate_gate` over every non-PI node in the network and returns
    a list of Gadget objects (one per gate).
    """
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
# Demo
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    def _parse_max_steps(value):
        """Accepts 'auto', 'none' (case-insensitive), or an integer."""
        v = value.strip().lower()
        if v == "auto":
            return "auto"
        if v == "none":
            return None
        try:
            return int(value)
        except ValueError:
            raise argparse.ArgumentTypeError(
                f"invalid --max-steps value: {value!r} "
                f"(expected 'auto', 'none', or an integer)"
            )

    parser = argparse.ArgumentParser(
        description="Z3-based reversible pebbling solver demo."
    )
    parser.add_argument(
        "--max-pebbles", type=int, default=5,
        help="Starting pebble count to try for the pebbling examples "
             "(default: 5). Auto-escalates if infeasible. For the "
             "random-DAG examples, this is automatically widened (if "
             "needed) to at least the number of POs in the generated "
             "network."
    )
    parser.add_argument(
        "--max-steps", type=_parse_max_steps, default="auto",
        help="Step cap for the solver: an integer, 'auto' (default, "
             "= 4 * number of nodes/gates), or 'none' for unbounded "
             "search per pebble-count attempt."
    )
    args = parser.parse_args()

    pebble_limit = args.max_pebbles
    max_steps = args.max_steps

    # --- Example 1: hand-built network ---------------------------------
    net = PebblingNetwork()
    p1 = net.create_pi("p1")
    p2 = net.create_pi("p2")
    p3 = net.create_pi("p3")
    n1 = net.create_xor_gate([p1, p2], "n1")   # xor(p1, p2)
    n2 = net.create_and_gate([n1, p3], "n2")   # and(n1, p3)
    net.create_po(n2)

    print("=" * 60)
    print("Example 1: hand-built network")
    print("=" * 60)

    print("\nDAG input:")
    net.print_summary()

    steps = pebble(net, pebbles=pebble_limit, max_steps=max_steps)
    groups = group_pebbles(steps, pebble_limit)
    print_groups(groups)

    gadgets = generate_all_gates(net)
    print_all_gadgets(gadgets)

    gates_1 = build_gate_groups(net, max_pebbles=pebble_limit)
    print_gate_node_groups(gates_1)
    display_gate_groups(gates_1)

    # --- Example 2: random DAG -------------------------------------------
    # `random_dag` makes every sink node a PO, so the resulting PO count
    # is data-dependent. Compute a per-network effective pebble limit
    # that's widened (if needed) to cover however many POs this
    # particular generated network ends up with, so a smaller
    # user-requested --max-pebbles doesn't hit the max_pebbles >=
    # len(net.pos) validation error.
    print("\n" + "=" * 60)
    print("Example 2: random DAG")
    print("=" * 60)
    seed = 42
    while True:
        rnd_net = random_dag(num_pis=6, num_gates=16, max_fanin=2,
                              seed=seed, xor_prob=0.4)
        rnd_pebble_limit = max(pebble_limit, len(rnd_net.pos))
        gates = build_gate_groups(rnd_net, max_pebbles=rnd_pebble_limit)
        if len(gates) > 3:
            break
        seed += 1

    print("\nDAG input:")
    rnd_net.print_summary()
    if rnd_pebble_limit != pebble_limit:
        print(f"\nNote: this network has {len(rnd_net.pos)} POs, so the "
              f"effective pebble limit for these examples was widened "
              f"from {pebble_limit} to {rnd_pebble_limit}.")

    steps = pebble(rnd_net, pebbles=rnd_pebble_limit, max_steps=max_steps)
    groups = group_pebbles(steps, rnd_pebble_limit)
    print_groups(groups)

    # --- Example 3: gate-group traversal + gate-level pebbling ----------
    print("\n" + "=" * 60)
    print("Example 3: gate-group traversal + gate-level pebbling")
    print("=" * 60)
    print_gate_node_groups(gates)
    display_gate_groups(gates)

    gate_steps = pebble_gates(gates, max_pebbles=rnd_pebble_limit, max_steps=max_steps)

    # --- Example 4: auto-find minimum pebble count -----------------------
    print("\n" + "=" * 60)
    print("Example 4: find_min_pebbles on the random DAG")
    print("=" * 60)
    min_p, min_steps = find_min_pebbles(rnd_net, start=1, max_steps=max_steps, verbose=True)

    # --- Example 5: BLIF file --------------------------------------------
    # Uncomment and point at a real .blif file to try this out. As with
    # the random DAG above, compute an effective pebble limit for THIS
    # network (widened to at least len(blif_net.pos)) rather than
    # assuming pebble_limit is sufficient.
    #
    # blif_net = read_blif("example.blif")
    # print("\nDAG input:")
    # blif_net.print_summary()
    # blif_pebble_limit = max(pebble_limit, len(blif_net.pos))
    # steps = pebble(blif_net, pebbles=blif_pebble_limit, max_steps=max_steps)
    # groups = group_pebbles(steps, blif_pebble_limit)
    # print_groups(groups)
    # gates = build_gate_groups(blif_net, max_pebbles=blif_pebble_limit)
    # print_gate_node_groups(gates)
    # display_gate_groups(gates)
    # pebble_gates(gates, max_pebbles=blif_pebble_limit, max_steps=max_steps)
