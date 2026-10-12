#!/usr/bin/env python3
"""
Standalone diagnostic: reads a pebbling debug JSON file (as produced by
`dump_pebbling_debug_json` in pebbling_solver.py -- e.g.
`bar_pebbling_debug.json`) and reports the number of toggle events
(compute_gate + uncompute_gate) per gate_id from its
`gate_level_schedule` section.

Usage:
    python analyze_gate_toggles.py path/to/bar_pebbling_debug.json
    python analyze_gate_toggles.py path/to/bar_pebbling_debug.json --top-n 50
    python analyze_gate_toggles.py path/to/bar_pebbling_debug.json --min-toggles 3
"""

import argparse
import json
import sys
from collections import defaultdict, Counter


def load_gate_level_schedule(path):
    with open(path, "r") as f:
        payload = json.load(f)

    schedule = payload.get("gate_level_schedule")
    gate_groups = payload.get("gate_groups")
    if schedule is None:
        raise ValueError(
            f"'{path}' has no 'gate_level_schedule' key -- is this a "
            f"valid pebbling debug JSON file? Top-level keys present: "
            f"{list(payload.keys())}"
        )
    return schedule, payload, gate_groups


def summarize_gate_toggle_counts(schedule):
    """
    Computes, from a `gate_level_schedule` list of
    {"step": k, "gate_id": gid, "op": op, "tag": tag} dicts, the number
    of toggle events (compute_gate + uncompute_gate) per gate_id.

    A "toggle" is a single compute_gate or uncompute_gate event for
    that gate. Under a well-behaved schedule:
      - a PO-owning group should show exactly 1 toggle (compute-only,
        never uncomputed).
      - a non-PO group that gets freed should show exactly 2 toggles
        (one compute_gate + one uncompute_gate).

    Any gate_id with MORE than 2 toggles indicates RECOMPUTATION
    (compute -> uncompute -> compute -> ...), which is legal but
    costs additional T-count/dirty-weight charges each time.

    Returns a dict:
        {
          gate_id: {
            "total_toggles": int,
            "compute_count": int,
            "uncompute_count": int,
            "steps": [list of step indices at which this gate toggled],
            "tag": str,  # "and" or "xor", from the first event seen
          },
          ...
        }
    """
    counts = defaultdict(lambda: {
        "total_toggles": 0,
        "compute_count": 0,
        "uncompute_count": 0,
        "steps": [],
        "tag": None,
    })

    for entry in schedule:
        gid = entry["gate_id"]
        op = entry["op"]
        step = entry["step"]
        tag = entry.get("tag")

        c = counts[gid]
        c["total_toggles"] += 1
        c["steps"].append(step)
        if c["tag"] is None:
            c["tag"] = tag
        if op == "compute_gate":
            c["compute_count"] += 1
        elif op == "uncompute_gate":
            c["uncompute_count"] += 1

    return dict(counts)


def print_gate_toggle_counts(counts, top_n=20, min_toggles=None):
    if not counts:
        print("No gate toggle data to analyze.")
        return

    sorted_gids = sorted(
        counts.keys(), key=lambda gid: counts[gid]["total_toggles"], reverse=True
    )

    if min_toggles is not None:
        sorted_gids = [
            gid for gid in sorted_gids
            if counts[gid]["total_toggles"] >= min_toggles
        ]

    total_toggle_events = sum(c["total_toggles"] for c in counts.values())
    recomputed = [gid for gid in counts if counts[gid]["total_toggles"] > 2]
    compute_only = [gid for gid in counts if counts[gid]["uncompute_count"] == 0]
    compute_and_uncompute = [
        gid for gid in counts
        if counts[gid]["compute_count"] >= 1 and counts[gid]["uncompute_count"] >= 1
        and counts[gid]["total_toggles"] == 2
    ]

    print(f"{len(counts)} gate(s) total, {total_toggle_events} total toggle "
          f"event(s) (compute_gate + uncompute_gate).")
    print(f"  {len(compute_only)} gate(s) compute-only (never uncomputed, "
          f"e.g. PO-owning groups)")
    print(f"  {len(compute_and_uncompute)} gate(s) computed exactly once "
          f"and uncomputed exactly once")
    print(f"  {len(recomputed)} gate(s) toggled MORE than twice "
          f"(recomputed at least once)")

    if min_toggles is not None:
        print(f"\nShowing gate(s) with >= {min_toggles} toggle(s), "
              f"sorted descending by toggle count "
              f"(top {min(top_n, len(sorted_gids))} of {len(sorted_gids)}):")
    else:
        print(f"\nPer-gate breakdown, sorted descending by toggle count "
              f"(top {min(top_n, len(sorted_gids))} of {len(sorted_gids)}):")

    print(f"  {'gate_id':>8s}  {'tag':>4s}  {'total':>6s}  "
          f"{'compute':>8s}  {'uncompute':>10s}  steps")
    for gid in sorted_gids[:top_n]:
        c = counts[gid]
        print(f"  {gid:>8}  {str(c['tag']):>4s}  {c['total_toggles']:>6d}  "
              f"{c['compute_count']:>8d}  {c['uncompute_count']:>10d}  "
              f"{c['steps']}")

    hist = Counter(c["total_toggles"] for c in counts.values())
    print(f"\nToggle-count distribution (all gates, regardless of filters above):")
    for toggle_count in sorted(hist):
        n_gates = hist[toggle_count]
        bar = "#" * min(n_gates, 60)
        print(f"    {toggle_count:>3d} toggle(s): {n_gates:>5d} gate(s)  {bar}")


def main():
    parser = argparse.ArgumentParser(
        description="Analyze per-gate toggle (compute/uncompute) counts "
                    "from a pebbling debug JSON file."
    )
    parser.add_argument("debug_json", help="Path to a *_pebbling_debug.json file.")
    parser.add_argument("--top-n", type=int, default=20,
                         help="Number of gates to show in the per-gate "
                              "breakdown (default: 20).")
    parser.add_argument("--min-toggles", type=int, default=None,
                         help="Only show gates with at least this many "
                              "toggles (useful for isolating recomputed "
                              "gates, e.g. --min-toggles 3).")
    args = parser.parse_args()

    try:
        schedule, payload, gate_groups = load_gate_level_schedule(args.debug_json)
    except (OSError, json.JSONDecodeError, ValueError) as e:
        print(f"Error loading '{args.debug_json}': {e}", file=sys.stderr)
        sys.exit(1)

    outcome = payload.get("outcome")
    if outcome:
        print(f"Debug JSON outcome: {outcome}")

    counts = summarize_gate_toggle_counts(schedule)
    print_gate_toggle_counts(counts, top_n=args.top_n, min_toggles=args.min_toggles)
    tot_nodes = 0
    for gate in gate_groups:
        print(f"Gate " + str(gate["gid"]) + " len: " + str(len(gate["nodes"])))
        tot_nodes += len(gate["nodes"])
    print(f"Total Nodes: " + str(tot_nodes))


if __name__ == "__main__":
    main()
