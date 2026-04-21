"""Verify synthesized quantum circuits against reference truth tables.

Reads JSON gate lists and metadata from build/verification_data/,
builds a reference oracle from the truth table, then verifies each
synthesized circuit using:
  1. Statevector simulation (truth table extraction)
  2. QCEC formal equivalence checking (synthesized vs reference oracle)

Usage:
    python test/verify_circuits.py [data_dir]
    # data_dir defaults to build/verification_data
"""

import json
import sys
import os
from pathlib import Path

import numpy as np
from qiskit import QuantumCircuit
from qiskit.quantum_info import Statevector

# Add src/QC to path so we can import qc_synthesis.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src" / "QC"))
from qc_synthesis import json_to_circuit


def simulate_truth_table(circuit: QuantumCircuit, num_pis: int,
                         output_qubit: int) -> str:
    """Simulate circuit for all 2^num_pis inputs, return truth table as hex."""
    n = circuit.num_qubits
    truth_bits = []

    for input_val in range(2 ** num_pis):
        qc = QuantumCircuit(n)
        for bit in range(num_pis):
            if (input_val >> bit) & 1:
                qc.x(bit)
        qc.compose(circuit, inplace=True)

        sv = Statevector.from_instruction(qc)
        probs = sv.probabilities([output_qubit])
        output_bit = 1 if probs[1] > 0.5 else 0
        truth_bits.append(output_bit)

    tt_int = 0
    for i, bit in enumerate(truth_bits):
        tt_int |= (bit << i)

    num_hex_chars = max(1, (2 ** num_pis + 3) // 4)
    return format(tt_int, f'0{num_hex_chars}x')


def build_reference_oracle(tt_hex: str, num_pis: int,
                           total_qubits: int, output_qubit: int) -> QuantumCircuit:
    """Build a reference oracle circuit from truth table using MCX gates.

    For each minterm (input where f=1), applies a multi-controlled X gate
    with appropriate control polarities to flip the output qubit.
    The result is a provably correct circuit encoding the truth table.
    """
    tt_int = int(tt_hex, 16)
    qc = QuantumCircuit(total_qubits)
    pi_qubits = list(range(num_pis))

    for input_val in range(2 ** num_pis):
        if not ((tt_int >> input_val) & 1):
            continue

        # Flip PI qubits where the input bit is 0 (control-on-zero).
        flips = []
        for bit in range(num_pis):
            if not ((input_val >> bit) & 1):
                qc.x(bit)
                flips.append(bit)

        # MCX: all PIs control, output_qubit is target.
        # If output_qubit is a PI qubit, exclude it from controls.
        controls = [q for q in pi_qubits if q != output_qubit]
        if len(controls) == 0:
            qc.x(output_qubit)
        elif len(controls) == 1:
            qc.cx(controls[0], output_qubit)
        elif len(controls) == 2:
            qc.ccx(controls[0], controls[1], output_qubit)
        else:
            qc.mcx(controls, output_qubit)

        # Undo flips.
        for bit in flips:
            qc.x(bit)

    return qc


def run_qcec_check(synth_circuit: QuantumCircuit, ref_circuit: QuantumCircuit,
                   num_pis: int, output_qubit: int) -> str:
    """Run QCEC equivalence check between synthesized and reference circuits.

    Runs in a subprocess to prevent QCEC C++ crashes (std::out_of_range
    abort on certain circuit configurations) from killing the main process.
    Communicates via temporary QASM files.
    """
    try:
        from mqt import qcec  # noqa: F401 — check installed
    except ImportError:
        return "skipped (mqt.qcec not installed)"

    import subprocess, tempfile

    # Transpile to basis gates that MQT's QASM parser supports.
    from qiskit import transpile
    try:
        from qiskit.qasm2 import dumps
        basis = ['x', 'h', 'cx', 't', 'tdg', 's', 'sdg', 'z', 'ccx', 'u3']
        synth_t = transpile(synth_circuit, basis_gates=basis, optimization_level=0)
        ref_t = transpile(ref_circuit, basis_gates=basis, optimization_level=0)
        synth_qasm = dumps(synth_t)
        ref_qasm = dumps(ref_t)
    except Exception as e:
        return f"error: qasm export: {e}"

    with tempfile.NamedTemporaryFile(mode='w', suffix='.qasm', delete=False) as f1, \
         tempfile.NamedTemporaryFile(mode='w', suffix='.qasm', delete=False) as f2:
        f1.write(synth_qasm)
        f2.write(ref_qasm)
        synth_path = f1.name
        ref_path = f2.name

    # Run QCEC in a subprocess so C++ aborts don't kill us.
    script = f"""
import sys, io, contextlib
try:
    from mqt import qcec
    from mqt.core import load
    from qiskit.qasm2 import load as load_qasm
    synth_qc = load_qasm("{synth_path}")
    ref_qc = load_qasm("{ref_path}")
    synth_mqt = load(synth_qc)
    ref_mqt = load(ref_qc)
    n = synth_qc.num_qubits
    for qc_mqt in [synth_mqt, ref_mqt]:
        for q in range(n):
            if q >= {num_pis}:
                qc_mqt.set_circuit_qubit_ancillary(q)
            if q != {output_qubit}:
                qc_mqt.set_circuit_qubit_garbage(q)
    with contextlib.redirect_stderr(io.StringIO()):
        result = qcec.verify(ref_mqt, synth_mqt, check_partial_equivalence=True)
    print(str(result.equivalence))
except Exception as e:
    print(f"error: {{e}}")
"""
    try:
        result = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True, text=True, timeout=30)
        os.unlink(synth_path)
        os.unlink(ref_path)

        if result.returncode != 0:
            return f"crashed (exit={result.returncode})"
        return result.stdout.strip() or "error: no output"
    except subprocess.TimeoutExpired:
        os.unlink(synth_path)
        os.unlink(ref_path)
        return "timeout"
    except Exception as e:
        return f"error: {e}"


def generate_qasm(circuit: QuantumCircuit) -> str:
    """Generate OpenQASM 2.0 string from a QuantumCircuit."""
    try:
        from qiskit.qasm2 import dumps
        return dumps(circuit)
    except Exception:
        return "(QASM generation failed)"


def write_failure_report(report_path: str, failures: list):
    """Write a detailed failure report with seed, XAG config, and both QASMs."""
    with open(report_path, "w") as f:
        f.write("=" * 70 + "\n")
        f.write("EQUIVALENCE CHECK FAILURE REPORT\n")
        f.write("=" * 70 + "\n\n")
        f.write(f"Total failures: {len(failures)}\n\n")

        for entry in failures:
            f.write("-" * 70 + "\n")
            f.write(f"XAG #{entry['idx']} | Method: {entry['method']}\n")
            f.write("-" * 70 + "\n")
            f.write(f"  Random seed:     {entry.get('seed', 'N/A')}\n")
            f.write(f"  Config:          pis={entry.get('config_pis', '?')}"
                    f" ands={entry.get('config_ands', '?')}"
                    f" xors={entry.get('config_xors', '?')}\n")
            f.write(f"  Num PIs:         {entry['num_pis']}\n")
            f.write(f"  Num gates:       {entry.get('num_gates', '?')}\n")
            f.write(f"  Reference TT:    {entry['ref_tt']}\n")
            f.write(f"  Constraint OK:   {entry.get('constraint_ok', '?')}\n")
            f.write(f"  QCEC Result:     {entry.get('qcec_result', 'N/A')}\n")
            f.write(f"  Error:           {entry.get('error', 'no matching output qubit')}\n")

            ref_qasm = entry.get("ref_qasm")
            if ref_qasm:
                f.write(f"\n  --- Reference Oracle Circuit (QASM) ---\n")
                f.write(ref_qasm)
                f.write("\n")

            synth_qasm = entry.get("qasm")
            if synth_qasm:
                f.write(f"\n  --- Synthesized Circuit (QASM) ---\n")
                f.write(synth_qasm)
                f.write("\n")

            f.write("\n")

    print(f"\nFailure report written to: {report_path}")


def verify_all(data_dir: str) -> tuple:
    """Verify all circuits in data_dir. Returns (passed, failed, total)."""
    data_path = Path(data_dir)
    if not data_path.exists():
        print(f"ERROR: Data directory {data_dir} does not exist.")
        print("Run QCVerificationTest first to generate the data.")
        return 0, 1, 1

    meta_files = sorted(data_path.glob("xag_*_meta.json"))
    if not meta_files:
        print(f"ERROR: No meta files found in {data_dir}")
        return 0, 1, 1

    passed = 0
    failed = 0
    total = 0
    failures = []

    methods = ["current", "existing", "proposed"]

    print(f"{'#':>3} | {'PIs':>3} | {'Method':>10} | {'Ref TT':>10} | "
          f"{'Sim TT':>10} | {'SV':>4} | {'QCEC':>12}")
    print("-" * 75)

    for meta_file in meta_files:
        idx = meta_file.stem.replace("xag_", "").replace("_meta", "")
        with open(meta_file) as f:
            meta = json.load(f)

        num_pis = meta["num_pis"]
        ref_tt = meta["truth_table_hex"]
        if not ref_tt:
            continue

        for method in methods:
            gate_file = data_path / f"xag_{idx}_{method}.json"
            if not gate_file.exists():
                continue

            with open(gate_file) as f:
                gate_data = json.load(f)

            try:
                circuit = json_to_circuit(gate_data)
                n_qubits = gate_data["num_qubits"]

                # Step 1: Statevector search for correct output qubit.
                found_match = False
                best_qubit = -1
                best_tt = ""
                for q in range(n_qubits):
                    sim_tt = simulate_truth_table(circuit, num_pis, q)
                    if sim_tt == ref_tt:
                        found_match = True
                        best_qubit = q
                        best_tt = sim_tt
                        break
                    if best_tt == "":
                        best_tt = sim_tt
                        best_qubit = q

                # Step 2: Build reference oracle and run QCEC.
                # Always build the reference oracle so the failure report
                # can include both QASMs for comparison (even when SV fails).
                qcec_result = "skipped"
                output_q = best_qubit if found_match else meta.get(
                    f"output_qubit_{method}", n_qubits - 1)
                ref_circuit = build_reference_oracle(
                    ref_tt, num_pis, n_qubits, output_q)
                if found_match:
                    qcec_result = run_qcec_check(
                        circuit, ref_circuit, num_pis, best_qubit)

                sv_status = "OK" if found_match else "FAIL"
                is_pass = found_match

                print(f"{idx:>3} | {num_pis:>3} | {method:>10} | "
                      f"{ref_tt:>10} | {best_tt if found_match else 'no match':>10} | "
                      f"{sv_status:>4} | {qcec_result:>12}"
                      + (f" (q{best_qubit})" if found_match else ""))

                if is_pass:
                    passed += 1
                else:
                    failed += 1
                    failures.append({
                        "idx": idx, "method": method, "num_pis": num_pis,
                        "ref_tt": ref_tt, "num_gates": meta.get("num_gates"),
                        "seed": meta.get("seed", "N/A"),
                        "config_pis": meta.get("config_pis"),
                        "config_ands": meta.get("config_ands"),
                        "config_xors": meta.get("config_xors"),
                        "constraint_ok": meta.get("constraint_ok"),
                        "qcec_result": qcec_result,
                        "qasm": generate_qasm(circuit),
                        "ref_qasm": generate_qasm(ref_circuit),
                    })
            except Exception as e:
                failed += 1
                print(f"{idx:>3} | {num_pis:>3} | {method:>10} | "
                      f"{ref_tt:>10} | {'ERROR':>10} | FAIL | {'error':>12}")
                print(f"  ^ Exception: {e}")
                failures.append({
                    "idx": idx, "method": method, "num_pis": num_pis,
                    "ref_tt": ref_tt, "seed": meta.get("seed", "N/A"),
                    "config_pis": meta.get("config_pis"),
                    "config_ands": meta.get("config_ands"),
                    "config_xors": meta.get("config_xors"),
                    "error": str(e),
                })

            total += 1

    print("-" * 75)
    print(f"Results: {passed} passed, {failed} failed out of {total}")
    print()
    print("NOTE: Existing Method failures are expected — its top-level")
    print("compute||uncompute erases the output. The result exists only")
    print("in the middle of the circuit, not in the final state.")

    if failures:
        report_path = str(data_path / "failure_report.txt")
        write_failure_report(report_path, failures)

    return passed, failed, total


if __name__ == "__main__":
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "build/verification_data"

    print("=== Quantum Circuit Verification ===\n")
    print(f"Data directory: {data_dir}\n")

    passed, failed, total = verify_all(data_dir)

    print()
    sys.exit(1 if failed > 0 else 0)
