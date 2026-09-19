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
       - on "compute": resolves the node's gadget rule (1-5) via
         `select_gadget_rule`, allocates a concrete qubit for the
         node's own wire (F) and, for rules that need one (1, 2, 4),
         a fresh ANCILLA qubit, then emits the gadget's op sequence
         with symbolic labels (A, B, F, a) substituted for the actual
         qubit indices of the node's fanins / itself / its ancilla.
       - on "uncompute": emits the INVERSE of that same gadget (ops in
         reverse order, T<->Tdg swapped, H/CNOT/TOFFOLI self-inverse),
         then frees the node's own qubit and its ancilla (if any) back
         to the appropriate pool.

Ancilla lifetime: inspecting the gadget diagrams (table image), the
ancilla wire in Rules 1/2/4 is NOT restored to |0> at the end of the
forward gadget (e.g. Rule 1 leaves it as A XOR a) -- it stays entangled
with the node's other wires until the matching UNCOMPUTE gadget (the
adjoint circuit) is run. Consequently ancilla qubits are allocated
together with the node's own output qubit at compute time, and freed
together with it at uncompute time -- they are NOT eagerly freed the
way ordinary "internal/dirty" pebbling nodes are.

Rule 3 (both fanins are PIs) and Rule 5 (XOR) need no ancilla.

Because a node's own wire (F) is simply the concrete qubit already
assigned to that node by `assign_qubits_to_pebbling`'s clean/dirty
allocation policy, this module reuses that SAME qubit allocator so the
two stay consistent -- gadget synthesis does not introduce its own
separate notion of which qubit represents a node's value.
"""

from pebbling_solver import (
    QOp,
    select_gadget_rule,
    _fanin_kinds,
    _is_xor_output_po,
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
        QOp("TOFFOLI", targets=[f], controls=[a, b]),
        QOp("CNOT", targets=[anc], controls=[a]),
        QOp("CNOT", targets=[f], controls=[anc]),
    ]


def _gadget_ops_rule5(a, b, f):
    return [
        QOp("CNOT", targets=[f], controls=[b]),
        QOp("CNOT", targets=[f], controls=[a]),
    ]


_INVERSE_KIND = {
    "T": "Tdg",
    "Tdg": "T",
    "H": "H",
    "CNOT": "CNOT",
    "TOFFOLI": "TOFFOLI",
}


def _invert_ops(ops):
    """Adjoint of a gadget: reverse op order, swap T<->Tdg, leave
    H/CNOT/TOFFOLI unchanged (all self-inverse)."""
    inverted = []
    for op in reversed(ops):
        inverted.append(QOp(_INVERSE_KIND[op.kind], targets=op.targets, controls=op.controls))
    return inverted


# ---------------------------------------------------------------------------
# Node-level replay: resolves symbolic gadget wires to concrete qubits
# ---------------------------------------------------------------------------

class CircuitBuildResult:
    def __init__(self):
        self.ops = []          # flat list of (node, phase, QOp) for inspection
        self.qubit_count = 0
        self.node_qubit = {}   # node -> its own concrete qubit (F wire)
        self.node_ancilla = {}  # node -> ancilla qubit, if it used one


def build_circuit_from_node_schedule(net, node_steps):
    """
    Replays a node-level compute/uncompute sequence (as produced by
    `expand_gate_schedule`) and returns a `CircuitBuildResult` holding
    the flat op list with CONCRETE qubit indices, ready to be lowered
    onto a real quantum circuit (e.g. via `apply_to_qiskit` below).

    `net` is required to re-derive each node's gadget rule (via
    `select_gadget_rule`) and its fanin/fanout structure.
    """
    result = CircuitBuildResult()
    alloc = QubitAllocation()
    children = net.build_children()
    pos_set = set(net.pos)

    # ancilla pool, separate from clean/dirty value pools -- ancilla
    # qubits are pure scratch space with no logical "value" meaning of
    # their own once freed, so they're safe to recycle independently.
    ancilla_free = []

    def alloc_ancilla():
        if ancilla_free:
            return ancilla_free.pop()
        return alloc._new_qubit()

    for pi in net.pis:
        q = alloc._new_qubit()
        alloc.assignment[pi] = q
        result.node_qubit[pi] = q

    node_gadget_cache = {}  # node -> (rule, ops_template_fn, needs_ancilla)

    def get_rule_info(node):
        if node in node_gadget_cache:
            return node_gadget_cache[node]
        rule = select_gadget_rule(node, net, children)
        needs_ancilla = rule in (1, 2, 4)
        node_gadget_cache[node] = (rule, needs_ancilla)
        return rule, needs_ancilla

    for node, action in node_steps:
        if node.is_pi:
            continue

        if action == "compute":
            rule, needs_ancilla = get_rule_info(node)

            # Resolve A/B: for XOR nodes only, a fanin may itself be an
            # aliased XOR with no dedicated qubit -- resolve through
            # aliasing chains just like the pebbling qubit allocator
            # does, for consistency.
            a_fanin = node.fanins[0] if len(node.fanins) > 0 else None
            b_fanin = node.fanins[1] if len(node.fanins) > 1 else None

            def resolve(fanin):
                qs = _resolve_control_qubits(fanin, alloc.assignment)
                if not qs:
                    raise RuntimeError(
                        f"Could not resolve a qubit for fanin '{fanin.name}' "
                        f"of node '{node.name}' -- was it computed yet?"
                    )
                return qs[-1]

            if node.is_xor:
                # XOR aliases one of its fanins' qubits rather than
                # allocating a fresh one -- mirrors
                # assign_qubits_to_pebbling's XOR-aliasing policy.
                target_fanin = node.fanins[-1] if node.fanins else None
                if target_fanin is not None:
                    f = resolve(target_fanin)
                else:
                    f = alloc._alloc_clean()
                alloc.assignment[node] = f
                result.node_qubit[node] = f

                a_q = resolve(a_fanin) if a_fanin is not None else f
                b_q = resolve(b_fanin) if b_fanin is not None else f
                ops = _gadget_ops_rule5(a_q, b_q, f)
                for op in ops:
                    result.ops.append((node, "compute", op))
                continue

            a_q = resolve(a_fanin) if a_fanin is not None else alloc._alloc_dirty()
            b_q = resolve(b_fanin) if b_fanin is not None else alloc._alloc_dirty()

            f = alloc._alloc_clean()
            alloc.assignment[node] = f
            result.node_qubit[node] = f

            if needs_ancilla:
                anc = alloc_ancilla()
                result.node_ancilla[node] = anc

            if rule == 1:
                ops = _gadget_ops_rule1(a_q, b_q, f, result.node_ancilla[node])
            elif rule == 2:
                ops = _gadget_ops_rule2(a_q, b_q, f, result.node_ancilla[node])
            elif rule == 3:
                ops = _gadget_ops_rule3(a_q, b_q, f)
            elif rule == 4:
                ops = _gadget_ops_rule4(a_q, b_q, f, result.node_ancilla[node])
            else:
                raise ValueError(f"Unexpected rule {rule} for AND-like node {node.name}")

            for op in ops:
                result.ops.append((node, "compute", op))

        elif action == "uncompute":
            rule, needs_ancilla = get_rule_info(node)
            f = alloc.assignment.get(node)
            if f is None:
                continue  # already released (e.g. XOR alias bookkeeping only)

            if node.is_xor:
                # Freeing an XOR alias is pure bookkeeping -- no
                # dedicated qubit to release, and no inverse ops to
                # apply (the CNOT cascade is applied once, forward,
                # and is its own adjoint only if replayed symmetrically;
                # since XOR nodes are never uncomputed independently of
                # their consumer in this framework's pebbling model, we
                # simply drop the bookkeeping entry here).
                del alloc.assignment[node]
                continue

            a_fanin = node.fanins[0] if len(node.fanins) > 0 else None
            b_fanin = node.fanins[1] if len(node.fanins) > 1 else None

            def resolve(fanin):
                qs = _resolve_control_qubits(fanin, alloc.assignment)
                return qs[-1] if qs else None

            a_q = resolve(a_fanin)
            b_q = resolve(b_fanin)
            anc = result.node_ancilla.get(node)

            if rule == 1:
                forward = _gadget_ops_rule1(a_q, b_q, f, anc)
            elif rule == 2:
                forward = _gadget_ops_rule2(a_q, b_q, f, anc)
            elif rule == 3:
                forward = _gadget_ops_rule3(a_q, b_q, f)
            elif rule == 4:
                forward = _gadget_ops_rule4(a_q, b_q, f, anc)
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
    """
    Lowers a `CircuitBuildResult` (concrete-qubit op list) onto a real
    Qiskit `QuantumCircuit`.
    """
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
