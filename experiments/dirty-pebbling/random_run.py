"""Generate random XAGs (the same generator the CI uses) and run the full
Phase-1 pipeline on them: synthesize, dump QASM, and compare dirty-solver
ancilla vs the translator.

The XAG set is a few hand-picked larger configs (HANDPICKED) plus --n random
ones. For each (pis, ands, xors, seed):
  XAGExport          -> XAG structure         (drives the SAT solver)
  QCVerificationTest -> translator metrics + gate lists (single-XAG mode)
  verify_circuits    -> QCEC equivalence gate (is the XAG real, not garbage?)
  gates_to_qasm      -> <name>.qasm           (the proposed-method circuit)
  dirty solver       -> solver_anc at the translator's T  (only if equivalent)

Both C++ tools call generate_random_xag() with the same seed in the same image,
so they build the identical graph; we assert the gate counts agree.

Unlike the fixed 14-config benchmark, you can ask for larger circuits (more
ANDs) where dirty pebbling has more room to win.

Usage (inside the container):
  python3 random_run.py --n 6 --pis 5 --ands 3 --xors 3 --out rnd_out
  python3 random_run.py --n 10 --pis-range 4 8 --ands-range 2 5 --xors-range 1 4 \
      --timeout 30 --master-seed 1
  python3 random_run.py --n 20 --ands 4 --no-solve        # QASM only, fast
"""
import argparse
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "..", "..", "src", "QC"))
from qc_synthesis import gates_to_qasm  # noqa: E402

from compare import min_ancilla  # noqa: E402
from xag import Xag  # noqa: E402

BUILD = os.environ.get("BUILD", "/xagtdep/build")
REPO = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")
VERIFY = os.path.join(REPO, "test", "verify_circuits.py")

# A few hand-picked larger configs, run alongside the random ones. They are NOT
# trusted blindly — they go through the same QCEC equivalence gate as the rest.
HANDPICKED = [(5, 3, 3, 1001), (6, 4, 3, 2002), (6, 5, 4, 3003), (5, 4, 2, 4004)]


def rand_tuples(args, rng):
    import random
    out = []
    for _ in range(args.n):
        pis = args.pis or rng.randint(*args.pis_range)
        ands = args.ands or rng.randint(*args.ands_range)
        xors = args.xors or rng.randint(*args.xors_range)
        seed = rng.randint(1, 2**31 - 1)
        out.append((pis, ands, xors, seed))
    return out


def export_xag(pis, ands, xors, seed):
    return subprocess.check_output(
        [f"{BUILD}/XAGExport", "--pis", str(pis), "--ands", str(ands),
         "--xors", str(xors), "--seed", str(seed)], text=True)


def translator_data(pis, ands, xors, seed, method, ddir):
    # --method all: write every method's gate list so the equivalence check can
    # verify all of them; we read back the chosen method for QASM + metrics.
    subprocess.run(
        [f"{BUILD}/QCVerificationTest", "--seed", str(seed), "--pis", str(pis),
         "--ands", str(ands), "--xors", str(xors), "--method", "all",
         "--data-dir", ddir],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    meta = json.load(open(os.path.join(ddir, "xag_0_meta.json")))
    gl = open(os.path.join(ddir, f"xag_0_{method}.json")).read()
    return meta, gl


def verify_equivalence(ddir):
    """QCEC equivalence gate: every synthesized circuit in ddir must match its
    XAG's truth table (verify_circuits.py --strict) — this is the check that the
    random XAGs aren't garbage. Returns 'OK' or 'FAIL'."""
    r = subprocess.run([sys.executable, VERIFY, "--strict", ddir],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return "OK" if r.returncode == 0 else "FAIL"


def main():
    import random
    ap = argparse.ArgumentParser()
    ap.add_argument("--n", type=int, default=6, help="number of random XAGs")
    ap.add_argument("--pis", type=int, default=0, help="fixed pis (0 = use range)")
    ap.add_argument("--ands", type=int, default=0)
    ap.add_argument("--xors", type=int, default=0)
    ap.add_argument("--pis-range", type=int, nargs=2, default=[4, 6])
    ap.add_argument("--ands-range", type=int, nargs=2, default=[2, 4])
    ap.add_argument("--xors-range", type=int, nargs=2, default=[1, 3])
    ap.add_argument("--method", default="proposed", choices=["proposed", "existing"])
    ap.add_argument("--out", default="rnd_out")
    ap.add_argument("--timeout", type=int, default=180)
    ap.add_argument("--master-seed", type=int, default=None,
                    help="seed for which random XAGs are chosen (default: random)")
    ap.add_argument("--solve", dest="solve", action="store_true", default=True)
    ap.add_argument("--no-solve", dest="solve", action="store_false")
    ap.add_argument("--handpicked", dest="handpicked", action="store_true",
                    default=True, help="include the fixed HANDPICKED configs")
    ap.add_argument("--no-handpicked", dest="handpicked", action="store_false")
    args = ap.parse_args()

    master = args.master_seed
    if master is None:
        master = int.from_bytes(os.urandom(4), "big")
    rng = random.Random(master)
    os.makedirs(args.out, exist_ok=True)
    tuples = (HANDPICKED if args.handpicked else []) + rand_tuples(args, rng)

    print(f"master-seed={master}  method={args.method}  timeout={args.timeout}s  "
          f"handpicked={len(HANDPICKED) if args.handpicked else 0}  random={args.n}\n")
    hdr = (f"{'name':<28} {'gates':>5} {'T':>4} {'tr_anc':>6} "
           f"{'solver_anc':>10} {'equiv':>5}  note")
    print(hdr + "\n" + "-" * len(hdr))

    rows, tot_tr, tot_sv, proved = [], 0, 0, 0
    for (pis, ands, xors, seed) in tuples:
        name = f"p{pis}_a{ands}_x{xors}_s{seed}"
        struct = export_xag(pis, ands, xors, seed)
        ddir = os.path.join(args.out, "_meta", name)
        os.makedirs(ddir, exist_ok=True)
        meta, gl = translator_data(pis, ands, xors, seed, args.method, ddir)

        gates_meta = meta["num_gates"]
        met = meta["metrics_" + args.method]
        T = met["t_count"]
        tr_anc = met["qubits"] - meta["num_pis"]

        with open(os.path.join(args.out, name + ".qasm"), "w") as q:
            q.write(gates_to_qasm(gl))

        xag = Xag.from_json(struct)
        G, nand = len(xag.gate_ids()), len(xag.and_ids())
        consistency = "" if G == gates_meta else f" [WARN struct gates {G}!=meta {gates_meta}]"

        # Equivalence gate first — don't trust a random XAG until QCEC confirms
        # the synthesized circuit matches its truth table.
        equiv = verify_equivalence(ddir)

        sv, note = "-", ""
        if equiv != "OK":
            note = "skipped (NOT equivalent)"
        elif args.solve and nand:
            t0 = time.time()
            # W = tr_anc: the translator never frees a wire, so its ancilla count
            # is its peak occupancy — the dirty solver never needs more. Using it
            # (instead of G+nand+1) keeps the SAT model small enough to solve.
            W = max(tr_anc, 2)
            mn, note = min_ancilla(xag, args.method, T, W, 2 * G + 2,
                                   args.timeout * 1000)
            note += f" ({time.time()-t0:.1f}s)"
            sv = mn if mn is not None else "?"
            if mn is not None and "proved" in note:
                tot_tr += tr_anc
                tot_sv += mn
                proved += 1
        elif nand == 0:
            note = "no-AND"
        rows.append((name, G, T, tr_anc, sv, equiv, note))
        print(f"{name:<28} {G:>5} {T:>4} {tr_anc:>6} {str(sv):>10} {equiv:>5}  "
              f"{note}{consistency}", flush=True)

    with open(os.path.join(args.out, "summary.tsv"), "w") as s:
        s.write("name\tgates\tT\ttr_anc\tsolver_anc\tequiv\tnote\n")
        for name, g, T, tr, sv, equiv, note in rows:
            s.write(f"{name}\t{g}\t{T}\t{tr}\t{sv}\t{equiv}\t{note}\n")
    n_equiv = sum(1 for r in rows if r[5] == "OK")
    print(f"\nequivalence: {n_equiv}/{len(rows)} XAGs verified equivalent (QCEC)")
    if proved:
        print(f"\nproved on {proved} XAGs: translator ancilla {tot_tr} -> "
              f"dirty {tot_sv}  ({100*(tot_tr-tot_sv)/tot_tr:.0f}% reduction)")
    print(f"wrote {len(rows)} .qasm + summary.tsv to {args.out}/")


if __name__ == "__main__":
    main()
