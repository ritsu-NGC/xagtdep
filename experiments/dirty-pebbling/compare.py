"""Compare dirty-solver minimum ancilla against the C++ translator baseline.

Joins exported XAGs (experiments/dirty-pebbling/xags/, from XAGExport) with the
translator metrics written by QCVerificationTest (build/verification_data/
*_meta.json), matching on (pis, ands, xors, seed). For each XAG it reports the
translator's ancilla (qubits - num_pis) at its T-count vs the dirty solver's
minimum ancilla at the same T.

Usage (inside the container):
    python3 compare.py xags/ /xagtdep/build/verification_data --method proposed \
        --timeout 30 [--glob 'p*_a2_*']
"""
import argparse
import glob
import json
import os
import time

from pebble_dirty import solve_status
from xag import Xag


def load_baseline(meta_dir, method):
    bl = {}
    for f in glob.glob(os.path.join(meta_dir, "*_meta.json")):
        d = json.load(open(f))
        key = (d["config_pis"], d["config_ands"], d["config_xors"], int(d["seed"]))
        m = d["metrics_" + method]
        bl[key] = {"qubits": m["qubits"], "pis": d["num_pis"], "t": m["t_count"]}
    return bl


def key_from_name(path):
    # p{pis}_a{ands}_x{xors}_s{seed}.json
    b = os.path.basename(path)[:-5]
    pis = int(b.split("p")[1].split("_")[0])
    ands = int(b.split("_a")[1].split("_")[0])
    xors = int(b.split("_x")[1].split("_")[0])
    seed = int(b.split("_s")[1], 0)
    return (pis, ands, xors, seed)


def min_ancilla(xag, method, T, W, K, timeout_ms):
    last = None
    for Q in range(W, 0, -1):
        st, _ = solve_status(xag, method, Q, T, W, K, timeout_ms)
        if st == "sat":
            last = Q
        elif st == "unsat":
            return last, "proved"
        else:
            return last, "timeout"
    return last, "hit-W"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("xags_dir")
    ap.add_argument("meta_dir")
    ap.add_argument("--method", default="proposed", choices=["proposed", "existing"])
    ap.add_argument("--timeout", type=int, default=30)
    ap.add_argument("--glob", default="*.json")
    args = ap.parse_args()

    bl = load_baseline(args.meta_dir, args.method)
    files = sorted(glob.glob(os.path.join(args.xags_dir, args.glob)))

    hdr = (f"{'tuple (p,a,x,seed)':<26} {'gates':>5} {'T':>4} "
           f"{'tr_anc':>6} {'solver_anc':>10} note")
    print(f"method={args.method}  timeout={args.timeout}s\n{hdr}\n{'-'*len(hdr)}")
    tot_tr = tot_sv = solved = 0
    for f in files:
        key = key_from_name(f)
        if key not in bl:
            print(f"{str(key):<26} (no baseline)")
            continue
        b = bl[key]
        tr_anc = b["qubits"] - b["pis"]
        T = b["t"]
        xag = Xag.from_json(open(f).read())
        G = len(xag.gate_ids())
        nand = len(xag.and_ids())
        if nand == 0:
            print(f"{str(key):<26} {G:>5} {T:>4} {tr_anc:>6} {'n/a':>10} no AND")
            continue
        W = G + nand + 1
        K = 2 * G + 2
        t0 = time.time()
        sv, note = min_ancilla(xag, args.method, T, W, K, args.timeout * 1000)
        dt = time.time() - t0
        svs = str(sv) if sv is not None else "?"
        print(f"{str(key):<26} {G:>5} {T:>4} {tr_anc:>6} {svs:>10} "
              f"{note} ({dt:.1f}s)")
        if sv is not None and note == "proved":
            tot_tr += tr_anc
            tot_sv += sv
            solved += 1
    if solved:
        print(f"\nproved on {solved} XAGs: translator ancilla {tot_tr} -> "
              f"dirty {tot_sv}  ({100*(tot_tr-tot_sv)/tot_tr:.0f}% reduction)")


if __name__ == "__main__":
    main()
