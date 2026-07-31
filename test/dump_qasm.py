#!/usr/bin/env python3
"""Dump OpenQASM 2.0 for every synthesized circuit in a verification-data dir.

QCVerificationTest / CIQCVerification write one gate-list JSON per (XAG, method)
as xag_<idx>_<method>.json (method = current | existing | proposed |
pebbling_proposed | pebbling_existing). This converts each of those to an
OpenQASM 2.0 file via the shared gates_to_qasm helper, into <data_dir>/qasm/
(or a given output dir). Non-circuit files (_meta / _structure / _sequence) are
skipped automatically.

Usage:
    python test/dump_qasm.py <data_dir> [<out_dir>]

Examples:
    ./build/QCVerificationTest --data-dir build/verification_data
    python test/dump_qasm.py build/verification_data
    # -> build/verification_data/qasm/xag_0_existing.qasm, ...
"""

import glob
import json
import os
import sys

# Import the shared JSON->QASM converter from src/QC.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(_HERE, "..", "src", "QC"))
from qc_synthesis import gates_to_qasm  # noqa: E402


def dump_dir(data_dir: str, out_dir: str) -> int:
    os.makedirs(out_dir, exist_ok=True)
    written = 0
    for path in sorted(glob.glob(os.path.join(data_dir, "xag_*.json"))):
        with open(path) as f:
            text = f.read()
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        # Circuit files have a gate list; skip meta/structure/sequence dumps.
        if "gates" not in data or "num_qubits" not in data:
            continue
        name = os.path.splitext(os.path.basename(path))[0]  # xag_<idx>_<method>
        try:
            qasm = gates_to_qasm(text)
        except Exception as e:  # noqa: BLE001 — report and keep going
            print(f"  SKIP {name}: {e}", file=sys.stderr)
            continue
        with open(os.path.join(out_dir, name + ".qasm"), "w") as f:
            f.write(qasm)
        written += 1
    return written


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: dump_qasm.py <data_dir> [<out_dir>]", file=sys.stderr)
        return 2
    data_dir = sys.argv[1]
    out_dir = sys.argv[2] if len(sys.argv) > 2 else os.path.join(data_dir, "qasm")
    if not os.path.isdir(data_dir):
        print(f"error: no such directory: {data_dir}", file=sys.stderr)
        return 1
    n = dump_dir(data_dir, out_dir)
    print(f"wrote {n} QASM files to {out_dir}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
