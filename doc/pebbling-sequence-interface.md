# Pebbling-Sequence Interface (SAT solver ⇄ QC Mapping)

How an external pebbling solver (David's z3/SAT tool) drives quantum-circuit
synthesis without touching C++. The C++ side is `SynthesisAlgorithm::
PebblingMethod` (`src/QC/PebblingMethod.{h,cpp}`), consuming a
`PebblingSequence` (`include/PebblingSequence.h`) read from JSON by
`src/QC/SequenceJson.{h,cpp}`.

## The sequence file

```json
{ "num_ancillas": 2,
  "steps": [ {"action":"pebble",   "node": 4, "anc": 0},
             {"action":"pebble",   "node": 5, "anc": 1},
             {"action":"unpebble", "node": 4, "anc": 0} ] }
```

- `steps` is executed in order. `action` ∈ {`"pebble"`, `"unpebble"`}.
- `node` is the **mockturtle node index** of a gate node (AND or XOR) in the
  XAG being synthesized (`ctx.xag`). Constants (index 0) and PIs (indices
  1..num_pis) are never pebbled.
- `anc` is an ancilla **slot**; slot `a` lives on qubit `num_pis + a`. Slot
  ids are qubit offsets, so number them **densely** `0..W-1` — sparse ids
  waste physical wires. The validator enforces the sanity cap
  `anc < number of steps`, which any dense schedule satisfies automatically
  (each used slot is pebbled at least once).
- `num_ancillas` is optional and must equal exactly `max(anc)+1` when
  present (it is re-derived and cross-checked on read; a mismatch is
  rejected). The example above uses slots 0 and 1, hence `2`.

Every case run by the verification harness also writes
`xag_<idx>_structure.json` — the exact graph (`num_pis`, `pos`, `gates` with
fanins and complement flags). **Work from that dump, not from
`(pis,ands,xors,seed)`**: random-XAG generation is not reproducible across
platforms/stdlibs, so node indices only match if you consume the dumped
structure (or generate in the same environment, e.g. the CI Docker image).

## Semantics the schedule must respect (validated at consume time)

A Pebble step computes its node's value onto the (clean, `|0⟩`) target wire
with a self-contained gadget; an Unpebble step replays the **exact adjoint**
of the gadget window that created the pebble, on the same wires. The
validator throws (with the step index and rule tag) on the first violation —
a bad schedule can never silently produce a wrong circuit.

Pebble(n, anc):

| Rule | Requirement |
|---|---|
| V1 | every fanin of `n` is available: a PI/constant, or currently pebbled |
| V2 | the target slot is free |
| V3 | the target is not one of `n`'s fanin wires (implied by V1+V2) |
| V5 | `n` is not already pebbled — **one live copy per node** (v1 restriction; do not model multi-residency) |

Unpebble(n, anc):

| Rule | Requirement |
|---|---|
| V6 | `n` is currently pebbled, on that slot |
| V7 | every ancilla wire `n`'s gadget **read** still holds the **same node** it held at pebble time |

End of sequence: exactly the gate-node POs are pebbled (POs that are PIs or
constants need no step).

## Encode V7, not the state-invariant placement rule

V7 is the **temporal** form of "never disturb what a pebble depends on": a
value that was freed must be **re-pebbled (rebuilt) onto the same slot**
before any consumer that read it there is unpebbled. Encode wire contents
per timestep and require, at each unpebble, that the read wires hold the
same nodes as at the matching pebble.

Do **not** encode the literal state invariant "no pebble may ever target a
slot some live pebble read" — it is stronger than correctness requires and
forbids exactly the schedules worth finding:

- **rebuild onto the pinned slot** (the mechanism above — the invariant
  would even forbid putting the value *back*),
- **squat-and-restore**: temporarily hosting another node on a freed slot,
  then rebuilding the original before its consumer's unpebble,
- reusing slots read only by pebbles that are never unpebbled (POs).

The C++ validator enforces V7 (with last-writer diagnostics), so any
schedule it accepts is sound; any schedule your encoding produces under V7
semantics will be accepted.

## Cost model notes

- v1 granularity: **every** gate node is a pebble step — XORs too (2 CNOTs
  each; AND gadgets cost 4 T intermediate / 6 T at a PO).
- One shared helper wire is appended after the ancilla slots when the XAG
  has an AND primary output (plus one more wire if a constant fanin occurs);
  peak width = `num_pis + num_ancillas (+1 helper) (+1 const)`.
- The Bennett baseline (`BennettSequence::build`) uses one slot per PO-cone
  gate node; beat it on `max(anc)+1`.

## Running a schedule

```bash
./build/QCVerificationTest --seed 42 --pis 4 --ands 1 --xors 2 \
    --method pebbling --sequence my_schedule.json
python3 test/verify_circuits.py --strict verification_data_single
```

The harness writes `xag_0_pebbling.json` (gate list), `xag_0_structure.json`
(graph), and `xag_0_meta.json` (truth table + per-method metrics); the
Python step checks statevector truth tables and QCEC equivalence against a
truth-table oracle. Omitting `--sequence` uses the Bennett fallback; a
`--sequence` file with an empty `steps` array is rejected (an explicit
schedule is never silently replaced by the fallback).

In C++ (tests, tools): `readSequenceFile(path)` →
`PebblingMethod::translate(ctx, seq)`, or set `ctx.pebbling` and use
`QC::evaluate(ctx, SynthesisAlgorithm::PebblingMethod)`.
