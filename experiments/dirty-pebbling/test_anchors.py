"""Phase-1 validation anchors. Run: python test_anchors.py  (needs z3-solver).

Anchor 1 (clean game, paper-faithful): reproduce the known optimum of Meuli's
6-node DAG — 4 pebbles. Proves the SAT machinery is correct.

Anchor 2 (dirty game): reproduce Phase-0 §5 — the 2-AND XAG must reach 4 ancilla
at T=8 (vs the translator's 7 ancilla), the schedule must pass the independent
legality checker, and 3 ancilla must be infeasible at any T.
"""
import sys

from check_schedule import check_schedule
from pebble_clean import min_pebbles
from pebble_dirty import min_ancilla_at_T, solve_dirty
from xag import paper_6node, phase0_two_and

fails = []


def expect(cond, msg):
    print(("PASS" if cond else "FAIL") + ": " + msg)
    if not cond:
        fails.append(msg)


# ── Anchor 1 — clean game vs the paper ──────────────────────────────────────
P, sched = min_pebbles(paper_6node(), K=24)
expect(P == 4, f"paper_6node clean min pebbles == 4 (got {P})")

# ── Anchor 2 — dirty game vs Phase-0 §5 ─────────────────────────────────────
xag = phase0_two_and()
W, K, T = 5, 9, 8  # Proposed: two one-PI ANDs => T = 4 + 4 = 8

mn, res = min_ancilla_at_T(xag, "proposed", T_B=T, W=W, K=K)
expect(mn == 4, f"phase0_two_and dirty min ancilla at T={T} == 4 (got {mn}); "
                f"translator uses 7")

if res is not None:
    m, model = res
    ok, t_chk, peak, errs = check_schedule(xag, "proposed", m.extract_grid(model))
    expect(ok, f"legality checker accepts the schedule (errors={errs})")
    expect(t_chk == T, f"independently recomputed T == {T} (got {t_chk})")
    expect(peak == 4, f"independently recomputed peak ancilla == 4 (got {peak})")

infeasible3 = solve_dirty(xag, "proposed", Q=3, T_B=40, W=W, K=K) is None
expect(infeasible3, "3 ancilla is infeasible at any T (UNSAT)")

print()
if fails:
    print(f"{len(fails)} anchor(s) FAILED")
    sys.exit(1)
print("all anchors passed")
