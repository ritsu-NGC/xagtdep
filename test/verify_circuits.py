"""Verify synthesized quantum circuits against reference truth tables.

Reads JSON gate lists and metadata from build/verification_data/,
simulates each circuit using Qiskit's Statevector, extracts the output
qubit's truth table, and compares against the reference truth table
from mockturtle simulation.

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
    """Simulate circuit for all 2^num_pis inputs, return truth table as hex.

    For each input combination, initialize PIs (qubits 0..num_pis-1) and
    measure the output_qubit. Returns a hex string matching kitty format.
    """
    n = circuit.num_qubits
    truth_bits = []

    for input_val in range(2 ** num_pis):
        # Build input state: set PI qubits according to input_val.
        qc = QuantumCircuit(n)
        for bit in range(num_pis):
            if (input_val >> bit) & 1:
                qc.x(bit)
        # Append the synthesis circuit.
        qc.compose(circuit, inplace=True)

        # Simulate and check probability of output_qubit being |1>.
        sv = Statevector.from_instruction(qc)
        probs = sv.probabilities([output_qubit])
        # probs[0] = P(output=0), probs[1] = P(output=1)
        output_bit = 1 if probs[1] > 0.5 else 0
        truth_bits.append(output_bit)

    # Convert to hex string (kitty format: LSB first, 4 bits per hex char).
    tt_int = 0
    for i, bit in enumerate(truth_bits):
        tt_int |= (bit << i)

    num_hex_chars = max(1, (2 ** num_pis + 3) // 4)
    return format(tt_int, f'0{num_hex_chars}x')


def verify_all(data_dir: str) -> tuple:
    """Verify all circuits in data_dir. Returns (passed, failed, total)."""
    data_path = Path(data_dir)
    if not data_path.exists():
        print(f"ERROR: Data directory {data_dir} does not exist.")
        print("Run QCVerificationTest first to generate the data.")
        return 0, 1, 1

    # Find all meta files.
    meta_files = sorted(data_path.glob("xag_*_meta.json"))
    if not meta_files:
        print(f"ERROR: No meta files found in {data_dir}")
        return 0, 1, 1

    passed = 0
    failed = 0
    total = 0

    methods = ["current", "existing", "proposed"]

    print(f"{'#':>3} | {'PIs':>3} | {'Method':>10} | {'Ref TT':>10} | "
          f"{'Sim TT':>10} | {'Match':>5}")
    print("-" * 60)

    for meta_file in meta_files:
        idx = meta_file.stem.replace("xag_", "").replace("_meta", "")
        with open(meta_file) as f:
            meta = json.load(f)

        num_pis = meta["num_pis"]
        ref_tt = meta["truth_table_hex"]

        for method in methods:
            gate_file = data_path / f"xag_{idx}_{method}.json"
            if not gate_file.exists():
                continue

            with open(gate_file) as f:
                gate_data = json.load(f)

            try:
                circuit = json_to_circuit(gate_data)
                n_qubits = gate_data["num_qubits"]

                # Try all possible output qubits — find the one matching ref.
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

                if found_match:
                    status = "OK"
                    print(f"{idx:>3} | {num_pis:>3} | {method:>10} | "
                          f"{ref_tt:>10} | {best_tt:>10} | {status:>5} (q{best_qubit})")
                    passed += 1
                else:
                    status = "FAIL"
                    print(f"{idx:>3} | {num_pis:>3} | {method:>10} | "
                          f"{ref_tt:>10} | {'no match':>10} | {status:>5}")
                    failed += 1
            except Exception as e:
                failed += 1
                print(f"{idx:>3} | {num_pis:>3} | {method:>10} | "
                      f"{ref_tt:>10} | {'ERROR':>10} | {'FAIL':>5}")
                print(f"  ^ Exception: {e}")

            total += 1

    print("-" * 60)
    print(f"Results: {passed} passed, {failed} failed out of {total}")
    print()
    print("NOTE: Existing Method failures are expected — its top-level")
    print("compute||uncompute erases the output. The result exists only")
    print("in the middle of the circuit, not in the final state.")
    return passed, failed, total


def try_qcec_check(data_dir: str):
    """Optional: run QCEC equivalence checks on same-qubit-count pairs."""
    try:
        from mqt import qcec
    except ImportError:
        print("\nmqt.qcec not installed — skipping QCEC checks.")
        print("Install with: pip install mqt.qcec")
        return

    data_path = Path(data_dir)
    meta_files = sorted(data_path.glob("xag_*_meta.json"))

    print("\n=== QCEC Equivalence Checks ===")
    print("Comparing Current vs Existing (same compute+uncompute structure):\n")

    checked = 0
    equiv = 0
    for meta_file in meta_files:
        idx = meta_file.stem.replace("xag_", "").replace("_meta", "")

        cur_file = data_path / f"xag_{idx}_current.json"
        ex_file = data_path / f"xag_{idx}_existing.json"
        if not cur_file.exists() or not ex_file.exists():
            continue

        with open(cur_file) as f:
            cur_data = json.load(f)
        with open(ex_file) as f:
            ex_data = json.load(f)

        # QCEC needs same qubit count.
        if cur_data["num_qubits"] != ex_data["num_qubits"]:
            continue

        try:
            cur_qc = json_to_circuit(cur_data)
            ex_qc = json_to_circuit(ex_data)
            result = qcec.verify(cur_qc, ex_qc)
            status = str(result.equivalence)
            print(f"  XAG {idx}: {status}")
            checked += 1
            if status == "EquivalenceCriterion.equivalent":
                equiv += 1
        except Exception as e:
            print(f"  XAG {idx}: ERROR — {e}")

    if checked > 0:
        print(f"\nQCEC: {equiv}/{checked} equivalent pairs found.")
    else:
        print("No same-qubit-count pairs to compare.")


if __name__ == "__main__":
    data_dir = sys.argv[1] if len(sys.argv) > 1 else "build/verification_data"

    print("=== Quantum Circuit Verification ===\n")
    print(f"Data directory: {data_dir}\n")

    passed, failed, total = verify_all(data_dir)

    # Optional QCEC check.
    try_qcec_check(data_dir)

    print()
    sys.exit(1 if failed > 0 else 0)
