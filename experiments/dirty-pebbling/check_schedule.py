"""Independent legality checker for dirty schedules.

Re-derives the move at each step from the wire-content grid alone (it does NOT
read the solver's action variables), then verifies preconditions, recomputes the
metrics, and checks boundary conditions. A bug in the SAT encoding that admits an
illegal schedule will be caught here, because this code shares no clauses with it.

Grid format: grid[i][w] is one of
    None        -> clean |0>
    'corrupt'   -> opaque dead
    <int>       -> external gate-node id held on that wire
"""
from __future__ import annotations

from gadgets import has_scratch, t_weight
from xag import Xag


def check_schedule(xag: Xag, method: str, grid):
    errors = []
    W = len(grid[0])
    K = len(grid) - 1
    gate_ops = {g: xag.nodes[g].op for g in xag.gate_ids()}
    gchildren = {g: xag.gate_children(g) for g in xag.gate_ids()}

    def live(val, row):
        return any(cell == val for cell in row)

    def is_node(cell):
        return cell is not None and cell != "corrupt"

    # init: all clean
    if any(cell is not None for cell in grid[0]):
        errors.append("step 0 is not all-clean")

    t_count = 0
    for i in range(K):
        a, b = grid[i], grid[i + 1]
        changed = [w for w in range(W) if a[w] != b[w]]

        if not changed:
            continue  # noop

        # classify the move by the change pattern
        becomes_node = [w for w in changed if is_node(b[w]) and not is_node(a[w])]
        becomes_clean = [w for w in changed if b[w] is None and is_node(a[w])]
        becomes_corrupt = [w for w in changed if b[w] == "corrupt"]

        ok = False
        # uncompute: a single node-wire goes clean
        if changed == becomes_clean and len(becomes_clean) == 1:
            w = becomes_clean[0]
            v = a[w]
            if not all(live(c, a) for c in gchildren[v]):
                errors.append(f"step {i}: uncompute {v} but children not live")
            if gate_ops[v] == "and":
                t_count += t_weight(xag.and_case(v), method)
            ok = True

        # compute with one visible wire change: XOR / both_pi AND (no scratch), or
        # a scratch AND whose scratch wire was ALREADY corrupt (re-corrupt is
        # invisible in the grid) — legal only if a corrupt wire was available.
        elif (len(becomes_node) == 1 and len(changed) == 1):
            r = becomes_node[0]
            v = b[r]
            if not all(live(c, a) for c in gchildren[v]):
                errors.append(f"step {i}: compute {v} but children not live")
            if gate_ops[v] == "and":
                case = xag.and_case(v)
                if has_scratch(case):
                    if not any(w != r and a[w] == "corrupt" for w in range(W)):
                        errors.append(
                            f"step {i}: scratch AND {v} ({case}) but no corrupt "
                            f"wire available to borrow")
                t_count += t_weight(case, method)
            ok = True

        # compute AND with scratch: one wire -> node, one wire -> corrupt
        elif (len(becomes_node) == 1 and len(becomes_corrupt) == 1
              and len(changed) == 2):
            r = becomes_node[0]
            sl = becomes_corrupt[0]
            v = b[r]
            case = xag.and_case(v) if gate_ops[v] == "and" else None
            if gate_ops[v] != "and" or not has_scratch(case):
                errors.append(f"step {i}: scratch-write but {v} is not a scratch AND")
            if not all(live(c, a) for c in gchildren[v]):
                errors.append(f"step {i}: compute {v} but children not live")
            if a[sl] in gchildren[v]:
                errors.append(f"step {i}: scratch wire held child {a[sl]} of {v}")
            if r == sl:
                errors.append(f"step {i}: result and scratch are the same wire")
            t_count += t_weight(case, method)
            ok = True

        if not ok:
            errors.append(f"step {i}: unrecognized/illegal move, changed wires {changed}")

    # final: each output live
    for v in xag.pos:
        if not live(v, grid[K]):
            errors.append(f"final: output {v} not live")

    # also forbid two wires holding the same node value at any step
    for i, row in enumerate(grid):
        seen = [c for c in row if is_node(c)]
        if len(seen) != len(set(seen)):
            errors.append(f"step {i}: a node value is duplicated across wires")

    peak_ancilla = max(sum(1 for c in row if c is not None) for row in grid)
    return (len(errors) == 0), t_count, peak_ancilla, errors
