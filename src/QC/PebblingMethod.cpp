// PebblingMethod.cpp — sequence-driven translator. See PebblingMethod.h for
// the gadget table and the phase-soundness argument.
//
// Validation rules (the professor's placement rules made executable):
//   Pebble(n, anc), target q = num_pis + anc:
//     V1  every fanin of n is live (PI/const, or a live pebble)
//     V2  q is free (free wires provably hold |0> by induction)
//     V3  q is not one of the resolved fanin wires (professor's rule (a);
//         auto-true given V1+V2, checked anyway)
//     V5  n is not already live (v1: one location per value)
//   Unpebble(n, anc):
//     V6  n is live, on that anc
//     V7  every wire the pebble's gadget read still holds the same node it
//         held at pebble time (wire-pinned exact adjoint, D5)
//   End: every gate-node PO is live; no non-PO gate node is live.
//
// V7 subsumes the professor's rule (b) ("never pebble onto a wire a live
// pebble depends on"): rule (b) as a state invariant would reject correct
// reuse schedules (rebuild onto the pinned wire, squat-and-restore, reuse of
// wires read only by never-unpebbled PO pebbles), while V7 rejects exactly
// the schedules whose unpebbles would misfire. last_writer_step_ recovers
// rule-(b)-quality error locality in the V7 message.
//
// Helper-wire invariant (not checkable by V7 since the helper holds no
// node): only PO-AND gadgets touch the helper, each restores it within its
// own recorded window, and gadget emission is strictly sequential — so the
// helper is |0> at every step boundary. Recorded windows are never trimmed;
// the adjoint must replay both helper CNOTs and any complement brackets.

#include "PebblingMethod.h"
#include "BennettSequence.h"

#include <algorithm>

using namespace xagtdep;

PebblingMethod::PebblingMethod(const mockturtle::xag_network &xag)
    : xag_(xag) {
  num_pis_ = xag_.num_pis();
  uint32_t next = 0;
  xag_.foreach_pi(
      [&](auto node) { pi_wire_[xag_.node_to_index(node)] = next++; });
  xag_.foreach_po([&](auto sig) {
    po_nodes_.insert(xag_.node_to_index(xag_.get_node(sig)));
  });
}

QCGateList PebblingMethod::translate(const XagContext &ctx) {
  if (!ctx.pebbling.empty())
    return translate(ctx, ctx.pebbling);
  return translate(ctx, BennettSequence::build(ctx.xag));
}

QCGateList PebblingMethod::translate(const XagContext &ctx,
                                     const PebblingSequence &seq) {
  PebblingMethod t(ctx.xag);
  t.run(seq);
  return t.result_;
}

void PebblingMethod::run(const PebblingSequence &seq) {
  validateStructure(seq);

  num_seq_ancillas_ = sequenceNumAncillas(seq);
  next_extra_ = num_pis_ + num_seq_ancillas_;
  result_.num_pis = num_pis_;

  for (size_t k = 0; k < seq.size(); ++k) {
    if (seq[k].action == PebbleAction::Pebble)
      doPebble(seq[k], k);
    else
      doUnpebble(seq[k], k);
  }

  finalize();

  result_.num_qubits = next_extra_;
  result_.num_ancillas = next_extra_ - num_pis_;
}

// Whole-sequence structural pass — untrusted input becomes a PebblingError
// BEFORE any gate is emitted (the V-rules then run per step).
void PebblingMethod::validateStructure(const PebblingSequence &seq) const {
  for (size_t k = 0; k < seq.size(); ++k) {
    const auto &s = seq[k];
    if (s.node >= xag_.size())
      fail(k, "structure",
           "node index " + std::to_string(s.node) + " out of range (xag has " +
               std::to_string(xag_.size()) + " nodes)");
    auto node = xag_.index_to_node(s.node);
    if (xag_.is_constant(node) || xag_.is_pi(node))
      fail(k, "structure",
           "node " + std::to_string(s.node) +
               " is not a gate node (constants/PIs are never pebbled)");
    // Any valid schedule introduces at most one new slot per step; this caps
    // absurd ancilla ids from malformed input before they size the circuit.
    if (s.anc >= seq.size())
      fail(k, "structure",
           "ancilla id " + std::to_string(s.anc) +
               " exceeds the step count (" + std::to_string(seq.size()) + ")");
  }
}

// ── Per-step handlers ─────────────────────────────────────────────────────

PebblingMethod::Fanin
PebblingMethod::resolveFanin(mockturtle::xag_network::signal sig,
                             size_t step_idx) {
  auto node = xag_.get_node(sig);
  NodeIndex idx = xag_.node_to_index(node);
  bool comp = xag_.is_complemented(sig);

  if (xag_.is_constant(node))
    return {constWire(), comp, /*from_ancilla=*/false, idx};
  if (xag_.is_pi(node))
    return {pi_wire_.at(idx), comp, /*from_ancilla=*/false, idx};

  auto it = node_loc_.find(idx);
  if (it == node_loc_.end())
    fail(step_idx, "V1",
         "fanin node " + std::to_string(idx) + " is not live");
  return {it->second, comp, /*from_ancilla=*/true, idx};
}

void PebblingMethod::doPebble(const PebbleStep &step, size_t step_idx) {
  NodeIndex idx = step.node;
  auto node = xag_.index_to_node(idx);
  uint32_t q = num_pis_ + step.anc;

  // V5: one location per value in v1.
  if (live_.count(idx))
    fail(step_idx, "V5",
         "node " + std::to_string(idx) + " is already live (on " +
             describeWire(node_loc_.at(idx)) + ")");

  // V2: clean-ancilla discipline — the target must be free.
  auto hold_it = qubit_holds_.find(q);
  if (hold_it != qubit_holds_.end()) {
    auto lw = last_writer_step_.find(q);
    fail(step_idx, "V2",
         describeWire(q) + " is not free: holds node " +
             std::to_string(hold_it->second) +
             (lw != last_writer_step_.end()
                  ? " (last written at step " + std::to_string(lw->second) + ")"
                  : ""));
  }

  // Resolve fanins (V1 inside).
  std::vector<mockturtle::xag_network::signal> fanins;
  xag_.foreach_fanin(node, [&](auto sig) { fanins.push_back(sig); });
  if (fanins.size() != 2)
    fail(step_idx, "structure",
         "node " + std::to_string(idx) + " does not have exactly 2 fanins");
  Fanin f0 = resolveFanin(fanins[0], step_idx);
  Fanin f1 = resolveFanin(fanins[1], step_idx);

  // V3 (professor's rule a): cannot compute onto a wire being read.
  // Auto-true given V1+V2 (fanins live elsewhere, target free) — kept as a
  // real check because it becomes load-bearing under dirty borrowing.
  if (q == f0.wire || q == f1.wire)
    fail(step_idx, "V3",
         describeWire(q) + " is one of the fanin wires of node " +
             std::to_string(idx));

  // mockturtle folds degenerate gates (AND(a,a) etc.), so two distinct
  // resolved wires is an invariant; the ladders would be malformed otherwise.
  if (f0.wire == f1.wire)
    fail(step_idx, "structure",
         "both fanins of node " + std::to_string(idx) + " resolve to " +
             describeWire(f0.wire));

  size_t gate_begin = result_.gates.size();
  if (xag_.is_and(node)) {
    if (isOutputNode(idx))
      emitAndOutputCore(q, f0, f1);
    else
      emitAndIntermediate(q, f0, f1);
  } else {
    emitXorPebble(q, f0, f1);
  }
  size_t gate_end = result_.gates.size();

  LivePebble rec;
  rec.anc = step.anc;
  rec.pebbled_at = step_idx;
  if (f0.from_ancilla)
    rec.read_pairs.emplace_back(f0.wire, f0.holds);
  if (f1.from_ancilla)
    rec.read_pairs.emplace_back(f1.wire, f1.holds);
  rec.gate_begin = gate_begin;
  rec.gate_end = gate_end;
  live_[idx] = std::move(rec);
  node_loc_[idx] = q;
  qubit_holds_[q] = idx;
  last_writer_step_[q] = step_idx;
}

void PebblingMethod::doUnpebble(const PebbleStep &step, size_t step_idx) {
  NodeIndex idx = step.node;

  // V6: the node must be live, on the slot the step names.
  auto it = live_.find(idx);
  if (it == live_.end())
    fail(step_idx, "V6", "node " + std::to_string(idx) + " is not live");
  const LivePebble &rec = it->second;
  if (rec.anc != step.anc)
    fail(step_idx, "V6",
         "node " + std::to_string(idx) + " lives on ancilla " +
             std::to_string(rec.anc) + ", not ancilla " +
             std::to_string(step.anc));

  // V7 (subsumes professor's rule b): the exact adjoint is only the inverse
  // if every wire the gadget read still holds the value it read — i.e. the
  // same node (rebuilt there if it was freed in between).
  for (const auto &[w, m] : rec.read_pairs) {
    auto hold_it = qubit_holds_.find(w);
    if (hold_it == qubit_holds_.end() || hold_it->second != m) {
      auto lw = last_writer_step_.find(w);
      fail(step_idx, "V7",
           "unpebbling node " + std::to_string(idx) + " (pebbled at step " +
               std::to_string(rec.pebbled_at) + ") needs " + describeWire(w) +
               " to hold node " + std::to_string(m) + ", but it " +
               (hold_it == qubit_holds_.end()
                    ? "is free"
                    : "holds node " + std::to_string(hold_it->second)) +
               (lw != last_writer_step_.end()
                    ? " (last written at step " + std::to_string(lw->second) +
                          ")"
                    : "") +
               " — rebuild node " + std::to_string(m) + " there first");
    }
  }

  uint32_t q = num_pis_ + rec.anc;
  appendAdjoint(rec.gate_begin, rec.gate_end);

  node_loc_.erase(idx);
  qubit_holds_.erase(q);
  last_writer_step_[q] = step_idx;
  live_.erase(it);
}

void PebblingMethod::finalize() {
  // End-of-sequence checks: exactly the (gate-node) POs remain pebbled.
  for (const auto &[idx, rec] : live_) {
    if (!isOutputNode(idx))
      fail(PebblingError::kNoStep, "end",
           "non-PO node " + std::to_string(idx) +
               " is still live on ancilla " + std::to_string(rec.anc) +
               " at the end of the sequence");
  }
  bool ok = true;
  std::string missing;
  xag_.foreach_po([&](auto sig) {
    auto node = xag_.get_node(sig);
    if (xag_.is_constant(node) || xag_.is_pi(node))
      return; // no pebble step exists for these; resolved below
    NodeIndex idx = xag_.node_to_index(node);
    if (!live_.count(idx)) {
      ok = false;
      missing += (missing.empty() ? "" : ", ") + std::to_string(idx);
    }
  });
  if (!ok)
    fail(PebblingError::kNoStep, "end",
         "PO node(s) " + missing + " are not live at the end of the sequence");

  // PO resolution: trailing X for a complemented PO (in place, matching the
  // existing translators' processSignal); output_qubit = last PO processed.
  xag_.foreach_po([&](auto sig) {
    auto node = xag_.get_node(sig);
    uint32_t wire;
    if (xag_.is_constant(node))
      wire = constWire();
    else if (xag_.is_pi(node))
      wire = pi_wire_.at(xag_.node_to_index(node));
    else
      wire = node_loc_.at(xag_.node_to_index(node));
    if (xag_.is_complemented(sig))
      emitGate(GateType::X, {}, wire);
    result_.output_qubit = wire;
  });
}

// ── Gadgets ───────────────────────────────────────────────────────────────

// Fig 5: XOR onto the |0> target. A complemented fanin is an X on the fresh
// target (XOR(¬a,b) = ¬(a⊕b)) — the existing translators' convention; never
// flip the source wire in place.
void PebblingMethod::emitXorPebble(uint32_t t, const Fanin &f0,
                                   const Fanin &f1) {
  emitGate(GateType::CNOT, {f0.wire}, t);
  if (f0.complemented)
    emitGate(GateType::X, {}, t);
  emitGate(GateType::CNOT, {f1.wire}, t);
  if (f1.complemented)
    emitGate(GateType::X, {}, t);
}

// Fig 8: relative-phase AND onto the |0> target (4 T, no helper — the target
// IS the H-conjugated wire). Complemented fanins bracket the whole gadget;
// the brackets close inside the recorded window, so sources are restored and
// the adjoint replays them correctly.
void PebblingMethod::emitAndIntermediate(uint32_t t, const Fanin &f0,
                                         const Fanin &f1) {
  if (f0.complemented)
    emitGate(GateType::X, {}, f0.wire);
  if (f1.complemented)
    emitGate(GateType::X, {}, f1.wire);

  emitGate(GateType::H, {}, t);
  emitGate(GateType::Tdg, {}, t);
  emitCCiwZ(f0.wire, f1.wire, t);
  emitGate(GateType::H, {}, t);

  if (f0.complemented)
    emitGate(GateType::X, {}, f0.wire);
  if (f1.complemented)
    emitGate(GateType::X, {}, f1.wire);
}

// Fig 2's iωZ core: AND at a primary output (6 T). Children are prior pebble
// steps, NOT inlined — the core acts on two live wires and the shared helper
// (dirty-borrowed: the CC-iωZ/CC-iωZ† pair and the CNOT pair cancel on it
// regardless of its state, so it is restored by construction). Roles are
// fixed: fanin[0] is the ladder control, fanin[1] the CNOT source.
void PebblingMethod::emitAndOutputCore(uint32_t t, const Fanin &f0,
                                       const Fanin &f1) {
  uint32_t h = helperWire();

  if (f0.complemented)
    emitGate(GateType::X, {}, f0.wire);
  if (f1.complemented)
    emitGate(GateType::X, {}, f1.wire);

  emitGate(GateType::H, {}, t);
  emitCCiwZ(f0.wire, t, h);
  emitGate(GateType::CNOT, {f1.wire}, h);
  emitCCiwZdg(f0.wire, t, h);
  emitGate(GateType::H, {}, t); // t now holds the AND result
  emitGate(GateType::CNOT, {f1.wire}, h); // restores the helper

  if (f0.complemented)
    emitGate(GateType::X, {}, f0.wire);
  if (f1.complemented)
    emitGate(GateType::X, {}, f1.wire);
}

// ── Lazy extra wires ──────────────────────────────────────────────────────

uint32_t PebblingMethod::helperWire() {
  if (helper_wire_ < 0)
    helper_wire_ = next_extra_++;
  return static_cast<uint32_t>(helper_wire_);
}

uint32_t PebblingMethod::constWire() {
  if (const_wire_ < 0)
    const_wire_ = next_extra_++;
  return static_cast<uint32_t>(const_wire_);
}

// ── Decomposition helpers (bodies copied verbatim from ProposedMethod) ────

// Fig 9: relative-phase CC-iωZ decomposition (3 T, 6 CNOT).
void PebblingMethod::emitCCiwZ(uint32_t q0, uint32_t q1, uint32_t q2) {
  emitGate(GateType::CNOT, {q2}, q0);
  emitGate(GateType::CNOT, {q1}, q2);
  emitGate(GateType::CNOT, {q0}, q1);
  emitGate(GateType::T, {}, q0);
  emitGate(GateType::Tdg, {}, q1);
  emitGate(GateType::T, {}, q2);
  emitGate(GateType::CNOT, {q0}, q1);
  emitGate(GateType::CNOT, {q1}, q2);
  emitGate(GateType::CNOT, {q2}, q0);
}

// Fig 10: CC-iωZ† — same network with T ↔ T†.
void PebblingMethod::emitCCiwZdg(uint32_t q0, uint32_t q1, uint32_t q2) {
  emitGate(GateType::CNOT, {q2}, q0);
  emitGate(GateType::CNOT, {q1}, q2);
  emitGate(GateType::CNOT, {q0}, q1);
  emitGate(GateType::Tdg, {}, q0);
  emitGate(GateType::T, {}, q1);
  emitGate(GateType::Tdg, {}, q2);
  emitGate(GateType::CNOT, {q0}, q1);
  emitGate(GateType::CNOT, {q1}, q2);
  emitGate(GateType::CNOT, {q2}, q0);
}

// Replay a recorded window in reverse with T ↔ T† — the Unpebble primitive.
void PebblingMethod::appendAdjoint(size_t startIdx, size_t endIdx) {
  for (size_t i = endIdx; i > startIdx; --i) {
    const auto &gate = result_.gates[i - 1];
    GateType dag = gate.type;
    switch (gate.type) {
    case GateType::T:
      dag = GateType::Tdg;
      break;
    case GateType::Tdg:
      dag = GateType::T;
      break;
    default:
      break;
    }
    emitGate(dag, gate.controls, gate.target);
  }
}

void PebblingMethod::emitGate(GateType type, std::vector<uint32_t> controls,
                              uint32_t target) {
  result_.gates.push_back({type, std::move(controls), target});
}

// ── Diagnostics ───────────────────────────────────────────────────────────

std::string PebblingMethod::describeWire(uint32_t wire) const {
  if (wire < num_pis_)
    return "PI wire q" + std::to_string(wire);
  if (wire < num_pis_ + num_seq_ancillas_)
    return "ancilla " + std::to_string(wire - num_pis_) + " (q" +
           std::to_string(wire) + ")";
  return "extra wire q" + std::to_string(wire);
}

void PebblingMethod::fail(size_t step_idx, const char *rule,
                          const std::string &msg) const {
  std::string where = step_idx == PebblingError::kNoStep
                          ? "end-of-sequence"
                          : "step " + std::to_string(step_idx);
  throw PebblingError("[PebblingMethod] " + where + " (" + rule + "): " + msg,
                      step_idx);
}
