"""
Standalone diagnostic script: builds gate groups for a chosen network,
solves once normally with pebble_gates, then searches for the true
minimum step count at that same pebble budget via find_min_gate_steps,
and reports whether pebble_gates's schedule contains extra
(unconstrained) toggles beyond what's actually required.
"""

import argparse

from pebbling_solver import (
    read_blif,
    build_gate_groups,
    pebble_gates,
    find_min_gate_steps,
)

parser = argparse.ArgumentParser()
parser.add_argument("--blif", default="adder16.blif",
                     help="Path to the BLIF file to load (default: adder16.blif "
                          "in the current directory).")
parser.add_argument("--max-pebbles", type=int, default=None,
                     help="Pebble budget P. Defaults to 2x the number of "
                          "primary outputs if omitted.")
args = parser.parse_args()

net = read_blif(args.blif)

P = args.max_pebbles
if P is None:
    P = max(1, len(net.pos) * 2)

print(f"Loaded {args.blif}: {len(net.pis)} PIs, {len(net.pos)} POs, "
      f"{len(net.nodes) - len(net.pis)} gates")
print(f"Using max_pebbles={P}")

gates = build_gate_groups(net, max_pebbles=P)

gate_steps = pebble_gates(gates, max_pebbles=P, net=net, verbose=False)
known_sat_steps = max(k for k, *_ in gate_steps)
print(f"\npebble_gates found a schedule using {known_sat_steps} steps.")

print(f"\nSearching downward for the true minimum step count at "
      f"max_pebbles={P}...")
min_steps, min_gate_steps = find_min_gate_steps(
    gates, max_pebbles=P, net=net, start_steps=known_sat_steps,
)

print(f"\npebble_gates steps: {known_sat_steps}")
print(f"True minimum steps: {min_steps}")
if min_steps < known_sat_steps:
    print(f"-> pebble_gates's schedule had {known_sat_steps - min_steps} "
          f"extra step(s) of unconstrained slack.")
else:
    print("-> pebble_gates's schedule was already minimal.")
