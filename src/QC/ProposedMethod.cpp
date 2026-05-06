// ProposedMethod.cpp — Algorithm 2 ("Proposed Method") implementation.
//
// Recursive depth-first XAG traversal producing quantum gates per:
//   XOR node  → Fig 5  (two CNOTs into output qubit)
//   AND node  → Fig 8  (relative-phase Toffoli, both inputs PI)
//              → Fig 2  (AND at overall output, with A/A† and CC-iωZ)
//              → Fig 12 (one input PI, with F sub-circuit and T/T†)
//
// Top-level: QC = PARSEXAG(xag) directly — NO uncompute appended.

#include "ProposedMethod.h"
#include "llvm/Support/raw_ostream.h"

using namespace xagtdep;

ProposedMethod::ProposedMethod(const mockturtle::xag_network &xag)
    : xag_(xag) {}

QCGateList ProposedMethod::translate(const XagContext &ctx) {
  ProposedMethod t(ctx.xag);

  // Assign qubits 0..num_pis-1 to primary inputs.
  ctx.xag.foreach_pi(
      [&](auto node) { t.node_to_qubit_[ctx.xag.node_to_index(node)] = t.next_qubit_++; });
  t.result_.num_pis = ctx.xag.num_pis();

  // Build set of PO node indices for isOutputNode detection.
  ctx.xag.foreach_po([&](auto signal) {
    t.po_nodes_.insert(ctx.xag.node_to_index(ctx.xag.get_node(signal)));
  });

  // Process each primary output — NO uncompute phase for Algorithm 2.
  ctx.xag.foreach_po([&](auto signal) { t.processSignal(signal); });

  t.result_.num_qubits = t.next_qubit_;
  t.result_.num_ancillas = t.next_qubit_ - ctx.xag.num_pis();

  return t.result_;
}

// ── Recursive traversal ──────────────────────────────────────────────────

uint32_t ProposedMethod::processSignal(mockturtle::xag_network::signal sig) {
  auto node = xag_.get_node(sig);
  bool complemented = xag_.is_complemented(sig);
  uint32_t qubit = processNode(node);
  if (complemented)
    emitGate(GateType::X, {}, qubit);
  return qubit;
}

uint32_t ProposedMethod::processNode(mockturtle::xag_network::node node) {
  uint32_t idx = xag_.node_to_index(node);

  auto it = node_to_qubit_.find(idx);
  if (it != node_to_qubit_.end())
    return it->second;

  if (idx == 0) {
    uint32_t qubit = next_qubit_++;
    node_to_qubit_[idx] = qubit;
    return qubit;
  }

  if (xag_.is_pi(node))
    return node_to_qubit_[idx];

  if (xag_.is_and(node))
    return processAndNode(node);

  return processXorNode(node);
}

// ── Fig 5: XOR node ──────────────────────────────────────────────────────

uint32_t ProposedMethod::processXorNode(mockturtle::xag_network::node node) {
  std::vector<mockturtle::xag_network::signal> fanins;
  xag_.foreach_fanin(node, [&](auto sig) { fanins.push_back(sig); });

  uint32_t q_b = processSignal(fanins[0]);
  uint32_t q_a = processSignal(fanins[1]);

  uint32_t q_out = next_qubit_++;
  emitGate(GateType::CNOT, {q_b}, q_out);
  emitGate(GateType::CNOT, {q_a}, q_out);

  node_to_qubit_[xag_.node_to_index(node)] = q_out;
  return q_out;
}

// ── AND node router ──────────────────────────────────────────────────────

uint32_t ProposedMethod::processAndNode(mockturtle::xag_network::node node) {
  std::vector<mockturtle::xag_network::signal> fanins;
  xag_.foreach_fanin(node, [&](auto sig) { fanins.push_back(sig); });

  auto child0 = xag_.get_node(fanins[0]);
  auto child1 = xag_.get_node(fanins[1]);
  bool simple0 = isSimpleNode(child0);
  bool simple1 = isSimpleNode(child1);

  if (simple0 && simple1) {
    uint32_t q_a = processSignal(fanins[0]);
    uint32_t q_b = processSignal(fanins[1]);
    return emitAndBothPI(node, q_a, q_b);
  }

  if (isOutputNode(node)) {
    return emitAndOutput(node, fanins);
  }

  if (simple0 || simple1) {
    return emitAndOnePI(node, fanins);
  }

  llvm::errs() << "[ProposedMethod] ERROR: both AND children are complex "
                  "sub-circuits (unsupported)\n";
  uint32_t q_err = next_qubit_++;
  node_to_qubit_[xag_.node_to_index(node)] = q_err;
  return q_err;
}

// ── Fig 8: AND both PI (relative-phase Toffoli) ─────────────────────────
// 3 wires: q_a (ctrl1), q_b (ctrl2), f (ancilla with H).
// This is H + CC-iωZ (Fig 9) + H on the ancilla wire.
//
// Gate sequence from Fig 8:
//   H(f), Tdg(f), CNOT(q_a,f), T(f), CNOT(q_b,f),
//   Tdg(f), CNOT(q_a,f), T(f), CNOT(q_b,f),
//   T(q_a), CNOT(q_b,q_a), T(q_b), Tdg(q_a), CNOT(q_b,q_a), H(f)

uint32_t ProposedMethod::emitAndBothPI(mockturtle::xag_network::node node,
                                       uint32_t q_a, uint32_t q_b) {
  uint32_t f = next_qubit_++;
  node_to_qubit_[xag_.node_to_index(node)] = f;

  emitGate(GateType::H, {}, f);
  emitCCiwZ(q_a, q_b, f);
  emitGate(GateType::H, {}, f);

  return f;
}

// ── Fig 2: AND output case ───────────────────────────────────────────────
// Same structure as Fig 1 but uses CC-iωZ/iωZ† (Figs 9/10) instead of
// CC-iZ/iZ† (Figs 6/7).
//
// Sequence:
//   1. H on |0⟩
//   2. CC-iωZ(B, |0⟩, a_i)
//   3. Compute A sub-circuit
//   4. CNOT(A_output, a_i)
//   5. CC-iωZ†(B, |0⟩, a_i)
//   6. A† (uncompute A)
//   7. H on |0⟩
//   8. CNOT(|0⟩, a_i)

uint32_t ProposedMethod::emitAndOutput(
    mockturtle::xag_network::node node,
    std::vector<mockturtle::xag_network::signal> &fanins) {
  auto child0 = xag_.get_node(fanins[0]);
  bool simple0 = isSimpleNode(child0);

  int piIdx = simple0 ? 0 : 1;
  int complexIdx = 1 - piIdx;

  uint32_t q_b = processSignal(fanins[piIdx]);

  uint32_t a_i = next_qubit_++;
  uint32_t q_zero = next_qubit_++;

  node_to_qubit_[xag_.node_to_index(node)] = a_i;

  // 1. H on |0⟩
  emitGate(GateType::H, {}, q_zero);

  // 2. CC-iωZ(B, |0⟩, a_i)
  emitCCiwZ(q_b, q_zero, a_i);

  // 3. Compute A sub-circuit
  size_t computeStart = result_.gates.size();
  uint32_t q_a = processSignal(fanins[complexIdx]);
  size_t computeEnd = result_.gates.size();

  // 4. CNOT(A_output, a_i)
  emitGate(GateType::CNOT, {q_a}, a_i);

  // 5. CC-iωZ†(B, |0⟩, a_i)
  emitCCiwZdg(q_b, q_zero, a_i);

  // 6. A† (uncompute A)
  appendAdjoint(computeStart, computeEnd);

  // 7. H on |0⟩
  emitGate(GateType::H, {}, q_zero);

  // 8. CNOT(|0⟩, a_i)
  emitGate(GateType::CNOT, {q_zero}, a_i);

  return a_i;
}

// ── Fig 12: AND one PI case ──────────────────────────────────────────────
// One input is PI (G), other requires sub-circuit (F).
// Uses T/T†/CNOT/H directly (no controlled phase gates).
//
// Wires: [F sub-circuit wires], G (PI), a (ancilla), f (ancilla)
// Sequence from Fig 12:
//   1. H(f)
//   2. CNOT(f, a)
//   3. CNOT(f, G)
//   4. T(G), Tdg(a)
//   5. Compute F sub-circuit
//   6. CNOT(F_out, G), CNOT(F_out, a)
//   7. Tdg(G), T(a)
//   8. CNOT(f, G), CNOT(f, a)
//   9. T(G), Tdg(a) — wait, need to re-read...
//
// Actually, reading Fig 12 carefully left-to-right:
//   H(f), CNOT(f,a), CNOT(a,G), T(G), Tdg(a),
//   CNOT(f,a), CNOT(a,G),
//   [F sub-circuit],
//   CNOT(F_out,G), CNOT(F_out,a),
//   Tdg(G), T(a),
//   CNOT(f,a), CNOT(a,G),
//   T(G), Tdg(a) — no, let me trace more carefully from image.
//
// Fig 12 gate sequence (3 lower wires: G, a, f; upper wires through F):
//   H(f)
//   CNOT(f, a)        — f controls, a target
//   CNOT(a, G)        — a controls, G target
//   T(G)
//   Tdg(a)
//   CNOT(f, a)        — f controls, a target
//   CNOT(a, G)        — a controls, G target
//   [F sub-circuit on upper wires]
//   CNOT(F_out, G)    — F output controls, G target
//   CNOT(F_out, a)    — F output controls, a target
//   Tdg(G)
//   T(a)
//   CNOT(f, a)
//   CNOT(a, G)
//   H(f)

uint32_t ProposedMethod::emitAndOnePI(
    mockturtle::xag_network::node node,
    std::vector<mockturtle::xag_network::signal> &fanins) {
  auto child0 = xag_.get_node(fanins[0]);
  bool simple0 = isSimpleNode(child0);

  int piIdx = simple0 ? 0 : 1;
  int complexIdx = 1 - piIdx;

  uint32_t q_g = processSignal(fanins[piIdx]); // G — primary input

  uint32_t a = next_qubit_++;  // a — ancilla
  uint32_t f = next_qubit_++;  // f — ancilla (H-prepared)

  node_to_qubit_[xag_.node_to_index(node)] = a;

  // Fig 12 gate sequence
  emitGate(GateType::H, {}, f);

  emitGate(GateType::CNOT, {f}, a);
  emitGate(GateType::CNOT, {a}, q_g);
  emitGate(GateType::T, {}, q_g);
  emitGate(GateType::Tdg, {}, a);

  emitGate(GateType::CNOT, {f}, a);
  emitGate(GateType::CNOT, {a}, q_g);

  // Compute F sub-circuit
  uint32_t q_f = processSignal(fanins[complexIdx]);

  // Continue Fig 12 after F
  emitGate(GateType::CNOT, {q_f}, q_g);
  emitGate(GateType::CNOT, {q_f}, a);
  emitGate(GateType::Tdg, {}, q_g);
  emitGate(GateType::T, {}, a);

  emitGate(GateType::CNOT, {f}, a);
  emitGate(GateType::CNOT, {a}, q_g);

  emitGate(GateType::H, {}, f);

  return a;
}

// ── Fig 9: CC-iωZ decomposition ─────────────────────────────────────────
// Doubly-controlled iωZ gate decomposed into T/T†/CNOT.
// 3 qubits: q0 (ctrl1), q1 (ctrl2), q2 (target).
//
// Gate sequence (from figure, same structure as Fig 6 but different T/Tdg
// placement reflecting the ω phase):
//   CNOT(q1,q2), T(q2), CNOT(q0,q2),
//   Tdg(q2), CNOT(q1,q2), T(q2), CNOT(q0,q2),
//   T(q1), CNOT(q0,q1), T(q0), Tdg(q1), CNOT(q0,q1)

void ProposedMethod::emitCCiwZ(uint32_t q0, uint32_t q1, uint32_t q2) {
  emitGate(GateType::CNOT, {q1}, q2);
  emitGate(GateType::T, {}, q2);
  emitGate(GateType::CNOT, {q0}, q2);
  emitGate(GateType::Tdg, {}, q2);
  emitGate(GateType::CNOT, {q1}, q2);
  emitGate(GateType::T, {}, q2);
  emitGate(GateType::CNOT, {q0}, q2);
  emitGate(GateType::T, {}, q1);
  emitGate(GateType::CNOT, {q0}, q1);
  emitGate(GateType::T, {}, q0);
  emitGate(GateType::Tdg, {}, q1);
  emitGate(GateType::CNOT, {q0}, q1);
}

// ── Fig 10: CC-iωZ† decomposition ───────────────────────────────────────
// Adjoint of Fig 9. Reversed gate order with T↔Tdg swapped.

void ProposedMethod::emitCCiwZdg(uint32_t q0, uint32_t q1, uint32_t q2) {
  emitGate(GateType::CNOT, {q0}, q1);
  emitGate(GateType::T, {}, q1);
  emitGate(GateType::Tdg, {}, q0);
  emitGate(GateType::CNOT, {q0}, q1);
  emitGate(GateType::Tdg, {}, q1);
  emitGate(GateType::CNOT, {q0}, q2);
  emitGate(GateType::Tdg, {}, q2);
  emitGate(GateType::CNOT, {q1}, q2);
  emitGate(GateType::T, {}, q2);
  emitGate(GateType::CNOT, {q0}, q2);
  emitGate(GateType::Tdg, {}, q2);
  emitGate(GateType::CNOT, {q1}, q2);
}

// ── Utility ──────────────────────────────────────────────────────────────

bool ProposedMethod::isSimpleNode(mockturtle::xag_network::node node) const {
  if (xag_.is_pi(node) || xag_.is_constant(node))
    return true;
  return node_to_qubit_.find(xag_.node_to_index(node)) != node_to_qubit_.end();
}

bool ProposedMethod::isOutputNode(mockturtle::xag_network::node node) const {
  return po_nodes_.count(xag_.node_to_index(node)) > 0;
}

void ProposedMethod::appendAdjoint(size_t startIdx, size_t endIdx) {
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

void ProposedMethod::emitGate(GateType type, std::vector<uint32_t> controls,
                              uint32_t target) {
  result_.gates.push_back({type, std::move(controls), target});
}
