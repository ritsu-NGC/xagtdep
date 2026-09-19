"""
Demo: random n-PI, m-PO Boolean bit mapping, Reed-Muller decomposed with
priority-cut-style node sharing, run through the full gate-grouping /
gate-level pebbling / qubit-assignment pipeline.

Sizing notes:
  - `NUM_VARS`/`NUM_OUTPUTS` default to a SMALL problem (4 PIs, 2 POs)
    deliberately, since gate count grows quickly with both: a single
    output can have up to `2**NUM_VARS - 1` monomials before sharing,
    and `GatePebbleSolver`'s incremental Z3 search re-adds constraints
    over ALL gates on every added step. Scale these up gradually (e.g.
    to 6/4) once you've confirmed a small run completes quickly.
  - `gate_limit` scales with the actual gate count instead of a fixed
    small constant, to avoid forcing an unnecessarily long incremental
    search for larger networks.
  - `max_steps="auto"` (NOT `None`) bounds the incremental step search
    to `4 * len(gates)` per pebble-count attempt, so a too-tight
    `gate_limit` fails fast (and `pebble_gates` auto-escalates the
    pebble cap, default `auto_increase_pebbles=True`) instead of
    searching indefinitely.
"""

from pebbling_solver import (
    build_reed_muller_network,
    random_boolean_bit_mapping,
    build_gate_groups,
    print_gate_node_groups,
    display_gate_groups,
    print_gate_pebbling_constraints,
    find_gate_dependency_cycle,
    pebble_gates,
    expand_gate_schedule,
    reconstruct_original_graph,
    assign_qubits_to_pebbling,
    print_qubit_allocation,
)
from reed_muller_priority_cuts import build_reed_muller_network_priority_cuts

# Small, fast defaults. Bump these up (e.g. 6/4) once a small run
# completes quickly and you want a more interesting comparison.
NUM_VARS = 4      # n PIs
NUM_OUTPUTS = 2   # m POs
SEED = 7
DENSITY = 0.5

if __name__ == "__main__":
    truth_tables = random_boolean_bit_mapping(
        NUM_VARS, num_outputs=NUM_OUTPUTS, seed=SEED, density=DENSITY
    )
    var_names = [f"x{i}" for i in range(NUM_VARS)]

    net_naive = build_reed_muller_network(
        NUM_VARS, truth_tables, var_names=var_names, share_cuts=True
    )
    net_pcut = build_reed_muller_network_priority_cuts(
        NUM_VARS, truth_tables, var_names=var_names
    )

    print("=" * 60)
    print(f"Random Boolean bit mapping: {NUM_VARS} PIs -> {NUM_OUTPUTS} POs "
          f"(seed={SEED}, density={DENSITY})")
    print("=" * 60)

    print(f"\nGate count, prefix-chain sharing (share_cuts=True): "
          f"{len(net_naive.nodes) - len(net_naive.pis)}")
    print(f"Gate count, priority-cut sharing:                    "
          f"{len(net_pcut.nodes) - len(net_pcut.pis)}")

    print("\nDAG input (priority-cut network):")
    net_pcut.print_summary()

    num_gates_estimate = len(net_pcut.nodes) - len(net_pcut.pis)
    gate_limit = max(len(net_pcut.pos), min(8, num_gates_estimate))

    gates = build_gate_groups(net_pcut, max_pebbles=gate_limit)
    print_gate_node_groups(gates)
    display_gate_groups(gates)

    print_gate_pebbling_constraints(gates, gate_limit, net=net_pcut)
    cycle = find_gate_dependency_cycle(gates)
    print(f"\nGate dependency cycle check: "
          f"{'CYCLE ' + str(cycle) if cycle else 'none (acyclic, as guaranteed)'}")

    # Bounded incremental step search (NOT max_steps=None), so an
    # infeasible gate_limit fails fast and auto-escalates instead of
    # searching forever.
    gate_steps = pebble_gates(gates, max_pebbles=gate_limit, max_steps="auto", net=net_pcut)

    print("\n" + "=" * 60)
    print("Reconstruction check")
    print("=" * 60)
    reconstructed = reconstruct_original_graph(gates, net_pcut.pis, net_pcut.pos)
    original_names = [n.name for n in net_pcut.nodes]
    reconstructed_names = [n.name for n in reconstructed.nodes]
    print(f"Original node count:      {len(original_names)}")
    print(f"Reconstructed node count: {len(reconstructed_names)}")
    print(f"Node sets match: {set(original_names) == set(reconstructed_names)}")

    node_level_steps = expand_gate_schedule(gates, gate_steps)
    print(f"\nExpanded {len(gate_steps)} gate-level steps into "
          f"{len(node_level_steps)} node-level compute/uncompute events.")

    alloc = assign_qubits_to_pebbling(net_pcut, gates, gate_steps)
    print_qubit_allocation(alloc)
