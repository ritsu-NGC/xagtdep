"""
Experiment harness: compares T-count between this project's gate-level
pebbling + gadget-synthesis pipeline and gmeuli/caterpillar (via
xagtdep's C++ integration), on a batch of randomly generated DAGs.

======================================================================
IMPORTANT NOTE
======================================================================
xagtdep does NOT expose a Python binding for caterpillar. Caterpillar
is used exclusively from C++ (`caterpillar::logic_network_synthesis` +
`xag_mapping_strategy`, mirrored from `test/RandomBooleanFunctionTest.cpp`).

This harness shells out to a small companion C++ binary,
`test/blif_to_tcount.cpp` (built as a CMake target in xagtdep), which:
  1. parses a BLIF file with a hand-rolled reader (NOT
     mockturtle::blif_reader -- that reader requires generic
     ntk.create_node(), which xag_network does not implement),
  2. builds a mockturtle::xag_network directly via create_and/create_xor,
  3. runs `caterpillar::logic_network_synthesis` with
     `xag_mapping_strategy` (the exact same call xagtdep's own
     RandomBooleanFunctionTest.cpp already makes),
  4. prints a single-line JSON object with gate/T-counts to stdout.

Build once, from the xagtdep repo root:

    cmake -B build
    cmake --build build -j$(nproc) --target blif_to_tcount

Then point this script at it with --caterpillar-bin build/blif_to_tcount
(path relative to wherever you run this script from). The shim takes the
BLIF path as a bare positional argument -- no flags.

======================================================================
BLIF EXPORT
======================================================================
`pebbling_solver.read_blif` already exists for READING BLIF; this file
adds the missing WRITER (`write_blif`), producing a standard
`.model`/`.inputs`/`.outputs`/`.names`/`.end` BLIF file with one
`.names` block per non-PI node. Supports:
  - 2-input AND nodes  -> cover "11 1"
  - 2-input XOR nodes  -> cover "01 1" / "10 1"
  - 1-input buffer/NOT nodes -> cover "1 1" (buffer) / "0 1" (NOT, when
    is_xor is set on a single-fanin node)
These conventions match `_is_xor_cover` in pebbling_solver.py on read,
so round-tripping through `read_blif(write_blif(net))` reproduces an
equivalent network.

NOTE: the 1-input buffer-vs-NOT interpretation (`is_xor` flag doubling
as "invert" for single-fanin nodes) has NOT been verified against
random_dag's actual generation logic -- check pebbling_solver.py's
random_dag() if you need to confirm this before trusting T-counts on
cases where 1-input nodes appear.

======================================================================
OUR OWN T-COUNT
======================================================================
For our side, we run the full existing pipeline:
    build_gate_groups -> pebble_gates -> expand_gate_schedule
    -> build_circuit_from_node_schedule (gate_schedule_to_circuit.py)
and count `QOp`s with kind "T" or "Tdg" in the resulting
`CircuitBuildResult.ops`.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time

from pebbling_solver import (
    PebblingNetwork,
    random_dag,
    build_gate_groups,
    pebble_gates,
    expand_gate_schedule,
)
from reed_muller_priority_cuts import random_reed_muller_priority_cut_dag
from gate_schedule_to_circuit import build_circuit_from_node_schedule


# ---------------------------------------------------------------------------
# BLIF writer (companion to pebbling_solver.read_blif)
# ---------------------------------------------------------------------------

def write_blif(net: PebblingNetwork, path, model_name="pebbling_dag"):
    """
    Writes `net` out as a standard combinational BLIF file.

    Supports:
      - 2-input AND/XOR nodes (canonical covers recognized by
        pebbling_solver._is_xor_cover on read).
      - 1-input buffer/NOT nodes: `is_xor` on a single-fanin node is
        treated as NOT (inverter); otherwise treated as a buffer
        (identity). NOTE: this interpretation is unverified against
        random_dag's actual semantics -- see module docstring.

    Raises `ValueError` if any non-PI node has a fanin count other than
    1 or 2.
    """
    for n in net.nodes:
        if n.is_pi:
            continue
        if len(n.fanins) not in (1, 2):
            raise ValueError(
                f"write_blif only supports 1- or 2-input gates; node "
                f"'{n.name}' has {len(n.fanins)} fanin(s)."
            )

    lines = []
    lines.append(f".model {model_name}")
    lines.append(".inputs " + " ".join(pi.name for pi in net.pis))
    lines.append(".outputs " + " ".join(po.name for po in net.pos))

    for n in net.nodes:
        if n.is_pi:
            continue

        if len(n.fanins) == 1:
            a = n.fanins[0]
            lines.append(f".names {a.name} {n.name}")
            if n.is_xor:
                lines.append("0 1")  # NOT: output=1 when input=0
            else:
                lines.append("1 1")  # buffer: output=1 when input=1
        else:
            a, b = n.fanins
            lines.append(f".names {a.name} {b.name} {n.name}")
            if n.is_xor:
                lines.append("01 1")
                lines.append("10 1")
            else:
                lines.append("11 1")

    lines.append(".end")

    with open(path, "w") as f:
        f.write("\n".join(lines) + "\n")

    return path


# ---------------------------------------------------------------------------
# caterpillar invocation via the blif_to_tcount JSON shim
# ---------------------------------------------------------------------------

def run_caterpillar(caterpillar_bin, blif_path, timeout=120):
    """
    Runs `caterpillar_bin <blif_path>` (built from
    test/blif_to_tcount.cpp) and parses its single-line JSON stdout.
    No -i/--args flags -- the shim takes the BLIF path as a bare
    positional argument.

    Returns a dict:
        {
          "ok": bool,
          "t_count": int or None,
          "cnot_count": int or None,
          "qubits": int or None,
          "elapsed_s": float,
          "error": str or None,
        }
    """
    if not caterpillar_bin:
        return {
            "ok": False, "t_count": None, "cnot_count": None, "qubits": None,
            "elapsed_s": 0.0,
            "error": "No --caterpillar-bin provided; skipped.",
        }

    if not os.path.isfile(caterpillar_bin) or not os.access(caterpillar_bin, os.X_OK):
        return {
            "ok": False, "t_count": None, "cnot_count": None, "qubits": None,
            "elapsed_s": 0.0,
            "error": f"caterpillar binary not found or not executable: "
                     f"{caterpillar_bin}. Build test/blif_to_tcount.cpp first "
                     f"(cmake --build build --target blif_to_tcount).",
        }

    start = time.time()
    try:
        proc = subprocess.run(
            [caterpillar_bin, blif_path], capture_output=True, text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "t_count": None, "cnot_count": None, "qubits": None,
            "elapsed_s": time.time() - start,
            "error": f"caterpillar shim timed out after {timeout}s",
        }
    except OSError as e:
        return {
            "ok": False, "t_count": None, "cnot_count": None, "qubits": None,
            "elapsed_s": time.time() - start,
            "error": f"failed to launch caterpillar shim: {e}",
        }

    elapsed = time.time() - start
    raw = (proc.stdout or "").strip()

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {
            "ok": False, "t_count": None, "cnot_count": None, "qubits": None,
            "elapsed_s": elapsed,
            "error": (
                f"caterpillar shim produced non-JSON stdout (exit "
                f"{proc.returncode}): stdout={raw!r} stderr={proc.stderr!r}"
            ),
        }

    if not payload.get("ok"):
        return {
            "ok": False, "t_count": None, "cnot_count": None, "qubits": None,
            "elapsed_s": elapsed,
            "error": payload.get("error", "unknown error from caterpillar shim"),
        }

    return {
        "ok": True,
        "t_count": payload.get("t_count"),
        "cnot_count": payload.get("cnot_count"),
        "qubits": payload.get("qubits"),
        "elapsed_s": elapsed,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Our own pipeline: gate groups -> pebbling -> circuit -> T-count
# ---------------------------------------------------------------------------

def run_our_pipeline(net, max_pebbles=None, max_steps="auto"):
    """
    Runs build_gate_groups -> pebble_gates -> expand_gate_schedule ->
    build_circuit_from_node_schedule on `net`, and returns:
        {
          "ok": bool,
          "t_count": int or None,
          "qubit_count": int or None,
          "num_ops": int or None,
          "elapsed_s": float,
          "error": str or None,
        }
    """
    if max_pebbles is None:
        num_gates_estimate = len(net.nodes) - len(net.pis)
        max_pebbles = max(len(net.pos), min(8, num_gates_estimate))

    start = time.time()
    try:
        gates = build_gate_groups(net, max_pebbles=max_pebbles)
        gate_steps = pebble_gates(
            gates, max_pebbles=max_pebbles, max_steps=max_steps, net=net,
            verbose=False,
        )
        node_steps = expand_gate_schedule(gates, gate_steps)
        circuit_result = build_circuit_from_node_schedule(net, node_steps)
    except Exception as e:  # noqa: BLE001 -- report, don't crash the batch
        return {
            "ok": False, "t_count": None, "qubit_count": None,
            "num_ops": None, "elapsed_s": time.time() - start,
            "error": f"{type(e).__name__}: {e}",
        }

    t_count = sum(
        1 for (_node, _phase, op) in circuit_result.ops
        if op.kind in ("T", "Tdg")
    )

    return {
        "ok": True,
        "t_count": t_count,
        "qubit_count": circuit_result.qubit_count,
        "num_ops": len(circuit_result.ops),
        "elapsed_s": time.time() - start,
        "error": None,
    }


# ---------------------------------------------------------------------------
# DAG generation for the experiment batch
# ---------------------------------------------------------------------------

def generate_dags(num_dags, num_pis, num_gates, num_outputs, seed_base, kind):
    """
    Yields (label, PebblingNetwork) pairs.

    `kind`:
      - "random"      : random_dag (arbitrary random AND/XOR DAG)
      - "reed_muller" : random_reed_muller_priority_cut_dag (ANF
                        decomposition of a random n-PI/m-PO Boolean bit
                        mapping, with priority-cut node sharing)
    """
    for i in range(num_dags):
        seed = seed_base + i
        if kind == "random":
            net = random_dag(
                num_pis=num_pis, num_gates=num_gates, max_fanin=2,
                seed=seed, xor_prob=0.4,
            )
            label = f"random_dag_seed{seed}"
        elif kind == "reed_muller":
            net = random_reed_muller_priority_cut_dag(
                num_vars=num_pis, num_outputs=num_outputs, seed=seed,
                density=0.5,
            )
            label = f"reed_muller_pcut_seed{seed}"
        else:
            raise ValueError(f"Unknown kind: {kind}")
        yield label, net


# ---------------------------------------------------------------------------
# Main experiment driver
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Compare T-count between this project's gate-level "
                    "pebbling pipeline and gmeuli/caterpillar (via "
                    "xagtdep's C++ integration) on random DAGs."
    )
    parser.add_argument("--num-dags", type=int, default=5,
                         help="Number of random DAGs to generate per kind.")
    parser.add_argument("--kinds", nargs="+", default=["random", "reed_muller"],
                         choices=["random", "reed_muller"],
                         help="Which DAG generators to include in the batch.")
    parser.add_argument("--num-pis", type=int, default=4)
    parser.add_argument("--num-gates", type=int, default=8,
                         help="Used only for --kinds random.")
    parser.add_argument("--num-outputs", type=int, default=2,
                         help="Used only for --kinds reed_muller.")
    parser.add_argument("--seed-base", type=int, default=1000)
    parser.add_argument("--max-pebbles", type=int, default=None,
                         help="Pebble cap for OUR pipeline; auto-sized if omitted.")
    parser.add_argument("--out-dir", default=None,
                         help="Directory to write .blif files into "
                              "(default: a fresh temp dir).")
    parser.add_argument(
        "--caterpillar-bin", default=None,
        help="Path to the built test/blif_to_tcount shim (see module "
             "docstring for build instructions). If omitted, caterpillar "
             "comparisons are skipped and only our own T-count is reported."
    )
    parser.add_argument("--timeout", type=float, default=120.0,
                         help="Per-DAG timeout (seconds) for the "
                              "caterpillar subprocess.")
    parser.add_argument("--keep-blif", action="store_true",
                         help="Don't delete the temp dir with generated "
                              ".blif files after the run.")
    args = parser.parse_args()

    out_dir = args.out_dir
    cleanup_dir = False
    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="pebbling_vs_caterpillar_")
        cleanup_dir = not args.keep_blif
    else:
        os.makedirs(out_dir, exist_ok=True)

    print(f"BLIF files will be written to: {out_dir}")
    if not args.caterpillar_bin:
        print("NOTE: --caterpillar-bin not provided; caterpillar T-counts "
              "will be skipped, only our own pipeline's T-count is reported.\n"
              "Build test/blif_to_tcount.cpp first, then pass its path here.")

    rows = []

    for kind in args.kinds:
        for label, net in generate_dags(
            args.num_dags, args.num_pis, args.num_gates, args.num_outputs,
            args.seed_base, kind,
        ):
            blif_path = os.path.join(out_dir, f"{label}.blif")
            try:
                write_blif(net, blif_path, model_name=label)
            except ValueError as e:
                rows.append({
                    "label": label, "kind": kind,
                    "num_pis": len(net.pis), "num_pos": len(net.pos),
                    "num_gates": len(net.nodes) - len(net.pis),
                    "our_t_count": None, "our_error": f"BLIF export failed: {e}",
                    "cat_t_count": None, "cat_error": None,
                })
                continue

            our = run_our_pipeline(net, max_pebbles=args.max_pebbles)
            cat = run_caterpillar(args.caterpillar_bin, blif_path, timeout=args.timeout)

            rows.append({
                "label": label,
                "kind": kind,
                "num_pis": len(net.pis),
                "num_pos": len(net.pos),
                "num_gates": len(net.nodes) - len(net.pis),
                "our_t_count": our["t_count"],
                "our_qubits": our["qubit_count"],
                "our_elapsed_s": our["elapsed_s"],
                "our_error": our["error"],
                "cat_t_count": cat["t_count"],
                "cat_qubits": cat["qubits"],
                "cat_elapsed_s": cat["elapsed_s"],
                "cat_error": cat["error"],
            })

    print("\n" + "=" * 100)
    print("RESULTS")
    print("=" * 100)
    header = (
        f"{'label':32s} {'kind':12s} {'pis':>4s} {'pos':>4s} {'gates':>6s} "
        f"{'ours-T':>7s} {'ours-q':>7s} {'cat-T':>6s} {'cat-q':>6s}"
    )
    print(header)
    print("-" * len(header))

    for r in rows:
        ours_t = str(r.get("our_t_count")) if r.get("our_t_count") is not None else "ERR"
        ours_q = str(r.get("our_qubits", "")) if r.get("our_qubits") is not None else "-"
        cat_t = str(r.get("cat_t_count")) if r.get("cat_t_count") is not None else "N/A"
        cat_q = str(r.get("cat_qubits")) if r.get("cat_qubits") is not None else "N/A"
        print(
            f"{r['label']:32s} {r['kind']:12s} {r['num_pis']:>4d} "
            f"{r['num_pos']:>4d} {r['num_gates']:>6d} "
            f"{ours_t:>7s} {ours_q:>7s} {cat_t:>6s} {cat_q:>6s}"
        )
        if r.get("our_error"):
            print(f"    [ours error] {r['our_error']}")
        if r.get("cat_error"):
            print(f"    [caterpillar] {r['cat_error']}")

    valid = [
        r for r in rows
        if r.get("our_t_count") is not None and r.get("cat_t_count") is not None
    ]
    if valid:
        print("\n" + "-" * 100)
        print("SUMMARY (only rows with both T-counts available)")
        print("-" * 100)
        total_ours = sum(r["our_t_count"] for r in valid)
        total_cat = sum(r["cat_t_count"] for r in valid)
        print(f"DAGs compared:             {len(valid)}")
        print(f"Total ours T-count:        {total_ours}")
        print(f"Total caterpillar T-count: {total_cat}")
        if total_cat > 0:
            print(f"Ours / caterpillar ratio:  {total_ours / total_cat:.3f}")
        for r in valid:
            delta = r["our_t_count"] - r["cat_t_count"]
            print(f"  {r['label']:32s} ours={r['our_t_count']:<6d} "
                  f"caterpillar={r['cat_t_count']:<6d} delta={delta:+d}")
    else:
        print("\nNo rows had both T-counts available -- nothing to summarize. "
              "Pass --caterpillar-bin to enable the comparison.")

    if cleanup_dir:
        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)
        print(f"\n(Temp BLIF dir {out_dir} removed; pass --keep-blif to retain it.)")


if __name__ == "__main__":
    main()
