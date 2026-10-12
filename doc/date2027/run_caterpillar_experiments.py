"""
Experiment harness: compares T-count between this project's gate-level
pebbling + gadget-synthesis pipeline and gmeuli/caterpillar (via
xagtdep's C++ integration), on a batch of randomly generated DAGs and/or
checked-in EPFL combinational benchmark BLIFs.

======================================================================
IMPORTANT NOTE
======================================================================
xagtdep does NOT expose a Python binding for caterpillar. This harness
shells out to a small companion C++ binary, `test/blif_to_tcount.cpp`
(built as a CMake target in xagtdep), which synthesizes a BLIF file via
caterpillar and prints a single-line JSON object with gate/T-counts to
stdout.

Build once, from the xagtdep repo root:

    cmake -B build
    cmake --build build -j$(nproc) --target blif_to_tcount

Then point this script at it with --caterpillar-bin build/blif_to_tcount.

======================================================================
EXCLUDING BENCHMARKS (--exclude-benchmarks)
======================================================================
Pass --exclude-benchmarks <name> [<name> ...] to drop one or more
benchmark names from the FINAL requested EPFL benchmark list, AFTER
`--epfl-benchmarks` (including `all`) has already been expanded.

======================================================================
CATERPILLAR-ONLY MODE (--caterpillar-only)
======================================================================
Pass --caterpillar-only to skip `run_our_pipeline` ENTIRELY for every
DAG in the batch -- only `run_caterpillar` (the C++ blif_to_tcount
shim) is invoked.

======================================================================
DIAGNOSTICS (--debug)
======================================================================
Pass --debug to print, for every DAG in the batch (ignored entirely in
--caterpillar-only mode):
  - the chosen pebble budget and (if --use-tcount-search is passed)
    every candidate pebble budget tried; or (by default) which
    scheduling METHOD `pebble_gates_greedy_first` ended up using.
  - the gate-group partition, gate-level toggle counts, and
    node-level compute/uncompute counts.

======================================================================
SCHEDULING METHOD (run_our_pipeline): GREEDY-FIRST vs. T-COUNT SAT SEARCH
======================================================================
`run_our_pipeline` defaults to `pebbling_solver.pebble_gates_
greedy_first` for its scheduling step. By DEFAULT, if greedy
scheduling DEADLOCKS (`solve_max_pebbles` smaller than some single
group's own weight -- which should be impossible given `build_gate_
groups`'s hard per-group weight ceiling, UNLESS the caller passes a
`--solve-max-pebbles` smaller than `--grouping-max-pebbles`), the
error PROPAGATES DIRECTLY as a row-level error -- it is NOT silently
caught and retried via the expensive Z3-based `pebble_gates` fallback.
Pass `--sat-fallback` to opt back into catching a greedy deadlock and
falling back to the Z3-based search instead of erroring out.

Pass `--use-tcount-search` to opt back into the PREVIOUS default
behavior entirely: `find_best_gate_pebbling_by_tcount`'s T-count-guided
bidirectional Z3 search, using `_precise_t_count_fn` to evaluate every
candidate pebble budget by actually synthesizing the real circuit.

`--clean-pebbles`, `--sat-fallback`, `--force-sat` expose
`pebble_gates_greedy_first`'s knobs through the CLI (ignored if
`--use-tcount-search` is passed).

======================================================================
GATE-GROUPING TOPOLOGICAL ORDER (--topo-order)
======================================================================
`build_gate_groups`'s Phase 1 greedy pebble-budget fill walks nodes in
some topological order -- WHICH order affects which nodes end up
adjacent, and therefore which nodes get grouped together. See
pebbling_solver.py's module docstring, "TOPOLOGICAL ORDER CHOICE", for
the three choices:
  - "creation" (net.nodes as-is)
  - "dependency_chain" (chains a producer directly to one of its own
    consumers, DFS-style) -- this script's own default.
  - "breadth_first" (keeps ALL siblings of a shared producer adjacent,
    which can help the "SHARED-FANOUT-CONSUMER COST EXEMPTION" and
    "SIBLING FANOUT MERGING" phases do less work, since Phase 1
    already co-locates fanout siblings).
`--topo-order` is forwarded directly to `build_gate_groups`. Ignored
entirely in --caterpillar-only mode.

======================================================================
PEBBLE BUDGET (grouping budget vs. solve-probe budget, decoupled)
======================================================================
`build_gate_groups`'s own `max_pebbles` parameter and the scheduling
step's `max_pebbles` are two DIFFERENT numbers, not the same value
reused twice. `run_our_pipeline` auto-sizes TWO separate values when
`--max-pebbles`/`--grouping-max-pebbles`/`--solve-max-pebbles` are
omitted:
  - `grouping_max_pebbles`: auto-sized to `max(1, len(net.pos) * 2)`.
  - `solve_max_pebbles`: auto-sized to `grouping_max_pebbles * 2`, to
    give the scheduler headroom for MULTIPLE groups to be
    simultaneously live.

IMPORTANT: since `build_gate_groups` now enforces a HARD per-group
weight ceiling equal to `grouping_max_pebbles` (see pebbling_solver.py
module docstring, "HARD PER-GROUP WEIGHT CEILING"), `solve_max_pebbles`
must always be `>= grouping_max_pebbles` for the default greedy-first
scheduling path to have any chance of succeeding -- the auto-sizing
above already guarantees this (`solve_max_pebbles = 2x
grouping_max_pebbles`), but passing a SMALLER `--solve-max-pebbles`
than `--grouping-max-pebbles` explicitly will reliably deadlock (and,
by default, raise immediately rather than silently falling back to
Z3 -- see "SCHEDULING METHOD" above).

======================================================================
ERROR-PATH PEBBLING DEBUG JSON DUMP
======================================================================
Previously, if `run_our_pipeline`'s scheduling step RAISED (e.g. a
greedy deadlock propagating through `pebble_gates_greedy_first` with
its new default `use_sat_fallback=False` -- see "SCHEDULING METHOD"
above -- or `build_gate_groups` itself raising `_validate_max_group_
weight`'s defensive `AssertionError`), NO pebbling debug JSON was ever
written for that DAG, even though `dump_json_path` was computed and
even though `build_gate_groups` may have already succeeded and
produced a full, inspectable set of gate groups. This made it hard to
post-mortem exactly which gate groups/weights caused a deadlock,
since the only artifact left behind was a one-line error string.

`run_our_pipeline`'s `except Exception` handler now ALSO calls
`pebbling_solver.dump_pebbling_debug_json` (when `dump_json_path` is
set), writing out:
  - `gate_groups`: the full set of gate groups, IF `build_gate_groups`
    itself succeeded before the failure (None otherwise, e.g. if
    `build_gate_groups` is what raised).
  - `extra.outcome`: `"error"`.
  - `extra.error`: the exact exception message.
  - `extra.method_used`: whichever scheduling method had already been
    selected at the point of failure (`None` if the failure occurred
    before any scheduling path even started, e.g. inside
    `build_gate_groups` itself).
  - `extra.greedy_error`: the deadlock message from
    `schedule_gates_greedy_pebble_limited`, if that specific function
    is what raised (`None` otherwise, e.g. if the SAT path or
    `build_circuit_from_node_schedule` is what failed instead).

This error-path dump is wrapped in its own `try/except` so that a
failure in the DEBUG DUMP ITSELF (e.g. a filesystem permission issue)
can never mask the original pipeline error or crash the whole batch --
it only prints a one-line warning and continues.

NOTE: if the SAT-based paths (`find_best_gate_pebbling_by_tcount`,
or `pebble_gates` via a greedy-fallback-then-SAT path) themselves
already wrote a JSON dump internally (e.g. an UNSAT-exhausted or
timeout outcome logged via their OWN `dump_json` parameter) before
re-raising/returning an error that `run_our_pipeline` then catches,
this OUTER error-path dump will simply OVERWRITE that file with the
higher-level error summary instead -- the inner dump's `escalation_log`
detail is not preserved in that specific overwrite case. This is an
accepted tradeoff for keeping the error path simple; the inner dumps
are still visible in the console log for that DAG even if the file on
disk reflects only the final error state.

======================================================================
PEBBLING DEBUG JSON DUMP / TIKZ NETWORK DIAGRAM DUMP (on by default)
======================================================================
Every DAG run through `run_our_pipeline` dumps its solved gate-level
schedule (on success, or whatever partial state exists on failure --
see "ERROR-PATH PEBBLING DEBUG JSON DUMP" above) and a TikZ diagram of
the network, unless disabled via --no-dump-pebbling-json /
--no-dump-tikz.

======================================================================
BLIF EXPORT
======================================================================
`write_blif` is the companion writer to `pebbling_solver.read_blif`.

======================================================================
CATERPILLAR "CORRECTED" T-COUNT ESTIMATE
======================================================================
    corrected_t = 2 * t_count - and_pos

======================================================================
SYMMETRIC PRE-OPTIMIZATION MODE (--pre-optimize / --optimizer-bin)
======================================================================
Pass --pre-optimize <const_fanin|cut_rewriting|resub> together with
--optimizer-bin <path/to/blif_optimize> to run ONE mockturtle XAG
optimization pass (via the `test/blif_optimize.cpp` shim) on a
benchmark's BLIF file ONCE, BEFORE it is fed into EITHER pipeline --
the SAME resulting optimized BLIF path is then used for BOTH
`run_our_pipeline` (via `read_blif`) AND `run_caterpillar` (via the
`blif_to_tcount` shim). This keeps the T-count comparison symmetric:
both pipelines see the identical (optimized) circuit, so any T-count
delta reflects a genuine difference in SYNTHESIS strategy, not a
difference in which circuit was measured.

This is OFF by default (today's unoptimized comparison is unchanged
unless you explicitly opt in). When enabled:
  - `blif_optimize` is invoked ONCE per benchmark/DAG, producing a
    temp optimized BLIF file under `out_dir` (named
    "<label>.optimized.<pass>.blif").
  - If `blif_optimize` fails for a given row (binary missing, BLIF
    parse error, unsupported construct, timeout, etc.), that row is
    recorded with `pre_optimize_error` set and BOTH `our_*`/`cat_*`
    fields are skipped (None) for that row -- we deliberately do NOT
    silently fall back to the unoptimized BLIF for just one row, since
    that would silently break symmetry for that single row without
    any visible signal in the results table.
  - Every row's dict gains a `pre_optimize` field (the pass name, or
    "none" when --pre-optimize is not passed) and
    `pre_optimize_nodes_before` / `pre_optimize_nodes_after` /
    `pre_optimize_and_before` / `pre_optimize_and_after` fields (all
    None when --pre-optimize is not passed), so optimized and
    unoptimized runs/rows are never silently conflated when reading a
    results table or any saved CSV.
  - `--pre-optimize` requires `--optimizer-bin` to also be given (and
    vice versa); passing only one of the two is a usage error.

Example:

    python run_caterpillar_experiments.py --epfl-benchmarks bar \\
        --caterpillar-bin build/blif_to_tcount \\
        --optimizer-bin build/blif_optimize \\
        --pre-optimize const_fanin

NOTE: `cut_rewriting`/`resub` are more aggressive than `const_fanin`
and may restructure node identities enough that other diagnostics
(e.g. --debug's per-node rule/cost breakdown) are no longer directly
comparable to an unoptimized baseline run for the same benchmark.
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
    find_best_gate_pebbling_by_tcount,
    pebble_gates_greedy_first,
    expand_gate_schedule,
    read_blif,
    select_gadget_rule,
    dump_pebbling_debug_json,
    print_po_lockin_diagnostics,
)

from reed_muller_priority_cuts import random_reed_muller_priority_cut_dag
from gate_schedule_to_circuit import build_circuit_from_node_schedule
from tikz_network_export import dump_tikz_network


# ---------------------------------------------------------------------------
# BLIF writer (companion to pebbling_solver.read_blif)
# ---------------------------------------------------------------------------

def write_blif(net: PebblingNetwork, path, model_name="pebbling_dag"):
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
                lines.append("0 1")
            else:
                lines.append("1 1")
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
# Symmetric pre-optimization via the blif_optimize shim
# ---------------------------------------------------------------------------

def run_blif_optimize(blif_path, optimizer_bin, pass_name, out_path, timeout=60):
    """
    Runs `optimizer_bin <blif_path> <out_path> --pass=<pass_name>` and
    parses its single-line JSON stdout. Returns a dict with keys:
    ok, out_path (None on failure), nodes_before, and_before, nodes_after,
    and_after, elapsed_s, error.

    The CALLER is responsible for using `out_path` (on success) for BOTH
    `run_our_pipeline` (via `read_blif`) and `run_caterpillar` -- this is
    what makes the comparison symmetric. See module docstring, "SYMMETRIC
    PRE-OPTIMIZATION MODE".
    """
    if not optimizer_bin:
        return {
            "ok": False, "out_path": None, "nodes_before": None,
            "and_before": None, "nodes_after": None, "and_after": None,
            "elapsed_s": 0.0, "error": "No --optimizer-bin provided; skipped.",
        }

    if not os.path.isfile(optimizer_bin) or not os.access(optimizer_bin, os.X_OK):
        return {
            "ok": False, "out_path": None, "nodes_before": None,
            "and_before": None, "nodes_after": None, "and_after": None,
            "elapsed_s": 0.0,
            "error": f"blif_optimize binary not found or not executable: "
                     f"{optimizer_bin}. Build test/blif_optimize.cpp first "
                     f"(cmake --build build --target blif_optimize).",
        }

    start = time.time()
    try:
        proc = subprocess.run(
            [optimizer_bin, blif_path, out_path, f"--pass={pass_name}"],
            capture_output=True, text=True, timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return {
            "ok": False, "out_path": None, "nodes_before": None,
            "and_before": None, "nodes_after": None, "and_after": None,
            "elapsed_s": time.time() - start,
            "error": f"blif_optimize shim timed out after {timeout}s",
        }
    except OSError as e:
        return {
            "ok": False, "out_path": None, "nodes_before": None,
            "and_before": None, "nodes_after": None, "and_after": None,
            "elapsed_s": time.time() - start,
            "error": f"failed to launch blif_optimize shim: {e}",
        }

    elapsed = time.time() - start
    raw = (proc.stdout or "").strip()

    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, ValueError):
        return {
            "ok": False, "out_path": None, "nodes_before": None,
            "and_before": None, "nodes_after": None, "and_after": None,
            "elapsed_s": elapsed,
            "error": (
                f"blif_optimize shim produced non-JSON stdout (exit "
                f"{proc.returncode}): stdout={raw!r} stderr={proc.stderr!r}"
            ),
        }

    if not payload.get("ok"):
        return {
            "ok": False, "out_path": None, "nodes_before": None,
            "and_before": None, "nodes_after": None, "and_after": None,
            "elapsed_s": elapsed,
            "error": payload.get("error", "unknown error from blif_optimize shim"),
        }

    return {
        "ok": True,
        "out_path": out_path,
        "nodes_before": payload.get("nodes_before"),
        "and_before": payload.get("and_before"),
        "nodes_after": payload.get("nodes_after"),
        "and_after": payload.get("and_after"),
        "elapsed_s": elapsed,
        "error": None,
    }


# ---------------------------------------------------------------------------
# caterpillar invocation via the blif_to_tcount JSON shim
# ---------------------------------------------------------------------------

def run_caterpillar(caterpillar_bin, blif_path, timeout=120):
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
# Diagnostics
# ---------------------------------------------------------------------------

_RULE_T_COST = {
    1: 4,
    2: None,
    3: 6,
    4: 7,
    5: 0,
    6: None,
}


def _precise_t_count_fn(net, gates, gate_steps):
    """
    `t_count_fn` passed to `find_best_gate_pebbling_by_tcount` (only
    used when `--use-tcount-search` is passed).
    """
    node_steps = expand_gate_schedule(gates, gate_steps)
    circuit_result = build_circuit_from_node_schedule(net, node_steps)
    return sum(
        1 for (_node, _phase, op) in circuit_result.ops
        if op.kind in ("T", "Tdg")
    )


def print_pipeline_diagnostics(label, net, gates, gate_steps, node_steps):
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
# Our own pipeline: gate groups -> scheduling -> circuit -> T-count
# ---------------------------------------------------------------------------

def run_our_pipeline(net, grouping_max_pebbles=None, solve_max_pebbles=None,
                      max_steps="auto", debug=False, label="",
                      dump_json_path=None, dump_tikz_path=None,
                      use_tcount_search=False, clean_pebbles=None,
                      use_sat_fallback=False, force_sat=False,
                      topo_order="dependency_chain"):
    """
    Runs build_gate_groups -> [scheduling] -> expand_gate_schedule ->
    build_circuit_from_node_schedule on `net`.

    `topo_order` is forwarded directly to `build_gate_groups` -- see
    module docstring, "GATE-GROUPING TOPOLOGICAL ORDER (--topo-order)",
    and pebbling_solver.py's own module docstring, "TOPOLOGICAL ORDER
    CHOICE", for the available choices ("creation",
    "dependency_chain", "breadth_first").

    `use_sat_fallback` (default False): if the default greedy-first
    scheduling path DEADLOCKS, the error PROPAGATES DIRECTLY (caught
    by this function's own try/except and reported as a row-level
    `our_error`) rather than silently falling back to the Z3-based
    search. Pass `use_sat_fallback=True` to restore the old fallback
    behavior. See module docstring, "SCHEDULING METHOD".

    If ANY step raises (build_gate_groups, the scheduling step, or
    build_circuit_from_node_schedule), and `dump_json_path` is set, a
    best-effort pebbling debug JSON is STILL written -- see module
    docstring, "ERROR-PATH PEBBLING DEBUG JSON DUMP" -- containing
    whatever gate groups were already built (if any), the error
    message, and the scheduling method/greedy_error already known at
    the point of failure.
    """
    if grouping_max_pebbles is None:
        grouping_max_pebbles = max(1, len(net.pos) * 2)

    if solve_max_pebbles is None:
        solve_max_pebbles = max(1, grouping_max_pebbles * 2)

    start = time.time()
    print(f"[run_our_pipeline] starting: label={label!r} "
          f"pis={len(net.pis)} pos={len(net.pos)} "
          f"gates={len(net.nodes) - len(net.pis)} "
          f"grouping_max_pebbles={grouping_max_pebbles} "
          f"solve_max_pebbles(probe)={solve_max_pebbles} "
          f"method={'tcount_sat_search' if use_tcount_search else 'greedy_first'} "
          f"topo_order={topo_order!r}")

    method_used = None
    greedy_error = None
    gates = None  # populated as soon as build_gate_groups succeeds, so the
                  # except block below can still dump gate-group info even
                  # if scheduling itself fails/raises.

    try:
        gates = build_gate_groups(net, max_pebbles=grouping_max_pebbles,
                                   topo_order=topo_order)

        po_lockin_info = print_po_lockin_diagnostics(gates, net)
        if po_lockin_info["total_po_clean_weight"] > solve_max_pebbles:
            print(f"[run_our_pipeline] WARNING: solve_max_pebbles="
                  f"{solve_max_pebbles} is already below the hard "
                  f"PO clean-weight floor of "
                  f"{po_lockin_info['total_po_clean_weight']} -- "
                  f"scheduling WILL deadlock regardless of method or "
                  f"topo_order; raise --solve-max-pebbles before "
                  f"retrying.")
            
        if use_tcount_search:
            search_result = find_best_gate_pebbling_by_tcount(
                gates, max_pebbles=solve_max_pebbles, net=net,
                t_count_fn=_precise_t_count_fn,
                max_steps=max_steps, verbose=False,
                dump_json=dump_json_path,
            )
            gate_steps = search_result["gate_steps"]
            chosen_max_pebbles = search_result["max_pebbles"]
            method_used = "tcount_sat_search"
        else:
            schedule_result = pebble_gates_greedy_first(
                gates, max_pebbles=solve_max_pebbles, net=net,
                clean_pebbles=clean_pebbles,
                use_sat_fallback=use_sat_fallback,
                force_sat=force_sat,
                verbose=False,
                max_steps=max_steps,
                dump_json=dump_json_path,
            )
            gate_steps = schedule_result["gate_steps"]
            chosen_max_pebbles = solve_max_pebbles
            method_used = schedule_result["method"]
            greedy_error = schedule_result["greedy_error"]

        node_steps = expand_gate_schedule(gates, gate_steps)

        if debug:
            if use_tcount_search:
                print(f"[debug] find_best_gate_pebbling_by_tcount chose "
                      f"max_pebbles={chosen_max_pebbles} "
                      f"(started from {solve_max_pebbles}; t_count="
                      f"{search_result['t_count']}); attempts tried: "
                      f"{search_result['tried']}")
            else:
                print(f"[debug] pebble_gates_greedy_first used method="
                      f"{method_used!r} at max_pebbles={chosen_max_pebbles}"
                      + (f" (greedy deadlock: {greedy_error})" if greedy_error else ""))
            print_pipeline_diagnostics(label, net, gates, gate_steps, node_steps)

        if dump_tikz_path:
            try:
                dump_tikz_network(dump_tikz_path, net, gates=gates, title=label)
            except Exception as e:  # noqa: BLE001 -- purely visual, don't crash the batch
                print(f"    [tikz export] failed for {label}: "
                      f"{type(e).__name__}: {e}")

        circuit_result = build_circuit_from_node_schedule(net, node_steps)
    except Exception as e:  # noqa: BLE001 -- report, don't crash the batch
        error_str = f"{type(e).__name__}: {e}"

        # Dump whatever partial pebbling-debug info we have, even on
        # failure -- e.g. a greedy deadlock (schedule_gates_greedy_
        # pebble_limited's RuntimeError) now propagates straight through
        # pebble_gates_greedy_first (default use_sat_fallback=False)
        # WITHOUT ever calling dump_pebbling_debug_json itself. Without
        # this, such a failure left NO debug JSON on disk at all, making
        # it hard to inspect which gate groups/weights caused the
        # deadlock. We still dump gate_groups (if build_gate_groups
        # itself succeeded) and the error message/method_used, even
        # though no gate_steps/node_steps exist in this case. See module
        # docstring, "ERROR-PATH PEBBLING DEBUG JSON DUMP".
        if dump_json_path:
            try:
                dump_pebbling_debug_json(
                    dump_json_path,
                    gate_groups=gates,
                    extra={
                        "outcome": "error",
                        "error": error_str,
                        "method_used": method_used,
                        "greedy_error": greedy_error,
                    },
                )
            except Exception as dump_e:  # noqa: BLE001 -- never let the debug dump itself crash the batch
                print(f"    [pebbling debug json] failed to dump error "
                      f"state for {label}: {type(dump_e).__name__}: {dump_e}")

        return {
            "ok": False, "t_count": None, "qubit_count": None,
            "num_ops": None, "max_pebbles": None,
            "grouping_max_pebbles": grouping_max_pebbles,
            "method": method_used, "greedy_error": greedy_error,
            "elapsed_s": time.time() - start,
            "error": error_str,
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
        "max_pebbles": chosen_max_pebbles,
        "grouping_max_pebbles": grouping_max_pebbles,
        "method": method_used,
        "greedy_error": greedy_error,
        "elapsed_s": time.time() - start,
        "error": None,
    }


def _skipped_ours_result():
    return {
        "ok": False,
        "t_count": None,
        "qubit_count": None,
        "num_ops": None,
        "max_pebbles": None,
        "grouping_max_pebbles": None,
        "method": None,
        "greedy_error": None,
        "elapsed_s": None,
        "error": "skipped (--caterpillar-only passed)",
    }


def _skipped_pre_optimize_row_fields(pre_optimize_name):
    """
    Shared field set for a row that was skipped entirely because
    --pre-optimize was requested but blif_optimize failed for this
    specific benchmark/DAG. See module docstring, "SYMMETRIC
    PRE-OPTIMIZATION MODE".
    """
    return {
        "pre_optimize": pre_optimize_name,
        "pre_optimize_nodes_before": None,
        "pre_optimize_and_before": None,
        "pre_optimize_nodes_after": None,
        "pre_optimize_and_after": None,
        "our_t_count": None, "our_qubits": None, "our_max_pebbles": None,
        "our_grouping_max_pebbles": None, "our_method": None,
        "our_elapsed_s": None,
        "cat_t_count": None, "cat_qubits": None, "cat_and_pos": None,
        "cat_corrected_t": None, "cat_elapsed_s": None,
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


def apply_benchmark_exclusions(requested, exclude_names):
    if not exclude_names:
        return requested
    exclude_set = set(exclude_names)
    return [name for name in requested if name not in exclude_set]


def generate_dags(num_dags, num_pis, num_gates, num_outputs, seed_base, kind):
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
        "--exclude-benchmarks", nargs="+", default=None,
        help="EPFL benchmark names to EXCLUDE from the final requested "
             "list, applied AFTER --epfl-benchmarks (including 'all') is "
             "expanded."
    )
    parser.add_argument(
                    "--epfl-blif-dir", default=default_epfl_blif_dir(),
                    help="Root directory containing arithmetic/ and random_control/ "
                         "EPFL BLIF subdirectories."
    )
    parser.add_argument("--num-pis", type=int, default=4)
    parser.add_argument("--num-gates", type=int, default=8,
                         help="Used only for --kinds random.")
    parser.add_argument("--num-outputs", type=int, default=2,
                         help="Used only for --kinds reed_muller.")
    parser.add_argument("--seed-base", type=int, default=1000)
    parser.add_argument(
        "--max-pebbles", type=int, default=None,
        help="CONVENIENCE ALIAS: sets BOTH --grouping-max-pebbles and "
             "--solve-max-pebbles to this same value. Ignored for either "
             "one that is ALSO given explicitly."
    )
    parser.add_argument(
        "--grouping-max-pebbles", type=int, default=None,
        help="HARD per-group weight ceiling passed to build_gate_groups. "
             "If omitted (and --max-pebbles is also omitted), auto-sized "
             "to 2x the number of primary outputs."
    )
    parser.add_argument(
        "--solve-max-pebbles", type=int, default=None,
        help="Pebble budget passed to OUR pipeline's scheduling step. "
             "MUST be >= --grouping-max-pebbles for the default "
             "greedy-first scheduling path to have any chance of "
             "succeeding (see module docstring, 'PEBBLE BUDGET'). If "
             "omitted (and --max-pebbles is also omitted), auto-sized to "
             "2x the resolved --grouping-max-pebbles value."
    )
    parser.add_argument(
        "--use-tcount-search", action="store_true",
        help="Use the T-count-guided find_best_gate_pebbling_by_tcount "
             "bidirectional Z3 search for OUR pipeline's scheduling step "
             "(the PREVIOUS default), instead of the new default "
             "pebble_gates_greedy_first."
    )
    parser.add_argument(
        "--clean-pebbles", type=int, default=None,
        help="Optional narrower cap on sustained CLEAN (boundary-output) "
             "weight, used by the greedy scheduler in the default "
             "pebble_gates_greedy_first path. Ignored if "
             "--use-tcount-search is passed."
    )
    parser.add_argument(
        "--sat-fallback", action="store_true",
        help="If greedy scheduling deadlocks (the default "
             "pebble_gates_greedy_first path), fall back to the Z3-based "
             "pebble_gates search instead of raising immediately (the "
             "new default). Ignored if --use-tcount-search is passed."
    )
    parser.add_argument(
        "--force-sat", action="store_true",
        help="Skip greedy scheduling entirely and go straight to the "
             "Z3-based pebble_gates search. Ignored if "
             "--use-tcount-search is passed."
    )
    parser.add_argument(
        "--topo-order", choices=["creation", "dependency_chain", "breadth_first"],
        default="dependency_chain",
        help="Topological order build_gate_groups's Phase 1 greedy "
             "pebble-budget fill uses. 'breadth_first' keeps all "
             "siblings of a shared fanout producer adjacent, which can "
             "reduce how much work the sibling-fanout-merge phase has "
             "to do -- see module docstring, 'GATE-GROUPING "
             "TOPOLOGICAL ORDER (--topo-order)'. Ignored in "
             "--caterpillar-only mode."
    )
    parser.add_argument("--out-dir", default=None,
                         help="Directory to write .blif files into "
                              "(default: a fresh temp dir).")
    parser.add_argument(
        "--caterpillar-bin", default=None,
        help="Path to the built test/blif_to_tcount shim."
    )
    parser.add_argument(
        "--caterpillar-only", action="store_true",
        help="Skip OUR pipeline entirely for every DAG in the batch -- "
             "only run the caterpillar shim."
    )
    parser.add_argument(
        "--pre-optimize", choices=["const_fanin", "cut_rewriting", "resub"],
        default=None,
        help="Run a mockturtle XAG optimization pass ONCE via the "
             "blif_optimize shim before feeding the SAME resulting BLIF "
             "into BOTH run_our_pipeline and run_caterpillar, for a "
             "symmetric, apples-to-apples comparison on the optimized "
             "circuit. Requires --optimizer-bin. Off by default (today's "
             "unoptimized comparison). See module docstring, 'SYMMETRIC "
             "PRE-OPTIMIZATION MODE'."
    )
    parser.add_argument(
        "--optimizer-bin", default=None,
        help="Path to the built test/blif_optimize shim. Required if "
             "--pre-optimize is set."
    )
    parser.add_argument("--timeout", type=float, default=120.0,
                         help="Per-DAG timeout (seconds) for the "
                              "caterpillar subprocess.")
    parser.add_argument(
        "--optimize-timeout", type=float, default=60.0,
        help="Per-DAG timeout (seconds) for the blif_optimize subprocess. "
             "Only used if --pre-optimize is passed."
    )
    parser.add_argument("--keep-blif", action="store_true",
                         help="Don't delete the temp dir with generated "
                              ".blif files after the run.")
    parser.add_argument(
        "--debug", action="store_true",
        help="Print scheduling diagnostics, gate-group composition, "
             "gate-level toggle counts, and node-level compute/uncompute "
             "counts for OUR pipeline, per DAG."
    )
    parser.add_argument(
        "--dump-json-dir", default=None,
        help="Directory to write per-DAG pebbling debug JSON files into."
    )
    parser.add_argument(
        "--no-dump-pebbling-json", action="store_true",
        help="Disable the default per-DAG pebbling debug JSON dump "
             "(including the error-path dump -- see module docstring, "
             "'ERROR-PATH PEBBLING DEBUG JSON DUMP')."
    )
    parser.add_argument(
        "--dump-tikz-dir", default=None,
        help="Directory to write per-DAG TikZ network diagrams into."
    )
    parser.add_argument(
        "--no-dump-tikz", action="store_true",
        help="Disable the default per-DAG TikZ network diagram dump."
    )
    args = parser.parse_args()

    if args.max_pebbles is not None:
        if args.grouping_max_pebbles is None:
            args.grouping_max_pebbles = args.max_pebbles
        if args.solve_max_pebbles is None:
            args.solve_max_pebbles = args.max_pebbles

    if args.use_tcount_search and (
            args.clean_pebbles is not None or args.sat_fallback or args.force_sat):
        parser.error("--clean-pebbles/--sat-fallback/--force-sat only "
                      "apply to the default greedy-first scheduling path, "
                      "not --use-tcount-search.")

    if bool(args.pre_optimize) != bool(args.optimizer_bin):
        parser.error("--pre-optimize and --optimizer-bin must be given "
                      "together (both or neither).")

    requested_kinds = args.kinds or []
    requested_epfl = expand_epfl_benchmark_names(args.epfl_benchmarks)

    excluded_requested = []
    if args.exclude_benchmarks:
        before_exclusion = set(requested_epfl)
        requested_epfl = apply_benchmark_exclusions(requested_epfl, args.exclude_benchmarks)
        excluded_requested = sorted(before_exclusion - set(requested_epfl))

    if not requested_kinds and not requested_epfl:
        parser.error("must request at least one input set via --kinds and/or "
                     "--epfl-benchmarks (after applying --exclude-benchmarks, "
                     "if any)")
    epfl_blif_dir = resolve_epfl_blif_dir(args.epfl_blif_dir)

    out_dir = args.out_dir
    cleanup_dir = False
    if out_dir is None:
        out_dir = tempfile.mkdtemp(prefix="pebbling_vs_caterpillar_")
        cleanup_dir = not args.keep_blif
    else:
        os.makedirs(out_dir, exist_ok=True)

    dump_json_enabled = (not args.no_dump_pebbling_json) and (not args.caterpillar_only)
    dump_json_dir = args.dump_json_dir or out_dir
    if dump_json_enabled:
        os.makedirs(dump_json_dir, exist_ok=True)

    dump_tikz_enabled = (not args.no_dump_tikz) and (not args.caterpillar_only)
    dump_tikz_dir = args.dump_tikz_dir or out_dir
    if dump_tikz_enabled:
        os.makedirs(dump_tikz_dir, exist_ok=True)

    def _dump_json_path_for(label):
        if not dump_json_enabled:
            return None
        return os.path.join(dump_json_dir, f"{label}_pebbling_debug.json")

    def _dump_tikz_path_for(label):
        if not dump_tikz_enabled:
            return None
        return os.path.join(dump_tikz_dir, f"{label}_network.tex")

    pre_optimize_name = args.pre_optimize or "none"

    def _optimized_blif_path_for(label):
        return os.path.join(out_dir, f"{label}.optimized.{args.pre_optimize}.blif")

    print(f"BLIF files will be written to: {out_dir}")
    if excluded_requested:
        print(f"Excluded {len(excluded_requested)} EPFL benchmark(s) via "
              f"--exclude-benchmarks: {', '.join(excluded_requested)}")
    if args.caterpillar_only:
        print("CATERPILLAR-ONLY MODE: our pipeline will NOT be run for any "
              "DAG in this batch.")
    else:
        print(f"Scheduling method: "
              f"{'--use-tcount-search (T-count-guided Z3 search)' if args.use_tcount_search else 'greedy-first (default; deadlock RAISES unless --sat-fallback is passed)'}")
        print(f"Pebble budgets: --grouping-max-pebbles="
              f"{args.grouping_max_pebbles if args.grouping_max_pebbles is not None else '(auto: 2x #POs)'}"
              f", --solve-max-pebbles="
              f"{args.solve_max_pebbles if args.solve_max_pebbles is not None else '(auto: 2x grouping budget)'}")
        print(f"Gate-grouping topo order: {args.topo_order!r} "
              f"(see module docstring, 'GATE-GROUPING TOPOLOGICAL "
              f"ORDER (--topo-order)')")
    if args.pre_optimize:
        print(f"PRE-OPTIMIZATION ENABLED: --pre-optimize={args.pre_optimize!r} "
              f"via --optimizer-bin={args.optimizer_bin!r}. BOTH pipelines "
              f"will be run on the SAME optimized BLIF per row -- see "
              f"module docstring, 'SYMMETRIC PRE-OPTIMIZATION MODE'.")
    if dump_json_enabled:
        print(f"Pebbling debug JSON files will be written to: {dump_json_dir} "
              f"(including a best-effort dump on scheduling/build failures -- "
              f"see module docstring, 'ERROR-PATH PEBBLING DEBUG JSON DUMP')")
    elif not args.caterpillar_only:
        print("Pebbling debug JSON dump disabled (--no-dump-pebbling-json).")
    if dump_tikz_enabled:
        print(f"TikZ network diagrams will be written to: {dump_tikz_dir}")
    elif not args.caterpillar_only:
        print("TikZ network diagram dump disabled (--no-dump-tikz).")
    if not args.caterpillar_bin:
        print("NOTE: --caterpillar-bin not provided; caterpillar T-counts "
              "will be skipped" +
              (", and --caterpillar-only has nothing to run." if args.caterpillar_only
               else ", only our own pipeline's T-count is reported."))

    rows = []

    def _run_ours(net, label):
        if args.caterpillar_only:
            return _skipped_ours_result()
        return run_our_pipeline(
            net, grouping_max_pebbles=args.grouping_max_pebbles,
            solve_max_pebbles=args.solve_max_pebbles,
            debug=args.debug, label=label,
            dump_json_path=_dump_json_path_for(label),
            dump_tikz_path=_dump_tikz_path_for(label),
            use_tcount_search=args.use_tcount_search,
            clean_pebbles=args.clean_pebbles,
            use_sat_fallback=args.sat_fallback,
            force_sat=args.force_sat,
            topo_order=args.topo_order,
        )

    def _maybe_pre_optimize(label, blif_path):
        """
        If --pre-optimize is enabled, runs blif_optimize on `blif_path`
        and returns (effective_blif_path, opt_info_fields, skip_row).
        `skip_row` is a complete row dict (already filled with
        _skipped_pre_optimize_row_fields-style None values) when
        blif_optimize FAILS for this benchmark -- the caller should
        append `skip_row` to `rows` and `continue` without running
        either pipeline, to avoid silently breaking symmetry for this
        one row. Returns (blif_path, {}, None) unchanged when
        --pre-optimize is not enabled.
        """
        if not args.pre_optimize:
            return blif_path, {
                "pre_optimize": "none",
                "pre_optimize_nodes_before": None,
                "pre_optimize_and_before": None,
                "pre_optimize_nodes_after": None,
                "pre_optimize_and_after": None,
            }, None

        opt_out_path = _optimized_blif_path_for(label)
        opt = run_blif_optimize(
            blif_path, args.optimizer_bin, args.pre_optimize, opt_out_path,
            timeout=args.optimize_timeout,
        )
        if not opt["ok"]:
            skip_row = {
                "label": label, "kind": "epfl",  # overwritten by caller if needed
                "num_pis": 0, "num_pos": 0, "num_gates": 0,
                "our_error": None,
                "cat_error": f"pre-optimize skipped this row: {opt['error']}",
            }
            skip_row.update(_skipped_pre_optimize_row_fields(pre_optimize_name))
            skip_row["pre_optimize_error"] = opt["error"]
            return None, {}, skip_row

        opt_fields = {
            "pre_optimize": pre_optimize_name,
            "pre_optimize_nodes_before": opt["nodes_before"],
            "pre_optimize_and_before": opt["and_before"],
            "pre_optimize_nodes_after": opt["nodes_after"],
            "pre_optimize_and_after": opt["and_after"],
        }
        return opt["out_path"], opt_fields, None

    for kind in requested_kinds:
        for label, net in generate_dags(
            args.num_dags, args.num_pis, args.num_gates, args.num_outputs,
            args.seed_base, kind,
        ):
            blif_path = os.path.join(out_dir, f"{label}.blif")
            try:
                write_blif(net, blif_path, model_name=label)
            except ValueError as e:
                row = {
                    "label": label, "kind": kind,
                    "num_pis": len(net.pis), "num_pos": len(net.pos),
                    "num_gates": len(net.nodes) - len(net.pis),
                    "our_t_count": None, "our_error": f"BLIF export failed: {e}",
                    "our_max_pebbles": None, "our_grouping_max_pebbles": None,
                    "our_method": None,
                    "cat_t_count": None, "cat_and_pos": None,
                    "cat_corrected_t": None, "cat_error": None,
                }
                row.update(_skipped_pre_optimize_row_fields(pre_optimize_name))
                rows.append(row)
                continue

            effective_blif_path, opt_fields, skip_row = _maybe_pre_optimize(label, blif_path)
            if skip_row is not None:
                skip_row["kind"] = kind
                skip_row["num_pis"] = len(net.pis)
                skip_row["num_pos"] = len(net.pos)
                skip_row["num_gates"] = len(net.nodes) - len(net.pis)
                rows.append(skip_row)
                continue

            if args.pre_optimize:
                # BOTH pipelines must now read the OPTIMIZED BLIF, not
                # the original net/blif_path -- re-parse it via
                # read_blif so "ours" sees the exact same optimized
                # circuit caterpillar will.
                try:
                    net = read_blif(effective_blif_path)
                except Exception as e:  # noqa: BLE001 -- report, don't crash the batch
                    row = {
                        "label": label, "kind": kind,
                        "num_pis": 0, "num_pos": 0, "num_gates": 0,
                        "our_error": f"failed to re-parse optimized BLIF: "
                                     f"{type(e).__name__}: {e}",
                        "cat_error": None,
                    }
                    row.update(_skipped_pre_optimize_row_fields(pre_optimize_name))
                    row.update(opt_fields)
                    rows.append(row)
                    continue

            our = _run_ours(net, label)
            cat = run_caterpillar(args.caterpillar_bin, effective_blif_path, timeout=args.timeout)

            row = {
                "label": label,
                "kind": kind,
                "num_pis": len(net.pis),
                "num_pos": len(net.pos),
                "num_gates": len(net.nodes) - len(net.pis),
                "our_t_count": our["t_count"],
                "our_qubits": our["qubit_count"],
                "our_max_pebbles": our["max_pebbles"],
                "our_grouping_max_pebbles": our["grouping_max_pebbles"],
                "our_method": our["method"],
                "our_elapsed_s": our["elapsed_s"],
                "our_error": our["error"],
                "cat_t_count": cat["t_count"],
                "cat_qubits": cat["qubits"],
                "cat_and_pos": cat["and_pos"],
                "cat_corrected_t": cat["corrected_t"],
                "cat_elapsed_s": cat["elapsed_s"],
                "cat_error": cat["error"],
            }
            row.update(opt_fields)
            rows.append(row)

    for label in requested_epfl:
        blif_path = resolve_epfl_blif_path(epfl_blif_dir, label)
        if blif_path is None:
            row = {
                "label": label,
                "kind": "epfl",
                "num_pis": 0,
                "num_pos": 0,
                "num_gates": 0,
                "our_t_count": None,
                "our_qubits": None,
                "our_max_pebbles": None,
                "our_grouping_max_pebbles": None,
                "our_method": None,
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
            }
            row.update(_skipped_pre_optimize_row_fields(pre_optimize_name))
            rows.append(row)
            continue

        try:
            net = read_blif(blif_path)
        except Exception as e:  # noqa: BLE001 -- report, don't crash the batch
            row = {
                "label": label,
                "kind": "epfl",
                "num_pis": 0,
                "num_pos": 0,
                "num_gates": 0,
                "our_t_count": None,
                "our_qubits": None,
                "our_max_pebbles": None,
                "our_grouping_max_pebbles": None,
                "our_method": None,
                "our_elapsed_s": None,
                "our_error": f"BLIF export failed: {type(e).__name__}: {e}",
                "cat_t_count": None,
                "cat_qubits": None,
                "cat_and_pos": None,
                "cat_corrected_t": None,
                "cat_elapsed_s": None,
                "cat_error": None,
            }
            row.update(_skipped_pre_optimize_row_fields(pre_optimize_name))
            rows.append(row)
            continue

        effective_blif_path, opt_fields, skip_row = _maybe_pre_optimize(label, blif_path)
        if skip_row is not None:
            skip_row["kind"] = "epfl"
            skip_row["num_pis"] = len(net.pis)
            skip_row["num_pos"] = len(net.pos)
            skip_row["num_gates"] = len(net.nodes) - len(net.pis)
            rows.append(skip_row)
            continue

        if args.pre_optimize:
            try:
                net = read_blif(effective_blif_path)
            except Exception as e:  # noqa: BLE001 -- report, don't crash the batch
                row = {
                    "label": label, "kind": "epfl",
                    "num_pis": 0, "num_pos": 0, "num_gates": 0,
                    "our_error": f"failed to re-parse optimized BLIF: "
                                 f"{type(e).__name__}: {e}",
                    "cat_error": None,
                }
                row.update(_skipped_pre_optimize_row_fields(pre_optimize_name))
                row.update(opt_fields)
                rows.append(row)
                continue

        our = _run_ours(net, label)
        cat = run_caterpillar(args.caterpillar_bin, effective_blif_path, timeout=args.timeout)

        row = {
            "label": label,
            "kind": "epfl",
            "num_pis": len(net.pis),
            "num_pos": len(net.pos),
            "num_gates": len(net.nodes) - len(net.pis),
            "our_t_count": our["t_count"],
            "our_qubits": our["qubit_count"],
            "our_max_pebbles": our["max_pebbles"],
            "our_grouping_max_pebbles": our["grouping_max_pebbles"],
            "our_method": our["method"],
            "our_elapsed_s": our["elapsed_s"],
            "our_error": our["error"],
            "cat_t_count": cat["t_count"],
            "cat_qubits": cat["qubits"],
            "cat_and_pos": cat["and_pos"],
            "cat_corrected_t": cat["corrected_t"],
            "cat_elapsed_s": cat["elapsed_s"],
            "cat_error": cat["error"],
        }
        row.update(opt_fields)
        rows.append(row)

    print("\n" + "=" * 126)
    print("RESULTS" + (f"  [pre-optimize={pre_optimize_name}]" if args.pre_optimize else ""))
    print("=" * 126)
    header = (
        f"{'label':32s} {'kind':12s} {'pis':>4s} {'pos':>4s} {'gates':>6s} "
        f"{'ours-T':>7s} {'ours-q':>7s} {'ours-p':>7s} {'grp-p':>6s} {'method':>8s} "
        f"{'cat-T':>6s} {'cat-q':>6s} "
        f"{'and_pos':>8s} {'corr-T':>7s} {'cat-s':>8s}"
    )
    if args.pre_optimize:
        header += f" {'opt':>10s} {'nodes_b':>8s} {'nodes_a':>8s}"
    print(header)
    print("-" * len(header))

    for r in rows:
        ours_t = str(r.get("our_t_count")) if r.get("our_t_count") is not None else (
            "SKIP" if args.caterpillar_only else "ERR"
        )
        ours_q = str(r.get("our_qubits", "")) if r.get("our_qubits") is not None else "-"
        ours_p = str(r.get("our_max_pebbles", "")) if r.get("our_max_pebbles") is not None else "-"
        grp_p = str(r.get("our_grouping_max_pebbles", "")) if r.get("our_grouping_max_pebbles") is not None else "-"
        method = r.get("our_method") or "-"
        cat_t = str(r.get("cat_t_count")) if r.get("cat_t_count") is not None else "N/A"
        cat_q = str(r.get("cat_qubits")) if r.get("cat_qubits") is not None else "N/A"
        and_pos = str(r.get("cat_and_pos")) if r.get("cat_and_pos") is not None else "N/A"
        corr_t = str(r.get("cat_corrected_t")) if r.get("cat_corrected_t") is not None else "N/A"
        cat_s = (
            f"{r.get('cat_elapsed_s'):.2f}" if r.get("cat_elapsed_s") is not None else "N/A"
        )
        line = (
            f"{r['label']:32s} {r['kind']:12s} {r['num_pis']:>4d} "
            f"{r['num_pos']:>4d} {r['num_gates']:>6d} "
            f"{ours_t:>7s} {ours_q:>7s} {ours_p:>7s} {grp_p:>6s} {method:>8s} "
            f"{cat_t:>6s} {cat_q:>6s} "
            f"{and_pos:>8s} {corr_t:>7s} {cat_s:>8s}"
        )
        if args.pre_optimize:
            opt_name = r.get("pre_optimize") or "-"
            nodes_b = str(r.get("pre_optimize_nodes_before")) if r.get("pre_optimize_nodes_before") is not None else "-"
            nodes_a = str(r.get("pre_optimize_nodes_after")) if r.get("pre_optimize_nodes_after") is not None else "-"
            line += f" {opt_name:>10s} {nodes_b:>8s} {nodes_a:>8s}"
        print(line)
        if r.get("our_error") and not args.caterpillar_only:
            print(f"    [ours error] {r['our_error']}")
        if r.get("cat_error"):
            print(f"    [caterpillar] {r['cat_error']}")
        if r.get("pre_optimize_error"):
            print(f"    [pre-optimize] {r['pre_optimize_error']}")

    if args.caterpillar_only:
        print("\n" + "-" * 126)
        print("SUMMARY skipped: --caterpillar-only was passed, so our "
              "pipeline's T-count was never computed for any row -- "
              "nothing to compare. See the 'cat-s' column above for "
              "per-row caterpillar wall-clock time.")
        cat_times = [r["cat_elapsed_s"] for r in rows if r.get("cat_elapsed_s") is not None]
        if cat_times:
            print(f"Total caterpillar wall-clock time across "
                  f"{len(cat_times)} row(s): {sum(cat_times):.2f}s")
    else:
        valid = [
            r for r in rows
            if r.get("our_t_count") is not None and r.get("cat_t_count") is not None
        ]
        if valid:
            print("\n" + "-" * 126)
            print("SUMMARY (only rows with both T-counts available)"
                  + (f"  [pre-optimize={pre_optimize_name}]" if args.pre_optimize else ""))
            print("-" * 126)
            total_ours = sum(r["our_t_count"] for r in valid)
            total_cat = sum(r["cat_t_count"] for r in valid)
            print(f"DAGs compared:             {len(valid)}")
            print(f"Total ours T-count:        {total_ours}")
            print(f"Total caterpillar T-count: {total_cat}")
            if total_cat > 0:
                print(f"Ours / caterpillar ratio:  {total_ours / total_cat:.3f}")
            method_counts = Counter(r.get("our_method") for r in valid)
            print(f"Scheduling methods used: "
                  f"{dict(method_counts)}")
            for r in valid:
                delta = r["our_t_count"] - r["cat_t_count"]
                print(f"  {r['label']:32s} ours={r['our_t_count']:<6d} "
                      f"caterpillar={r['cat_t_count']:<6d} delta={delta:+d} "
                      f"method={r.get('our_method')}")

            valid_corrected = [r for r in valid if r.get("cat_corrected_t") is not None]
            if valid_corrected:
                print("\n" + "-" * 126)
                print("SUMMARY (corrected caterpillar T-count: "
                      "2*t_count - and_pos, see module docstring)")
                print("-" * 126)
                total_corrected = sum(r["cat_corrected_t"] for r in valid_corrected)
                print(f"Total corrected caterpillar T-count: {total_corrected}")
                if total_corrected > 0:
                    total_ours_corrected_set = sum(
                        r["our_t_count"] for r in valid_corrected
                    )
                    print(f"Ours / corrected-caterpillar ratio:  "
                          f"{total_ours_corrected_set / total_corrected:.3f}")

                delta_ratios = [
                    (r["our_t_count"] - r["cat_corrected_t"]) / r["cat_corrected_t"]
                    for r in valid_corrected
                    if r["cat_corrected_t"] != 0
                ]
                skipped_zero_corrected = [
                    r for r in valid_corrected if r["cat_corrected_t"] == 0
                ]
                if delta_ratios:
                    avg_delta_ratio = sum(delta_ratios) / len(delta_ratios)
                    print(f"Average delta/corrected-cat ratio:  "
                          f"{avg_delta_ratio:+.3f} "
                          f"(mean of per-circuit (ours-T - corrected-cat-T) / "
                          f"corrected-cat-T, unweighted across "
                          f"{len(delta_ratios)} row(s))")
                if skipped_zero_corrected:
                    skipped_labels = ", ".join(r["label"] for r in skipped_zero_corrected)
                    print(f"  (skipped {len(skipped_zero_corrected)} row(s) with "
                          f"corrected-cat-T == 0 when averaging the ratio: "
                          f"{skipped_labels})")

                for r in valid_corrected:
                    delta = r["our_t_count"] - r["cat_corrected_t"]
                    print(f"  {r['label']:32s} ours={r['our_t_count']:<6d} "
                          f"corrected_cat={r['cat_corrected_t']:<6d} "
                          f"(raw_cat={r['cat_t_count']}, and_pos={r['cat_and_pos']}) "
                          f"delta={delta:+d}")
        else:
            print("\nNo rows had both T-counts available -- nothing to summarize. "
                  "Pass --caterpillar-bin to enable the comparison.")

    if args.pre_optimize:
        skipped_opt = [r for r in rows if r.get("pre_optimize_error")]
        if skipped_opt:
            print("\n" + "-" * 126)
            print(f"{len(skipped_opt)} row(s) SKIPPED entirely due to "
                  f"blif_optimize failure (neither pipeline was run, to "
                  f"avoid breaking symmetry for just that row):")
            for r in skipped_opt:
                print(f"  {r['label']:32s} {r['pre_optimize_error']}")

    if cleanup_dir:
        import shutil
        shutil.rmtree(out_dir, ignore_errors=True)
        print(f"\n(Temp BLIF dir {out_dir} removed; pass --keep-blif to retain it.)")


if __name__ == "__main__":
    main()
