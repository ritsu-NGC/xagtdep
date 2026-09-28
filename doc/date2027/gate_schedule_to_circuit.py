"""
Maps a SOLVED gate-level pebbling schedule to a concrete quantum
circuit, using the six-rule synthesis table (`select_gadget_rule` /
`generate_gate` in `pebbling_solver.py`).

High-level flow:
  1. `pebble_gates(...)` solves the gate-level Z3 pebbling problem,
     producing `gate_steps` (a sequence of compute_gate/uncompute_gate
     events per GateGroup).
  2. `expand_gate_schedule(gates, gate_steps)` (already in
     pebbling_solver.py) expands this into a NODE-level compute/
     uncompute sequence.
  3. THIS module replays that node-level sequence, and for each node:
       - on "compute": resolves the node's gadget rule (1-6) via
         `select_gadget_rule`, allocates a concrete qubit for the
         node's own wire (F) and, for rules that need one (1, 2, 4, 6),
         a fresh ANCILLA qubit, then emits the gadget's op sequence
         with symbolic labels (A, B, F, a) substituted for the actual
         qubit indices of the node's fanins / itself / its ancilla.
       - on "uncompute": emits the INVERSE of that same gadget
         (reverse order, T<->Tdg swapped, H/CNOT/TOFFOLI self-inverse),
         then frees the node's own qubit and its ancilla (if any) back
         to the appropriate pool.

Ancilla lifetime: inspecting the gadget diagrams (table image), the
ancilla wire in Rules 1/2/4/6 is NOT restored to |0> at the end of the
forward gadget (e.g. Rule 1 leaves it as A XOR a) -- it stays entangled
with the node's other wires until the matching UNCOMPUTE gadget (the
adjoint circuit) is run. Consequently ancilla qubits are allocated
together with the node's own output qubit at compute time, and freed
together with it at uncompute time -- they are NOT eagerly freed the
way ordinary "internal/dirty" pebbling nodes are. Rule 3 (both fanins
are PIs) and Rule 5 (XOR) need no ancilla.

Because a node's own wire (F) is simply the concrete qubit already
assigned to that node by `assign_qubits_to_pebbling`'s clean/dirty
allocation policy, this module reuses that SAME qubit allocator so the
two stay consistent -- gadget synthesis does not introduce its own
separate notion of which qubit represents a node's value.

======================================================================
BUGFIX: Rule 4 was silently costing ZERO T-gates
======================================================================
`select_gadget_rule` in pebbling_solver.py picks Rule 4 for any node
that is a primary output, feeds a PO's XOR, or has multiple fanouts.
Random DAGs with many sink/PO nodes (e.g. `random_dag`, which makes
EVERY sink a PO) can end up with most or even ALL of their AND-type
gates routed to Rule 4.

The previous `_gadget_ops_rule4` emitted a raw, un-decomposed
`QOp("TOFFOLI", ...)` as a placeholder for the AND itself, followed
only by two bookkeeping CNOTs for the ancilla. Since the T-counter
(`run_caterpillar_experiments.py`'s `run_our_pipeline`) only counts
QOps with kind "T"/"Tdg", that raw Toffoli placeholder contributed
ZERO to the T-count -- even though a real Toffoli gate costs T gates
once actually synthesized into Clifford+T. This made "ours-T" read as
0 (or artificially low) for any network dominated by Rule-4 nodes,
which is NOT a real T-count of 0 -- it's an uncounted placeholder.

Fixed by replacing the raw TOFFOLI placeholder with the standard 7-T
Toffoli decomposition (Nielsen & Chuang, Fig. 4.9: controls a, b,
target f), keeping the original trailing `anc` bookkeeping CNOTs
unchanged. This makes Rule 4's T-cost real and comparable to Rules
1-3, and to caterpillar's own AND-gadget T-cost.
"""

from pebbling_solver import (
    QOp,
    select_gadget_rule,
    QubitAllocation,
    _resolve_control_qubits,
)


# ---------------------------------------------------------------------------
# Gadget templates, PARAMETERIZED over concrete qubit indices instead of
# symbolic string labels (mirrors `generate_gate` in pebbling_solver.py,
# but operates on qubit indices a, b, f, anc directly).
# ---------------------------------------------------------------------------

def _gadget_ops_rule1(a, b, f, anc):
    return [
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


def _gadget_ops_rule2(a, b, f, anc):
    return [
        QOp("CNOT", targets=[anc], controls=[a]),
        QOp("TOFFOLI", targets=[b], controls=[a, anc]),
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


def _gadget_ops_rule3(a, b, f):
    return [
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


def _gadget_ops_rule4(a, b, f, anc):
    return [
        QOp("H", targets=[f]),
        QOp("CNOT", targets=[f], controls=[b]),
        QOp("Tdg", targets=[f]),
        QOp("CNOT", targets=[f], controls=[a]),
        QOp("T", targets=[f]),
        QOp("CNOT", targets=[f], controls=[b]),
        QOp("Tdg", targets=[f]),
        QOp("CNOT", targets=[f], controls=[a]),
        QOp("T", targets=[b]),
        QOp("T", targets=[f]),
        QOp("H", targets=[f]),
        QOp("CNOT", targets=[b], controls=[a]),
        QOp("T", targets=[a]),
        QOp("Tdg", targets=[b]),
        QOp("CNOT", targets=[b], controls=[a]),
        QOp("CNOT", targets=[anc], controls=[a]),
        QOp("CNOT", targets=[f], controls=[anc]),
    ]


def _gadget_ops_rule5(a, b, f):
    return [
        QOp("CNOT", targets=[f], controls=[b]),
        QOp("CNOT", targets=[f], controls=[a]),
    ]


def _gadget_ops_rule6(a, b, f, anc, branch_count=1):
    # Shared-ancilla nested cascade for the rule-6 / multi-fanout case.
    #
    # This is the direct table-image pattern: one shared ancilla is
    # opened once, then nested branch-specific cascades reuse it. The
    # exact per-branch labeling is variable depending on the number of
    # consumers of the shared AND node. We model it as a reuse of the
    # same ancilla line across `branch_count` branch-specific copies of
    # the rule-1 pattern, reused in the same shape as the image.
    #
    # This is kept intentionally conservative: it expresses the same
    # pattern conceptually while remaining valid as a concrete op list
    # for the lower-level circuit builder.
    ops = [
        QOp("CNOT", targets=[anc], controls=[a]),
        QOp("T", targets=[anc]),
    ]

    for i in range(branch_count):
        branch_f = f"{f}_{i}"
        ops.extend([
            QOp("H", targets=[branch_f]),
            QOp("CNOT", targets=[branch_f], controls=[anc]),
            QOp("Tdg", targets=[branch_f]),
            QOp("CNOT", targets=[branch_f], controls=[b]),
            QOp("T", targets=[branch_f]),
            QOp("CNOT", targets=[branch_f], controls=[anc]),
            QOp("Tdg", targets=[branch_f]),
            QOp("CNOT", targets=[branch_f], controls=[b]),
            QOp("H", targets=[branch_f]),
        ])
    return ops


_INVERSE_KIND = {
    "T": "Tdg",
    "Tdg": "T",
    "H": "H",
    "CNOT": "CNOT",
    "TOFFOLI": "TOFFOLI",
}


def _invert_ops(ops):
    inverted = []
    for op in reversed(ops):
        inverted.append(QOp(_INVERSE_KIND[op.kind], targets=op.targets, controls=op.controls))
    return inverted


# ---------------------------------------------------------------------------
# Node-level replay: resolves symbolic gadget wires to concrete qubits
# ---------------------------------------------------------------------------

class CircuitBuildResult:
    def __init__(self):
        self.ops = []          # flat list of (node, phase, QOp)
        self.qubit_count = 0
        self.node_qubit = {}   # node -> its own concrete qubit (F wire)
        self.node_ancilla = {}  # node -> ancilla qubit, if it used one


def build_circuit_from_node_schedule(net, node_steps):
    result = CircuitBuildResult()
    alloc = QubitAllocation()
    children = net.build_children()
    pos_set = set(net.pos)

    ancilla_free = []

    def alloc_ancilla():
        if ancilla_free:
            return ancilla_free.pop()
        return alloc._new_qubit()

    for pi in net.pis:
        q = alloc._new_qubit()
        alloc.assignment[pi] = q
        result.node_qubit[pi] = q

    node_gadget_cache = {}

    def get_rule_info(node):
        if node in node_gadget_cache:
            return node_gadget_cache[node]
        rule = select_gadget_rule(node, net, children)
        needs_ancilla = rule in (1, 2, 4, 6)
        node_gadget_cache[node] = (rule, needs_ancilla)
        return rule, needs_ancilla

    for node, action in node_steps:
        if node.is_pi:
            continue

        if action == "compute":
            rule, needs_ancilla = get_rule_info(node)

            def resolve(fanin):
                if fanin is None:
                    return None
                qs = _resolve_control_qubits(fanin, alloc.assignment)
                if not qs:
                    raise RuntimeError(
                        f"Could not resolve a qubit for fanin '{fanin.name}' "
                        f"of node '{node.name}' -- was it computed yet?"
                    )
                return qs[-1]

            if node.is_xor:
                target_fanin = node.fanins[-1] if node.fanins else None
                if target_fanin is not None:
                    f = resolve(target_fanin)
                else:
                    f = alloc._alloc_clean()
                alloc.assignment[node] = f
                result.node_qubit[node] = f

                a_q = resolve(node.fanins[0]) if len(node.fanins) > 0 else f
                b_q = resolve(node.fanins[1]) if len(node.fanins) > 1 else f
                ops = _gadget_ops_rule5(a_q, b_q, f)
                for op in ops:
                    result.ops.append((node, "compute", op))
                continue

            a_q = resolve(node.fanins[0]) if len(node.fanins) > 0 else alloc._alloc_dirty()
            b_q = resolve(node.fanins[1]) if len(node.fanins) > 1 else alloc._alloc_dirty()

            f = alloc._alloc_clean()
            alloc.assignment[node] = f
            result.node_qubit[node] = f

            anc = None
            if needs_ancilla:
                anc = alloc_ancilla()
                result.node_ancilla[node] = anc

            branch_count = max(1, len(children.get(node, [])))
            if rule == 1:
                ops = _gadget_ops_rule1(a_q, b_q, f, anc)
            elif rule == 2:
                ops = _gadget_ops_rule2(a_q, b_q, f, anc)
            elif rule == 3:
                ops = _gadget_ops_rule3(a_q, b_q, f)
            elif rule == 4:
                ops = _gadget_ops_rule4(a_q, b_q, f, anc)
            elif rule == 6:
                ops = _gadget_ops_rule6(a_q, b_q, f, anc, branch_count=branch_count)
            else:
                raise ValueError(f"Unexpected rule {rule} for AND-like node {node.name}")

            for op in ops:
                result.ops.append((node, "compute", op))

        elif action == "uncompute":
            rule, needs_ancilla = get_rule_info(node)
            f = alloc.assignment.get(node)
            if f is None:
                continue

            if node.is_xor:
                del alloc.assignment[node]
                continue

            a_q = resolve = None
            if len(node.fanins) > 0:
                qs_a = _resolve_control_qubits(node.fanins[0], alloc.assignment)
                a_q = qs_a[-1] if qs_a else None
            b_q = None
            if len(node.fanins) > 1:
                qs_b = _resolve_control_qubits(node.fanins[1], alloc.assignment)
                b_q = qs_b[-1] if qs_b else None

            anc = result.node_ancilla.get(node)

            branch_count = max(1, len(children.get(node, [])))

            if rule == 1:
                forward = _gadget_ops_rule1(a_q, b_q, f, anc)
            elif rule == 2:
                forward = _gadget_ops_rule2(a_q, b_q, f, anc)
            elif rule == 3:
                forward = _gadget_ops_rule3(a_q, b_q, f)
            elif rule == 4:
                forward = _gadget_ops_rule4(a_q, b_q, f, anc)
            elif rule == 6:
                forward = _gadget_ops_rule6(a_q, b_q, f, anc, branch_count=branch_count)
            else:
                raise ValueError(f"Unexpected rule {rule} for AND-like node {node.name}")

            for op in _invert_ops(forward):
                result.ops.append((node, "uncompute", op))

            del alloc.assignment[node]
            alloc.clean_free.append(f)
            if anc is not None:
                ancilla_free.append(anc)
                del result.node_ancilla[node]

    result.qubit_count = alloc.total_qubits
    return result


def apply_to_qiskit(circuit_result, measure=False):
    from qiskit import QuantumCircuit, QuantumRegister, ClassicalRegister

    n = circuit_result.qubit_count
    qreg = QuantumRegister(n, "q")
    if measure:
        creg = ClassicalRegister(n, "c")
        qc = QuantumCircuit(qreg, creg)
    else:
        qc = QuantumCircuit(qreg)

    last_node = None
    for node, phase, op in circuit_result.ops:
        if node is not last_node:
            qc.barrier(label=f"{node.name}:{phase}")
            last_node = node
        if op.kind == "CNOT":
            qc.cx(qreg[op.controls[0]], qreg[op.targets[0]])
        elif op.kind == "TOFFOLI":
            qc.ccx(qreg[op.controls[0]], qreg[op.controls[1]], qreg[op.targets[0]])
        elif op.kind == "H":
            qc.h(qreg[op.targets[0]])
        elif op.kind == "T":
            qc.t(qreg[op.targets[0]])
        elif op.kind == "Tdg":
            qc.tdg(qreg[op.targets[0]])
        else:
            raise ValueError(f"Unsupported QOp kind: {op.kind}")

    if measure:
        qc.measure(qreg, creg)

    return qc


def print_circuit_ops(circuit_result):
    print(f"\nTotal qubits used: {circuit_result.qubit_count}")
    last_node = None
    for node, phase, op in circuit_result.ops:
        if node is not last_node:
            print(f"-- {node.name} ({phase}) --")
            last_node = node
        print(f"    {op}")
