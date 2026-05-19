"""Compare T-count/T-depth metrics of our synthesis algorithms against q3SATlib.

Reads JSON metadata from build/verification_data/, converts truth tables to
Boolean expressions, compiles with q3SATlib, and compares gate metrics.

Usage:
    python test/compare_metrics.py [data_dir] [q3satlib_path]
    # data_dir defaults to build/verification_data
    # q3satlib_path defaults to /tmp/xagtdep-deps/q3SATlib/src
"""

import json
import sys
from pathlib import Path


def truth_table_to_expr(tt_hex: str, num_pis: int) -> str:
    """Convert a truth table hex string to a Boolean expression (SOP form).

    Uses variable names xa, xb, xc, ... as expected by q3SATlib.
    Only supports num_pis <= 6 (64-bit truth table max).
    """
    tt_int = int(tt_hex, 16)
    var_names = [f"x{chr(ord('a') + i)}" for i in range(num_pis)]

    minterms = []
    for i in range(2 ** num_pis):
        if (tt_int >> i) & 1:
            terms = []
            for v in range(num_pis):
                if (i >> v) & 1:
                    terms.append(var_names[v])
                else:
                    terms.append(f"~{var_names[v]}")
            minterms.append("(" + " & ".join(terms) + ")")

    if not minterms:
        return "0"
    return " | ".join(minterms)


def count_gates_from_qiskit(circuit) -> dict:
    """Count T, Tdg, CNOT, H gates in a Qiskit QuantumCircuit."""
    counts = {"t": 0, "tdg": 0, "cnot": 0, "h": 0, "total": 0}
    for instruction in circuit.data:
        name = instruction.operation.name.lower()
        if name == "t":
            counts["t"] += 1
        elif name == "tdg":
            counts["tdg"] += 1
        elif name in ("cx", "cnot"):
            counts["cnot"] += 1
        elif name == "h":
            counts["h"] += 1
        counts["total"] += 1
    counts["t_count"] = counts["t"] + counts["tdg"]
    return counts


def compare_all(data_dir: str, q3satlib_path: str):
    """Compare our metrics against q3SATlib for each XAG."""
    data_path = Path(data_dir)
    if not data_path.exists():
        print(f"ERROR: Data directory {data_dir} does not exist.")
        return

    # Try to import q3SATlib (needs both src/ and src/quantum_compiler/ on path).
    sys.path.insert(0, q3satlib_path)
    sys.path.insert(0, str(Path(q3satlib_path) / "quantum_compiler"))
    try:
        from quantum_compiler import OptimizedQuantumCompiler
        compiler = OptimizedQuantumCompiler()
    except ImportError as e:
        print(f"ERROR: Could not import q3SATlib from {q3satlib_path}")
        print(f"  {e}")
        print(f"  Clone it: git clone https://github.com/ritsu-NGC/q3SATlib /tmp/xagtdep-deps/q3SATlib")
        return

    meta_files = sorted(data_path.glob("xag_*_meta.json"))
    if not meta_files:
        print(f"ERROR: No meta files found in {data_dir}")
        return

    print(f"{'#':>3} | {'PIs':>3} | {'Method':>10} | {'T-cnt':>5} | "
          f"{'CNOTs':>5} | {'Total':>5} || {'q3SAT T':>7} | "
          f"{'q3SAT CX':>8} | {'q3SAT Tot':>9}")
    print("-" * 85)

    for meta_file in meta_files:
        idx = meta_file.stem.replace("xag_", "").replace("_meta", "")
        with open(meta_file) as f:
            meta = json.load(f)

        num_pis = meta["num_pis"]
        tt_hex = meta["truth_table_hex"]

        if not tt_hex:
            continue

        # q3SATlib only handles 3-variable functions well.
        if num_pis > 3:
            continue

        # Convert truth table to Boolean expression.
        try:
            expr = truth_table_to_expr(tt_hex, num_pis)
        except Exception as e:
            print(f"{idx:>3} | {num_pis:>3} | (expr conversion failed: {e})")
            continue

        # Compile with q3SATlib.
        try:
            q3_circuit = compiler.compile_function_optimized(expr)
            q3_metrics = count_gates_from_qiskit(q3_circuit)
        except Exception as e:
            q3_metrics = {"t_count": "ERR", "cnot": "ERR", "total": "ERR"}

        # Print comparison for each of our methods.
        for method_key in ["current", "existing", "proposed"]:
            m = meta.get(f"metrics_{method_key}", {})
            t_cnt = m.get("t_count", "?")
            cnot_cnt = m.get("cnot_count", "?")
            total = m.get("total", "?")

            print(f"{idx:>3} | {num_pis:>3} | {method_key:>10} | "
                  f"{t_cnt:>5} | {cnot_cnt:>5} | {total:>5} || "
                  f"{str(q3_metrics.get('t_count', '?')):>7} | "
                  f"{str(q3_metrics.get('cnot', '?')):>8} | "
                  f"{str(q3_metrics.get('total', '?')):>9}")

    print("-" * 85)
    print("NOTE: q3SATlib metrics are for phase oracles (different circuit style).")
    print("Direct comparison of absolute numbers may not be meaningful —")
    print("the relative improvement between Existing and Proposed is the key metric.")


if __name__ == "__main__":
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "build/verification_data"
    q3satlib_path = sys.argv[2] if len(sys.argv) > 2 else "/tmp/xagtdep-deps/q3SATlib/src"

    print("=== q3SATlib Metrics Comparison ===\n")
    print(f"Data directory: {data_dir}")
    print(f"q3SATlib path:  {q3satlib_path}\n")

    compare_all(data_dir, q3satlib_path)
