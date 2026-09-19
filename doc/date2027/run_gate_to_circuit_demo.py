"""
Demo: build gate groups, solve gate-level pebbling, expand to node
level, then map that node schedule onto a concrete quantum circuit
using the six-rule synthesis table with real qubit indices.
"""

from pebbling_solver import (
    build_reed_muller_network,
    truth_table_from_function,
    build_gate_groups,
    pebble_gates,
    expand_gate_schedule,
)
from gate_schedule_to_circuit import (
    build_circuit_from_node_schedule,
    print_circuit_ops,
    apply_to_qiskit,
)

if __name__ == "__main__":
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

    gate_limit = max(2, len(net.pos))
    gates = build_gate_groups(net, max_pebbles=gate_limit)
    gate_steps = pebble_gates(gates, max_pebbles=gate_limit, max_steps="auto", net=net)
    node_steps = expand_gate_schedule(gates, gate_steps)

    circuit_result = build_circuit_from_node_schedule(net, node_steps)
    print_circuit_ops(circuit_result)

    try:
        qc = apply_to_qiskit(circuit_result)
        print(f"\nBuilt Qiskit circuit with {qc.num_qubits} qubits, "
              f"{len(qc.data)} instructions.")
    except ImportError:
        print("\n(qiskit not installed; skipping QuantumCircuit construction.)")
