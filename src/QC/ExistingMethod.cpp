// ExistingMethod.cpp — Algorithm 1 ("Existing Method") implementation.
//
// Recursive depth-first XAG traversal producing quantum gates per:
//   XOR node  → Fig 5  (two CNOTs into output qubit)
//   AND node  → Fig 6  (CC-iZ decomposition, both inputs PI)
//              → Fig 1  (AND at overall output, with A/A† and CC-Z)
//              → Fig 11 (one input PI, with F sub-circuit and CC-iZ)
//
// Top-level: QC = compute || uncompute (adjoint of compute appended).

#include "ExistingMethod.h"
#include "llvm/Support/raw_ostream.h"

using namespace xagtdep;

ExistingMethod::ExistingMethod(const mockturtle::xag_network &xag)
    : xag_(xag) {}

QCGateList ExistingMethod::translate(const XagContext &ctx) {
  ExistingMethod t(ctx.xag);

  // Assign qubits 0..num_pis-1 to primary inputs.
  ctx.xag.foreach_pi(
      [&](auto node) { t.node_to_qubit_[ctx.xag.node_to_index(node)] = t.next_qubit_++; });
  t.result_.num_pis = ctx.xag.num_pis();

  // Build set of PO node indices for isOutputNode detection.
  ctx.xag.foreach_po([&](auto signal) {
    t.po_nodes_.insert(ctx.xag.node_to_index(ctx.xag.get_node(signal)));
  });

  // Compute phase: process each primary output.
  ctx.xag.foreach_po([&](auto signal) { t.processSignal(signal); });

  size_t computeEnd = t.result_.gates.size();

  // Uncompute phase: append adjoint of entire compute circuit.
  t.appendAdjoint(0, computeEnd);

  t.result_.num_qubits = t.next_qubit_;
  t.result_.num_ancillas = t.next_qubit_ - ctx.xag.num_pis();

  return t.result_;
}

// ── Recursive traversal ──────────────────────────────────────────────────

uint32_t ExistingMethod::processSignal(mockturtle::xag_network::signal sig) {
  auto node = xag_.get_node(sig);
  bool complemented = xag_.is_complemented(sig);
  uint32_t qubit = processNode(node);
  if (complemented)
    emitGate(GateType::X, {}, qubit);
  return qubit;
}

uint32_t ExistingMethod::processNode(mockturtle::xag_network::node node) {
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
// Sub-circuits B then A, each CNOT their output into a fresh output qubit.

uint32_t ExistingMethod::processXorNode(mockturtle::xag_network::node node) {
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

uint32_t ExistingMethod::processAndNode(mockturtle::xag_network::node node) {
  std::vector<mockturtle::xag_network::signal> fanins;
  xag_.foreach_fanin(node, [&](auto sig) { fanins.push_back(sig); });

  auto child0 = xag_.get_node(fanins[0]);
  auto child1 = xag_.get_node(fanins[1]);
  bool simple0 = isSimpleNode(child0);
  bool simple1 = isSimpleNode(child1);

  if (simple0 && simple1) {
    // Case 1: Both inputs are primary inputs / already on qubits → Fig 6
    uint32_t q_a = processSignal(fanins[0]);
    uint32_t q_b = processSignal(fanins[1]);
    return emitAndBothPI(node, q_a, q_b);
  }

  if (isOutputNode(node)) {
    // Case 2: Node output is overall output → Fig 1
    return emitAndOutput(node, fanins);
  }

  if (simple0 || simple1) {
    // Case 3: One input is PI, other is sub-circuit → Fig 11
    return emitAndOnePI(node, fanins);
  }

  // Case 4: Both complex → error
  llvm::errs() << "[ExistingMethod] ERROR: both AND children are complex "
                  "sub-circuits (unsupported)\n";
  uint32_t q_err = next_qubit_++;
  node_to_qubit_[xag_.node_to_index(node)] = q_err;
  return q_err;
}

// ── Fig 6: AND both PI (CC-iZ decomposition) ────────────────────────────
// Both inputs already on qubits. Allocate ancilla, apply CC-iZ.
// 3 wires: q_a (ctrl), q_b (ctrl), a_out (target ancilla).

uint32_t ExistingMethod::emitAndBothPI(mockturtle::xag_network::node node,
                                       uint32_t q_a, uint32_t q_b) {
  uint32_t a_out = next_qubit_++;
  node_to_qubit_[xag_.node_to_index(node)] = a_out;

  // The AND output ancilla needs H to go from |0⟩ to |+⟩, then CC-iZ,
  // then H back. This implements the Toffoli-like AND gate.
  emitGate(GateType::H, {}, a_out);
  emitCCiZ(q_a, q_b, a_out);
  emitGate(GateType::H, {}, a_out);

  return a_out;
}

// ── Fig 1: AND output case ───────────────────────────────────────────────
// Node output is a primary output. Uses A/A† sub-circuits with controlled
// phase gates and ancilla.
//
// Wires: [A sub-circuit wires], B (PI), a_i (ancilla), |0⟩ (ancilla)
// Sequence:
//   1. H on |0⟩
//   2. CC-iZ(B, |0⟩, a_i)   — controlled phase
//   3. Compute A sub-circuit
//   4. CNOT(A_output, a_i)
//   5. CC-iZ†(B, |0⟩, a_i)  — controlled phase adjoint
//   6. A† sub-circuit (uncompute A)
//   7. H on |0⟩
//   8. CNOT(|0⟩, a_i)

uint32_t ExistingMethod::emitAndOutput(
    mockturtle::xag_network::node node,
    std::vector<mockturtle::xag_network::signal> &fanins) {
  auto child0 = xag_.get_node(fanins[0]);
  auto child1 = xag_.get_node(fanins[1]);
  bool simple0 = isSimpleNode(child0);

  // Identify which child is the PI (B) and which is the sub-circuit (A).
  int piIdx = simple0 ? 0 : 1;
  int complexIdx = 1 - piIdx;

  uint32_t q_b = processSignal(fanins[piIdx]); // B — primary input

  // Allocate ancilla qubits.
  uint32_t a_i = next_qubit_++;   // a_i — AND output ancilla
  uint32_t q_zero = next_qubit_++; // |0⟩ — helper ancilla

  node_to_qubit_[xag_.node_to_index(node)] = a_i;

  // 1. H on |0⟩
  emitGate(GateType::H, {}, q_zero);

  // 2. CC-iZ(B, |0⟩, a_i)
  emitCCiZ(q_b, q_zero, a_i);

  // 3. Compute A sub-circuit
  size_t computeStart = result_.gates.size();
  uint32_t q_a = processSignal(fanins[complexIdx]);
  size_t computeEnd = result_.gates.size();

  // 4. CNOT(A_output, a_i)
  emitGate(GateType::CNOT, {q_a}, a_i);

  // 5. CC-iZ†(B, |0⟩, a_i)
  emitCCiZdg(q_b, q_zero, a_i);

  // 6. A† (uncompute A sub-circuit)
  appendAdjoint(computeStart, computeEnd);

  // 7. H on |0⟩
  emitGate(GateType::H, {}, q_zero);

  // 8. CNOT(|0⟩, a_i)
  emitGate(GateType::CNOT, {q_zero}, a_i);

  return a_i;
}

// ── Fig 11: AND one PI case ──────────────────────────────────────────────
// One input is PI (G), other requires sub-circuit (F).
//
// Wires: [F sub-circuit wires], G (PI), a_0 (ancilla), |0⟩ (ancilla)
// Sequence:
//   1. H on |0⟩
//   2. CC-iZ(G, |0⟩, a_0)
//   3. Compute F sub-circuit
//   4. CNOT(F_output, a_0)
//   5. CC-iZ†(G, |0⟩, a_0)
//   6. H on |0⟩
//   7. CNOT(|0⟩, a_0)

uint32_t ExistingMethod::emitAndOnePI(
    mockturtle::xag_network::node node,
    std::vector<mockturtle::xag_network::signal> &fanins) {
  auto child0 = xag_.get_node(fanins[0]);
  auto child1 = xag_.get_node(fanins[1]);
  bool simple0 = isSimpleNode(child0);

  int piIdx = simple0 ? 0 : 1;
  int complexIdx = 1 - piIdx;

  uint32_t q_g = processSignal(fanins[piIdx]); // G — primary input

  // Allocate ancilla qubits.
  uint32_t a_0 = next_qubit_++;    // a_0 — AND output ancilla
  uint32_t q_zero = next_qubit_++; // |0⟩ — helper ancilla

  node_to_qubit_[xag_.node_to_index(node)] = a_0;

  // 1. H on |0⟩
  emitGate(GateType::H, {}, q_zero);

  // 2. CC-iZ(G, |0⟩, a_0)
  emitCCiZ(q_g, q_zero, a_0);

  // 3. Compute F sub-circuit
  uint32_t q_f = processSignal(fanins[complexIdx]);

  // 4. CNOT(F_output, a_0)
  emitGate(GateType::CNOT, {q_f}, a_0);

  // 5. CC-iZ†(G, |0⟩, a_0)
  emitCCiZdg(q_g, q_zero, a_0);

  // 6. H on |0⟩
  emitGate(GateType::H, {}, q_zero);

  // 7. CNOT(|0⟩, a_0)
  emitGate(GateType::CNOT, {q_zero}, a_0);

  return a_0;
}

// ── Fig 6: CC-iZ decomposition ───────────────────────────────────────────
// Doubly-controlled iZ gate decomposed into T/T†/CNOT.
// 3 qubits: q0 (ctrl1), q1 (ctrl2), q2 (target).
//
// Gate sequence (from figure):
//   Tdg(q2), CNOT(q1,q2), T(q2), CNOT(q0,q2),
//   Tdg(q2), CNOT(q1,q2), T(q2), CNOT(q0,q2),
//   T(q1), CNOT(q0,q1), T(q0), Tdg(q1), CNOT(q0,q1)

void ExistingMethod::emitCCiZ(uint32_t q0, uint32_t q1, uint32_t q2) {
  emitGate(GateType::Tdg, {}, q2);
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

// ── Fig 7: CC-iZ† decomposition ─────────────────────────────────────────
// Adjoint of Fig 6. Gate sequence reversed with T↔Tdg swapped.

void ExistingMethod::emitCCiZdg(uint32_t q0, uint32_t q1, uint32_t q2) {
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
  emitGate(GateType::T, {}, q2);
}

// ── Utility ──────────────────────────────────────────────────────────────

bool ExistingMethod::isSimpleNode(mockturtle::xag_network::node node) const {
  if (xag_.is_pi(node) || xag_.is_constant(node))
    return true;
  return node_to_qubit_.find(xag_.node_to_index(node)) != node_to_qubit_.end();
}

bool ExistingMethod::isOutputNode(mockturtle::xag_network::node node) const {
  return po_nodes_.count(xag_.node_to_index(node)) > 0;
}

void ExistingMethod::appendAdjoint(size_t startIdx, size_t endIdx) {
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
      break; // H, X, CNOT, Toffoli are self-adjoint
    }
    emitGate(dag, gate.controls, gate.target);
  }
}

void ExistingMethod::emitGate(GateType type, std::vector<uint32_t> controls,
                              uint32_t target) {
  result_.gates.push_back({type, std::move(controls), target});
}
