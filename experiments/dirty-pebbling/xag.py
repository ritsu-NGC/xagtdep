"""XAG data model + JSON parser + the two Phase-1 validation anchors.

An XAG here is the structural DAG only (no truth tables). Primary inputs and the
constant are *not* pebbled (always available), matching Meuli et al.; only AND/XOR
gate nodes are pebbled. Complement bits are irrelevant to scheduling (they become
free X gates at gate-emission time) and are dropped.

See doc/dirty-ancilla-phase1.md for how this fits Phase 1.
"""
from __future__ import annotations

import json
from dataclasses import dataclass


@dataclass(frozen=True)
class Node:
    id: int
    op: str            # 'and' | 'xor'
    fanin: tuple        # tuple of fanin node ids (may reference PIs/const or gates)


class Xag:
    def __init__(self, pis, gates, pos, const0=None):
        self.pis = set(pis)
        self.const0 = const0
        self.nodes = {n.id: n for n in gates}     # gate nodes only
        self.pos = list(pos)                      # output gate-node ids

    # ── predicates ────────────────────────────────────────────────────────
    def is_input(self, nid: int) -> bool:
        """PI or constant — always available, never pebbled."""
        return nid in self.pis or (self.const0 is not None and nid == self.const0)

    def gate_ids(self):
        return list(self.nodes.keys())

    def and_ids(self):
        return [i for i, n in self.nodes.items() if n.op == "and"]

    def gate_children(self, nid: int):
        """Children that are themselves gate nodes (PI/const children dropped)."""
        return [c for c in self.nodes[nid].fanin if not self.is_input(c)]

    def and_case(self, nid: int) -> str:
        """Replicates ProposedMethod::processAndNode precedence exactly:
        both-PI takes priority over output, output over one-PI."""
        n = self.nodes[nid]
        assert n.op == "and"
        simple = [self.is_input(c) for c in n.fanin]
        if all(simple):
            return "both_pi"
        if nid in self.pos:
            return "output"
        if any(simple):
            return "one_pi"
        return "complex"   # rejected by validate_and_constraint; should not occur

    # ── construction ──────────────────────────────────────────────────────
    @staticmethod
    def from_json(text: str) -> "Xag":
        d = json.loads(text)
        gates = [
            Node(g["id"], g["op"], tuple(g["fanin"]))
            for g in d["nodes"]
            if g["op"] in ("and", "xor")
        ]
        pos = [p["node"] for p in d["pos"]]
        return Xag(d["pis"], gates, pos, d.get("constant"))


# ── Validation anchors ─────────────────────────────────────────────────────

def paper_6node() -> Xag:
    """Meuli et al. DATE 2019, Figs 2-3 (also Phase-0 explainer §2.4).
    z1=A(x2,x3) z2=C(z1,x3) z3=B(x3,x4) z4=D(z3,x3) y1=E(z2,z4) y2=F(x1,z1)
    Outputs {E,F}. Known optimum: 4 pebbles (Bennett uses 6).
    Op types are irrelevant to the clean game; marked 'and' arbitrarily."""
    x1, x2, x3, x4 = 1, 2, 3, 4
    A, B, C, D, E, F = 10, 11, 12, 13, 14, 15
    gates = [
        Node(A, "and", (x2, x3)),
        Node(B, "and", (x3, x4)),
        Node(C, "and", (A, x3)),
        Node(D, "and", (B, x3)),
        Node(E, "and", (C, D)),
        Node(F, "and", (A, x1)),
    ]
    return Xag([x1, x2, x3, x4], gates, pos=[E, F])


def phase0_two_and() -> Xag:
    """Phase-0 §5: n8 = [x2 ∧ (x0⊕x1)] ⊕ [x1 ∧ (x0⊕x3)].
    Both ANDs are one-PI (have scratch). Dirty target: 4 ancilla at T=8."""
    x0, x1, x2, x3 = 0, 1, 2, 3
    n4, n6, n5, n7, n8 = 10, 11, 12, 13, 14
    gates = [
        Node(n4, "xor", (x0, x1)),
        Node(n6, "xor", (x0, x3)),
        Node(n5, "and", (x2, n4)),
        Node(n7, "and", (x1, n6)),
        Node(n8, "xor", (n5, n7)),
    ]
    return Xag([x0, x1, x2, x3], gates, pos=[n8])
