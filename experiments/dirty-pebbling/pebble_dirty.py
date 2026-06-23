"""Stage 1b — the dirty-ancilla pebbling game as Boolean SAT (model A, conservative).

Single-action-per-step encoding (one move per step → trivial frame axioms).
Wire content is an Int: 0 = clean |0>, -1 = corrupt/opaque-dead, v>0 = holds gate
node v's value (a live pebble). PIs/const are always available and are not modeled
as work wires.

Moves (Phase-0 §1.3, grounded in the gadgets):
  - compute_xor(v, r):       r clean, gate-children live      → r = <v>.           T 0
  - compute_and(v, r[, s]):  r clean, children live;          → r = <v>;           T = weight
                             scratch s (one_pi/output only): s != r, s not holding
                             a child of v; s may be clean/corrupt/any other pebble
                             → s becomes corrupt (model A: opaque).
  - uncompute(v, w):         w holds v, children live          → w clean.
                             T 0 if v is XOR (free reclaim), else AND weight.
  - noop.

Borrowing legality is *implicit*: corrupting a value still needed downstream makes
a later precondition (child-live / output-live) UNSAT, so the solver never does it.

Axes (both pseudo-Boolean, swept as decision queries — pure SAT):
  qubits:  sum_w occupied[w,i] <= Q   (work wires; total qubits = num_pis + Q)
  T-count: sum over AND compute/uncompute actions of weight <= T_B
"""
from __future__ import annotations

import z3

from gadgets import has_scratch, t_weight
from xag import Xag

CLEAN, CORRUPT = 0, -1


class DirtyModel:
    def __init__(self, xag: Xag, method: str, W: int, K: int):
        self.xag = xag
        self.method = method
        self.W = W
        self.K = K
        # Map gate node ids -> internal 1..G (so 0/-1 are free for clean/corrupt).
        gids = xag.gate_ids()
        self.intern = {g: i + 1 for i, g in enumerate(gids)}
        self.extern = {v: g for g, v in self.intern.items()}
        self.G = len(gids)
        self.is_xor = {self.intern[g]: (xag.nodes[g].op == "xor") for g in gids}
        self.case = {
            self.intern[g]: (xag.and_case(g) if xag.nodes[g].op == "and" else None)
            for g in gids
        }
        self.children = {
            self.intern[g]: [self.intern[c] for c in xag.gate_children(g)]
            for g in gids
        }
        self.outputs = [self.intern[g] for g in xag.pos]
        self.and_ids = [self.intern[g] for g in xag.and_ids()]
        self.xor_ids = [v for v in self.intern.values() if self.is_xor[v]]

    # content cell variable
    def _c(self, w, i):
        return z3.Int(f"c_{w}_{i}")

    def _live(self, v, i):
        return z3.Or([self._c(w, i) == v for w in range(self.W)])

    def _weight(self, v):
        return 0 if self.is_xor[v] else t_weight(self.case[v], self.method)

    def build(self, Q: int, T_B: int):
        s = z3.Solver()
        W, K = self.W, self.K
        c = self._c

        # Domain + init + final.
        for i in range(K + 1):
            for w in range(W):
                s.add(c(w, i) >= CORRUPT, c(w, i) <= self.G)
        for w in range(W):
            s.add(c(w, 0) == CLEAN)
        for v in self.outputs:
            s.add(self._live(v, K))
        # A value sits on at most one wire (per step).
        for i in range(K + 1):
            for v in range(1, self.G + 1):
                s.add(z3.AtMost(*[c(w, i) == v for w in range(W)], 1))
        # Qubit bound (occupied = non-clean work wires).
        for i in range(K + 1):
            s.add(z3.Sum([z3.If(c(w, i) != CLEAN, 1, 0) for w in range(W)]) <= Q)

        # Symmetry breaking: work wires are interchangeable. Force them to be
        # *used* in index order — wire w may ever be occupied only if wire w-1
        # is also occupied at some step. Cuts the W! permutation symmetry, which
        # is what makes UNSAT proofs slow.
        used = [z3.Bool(f"used_{w}") for w in range(W)]
        for w in range(W):
            s.add(used[w] == z3.Or([c(w, i) != CLEAN for i in range(K + 1)]))
        for w in range(1, W):
            s.add(z3.Implies(used[w], used[w - 1]))

        t_terms = []
        for i in range(K):
            actions = []  # (bool, t_cost)

            def add(pre, eff, cost=0):
                a = z3.Bool(f"a_{i}_{len(actions)}")
                s.add(z3.Implies(a, z3.And(pre, eff)))
                actions.append(a)
                if cost:
                    t_terms.append(z3.If(a, cost, 0))

            def unchanged(except_wires):
                return [c(w, i + 1) == c(w, i)
                        for w in range(W) if w not in except_wires]

            # noop
            add(z3.BoolVal(True), z3.And(unchanged(set())))

            # compute XOR(v) on r
            for v in self.xor_ids:
                for r in range(W):
                    pre = z3.And(c(r, i) == CLEAN,
                                 *[self._live(ch, i) for ch in self.children[v]])
                    eff = z3.And(c(r, i + 1) == v, *unchanged({r}))
                    add(pre, eff)

            # compute AND(v) on r (+ scratch s if the gadget has one)
            for v in self.and_ids:
                scratch = has_scratch(self.case[v])
                wcost = self._weight(v)
                for r in range(W):
                    if not scratch:
                        pre = z3.And(c(r, i) == CLEAN,
                                     *[self._live(ch, i) for ch in self.children[v]])
                        eff = z3.And(c(r, i + 1) == v, *unchanged({r}))
                        add(pre, eff, wcost)
                    else:
                        for sl in range(W):
                            if sl == r:
                                continue
                            pre = z3.And(
                                c(r, i) == CLEAN,
                                *[self._live(ch, i) for ch in self.children[v]],
                                # s must not currently hold a child of v
                                *[c(sl, i) != ch for ch in self.children[v]],
                            )
                            eff = z3.And(c(r, i + 1) == v,
                                         c(sl, i + 1) == CORRUPT,
                                         *unchanged({r, sl}))
                            add(pre, eff, wcost)

            # uncompute(v) on wire w
            for v in range(1, self.G + 1):
                wcost = self._weight(v)  # 0 for XOR, full weight for AND
                for w in range(W):
                    pre = z3.And(c(w, i) == v,
                                 *[self._live(ch, i) for ch in self.children[v]])
                    eff = z3.And(c(w, i + 1) == CLEAN, *unchanged({w}))
                    add(pre, eff, wcost)

            s.add(z3.AtMost(*actions, 1))
            s.add(z3.Or(*actions))  # exactly one action per step (noop allowed)

        s.add(z3.Sum(t_terms) <= T_B)
        return s

    def extract_grid(self, model) -> list:
        """Grid in check_schedule format: None=clean, 'corrupt', or external id."""
        grid = []
        for i in range(self.K + 1):
            row = []
            for w in range(self.W):
                val = model.eval(self._c(w, i)).as_long()
                if val == CLEAN:
                    row.append(None)
                elif val == CORRUPT:
                    row.append("corrupt")
                else:
                    row.append(self.extern[val])
            grid.append(row)
        return grid

    def extract(self, model) -> list:
        """Decode content per step into a readable schedule of external node ids /
        'corrupt' / '.'(clean)."""
        out = []
        for i in range(self.K + 1):
            row = []
            for w in range(self.W):
                val = model.eval(self._c(w, i)).as_long()
                if val == CLEAN:
                    row.append(".")
                elif val == CORRUPT:
                    row.append("x")
                else:
                    row.append(str(self.extern[val]))
            out.append(row)
        return out


def solve_dirty(xag: Xag, method: str, Q: int, T_B: int, W: int, K: int):
    status, payload = solve_status(xag, method, Q, T_B, W, K)
    return payload if status == "sat" else None


def solve_status(xag: Xag, method: str, Q: int, T_B: int, W: int, K: int,
                 timeout_ms: int | None = None):
    """Returns ('sat',(model_obj, model)) | ('unsat', None) | ('unknown', None)."""
    m = DirtyModel(xag, method, W, K)
    s = m.build(Q, T_B)
    if timeout_ms:
        s.set("timeout", timeout_ms)
    r = s.check()
    if r == z3.sat:
        return "sat", (m, s.model())
    if r == z3.unsat:
        return "unsat", None
    return "unknown", None


def min_ancilla_at_T(xag: Xag, method: str, T_B: int, W: int, K: int):
    """Fewest work wires (ancilla) achievable at a fixed T budget T_B. The
    Phase-1 headline: hold T at the method's own count, minimize qubits.

    Scans Q downward from W (feasibility is monotone in Q, so this hits only the
    cheap SAT solves plus a single UNSAT proof at the bottom — far faster than
    scanning up through several UNSATs). Returns (min_ancilla, model_tuple)."""
    last = (None, None)
    for Q in range(W, 0, -1):
        res = solve_dirty(xag, method, Q, T_B, W, K)
        if res is None:
            break
        last = (Q, res)
    return last


def frontier(xag: Xag, method: str, W: int, K: int, t_max: int):
    """For each work-wire bound Q from 1..W, the minimum T achievable (binary
    search on the T bound). Returns the Pareto points (Q_ancilla, min_T)."""
    pts = []
    best_t = t_max
    for Q in range(1, W + 1):
        if solve_dirty(xag, method, Q, best_t, W, K) is None:
            continue  # infeasible even at the loosest T budget
        lo, hi = 0, best_t
        while lo < hi:                      # min T_B that stays SAT at this Q
            mid = (lo + hi) // 2
            if solve_dirty(xag, method, Q, mid, W, K) is not None:
                hi = mid
            else:
                lo = mid + 1
        pts.append((Q, lo))
        best_t = lo                          # more qubits never needs more T
    return pts


if __name__ == "__main__":
    from check_schedule import check_schedule
    from xag import phase0_two_and

    xag = phase0_two_and()
    W, K = 6, 14
    print("phase0_two_and, method=proposed", flush=True)

    res = solve_dirty(xag, "proposed", Q=4, T_B=8, W=W, K=K)
    print(f"  Q=4, T=8 -> {'SAT' if res else 'UNSAT'} (expect SAT)", flush=True)
    if res:
        m, model = res
        grid = m.extract_grid(model)
        ok, t, peak, errs = check_schedule(xag, "proposed", grid)
        print(f"  legality: ok={ok} T={t} peak_ancilla={peak} errors={errs}",
              flush=True)

    res3 = solve_dirty(xag, "proposed", Q=3, T_B=100, W=W, K=K)
    print(f"  Q=3, any T -> {'SAT' if res3 else 'UNSAT'} (expect UNSAT)", flush=True)

    print("  frontier (ancilla, minT):",
          frontier(xag, "proposed", W, K, t_max=40), flush=True)
