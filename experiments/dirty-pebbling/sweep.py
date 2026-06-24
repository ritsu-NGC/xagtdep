"""Run the dirty-pebbling frontier over a directory of exported XAGs.

Inputs are JSON files produced by the C++ XAGExport tool (built in the project):

    for cfg in "3 1 1" "4 2 2" ...; do
      for seed in 42 12345 0xdeadbeef; do
        read pis ands xors <<< "$cfg"
        ./build/XAGExport --pis $pis --ands $ands --xors $xors --seed $seed \
          > experiments/dirty-pebbling/xags/p${pis}_a${ands}_x${xors}_s${seed}.json
      done
    done

For each XAG this reports the dirty solver's minimum ancilla at the method's own
T-count (T_target = sum of per-AND gadget weights, i.e. each AND computed once,
which is exactly what the translator pays). The translator's *ancilla* baseline
comes from the C++ metrics (optional --baseline JSON); without it we still report
the solver's achievable minimum and the structural T.

Usage:
    python sweep.py xags/                  # solver-only
    python sweep.py xags/ --method existing --timeout 30
"""
import argparse
import glob
import os
import time

from gadgets import t_weight
from pebble_dirty import solve_status
from xag import Xag


def translator_t(xag: Xag, method: str) -> int:
    """T paid by the straight-line translator: each AND computed exactly once."""
    return sum(t_weight(xag.and_case(v), method) for v in xag.and_ids())


def min_ancilla(xag: Xag, method: str, T_B: int, W: int, K: int, timeout_ms: int):
    """Downward scan; returns (min_ancilla|None, note)."""
    last = None
    for Q in range(W, 0, -1):
        status, _ = solve_status(xag, method, Q, T_B, W, K, timeout_ms)
        if status == "sat":
            last = Q
        elif status == "unsat":
            return last, ("proved" if last else "infeasible")
        else:  # unknown (timeout)
            return last, (f"timeout below {last}" if last else "timeout")
    return last, "minimal"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xags_dir")
    ap.add_argument("--method", default="proposed", choices=["proposed", "existing"])
    ap.add_argument("--timeout", type=int, default=60, help="per-query seconds")
    args = ap.parse_args()

    files = sorted(glob.glob(os.path.join(args.xags_dir, "*.json")))
    if not files:
        print(f"no *.json in {args.xags_dir} (run XAGExport first)")
        return

    print(f"method={args.method}  per-query timeout={args.timeout}s\n")
    hdr = f"{'xag':<32} {'gates':>5} {'ands':>4} {'T':>4} {'min_anc':>7} note"
    print(hdr)
    print("-" * len(hdr))
    for f in files:
        xag = Xag.from_json(open(f).read())
        G = len(xag.gate_ids())
        nand = len(xag.and_ids())
        if nand == 0:
            print(f"{os.path.basename(f):<32} {G:>5} {0:>4} {'-':>4} {'-':>7} no AND")
            continue
        T = translator_t(xag, args.method)
        W = G + nand + 1          # gates + one scratch per AND + slack
        K = 2 * G + 2
        t0 = time.time()
        mn, note = min_ancilla(xag, args.method, T, W, K, args.timeout * 1000)
        dt = time.time() - t0
        mns = str(mn) if mn is not None else "?"
        print(f"{os.path.basename(f):<32} {G:>5} {nand:>4} {T:>4} {mns:>7} "
              f"{note} ({dt:.1f}s)")


if __name__ == "__main__":
    main()
