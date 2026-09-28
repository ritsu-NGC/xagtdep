"""
Example 4-input, 2-output DAG exercising all 5 gadget-rule branches in
select_gadget_rule (pebbling_solver.py):

  n1  = pi0 AND pi1                  -> Rule 4 (multiple fanouts: n3, n4, n6)
  n2  = pi2 AND pi3                  -> Rule 3 (both fanins are PI)
  n3  = n1  AND pi2                  -> Rule 1 (mixed PI/non-PI fanins)
  n4  = n1  XOR n2                   -> Rule 5 (XOR, always rule 5)
  n5  = n3  AND n4  [[PRIMARY OUTPUT]] -> Rule 4 (is_po overrides "both non-PI")
  n6  = n1  AND pi3                  -> Rule 1 (mixed PI/non-PI fanins)
  n7  = n6  AND n2                   -> Rule 2 (both fanins non-PI, non-PO,
                                                 non-XOR-PO-feeding consumer)
  n9  = n7  AND pi0                  -> Rule 4 (xor_po_fanout: sole consumer
                                                 n10 is an XOR that is a PO)
  n10 = n9  XOR pi1 [[PRIMARY OUTPUT]] -> Rule 5 (XOR, always rule 5; NOT
                                                    counted in and_pos)

Outputs: PO1 = n5 (AND-driven -> counted in and_pos), PO2 = n10
(XOR-driven -> not counted in and_pos). Expect and_pos == 1.

Run this file directly to print the network summary, generated gadgets
(with resolved rule per node), gate groups, cycle check, and final
T-count/qubit-count via the existing pebbling_solver / gate_schedule_to_
circuit pipeline. Use --tikz to also dump a standalone TikZ picture of
the DAG, with gate-group boundaries overlaid (--no-group-boxes to
disable).
"""

import argparse
from collections import defaultdict

from pebbling_solver import (
    PebblingNetwork,
    generate_all_gates,
    print_all_gadgets,
    build_gate_groups,
    display_gate_groups,
    print_gate_pebbling_constraints,
    find_gate_dependency_cycle,
    pebble_gates,
    expand_gate_schedule,
)
from gate_schedule_to_circuit import build_circuit_from_node_schedule


def build_example_dag():
    """Constructs and returns the 4-input/2-output example PebblingNetwork
    described in the module docstring."""
    net = PebblingNetwork()
    pi0, pi1, pi2, pi3 = (net.create_pi(f"pi{i}") for i in range(4))

    n1 = net.create_and_gate([pi0, pi1], name="n1")
    n2 = net.create_and_gate([pi2, pi3], name="n2")
    n3 = net.create_and_gate([n1, pi2], name="n3")
    n4 = net.create_xor_gate([n1, n2], name="n4")
    n5 = net.create_and_gate([n3, n4], name="n5")
    n6 = net.create_and_gate([n1, pi3], name="n6")
    n7 = net.create_and_gate([n6, n2], name="n7")
    n9 = net.create_and_gate([n7, pi0], name="n9")
    n10 = net.create_xor_gate([n9, pi1], name="n10")

    net.create_po(n5)
    net.create_po(n10)

    return net


# ---------------------------------------------------------------------------
# TikZ dump
# ---------------------------------------------------------------------------

# A small fixed palette for gate-group boxes, cycled if there are more
# groups than colors. All are TikZ/xcolor named colors so no \definecolor
# is required.
_GROUP_COLORS = [
    "red", "teal", "violet", "orange", "cyan",
    "magenta", "olive", "purple", "lime", "brown",
]


def _compute_layers(net: PebblingNetwork):
    """
    Assigns each node an integer layer: PIs are layer 0; every other node
    is placed one layer past the deepest (max-layer) fanin. Since
    net.nodes is already topological, a single left-to-right pass
    suffices (all fanins are guaranteed to already have a layer
    assigned by the time we reach a given node).
    """
    layer = {}
    for n in net.nodes:
        if n.is_pi:
            layer[n] = 0
        else:
            layer[n] = 1 + max(layer[f] for f in n.fanins)
    return layer


def _compute_positions(net: PebblingNetwork, x_spacing, y_spacing):
    """
    Returns {node: (x, y)} for every node in `net`, using the same
    layer-by-topological-depth / centered-within-layer scheme used by
    `dump_tikz`. Factored out so gate-group bounding boxes can be
    computed from the exact same coordinates used to place the nodes.
    """
    layer = _compute_layers(net)

    nodes_by_layer = defaultdict(list)
    for n in net.nodes:
        nodes_by_layer[layer[n]].append(n)

    pos = {}
    for lay, nodes_in_layer in nodes_by_layer.items():
        count = len(nodes_in_layer)
        start = -(count - 1) / 2.0
        for i, n in enumerate(nodes_in_layer):
            x = lay * x_spacing
            y = (start + i) * y_spacing
            pos[n] = (x, y)
    return pos


def _safe_name(n):
    # TikZ node ids can't contain some characters that node names might
    # have; node names here are already identifier-safe, but guard
    # against stray characters just in case.
    return "n_" + "".join(c if c.isalnum() or c == "_" else "_" for c in n.name)


def dump_tikz(net: PebblingNetwork, path=None, x_spacing=2.2, y_spacing=1.4,
              standalone=True, gates=None, group_box_padding=0.55):
    """
    Renders `net` as a TikZ picture (string), and optionally writes it to
    `path`. Nodes are auto-positioned: x-coordinate by topological layer
    (`_compute_layers`), y-coordinate by within-layer order (so parallel
    nodes at the same "depth" are stacked vertically without overlap).

    Node styling:
      - PI nodes:  circle, "pi" style
      - AND nodes: rectangle, "and" style
      - XOR nodes: diamond, "xor" style
      - Primary outputs get a double border (regardless of AND/XOR kind)

    If `gates` is provided (a list of GateGroup objects, e.g. from
    `build_gate_groups`), a labeled dashed bounding box is drawn behind
    the nodes for each group, colored from a small fixed palette
    (cycled if there are more groups than colors), with a
    "Gate <gid>" label in the box's top-left corner. The box is sized
    directly from the min/max node coordinates of that group's owned
    nodes (plus `group_box_padding`), so it always exactly encloses
    that group's nodes regardless of layout changes.

    Edges are drawn as directed arrows from each fanin to its consumer.

    Returns the TikZ source as a string.
    """
    pos = _compute_positions(net, x_spacing, y_spacing)
    pos_set = set(net.pos)

    def node_style(n):
        if n.is_pi:
            base = "pinode"
        elif n.is_xor:
            base = "xornode"
        else:
            base = "andnode"
        if n in pos_set:
            return base + ",ponode"
        return base

    lines = []

    if standalone:
        lines.append(r"\documentclass[tikz,border=5pt]{standalone}")
        lines.append(r"\usetikzlibrary{arrows.meta,positioning}")
        lines.append(r"\begin{document}")

    lines.append(r"\begin{tikzpicture}[")
    lines.append(r"  pinode/.style={circle,draw,minimum size=8mm,fill=gray!15},")
    lines.append(r"  andnode/.style={rectangle,draw,minimum size=8mm,fill=blue!10},")
    lines.append(r"  xornode/.style={diamond,draw,minimum size=9mm,fill=orange!15,")
    lines.append(r"                  aspect=1.6},")
    lines.append(r"  ponode/.style={double,double distance=1pt},")
    lines.append(r"  >={Stealth[length=2mm]},")
    lines.append(r"  every path/.style={-> ,thick},")
    lines.append(r"]")

    # --- Gate-group bounding boxes (drawn first, so nodes render on top) ---
    if gates:
        lines.append("")
        lines.append("  % Gate-group boundaries")
        for i, g in enumerate(gates):
            if not g.nodes:
                continue
            color = _GROUP_COLORS[i % len(_GROUP_COLORS)]
            xs = [pos[n][0] for n in g.nodes]
            ys = [pos[n][1] for n in g.nodes]
            # Node half-width/height roughly matches minimum size=8-9mm
            # (~0.45cm radius); pad a bit further so the box clearly
            # encloses each node's full shape, not just its center.
            x_min = min(xs) - group_box_padding
            x_max = max(xs) + group_box_padding
            y_min = min(ys) - group_box_padding
            y_max = max(ys) + group_box_padding
            lines.append(
                f"  \\draw[{color},dashed,thick,rounded corners=4pt] "
                f"({x_min:.2f},{y_min:.2f}) rectangle ({x_max:.2f},{y_max:.2f});"
            )
            lines.append(
                f"  \\node[{color},anchor=north west,font=\\scriptsize\\bfseries] "
                f"at ({x_min:.2f},{y_max:.2f}) {{Gate {g.gid}}};"
            )
        lines.append("")

    # --- Nodes ---
    for n in net.nodes:
        x, y = pos[n]
        label = n.name.replace("_", r"\_")
        lines.append(
            f"  \\node[{node_style(n)}] ({_safe_name(n)}) at ({x:.2f},{y:.2f}) "
            f"{{{label}}};"
        )

    # --- Edges ---
    lines.append("")
    for n in net.nodes:
        for fanin in n.fanins:
            lines.append(f"  \\draw ({_safe_name(fanin)}) -- ({_safe_name(n)});")

    lines.append(r"\end{tikzpicture}")

    if standalone:
        lines.append(r"\end{document}")

    tikz_source = "\n".join(lines) + "\n"

    if path is not None:
        with open(path, "w") as f:
            f.write(tikz_source)

    return tikz_source


def main():
    parser = argparse.ArgumentParser(
        description="Build and inspect the example 4-input/2-output DAG "
                    "exercising all select_gadget_rule branches."
    )
    parser.add_argument("--max-pebbles", type=int, default=3,
                         help="Pebble budget for build_gate_groups/pebble_gates.")
    parser.add_argument("--tikz", default=None,
                         help="If given, write a standalone TikZ picture of "
                              "the DAG to this path (e.g. example_dag.tex).")
    parser.add_argument("--tikz-fragment", action="store_true",
                         help="With --tikz, emit only the tikzpicture "
                              "environment (no \\documentclass/\\begin{document} "
                              "wrapper), suitable for \\input{} into an "
                              "existing LaTeX document.")
    parser.add_argument("--no-group-boxes", action="store_true",
                         help="With --tikz, don't overlay gate-group "
                              "bounding boxes (they are drawn by default, "
                              "using the same --max-pebbles gate groups "
                              "computed for the rest of this script's "
                              "output).")
    args = parser.parse_args()

    net = build_example_dag()

    net.print_summary()

    gadgets = generate_all_gates(net)
    print_all_gadgets(gadgets)

    gates = build_gate_groups(net, max_pebbles=args.max_pebbles)
    display_gate_groups(gates)
    print_gate_pebbling_constraints(gates, args.max_pebbles, net=net)

    cycle = find_gate_dependency_cycle(gates)
    print(f"\nCycle check: "
          f"{'CYCLE ' + str(cycle) if cycle else 'none (acyclic, guaranteed)'}")

    gate_steps = pebble_gates(gates, max_pebbles=args.max_pebbles, net=net, verbose=True)
    node_steps = expand_gate_schedule(gates, gate_steps)
    circuit_result = build_circuit_from_node_schedule(net, node_steps)

    t_count = sum(
        1 for (_n, _p, op) in circuit_result.ops if op.kind in ("T", "Tdg")
    )
    print(f"\nTotal qubits: {circuit_result.qubit_count}, T-count: {t_count}, "
          f"total ops: {len(circuit_result.ops)}")

    if args.tikz:
        tikz_source = dump_tikz(
            net, path=args.tikz,
            standalone=not args.tikz_fragment,
            gates=None if args.no_group_boxes else gates,
        )
        print(f"\nWrote TikZ picture to {args.tikz} "
              f"({'fragment' if args.tikz_fragment else 'standalone document'}, "
              f"{'no ' if args.no_group_boxes else ''}group boxes, "
              f"{len(tikz_source)} chars)")


if __name__ == "__main__":
    main()
