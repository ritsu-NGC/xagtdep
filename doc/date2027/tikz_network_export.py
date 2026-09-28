r"""
Dumps a PebblingNetwork's logic DAG (PIs, AND/XOR gates, POs, and their
fanin edges) as a plain TikZ diagram -- NOT a circuit diagram. Each
node is drawn as a labeled box/circle; edges are drawn from each
node's fanins to itself, with an arrowhead at the consumer.

If `gates` (the GateGroup list from `build_gate_groups`) is provided,
every group is drawn with a distinct colored bounding box around its
member nodes, so you can see the pebbling partition directly overlaid
on the network structure.

This is a VISUALIZATION-only tool. It does not affect solving, T-count,
or qubit allocation -- it only reads already-built `PebblingNetwork`
and (optionally) `GateGroup` structures.

Usage:
    from pebbling_solver import build_gate_groups
    from tikz_network_export import dump_tikz_network

    gates = build_gate_groups(net, max_pebbles=...)
    dump_tikz_network("network.tex", net, gates=gates, title="max.blif")

Then compile with:
    pdflatex network.tex

Layout: nodes are placed by a simple layered (topological-depth) layout
-- depth(PI) = 0, depth(gate) = 1 + max(depth(fanin) for fanin in
fanins) -- with nodes at the same depth spread horizontally in
`net.nodes` order. This is a lightweight, dependency-free layout (no
external graph-layout library required); for very large or densely-
connected networks it may produce a cluttered diagram, in which case
consider post-processing the .tex with a proper TikZ/graphviz layout
tool.

AUTO-SCALING (avoids "Dimension too large" TeX errors): TeX dimension
registers max out at roughly 16383pt (~575cm). A wide/deep circuit
(e.g. an EPFL benchmark with hundreds of gates sharing the same
topological depth) can easily exceed this if drawn with a fixed
per-node spacing, since total width/height scales linearly with row
size / depth. `compute_layered_layout` therefore auto-scales
`x_spacing`/`y_spacing` DOWN (never up) so that the largest row width
and total depth both stay under a safe physical size cap
(`max_extent_cm`, default 400cm -- comfortably under the ~575cm TeX
limit, leaving headroom for the bounding-box padding added around gate
groups). Pass a smaller `max_extent_cm` if you still hit dimension
errors (e.g. from very large gate-group padding), or a larger one if
you have a custom TeX build with expanded dimension registers.

BACKGROUND LAYER COMPATIBILITY: gate-group bounding boxes are drawn
using the classic `\begin{pgfonlayer}{background} ... \end{pgfonlayer}`
syntax (rather than the newer `on background layer` scope option),
since the latter requires a more recent TikZ/pgf release than some
installs have. `\pgfdeclarelayer{background}` +
`\pgfsetlayers{background,main}` must be issued before
`\begin{tikzpicture}` for this to work -- `generate_tikz_network`
already does this in the preamble.
"""

from collections import defaultdict


# ---------------------------------------------------------------------------
# Layout: assign each node an (x, y) grid position based on topological depth
# ---------------------------------------------------------------------------

def compute_layered_layout(net, x_spacing=2.2, y_spacing=1.6, max_extent_cm=400.0):
    """
    Returns dict node -> (x, y) TikZ coordinates, in cm.

    depth(node) = 0 if node.is_pi, else 1 + max(depth(fanin) for fanin
    in node.fanins). Nodes are grouped into rows by depth (y = -depth *
    y_spacing, so PIs are at the top and outputs flow downward), and
    spread left-to-right within a row in `net.nodes` order (x = index_
    within_row * x_spacing).

    `x_spacing`/`y_spacing` are auto-scaled DOWN (never up) so that the
    widest row and the total depth both stay within `max_extent_cm` --
    see module docstring, "AUTO-SCALING". This prevents TeX's
    "Dimension too large" error on big circuits (e.g. EPFL benchmarks
    with hundreds of same-depth gates) without requiring the caller to
    manually guess a safe spacing.
    """
    depth = {}
    for n in net.nodes:
        if n.is_pi:
            depth[n] = 0
        else:
            depth[n] = 1 + max((depth[f] for f in n.fanins), default=0)

    rows = defaultdict(list)
    for n in net.nodes:
        rows[depth[n]].append(n)

    max_row_width = max((len(r) for r in rows.values()), default=1)
    max_depth = max(rows.keys(), default=0)

    # Total layout extent (before scaling) if we used the requested
    # spacing as-is.
    total_width = max_row_width * x_spacing
    total_height = max_depth * y_spacing

    # Scale down (never up) so neither dimension exceeds max_extent_cm.
    scale = 1.0
    if total_width > max_extent_cm:
        scale = min(scale, max_extent_cm / total_width)
    if total_height > max_extent_cm:
        scale = min(scale, max_extent_cm / total_height)

    eff_x_spacing = x_spacing * scale
    eff_y_spacing = y_spacing * scale

    pos = {}
    for d, row_nodes in rows.items():
        row_width = len(row_nodes)
        offset = -(row_width - 1) / 2.0
        for i, n in enumerate(row_nodes):
            x = (offset + i) * eff_x_spacing
            y = -d * eff_y_spacing
            pos[n] = (x, y)

    return pos


# ---------------------------------------------------------------------------
# Node styling
# ---------------------------------------------------------------------------

def _node_style_and_shape(node, pos_set):
    if node.is_pi:
        return "circle, draw, fill=gray!15, minimum size=7mm", "circle"
    if node in pos_set:
        return "rectangle, draw, thick, fill=yellow!25, minimum size=7mm", "rect"
    if node.is_xor:
        return "circle, draw, fill=green!20, minimum size=7mm", "circle"
    return "rectangle, draw, fill=blue!12, minimum size=7mm", "rect"


def _escape_tex(label):
    return label.replace("_", "\\_")


def _safe_tikz_id(node, index):
    # TikZ node names must be alnum-ish; index guarantees uniqueness
    # even if two logical node names collide after sanitizing.
    sanitized = "".join(c if c.isalnum() else "_" for c in node.name)
    return f"n{index}_{sanitized}"


# ---------------------------------------------------------------------------
# Gate-group bounding boxes
# ---------------------------------------------------------------------------

_PALETTE = [
    "red", "blue", "orange!90!black", "purple", "teal", "magenta",
    "brown", "cyan!70!black", "olive", "violet",
]


def _group_bbox(nodes, pos, pad=0.55):
    xs = [pos[n][0] for n in nodes]
    ys = [pos[n][1] for n in nodes]
    return (min(xs) - pad, min(ys) - pad, max(xs) + pad, max(ys) + pad)


# ---------------------------------------------------------------------------
# Full .tex assembly
# ---------------------------------------------------------------------------

def generate_tikz_network(net, gates=None, title=None, x_spacing=2.2, y_spacing=1.6,
                           max_extent_cm=400.0):
    """
    Returns a full, compilable LaTeX document (string) containing one
    `tikzpicture` drawing `net`'s DAG. If `gates` is provided, each
    GateGroup's member nodes get a dashed colored bounding box, cycling
    through `_PALETTE` by `gid % len(_PALETTE)`.

    `max_extent_cm` bounds the largest row width / total depth of the
    auto-scaled layout -- see `compute_layered_layout` and the module
    docstring, "AUTO-SCALING", for why this exists.
    """
    pos = compute_layered_layout(
        net, x_spacing=x_spacing, y_spacing=y_spacing, max_extent_cm=max_extent_cm,
    )
    pos_set = set(net.pos)
    tikz_id = {n: _safe_tikz_id(n, i) for i, n in enumerate(net.nodes)}

    lines = []
    lines.append("\\documentclass[tikz,border=4pt]{standalone}")
    lines.append("\\usetikzlibrary{arrows.meta,positioning,fit,backgrounds}")
    lines.append("\\begin{document}")
    if title:
        lines.append(f"% {_escape_tex(title)}")

    # Declare an explicit background layer (classic, widely-compatible
    # pgf syntax) so we can draw gate-group boxes BEHIND the nodes
    # without relying on the newer `on background layer` scope option,
    # which some TikZ/pgf installs don't yet support.
    lines.append("\\pgfdeclarelayer{background}")
    lines.append("\\pgfsetlayers{background,main}")

    lines.append("\\begin{tikzpicture}[")
    lines.append("  >=Latex,")
    lines.append("  every node/.style={font=\\small},")
    lines.append("]")

    # --- gate group bounding boxes, drawn on the background layer so
    # nodes/edges (drawn on the default 'main' layer below) render on
    # top of them ---
    if gates:
        lines.append("  \\begin{pgfonlayer}{background}")
        for g in gates:
            if not g.nodes:
                continue
            color = _PALETTE[g.gid % len(_PALETTE)]
            x0, y0, x1, y1 = _group_bbox(g.nodes, pos)
            lines.append(
                f"    \\draw[dashed, rounded corners, thick, {color}] "
                f"({x0:.2f},{y0:.2f}) rectangle ({x1:.2f},{y1:.2f});"
            )
            lines.append(
                f"    \\node[anchor=south west, {color}, font=\\scriptsize] "
                f"at ({x0:.2f},{y1:.2f}) {{gate {g.gid}}};"
            )
        lines.append("  \\end{pgfonlayer}")

    # --- nodes ---
    for n in net.nodes:
        x, y = pos[n]
        style, _shape = _node_style_and_shape(n, pos_set)
        label = _escape_tex(n.name)
        lines.append(
            f"  \\node[{style}] ({tikz_id[n]}) at ({x:.2f},{y:.2f}) "
            f"{{{label}}};"
        )

    # --- edges (fanin -> node) ---
    for n in net.nodes:
        for fi in n.fanins:
            lines.append(f"  \\draw[->] ({tikz_id[fi]}) -- ({tikz_id[n]});")

    lines.append("\\end{tikzpicture}")
    lines.append("\\end{document}")
    return "\n".join(lines)


def dump_tikz_network(path, net, gates=None, title=None, x_spacing=2.2, y_spacing=1.6,
                       max_extent_cm=400.0):
    """
    Writes the TikZ network diagram (see `generate_tikz_network`) to
    `path`. Purely a visualization/debug export.
    """
    tex = generate_tikz_network(
        net, gates=gates, title=title, x_spacing=x_spacing, y_spacing=y_spacing,
        max_extent_cm=max_extent_cm,
    )
    with open(path, "w") as f:
        f.write(tex)
    print(f"\nWrote TikZ network diagram to {path} "
          f"({len(net.nodes)} nodes, "
          f"{sum(len(n.fanins) for n in net.nodes)} edges"
          + (f", {len(gates)} gate group(s)" if gates else "")
          + ")")
    return path
