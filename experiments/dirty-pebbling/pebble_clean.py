"""Stage 1a — the clean reversible pebbling game as Boolean SAT (Meuli et al.).

Faithful to the paper: pebble variables p[v,i] for gate nodes only; PI/const
children are always available. Move clause: a node may toggle between steps i and
i+1 only if all its gate-children are pebbled at both i and i+1. Cardinality
≤ P pebbles per step. We sweep P (and increment K) — pure SAT decision queries,
no optimization.

T-weighted variant (the §2 reshaping): a node placed at step i contributes its
gadget T-cost; XOR placements cost 0. This is reported alongside the pebble count
but the pebble cardinality is the qubit axis exactly as in the paper.
"""
from __future__ import annotations

import z3

from gadgets import t_weight
from xag import Xag


def solve_clean(xag: Xag, P: int, K: int):
    """Return a schedule (list of pebbled-node sets per step 0..K) for ≤ P
    pebbles in ≤ K steps, or None if UNSAT."""
    gates = xag.gate_ids()
    outs = set(xag.pos)
    s = z3.Solver()
    p = {(v, i): z3.Bool(f"p_{v}_{i}") for v in gates for i in range(K + 1)}

    for v in gates:                       # init: empty board
        s.add(z3.Not(p[(v, 0)]))
    for v in gates:                       # final: outputs pebbled, rest clean
        s.add(p[(v, K)] if v in outs else z3.Not(p[(v, K)]))

    for i in range(K):
        for v in gates:
            toggled = z3.Xor(p[(v, i)], p[(v, i + 1)])
            for w in xag.gate_children(v):
                s.add(z3.Implies(toggled, z3.And(p[(w, i)], p[(w, i + 1)])))
    for i in range(K + 1):                # cardinality each step
        s.add(z3.AtMost(*[p[(v, i)] for v in gates], P))

    if s.check() != z3.sat:
        return None
    m = s.model()
    return [
        sorted(v for v in gates if z3.is_true(m[p[(v, i)]]))
        for i in range(K + 1)
    ]


def min_pebbles(xag: Xag, K: int, p_lo: int = 1, p_hi: int | None = None):
    """Smallest P for which a schedule exists within K steps. Returns (P, sched)."""
    if p_hi is None:
        p_hi = len(xag.gate_ids())
    for P in range(p_lo, p_hi + 1):
        sched = solve_clean(xag, P, K)
        if sched is not None:
            return P, sched
    return None, None


def schedule_t_cost(xag: Xag, sched, method: str) -> int:
    """Sum gadget T over every *placement* (clean→pebbled transition) in sched.
    XOR placements cost 0; AND placements cost their case weight. Recomputes are
    counted each time (re-paying T), uncomputes of ANDs also re-pay (handled by
    counting every fresh placement)."""
    t = 0
    for i in range(1, len(sched)):
        placed = set(sched[i]) - set(sched[i - 1])
        for v in placed:
            if xag.nodes[v].op == "and":
                t += t_weight(xag.and_case(v), method)
    return t


if __name__ == "__main__":
    from xag import paper_6node

    xag = paper_6node()
    K = 24
    P, sched = min_pebbles(xag, K)
    print(f"paper_6node: min pebbles = {P} (expected 4) within K={K}")
    if sched is not None:
        peak = max(len(s) for s in sched)
        print(f"  peak occupancy = {peak}, steps = {len(sched) - 1}")
        for i, s in enumerate(sched):
            names = {10: "A", 11: "B", 12: "C", 13: "D", 14: "E", 15: "F"}
            print(f"  {i:2d}: {{{', '.join(names[v] for v in s)}}}")
