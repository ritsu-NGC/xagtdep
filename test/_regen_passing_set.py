"""One-off helper: recompute the strict-QCEC passing set from the new sweep.

For every xag_*_meta.json in the data dir, run statevector + strict QCEC for
all three methods and record per-(tuple, method) results. A tuple qualifies for
the passing set only when current AND existing AND proposed are all strictly
EquivalenceCriterion.equivalent — matching the original ci_passing_set.json
semantics. Prints a JSON report to stdout.

Not part of the build; used to regenerate the regression gate after the RNG fix.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import verify_circuits as V

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "QC"))
from qc_synthesis import json_to_circuit

METHODS = ["current", "existing", "proposed"]


def strict_result(data_path, idx, method, meta):
    gate_file = data_path / f"xag_{idx}_{method}.json"
    if not gate_file.exists():
        return "missing"
    with open(gate_file) as f:
        gate_data = json.load(f)
    circuit = json_to_circuit(gate_data)
    n_qubits = gate_data["num_qubits"]
    num_pis = meta["num_pis"]
    ref_tt = meta["truth_table_hex"]

    # Find output qubit by statevector match (same as verify_all).
    best_q = -1
    for q in range(n_qubits):
        if V.simulate_truth_table(circuit, num_pis, q) == ref_tt:
            best_q = q
            break
    if best_q < 0:
        return "sv_fail"
    ref_circuit = V.build_reference_oracle(ref_tt, num_pis, n_qubits, best_q)
    return V.run_qcec_check(circuit, ref_circuit, num_pis, best_q)


def main():
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "build/verification_data"
    data_path = Path(data_dir)
    meta_files = sorted(data_path.glob("xag_*_meta.json"))

    tuples = []
    for meta_file in meta_files:
        idx = meta_file.stem.replace("xag_", "").replace("_meta", "")
        with open(meta_file) as f:
            meta = json.load(f)
        per_method = {}
        for m in METHODS:
            r = strict_result(data_path, idx, m, meta)
            per_method[m] = r
        all_eq = all(per_method[m] == "EquivalenceCriterion.equivalent"
                     for m in METHODS)
        tuples.append({
            "idx": int(idx),
            "config": {"pis": meta["config_pis"], "ands": meta["config_ands"],
                       "xors": meta["config_xors"], "seed": meta["seed"]},
            "results": per_method,
            "all_equivalent": all_eq,
        })
        print(f"idx {idx:>2} pis={meta['config_pis']} ands={meta['config_ands']} "
              f"xors={meta['config_xors']} seed={meta['seed']} -> "
              f"{'PASS' if all_eq else 'FAIL'}  "
              f"cur={per_method['current'].split('.')[-1]} "
              f"ex={per_method['existing'].split('.')[-1]} "
              f"pr={per_method['proposed'].split('.')[-1]}",
              file=sys.stderr)

    passing = [t for t in tuples if t["all_equivalent"]]
    print(json.dumps({"all": tuples, "passing": passing}, indent=2))
    print(f"\n=== {len(passing)}/{len(tuples)} tuples fully pass strict QCEC ===",
          file=sys.stderr)


if __name__ == "__main__":
    main()
