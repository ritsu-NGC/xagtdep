"""Dump OpenQASM for each benchmark XAG together with the pebbling comparison.

For every exported XAG it writes <tuple>.qasm (the proposed-method synthesized
circuit, via qc_synthesis.gates_to_qasm) into --out, and a summary.tsv row
combining the comparison data:

    pis ands xors seed  gates  T  tr_anc  solver_anc  note  qasm

Usage (inside the container):
    python3 dump_qasm.py /xagtdep/build/verification_data xags/ \
        --method proposed --out qasm_out [--no-solve] [--timeout 20] [--glob '*.json']
"""

import argparse
import glob
import json
import os
import sys

# qc_synthesis lives in src/QC (gates_to_qasm: gate-list JSON -> OpenQASM 2.0).
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "src", "QC")
)
from compare import key_from_name, min_ancilla  # noqa: E402
from qc_synthesis import gates_to_qasm  # noqa: E402

from xag import Xag  # noqa: E402


def scan_meta(data_dir, method):
    """Map (pis,ands,xors,seed) -> {idx, pis, gates, T, qubits} from meta files."""
    out = {}
    for f in glob.glob(os.path.join(data_dir, "xag_*_meta.json")):
        idx = int(os.path.basename(f).split("_")[1])
        d = json.load(open(f))
        key = (d["config_pis"], d["config_ands"], d["config_xors"], int(d["seed"]))
        met = d["metrics_" + method]
        out[key] = {
            "idx": idx,
            "pis": d["num_pis"],
            "gates": d["num_gates"],
            "T": met["t_count"],
            "qubits": met["qubits"],
        }
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("data_dir", help="build/verification_data (gate lists + meta)")
    ap.add_argument("xags_dir", help="exported XAG JSONs (from XAGExport)")
    ap.add_argument(
        "--method", default="proposed", choices=["proposed", "existing", "current"]
    )
    ap.add_argument("--out", default="qasm_out")
    ap.add_argument("--solve", dest="solve", action="store_true", default=True)
    ap.add_argument("--no-solve", dest="solve", action="store_false")
    ap.add_argument("--timeout", type=int, default=20)
    ap.add_argument("--glob", default="*.json")
    args = ap.parse_args()

    # The SAT model only covers the gadget-based methods.
    solve = args.solve and args.method in ("proposed", "existing")
    os.makedirs(args.out, exist_ok=True)
    meta = scan_meta(args.data_dir, args.method)
    files = sorted(glob.glob(os.path.join(args.xags_dir, args.glob)))

    rows = []
    for f in files:
        key = key_from_name(f)
        if key not in meta:
            print(f"skip {os.path.basename(f)} (no metrics)")
            continue
        info = meta[key]
        name = os.path.basename(f)[:-5]  # strip .json

        gl_path = os.path.join(args.data_dir, f"xag_{info['idx']}_{args.method}.json")
        qasm = gates_to_qasm(open(gl_path).read())
        with open(os.path.join(args.out, name + ".qasm"), "w") as q:
            q.write(qasm)

        tr_anc = info["qubits"] - info["pis"]
        sv, note = "-", ""
        if solve:
            xag = Xag.from_json(open(f).read())
            G, nand = len(xag.gate_ids()), len(xag.and_ids())
            if nand:
                mn, note = min_ancilla(
                    xag,
                    args.method,
                    info["T"],
                    G + nand + 1,
                    2 * G + 2,
                    args.timeout * 1000,
                )
                sv = mn if mn is not None else "?"
            else:
                note = "no-AND"
        rows.append((key, info["gates"], info["T"], tr_anc, sv, note, name + ".qasm"))
        print(
            f"{name}.qasm   gates={info['gates']} T={info['T']} "
            f"tr_anc={tr_anc} solver_anc={sv} {note}",
            flush=True,
        )

    with open(os.path.join(args.out, "summary.tsv"), "w") as s:
        s.write("pis\tands\txors\tseed\tgates\tT\ttr_anc\tsolver_anc\tnote\tqasm\n")
        for key, g, T, tr, sv, note, q in rows:
            s.write(
                f"{key[0]}\t{key[1]}\t{key[2]}\t{key[3]}\t{g}\t{T}\t{tr}\t"
                f"{sv}\t{note}\t{q}\n"
            )
    print(f"\nwrote {len(rows)} .qasm files + summary.tsv to {args.out}/")


if __name__ == "__main__":
    main()
