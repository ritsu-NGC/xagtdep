"""
Experiment harness: compares T-count between this project's gate-level
pebbling + gadget-synthesis pipeline and gmeuli/caterpillar (via
xagtdep's C++ integration), on a batch of randomly generated DAGs and/or
checked-in EPFL combinational benchmark BLIFs.

======================================================================
IMPORTANT NOTE
======================================================================
xagtdep does NOT expose a Python binding for caterpillar. Caterpillar
is used exclusively from C++ (`caterpillar::logic_network_synthesis` +
`xag_mapping_strategy`, plus `caterpillar::decompose_with_ands` to
actually expand ANDs into their T-gate decomposition -- see
test/blif_to_tcount.cpp).

This harness shells out to a small companion C++ binary,
`test/blif_to_tcount.cpp` (built as a CMake target in xagtdep), which:
  1. parses a BLIF file with a hand-rolled reader,
  2. builds a mockturtle::xag_network directly via create_and/create_xor,
  3. runs `caterpillar::logic_network_synthesis` with
     `xag_mapping_strategy`,
  4. runs `caterpillar::decompose_with_ands` to expand each AND into
     its actual Hadamard/T/CNOT synthesis sequence,
  5. prints a single-line JSON object with gate/T-counts (including
     "and_pos") to stdout.

Build once, from the xagtdep repo root:

    cmake -B build
    cmake --build build -j$(nproc) --target blif_to_tcount

Then point this script at it with --caterpillar-bin build/blif_to_tcount.

Examples:

    python run_caterpillar_experiments.py \
        --epfl-benchmarks all \
        --caterpillar-bin build/blif_to_tcount

    python run_caterpillar_experiments.py \
        --epfl-benchmarks adder max sqrt \
        --kinds random \
        --num-dags 3 \
        --caterpillar-bin build/blif_to_tcount

======================================================================
DIAGNOSTICS (--debug)
======================================================================
Pass --debug to print, for every DAG in the batch:
  - the gate-group partition: group id, node names, and each node's
    resolved gadget rule (1-5) and its per-node T/Tdg cost.
  - the gate-LEVEL schedule: how many times each group id was toggled
    (compute_gate / uncompute_gate events), from `gate_steps` (the
    output of `pebble_gates`, BEFORE `expand_gate_schedule` unpacks it
    into individual nodes).
  - the node-LEVEL compute counts after `expand_gate_schedule`: how
    many times each individual node was actually computed/uncomputed,
    which is what really drives the total T-count (since a group's
    toggle event pays for EVERY node in that group at once -- see
    `expand_gate_schedule` in pebbling_solver.py).

This was added to answer: "why is ours-T so much higher than the raw
gate count suggests?" -- the total T-count is NOT simply
(num_gate_level_toggles) x (some fixed per-toggle cost), because (a)
gadget rules cost different amounts (Rule 1=4, Rule 3=6, Rule 4=7,
Rule 5/XOR=0 T/Tdg gates; Rule 2 still contains an undecomposed raw
TOFFOLI placeholder and its true cost is not yet consistently counted
-- flagged here as a known open issue), and (b) each gate-level toggle
fires EVERY node in that group at once, and groups can vary in size.
Only the actual instrumented counts below can tell you whether the
blowup is from group size, recomputation, or both.

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
These conventions match `_is_xor_cover` in pebbling_solver.py on read.

NOTE: the 1-input buffer-vs-NOT interpretation has NOT been verified
against random_dag's actual generation logic -- check
pebbling_solver.py's random_dag() if you need to confirm this before
trusting T-counts on cases where 1-input nodes appear.

======================================================================
CATERPILLAR "CORRECTED" T-COUNT ESTIMATE
======================================================================
caterpillar's decompose_with_ands gives every internal AND's
UNCOMPUTE step a "free" (0-T) pass whenever the exact same
(control, control, target) triple was already computed earlier in the
circuit. To estimate what the T-count WOULD be if every AND's
uncompute cost the same as its compute, except output-driving ANDs
(which need a full Toffoli, cost 7, instead of the relative-phase
gadget, cost 4):

    corrected_t = 2 * t_count - and_pos

where `and_pos` (reported directly by blif_to_tcount.cpp) is the
number of primary outputs whose driving node is itself an AND gate.
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter

from pebbling_solver import (
    PebblingNetwork,
    random_dag,
    build_gate_groups,
    pebble_gates,
    expand_gate_schedule,
    read_blif,
    select_gadget_rule,
)
from reed_muller_priority_cuts import random_reed_muller_priority_cut_dag
from gate_schedule_to_circuit import build_circuit_from_node_schedule


# ---------------------------------------------------------------------------
# BLIF writer (companion to pebbling_solver.read_blif)
# ---------------------------------------------------------------------------

def write_blif(net: PebblingNetwork, path, model_name="pebbling_dag"):
    """
    Writes `net` out as a standard combinational BLIF file.

    Supports 2-input AND/XOR nodes and 1-input buffer/NOT nodes. Raises
    `ValueError` if any non-PI node has a fanin count other than 1 or 2.
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
    Runs `caterpillar_bin <blif_path>` and parses its single-line JSON
    stdout. Returns a dict with t_count/cnot_count/qubits/and_pos/
    corrected_t/elapsed_s/error/ok.
    """
    if not caterpillar_bin:
        return {
            "ok": False, "t_count": None, "cnot_count": None, "qubits": None,
            "and_pos": None, "corrected_t": None, "elapsed_s": 0.0,
            "error": "No --caterpillar-bin provided; skipped.",
        }

    if not os.path.isfile(caterpillar_bin) or not os.access(caterpillar_bin, os.X_OK):
        return {
            "ok": False, "t_count": None, "cnot_count": None, "qubits": None,
            "and_pos": None, "corrected_t": None, "elapsed_s": 0.0,
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
            "and_pos": None, "corrected_t": None, "elapsed_s": time.time() - start,
            "error": f"caterpillar shim timed out after {timeout}s",
        }
    except OSError as e:
        return {
            "ok": False, "t_count": None, "cnot_count": None, "qubits": None,
            "and_pos": None, "corrected_t": None, "elapsed_s": time.time() - start,
            "error": f"failed to launch caterpillar shim: {e}",
        }

    elapsed = time.time() - start
    raw = (proc.stdout or "").strip()

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {
            "ok": False, "t_count": None, "cnot_count": None, "qubits": None,
            "and_pos": None, "corrected_t": None, "elapsed_s": elapsed,
            "error": (
                f"caterpillar shim produced non-JSON stdout (exit "
                f"{proc.returncode}): stdout={raw!r} stderr={proc.stderr!r}"
            ),
        }

    if not payload.get("ok"):
        return {
            "ok": False, "t_count": None, "cnot_count": None, "qubits": None,
            "and_pos": None, "corrected_t": None, "elapsed_s": elapsed,
            "error": payload.get("error", "unknown error from caterpillar shim"),
        }

    t_count = payload.get("t_count")
    and_pos = payload.get("and_pos")
    corrected_t = (
        2 * t_count - and_pos
        if (t_count is not None and and_pos is not None)
        else None
    )

    return {
        "ok": True,
        "t_count": t_count,
        "cnot_count": payload.get("cnot_count"),
        "qubits": payload.get("qubits"),
        "and_pos": and_pos,
        "corrected_t": corrected_t,
        "elapsed_s": elapsed,
        "error": None,
    }


# ---------------------------------------------------------------------------
# Diagnostics: gate-group composition, gate-level toggles, node-level
# compute counts. See module docstring, "DIAGNOSTICS (--debug)".
# ---------------------------------------------------------------------------

_RULE_T_COST = {
    1: 4,
    2: None,  # unresolved: still contains an undecomposed raw TOFFOLI
              # placeholder in gate_schedule_to_circuit.py's
              # _gadget_ops_rule2 -- true cost not yet consistently counted.
    3: 6,
    4: 7,
    5: 0,  # XOR
}


def print_pipeline_diagnostics(label, net, gates, gate_steps, node_steps):
    """
    Prints, for a single DAG:
      - gate-group composition (nodes, resolved rule, per-node T cost)
      - gate-level toggle counts (compute_gate/uncompute_gate per gid,
        straight from `gate_steps` -- BEFORE node expansion)
      - node-level compute/uncompute counts (AFTER `expand_gate_schedule`
        unpacks groups into individual nodes)
    """
    children = net.build_children()

    print(f"\n{'=' * 100}")
    print(f"[debug] Pipeline diagnostics for: {label}")
    print(f"{'=' * 100}")

    print(f"\n[debug] Gate groups ({len(gates)} total):")
    for g in gates:
        node_descr = []
        for n in g.nodes:
            if n.is_xor:
                rule = 5
            else:
                rule = select_gadget_rule(n, net, children)
            cost = _RULE_T_COST.get(rule)
            cost_str = str(cost) if cost is not None else "UNRESOLVED"
            node_descr.append(f"{n.name}(rule={rule},T={cost_str})")
        print(f"  Gate {g.gid} [{len(g.nodes)} node(s)]: {', '.join(node_descr)}")

    gate_toggle_counts = Counter()
    gate_toggle_kinds = {}
    for (_k, gid, op, _tag) in gate_steps:
        gate_toggle_counts[gid] += 1
        gate_toggle_kinds.setdefault(gid, []).append(op)

    print(f"\n[debug] Gate-LEVEL toggle counts ({len(gate_steps)} total events):")
    for g in gates:
        count = gate_toggle_counts.get(g.gid, 0)
        kinds = gate_toggle_kinds.get(g.gid, [])
        print(f"  Gate {g.gid}: {count} toggle(s) -> {kinds}")

    node_compute_counts = Counter()
    node_uncompute_counts = Counter()
    for n, action in node_steps:
        if n.is_pi:
            continue
        if action == "compute":
            node_compute_counts[n.name] += 1
        else:
            node_uncompute_counts[n.name] += 1

    print(f"\n[debug] Node-LEVEL compute/uncompute counts "
          f"({len(node_steps)} total node-events):")
    all_names = sorted(set(node_compute_counts) | set(node_uncompute_counts))
    for name in all_names:
        c = node_compute_counts.get(name, 0)
        u = node_uncompute_counts.get(name, 0)
        flag = "  <-- recomputed!" if c > 1 else ""
        print(f"  {name}: compute x{c}, uncompute x{u}{flag}")

    total_toggles = sum(gate_toggle_counts.values())
    total_node_events = len(node_steps)
    print(f"\n[debug] Summary: {len(gates)} groups, {total_toggles} gate-level "
          f"toggle(s), {total_node_events} node-level event(s) "
          f"(avg {total_node_events / total_toggles:.2f} node-events per "
          f"toggle, reflecting group size)." if total_toggles else "")


# ---------------------------------------------------------------------------
# Our own pipeline: gate groups -> pebbling -> circuit -> T-count
# ---------------------------------------------------------------------------

def run_our_pipeline(net, max_pebbles=None, max_steps="auto", debug=False, label=""):
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

    If `debug` is True, prints gate-group/toggle/node-event diagnostics
    (see `print_pipeline_diagnostics`) before returning.
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

        if debug:
            print_pipeline_diagnostics(label, net, gates, gate_steps, node_steps)

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

EPFL_BENCHMARKS = {
    "arithmetic": [
        "adder", "bar", "div", "hyp", "max",
        "multiplier", "sin", "sqrt", "square",
    ],
    "random_control": [
        "arbiter", "cavlc", "ctrl", "dec", "i2c",
        "int2float", "mem_ctrl", "priority", "router", "voter",
    ],
}

EPFL_BENCHMARK_ORDER = [
    *EPFL_BENCHMARKS["arithmetic"],
    *EPFL_BENCHMARKS["random_control"],
]


def default_epfl_blif_dir():
    return "epfl_blif"


def resolve_epfl_blif_dir(path):
    if os.path.isabs(path):
        return path
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), path))


def resolve_epfl_blif_path(root_dir, name):
    for category in ("arithmetic", "random_control"):
        candidate = os.path.join(root_dir, category, f"{name}.blif")
        if os.path.isfile(candidate):
            return candidate
    return None


def expand_epfl_benchmark_names(names):
    if not names:
        return []
    if "all" in names:
        requested = list(EPFL_BENCHMARK_ORDER)
        requested_set = set(requested)
        for name in names:
            if name == "all" or name in requested_set:
                continue
            requested.append(name)
        return requested
    return names

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
                    "xagtdep's C++ integration) on random DAGs and/or checked-in "
                    "EPFL benchmark BLIF files."
    )
    parser.add_argument("--num-dags", type=int, default=5,
                         help="Number of random DAGs to generate per kind.")
    parser.add_argument("--kinds", nargs="*",
                         choices=["random", "reed_muller"],
                         help="Which synthetic DAG generators to include.")
    parser.add_argument(
                    "--epfl-benchmarks", nargs="+", default=None,
                    help="Checked-in EPFL BLIF benchmarks to include by circuit name, "
                         "or 'all' for all 19."
    )
    parser.add_argument(
                    "--epfl-blif-dir", default=default_epfl_blif_dir(),
                    help="Root directory containing arithmetic/ and random_control/ "
                         "EPFL BLIF subdirectories (relative paths are resolved from "
                         "this script's directory)."
    )
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
        help="Path to the built test/blif_to_tcount shim. If omitted, "
             "caterpillar comparisons are skipped."
    )
    parser.add_argument("--timeout", type=float, default=120.0,
                         help="Per-DAG timeout (seconds) for the "
                              "caterpillar subprocess.")
    parser.add_argument("--keep-blif", action="store_true",
                         help="Don't delete the temp dir with generated "
                              ".blif files after the run.")
    parser.add_argument(
        "--debug", action="store_true",
        help="Print gate-group composition, gate-level toggle counts, and "
             "node-level compute/uncompute counts for OUR pipeline, per "
             "DAG. Use this to see exactly why ours-T is what it is -- "
             "see module docstring, 'DIAGNOSTICS (--debug)'."
    )
    args = parser.parse_args()

    requested_kinds = args.kinds or []
    requested_epfl = expand_epfl_benchmark_names(args.epfl_benchmarks)
    if not requested_kinds and not requested_epfl:
        parser.error("must request at least one input set via --kinds and/or "
                     "--epfl-benchmarks")
    epfl_blif_dir = resolve_epfl_blif_dir(args.epfl_blif_dir)

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

    for kind in requested_kinds:
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
                    "cat_t_count": None, "cat_and_pos": None,
                    "cat_corrected_t": None, "cat_error": None,
                })
                continue

            our = run_our_pipeline(
                net, max_pebbles=args.max_pebbles, debug=args.debug, label=label,
            )
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
                "cat_and_pos": cat["and_pos"],
                "cat_corrected_t": cat["corrected_t"],
                "cat_elapsed_s": cat["elapsed_s"],
                "cat_error": cat["error"],
            })

    for label in requested_epfl:
        blif_path = resolve_epfl_blif_path(epfl_blif_dir, label)
        if blif_path is None:
            rows.append({
                "label": label,
                "kind": "epfl",
                "num_pis": 0,
                "num_pos": 0,
                "num_gates": 0,
                "our_t_count": None,
                "our_qubits": None,
                "our_elapsed_s": None,
                "our_error": (
                    f"BLIF export failed: EPFL benchmark '{label}' not found under "
                    f"{epfl_blif_dir}"
                ),
                "cat_t_count": None,
                "cat_qubits": None,
                "cat_and_pos": None,
                "cat_corrected_t": None,
                "cat_elapsed_s": None,
                "cat_error": None,
            })
            continue

        try:
            net = read_blif(blif_path)
        except Exception as e:  # noqa: BLE001 -- report, don't crash the batch
            rows.append({
                "label": label,
                "kind": "epfl",
                "num_pis": 0,
                "num_pos": 0,
                "num_gates": 0,
                "our_t_count": None,
                "our_qubits": None,
                "our_elapsed_s": None,
                "our_error": f"BLIF export failed: {type(e).__name__}: {e}",
                "cat_t_count": None,
                "cat_qubits": None,
                "cat_and_pos": None,
                "cat_corrected_t": None,
                "cat_elapsed_s": None,
                "cat_error": None,
            })
            continue

        our = run_our_pipeline(
            net, max_pebbles=args.max_pebbles, debug=args.debug, label=label,
        )
        cat = run_caterpillar(args.caterpillar_bin, blif_path, timeout=args.timeout)

        rows.append({
            "label": label,
            "kind": "epfl",
            "num_pis": len(net.pis),
            "num_pos": len(net.pos),
            "num_gates": len(net.nodes) - len(net.pis),
            "our_t_count": our["t_count"],
            "our_qubits": our["qubit_count"],
            "our_elapsed_s": our["elapsed_s"],
            "our_error": our["error"],
            "cat_t_count": cat["t_count"],
            "cat_qubits": cat["qubits"],
            "cat_and_pos": cat["and_pos"],
            "cat_corrected_t": cat["corrected_t"],
            "cat_elapsed_s": cat["elapsed_s"],
            "cat_error": cat["error"],
        })

    print("\n" + "=" * 110)
    print("RESULTS")
    print("=" * 110)
    header = (
        f"{'label':32s} {'kind':12s} {'pis':>4s} {'pos':>4s} {'gates':>6s} "
        f"{'ours-T':>7s} {'ours-q':>7s} {'cat-T':>6s} {'cat-q':>6s} "
        f"{'and_pos':>8s} {'corr-T':>7s}"
    )
    print(header)
    print("-" * len(header))

    for r in rows:
        ours_t = str(r.get("our_t_count")) if r.get("our_t_count") is not None else "ERR"
        ours_q = str(r.get("our_qubits", "")) if r.get("our_qubits") is not None else "-"
        cat_t = str(r.get("cat_t_count")) if r.get("cat_t_count") is not None else "N/A"
        cat_q = str(r.get("cat_qubits")) if r.get("cat_qubits") is not None else "N/A"
        and_pos = str(r.get("cat_and_pos")) if r.get("cat_and_pos") is not None else "N/A"
        corr_t = str(r.get("cat_corrected_t")) if r.get("cat_corrected_t") is not None else "N/A"
        print(
            f"{r['label']:32s} {r['kind']:12s} {r['num_pis']:>4d} "
            f"{r['num_pos']:>4d} {r['num_gates']:>6d} "
            f"{ours_t:>7s} {ours_q:>7s} {cat_t:>6s} {cat_q:>6s} "
            f"{and_pos:>8s} {corr_t:>7s}"
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
        print("\n" + "-" * 110)
        print("SUMMARY (only rows with both T-counts available)")
        print("-" * 110)
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

        valid_corrected = [r for r in valid if r.get("cat_corrected_t") is not None]
        if valid_corrected:
            print("\n" + "-" * 110)
            print("SUMMARY (corrected caterpillar T-count: "
                  "2*t_count - and_pos, see module docstring)")
            print("-" * 110)
            total_corrected = sum(r["cat_corrected_t"] for r in valid_corrected)
            print(f"Total corrected caterpillar T-count: {total_corrected}")
            if total_corrected > 0:
                total_ours_corrected_set = sum(
                    r["our_t_count"] for r in valid_corrected
                )
                print(f"Ours / corrected-caterpillar ratio:  "
                      f"{total_ours_corrected_set / total_corrected:.3f}")
            for r in valid_corrected:
                delta = r["our_t_count"] - r["cat_corrected_t"]
                print(f"  {r['label']:32s} ours={r['our_t_count']:<6d} "
                      f"corrected_cat={r['cat_corrected_t']:<6d} "
                      f"(raw_cat={r['cat_t_count']}, and_pos={r['cat_and_pos']}) "
                      f"delta={delta:+d}")
    else:
        print("\nNo rows had both T-counts available -- nothing to summarize. "
              "Pass --caterpillar-bin to enable the comparison.")

    if cleanup_dir:
        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)
        print(f"\n(Temp BLIF dir {out_dir} removed; pass --keep-blif to retain it.)")


if __name__ == "__main__":
    main()
