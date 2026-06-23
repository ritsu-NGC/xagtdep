"""Gadget cost model (Interpretation A — schedule the existing spec gadgets).

T-weights are the verified per-AND-case costs from SPEC-conformance-notes.md §1.
The AND *case* (both_pi / one_pi / output) is structural (xag.and_case), matching
ProposedMethod::processAndNode's precedence.

Scratch requirement, from reading the gadgets:
  - both_pi (Fig 6+H / Fig 8): a 3-wire relative-phase Toffoli — NO scratch wire.
  - one_pi (Fig 11 / Fig 12): uses a borrowable dirty ancilla `a` (scratch).
  - output (Fig 1 / Fig 2): uses a dirty ancilla `a_i`; it is *restored* and the
    A-cone is uncomputed inside the gadget (so the cone child is consumed there).

Dirty borrowing therefore only has a scratch slot to reuse on one_pi/output ANDs.
XOR/NOT cost 0 T and need no scratch.

Interpretation B (always pre-pebble both children → Fig 8 everywhere, 4T, no
scratch) is cheaper but changes which gadget the method emits — a method change,
out of scope under the Phase-1 defaults (Q1/Q2). Recorded, not implemented.
"""

# T-cost per AND case, per method.
T_WEIGHT = {
    "existing": {"both_pi": 4, "one_pi": 8, "output": 8},
    "proposed": {"both_pi": 4, "one_pi": 4, "output": 6},
}

# Whether the gadget for this AND case uses a (borrowable) scratch ancilla.
HAS_SCRATCH = {"both_pi": False, "one_pi": True, "output": True}


def t_weight(case: str, method: str) -> int:
    return T_WEIGHT[method][case]


def has_scratch(case: str) -> bool:
    return HAS_SCRATCH[case]
