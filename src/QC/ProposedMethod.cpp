// ProposedMethod.cpp — Algorithm 2 ("Proposed Method") implementation.
//
// Recursive depth-first XAG traversal producing quantum gates per:
//   XOR node  → Fig 5  (two CNOTs into a fresh output qubit)
//   AND node  → Fig 8  (relative-phase Toffoli: H; T†; Fig-9 ladder; H)
//              → Fig 2  (AND at overall output: CC-iωZ/CC-iωZ† around A/A†)
//              → Fig 12 (one input PI: dirty-ancilla AND, F computed once)
//
// Like Algorithm 1, the AND result (Figs 2 and 12) lands on the H-conjugated
// |0> wire (f / z), not on the a-ancilla. The a-wires are dirty ancillas. The
// cheaper relative-phase gates leave a residual phase on the inputs (i^{ab}
// per Fig 8, i^{gh} per Fig 12, ω^{−g} per Fig 2) that is invisible to
// measurement — the price of the lower T-count. Complemented fanins bracket
// the consuming gate(s)/gadget with X..X; only a top-level PO keeps a
// trailing X on the result wire.
//
// Top-level: QC = parseXAG(xag) directly. No top-level uncompute.

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
  ctx.xag.foreach_po(
      [&](auto signal) { t.result_.output_qubit = t.processSignal(signal); });

  t.result_.num_qubits = t.next_qubit_;
  t.result_.num_ancillas = t.next_qubit_ - ctx.xag.num_pis();

  return t.result_;
}

// ── Recursive traversal ──────────────────────────────────────────────────

// Top-level (PO) entry: complement becomes a trailing X on the RESULT wire.
uint32_t ProposedMethod::processSignal(mockturtle::xag_network::signal sig) {
  auto [qubit, complemented] = resolveSignal(sig);
  if (complemented)
    emitGate(GateType::X, {}, qubit);
  return qubit;
}

// Resolve a signal WITHOUT emitting any complement gate.
std::pair<uint32_t, bool>
ProposedMethod::resolveSignal(mockturtle::xag_network::signal sig) {
  return {processNode(xag_.get_node(sig)), xag_.is_complemented(sig)};
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

  auto [q_a, c_a] = resolveSignal(fanins[0]);
  auto [q_b, c_b] = resolveSignal(fanins[1]);

  uint32_t q_out = next_qubit_++;
  emitGate(GateType::CNOT, {q_a}, q_out);
  if (c_a)
    emitGate(GateType::X, {}, q_out);
  emitGate(GateType::CNOT, {q_b}, q_out);
  if (c_b)
    emitGate(GateType::X, {}, q_out);

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
    return emitAndBothPI(node, fanins);
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

// ── Fig 8: relative-phase Toffoli (4 T) ─────────────────────────────────
// H(f); T†(f); [Fig-9 ladder on (q_a, q_b, f)]; H(f)
//   ⇒ f = a∧b with relative phase i^{ab}.
// (T†(f) + Fig-9 ladder ≡ the exact CC-iZ of Fig 6.)
// Complemented controls bracket the whole gadget — it is self-contained.

uint32_t ProposedMethod::emitAndBothPI(
    mockturtle::xag_network::node node,
    std::vector<mockturtle::xag_network::signal> &fanins) {
  auto [q_a, c_a] = resolveSignal(fanins[0]);
  auto [q_b, c_b] = resolveSignal(fanins[1]);

  uint32_t f = next_qubit_++;

  if (c_a)
    emitGate(GateType::X, {}, q_a);
  if (c_b)
    emitGate(GateType::X, {}, q_b);

  emitGate(GateType::H, {}, f);
  emitGate(GateType::Tdg, {}, f);
  emitCCiwZ(q_a, q_b, f);
  emitGate(GateType::H, {}, f);

  if (c_a)
    emitGate(GateType::X, {}, q_a);
  if (c_b)
    emitGate(GateType::X, {}, q_b);

  node_to_qubit_[xag_.node_to_index(node)] = f;
  return f;
}

// ── Fig 2: AND at overall output (6 T) ──────────────────────────────────
// Same structure as Fig 1 but with the 3-T CC-iωZ/CC-iωZ† pair (Figs 9/10).
// The stray ω-phases cancel pairwise up to a residual ω^{−g(x)} relative
// phase on the inputs (measurement-invisible).
//
//   1. H(z)
//   2. CC-iωZ(B, z → a_i)
//   3. compute A → q_g
//   4. CNOT(q_g → a_i)
//   5. CC-iωZ†(B, z → a_i)
//   6. H(z)                      (z holds B∧g — the RESULT)
//   7. CNOT(q_g → a_i)           (restores a_i)
//   8. A† (uncompute A)

uint32_t ProposedMethod::emitAndOutput(
    mockturtle::xag_network::node node,
    std::vector<mockturtle::xag_network::signal> &fanins) {
  auto child0 = xag_.get_node(fanins[0]);
  bool simple0 = isSimpleNode(child0);

  int piIdx = simple0 ? 0 : 1;
  int complexIdx = 1 - piIdx;

  auto [q_b, c_b] = resolveSignal(fanins[piIdx]);

  uint32_t a_i = next_qubit_++;    // dirty ancilla
  uint32_t q_zero = next_qubit_++; // z — kickback wire, carries the result

  // 1. H(z)
  emitGate(GateType::H, {}, q_zero);

  // 2. CC-iωZ(B, z → a_i)
  if (c_b)
    emitGate(GateType::X, {}, q_b);
  emitCCiwZ(q_b, q_zero, a_i);
  if (c_b)
    emitGate(GateType::X, {}, q_b);

  // 3. Compute A sub-circuit (recorded window for the A† uncompute).
  size_t computeStart = result_.gates.size();
  std::unordered_set<uint32_t> keysBefore;
  for (const auto &kv : node_to_qubit_)
    keysBefore.insert(kv.first);
  auto [q_g, c_g] = resolveSignal(fanins[complexIdx]);
  size_t computeEnd = result_.gates.size();

  // 4. CNOT(A_output → a_i)
  if (c_g)
    emitGate(GateType::X, {}, q_g);
  emitGate(GateType::CNOT, {q_g}, a_i);
  if (c_g)
    emitGate(GateType::X, {}, q_g);

  // 5. CC-iωZ†(B, z → a_i)
  if (c_b)
    emitGate(GateType::X, {}, q_b);
  emitCCiwZdg(q_b, q_zero, a_i);
  if (c_b)
    emitGate(GateType::X, {}, q_b);

  // 6. H(z) — z-wire now holds the AND result.
  emitGate(GateType::H, {}, q_zero);

  // 7. CNOT(A_output → a_i) — restores a_i (before the uncompute).
  if (c_g)
    emitGate(GateType::X, {}, q_g);
  emitGate(GateType::CNOT, {q_g}, a_i);
  if (c_g)
    emitGate(GateType::X, {}, q_g);

  // 8. A† — uncompute the A window.
  appendAdjoint(computeStart, computeEnd);

  // Drop cache entries for nodes whose wires were just uncomputed.
  std::vector<uint32_t> toErase;
  for (const auto &kv : node_to_qubit_)
    if (keysBefore.find(kv.first) == keysBefore.end())
      toErase.push_back(kv.first);
  for (uint32_t key : toErase)
    node_to_qubit_.erase(key);

  node_to_qubit_[xag_.node_to_index(node)] = q_zero;
  return q_zero;
}

// ── Fig 12: intermediate AND with one simple input (4 T) ────────────────
// Wires: [F sub-circuit wires], G (simple), a (dirty ancilla), f (|0⟩).
// Figure sequence:
//
//   0. compute F → q_h   (HOISTED — the gadget transiently corrupts G, so
//                         F, whose cone may read G, must be computed first)
//   1. H(f)
//   2. CX(f→a); CX(a→G); T(G); T†(a); CX(G→a)
//   3. CNOT(q_h → G)
//   4. CX(G→a); T†(G); T(a); CX(a→G); CX(f→a)
//   5. H(f)
//
// Result: f = G∧h (relative phase i^{Gh}); G restored; a = d⊕h (generated
// dirty ancilla); F's output stays as garbage. Works for any initial a.
// A complemented G brackets the WHOLE gadget (safe only because F was
// hoisted); a complemented F-output brackets the single CNOT in step 3.

uint32_t ProposedMethod::emitAndOnePI(
    mockturtle::xag_network::node node,
    std::vector<mockturtle::xag_network::signal> &fanins) {
  auto child0 = xag_.get_node(fanins[0]);
  bool simple0 = isSimpleNode(child0);

  int piIdx = simple0 ? 0 : 1;
  int complexIdx = 1 - piIdx;

  // 0. Hoist the F-compute out of the gadget.
  auto [q_h, c_h] = resolveSignal(fanins[complexIdx]);
  auto [q_g, c_g] = resolveSignal(fanins[piIdx]);

  uint32_t a = next_qubit_++; // dirty ancilla
  uint32_t f = next_qubit_++; // |0⟩ kickback wire, carries the result

  if (c_g)
    emitGate(GateType::X, {}, q_g); // whole-gadget bracket (open)

  // 1.
  emitGate(GateType::H, {}, f);
  // 2.
  emitGate(GateType::CNOT, {f}, a);
  emitGate(GateType::CNOT, {a}, q_g);
  emitGate(GateType::T, {}, q_g);
  emitGate(GateType::Tdg, {}, a);
  emitGate(GateType::CNOT, {q_g}, a);
  // 3. CNOT(F_output → G)
  if (c_h)
    emitGate(GateType::X, {}, q_h);
  emitGate(GateType::CNOT, {q_h}, q_g);
  if (c_h)
    emitGate(GateType::X, {}, q_h);
  // 4.
  emitGate(GateType::CNOT, {q_g}, a);
  emitGate(GateType::Tdg, {}, q_g);
  emitGate(GateType::T, {}, a);
  emitGate(GateType::CNOT, {a}, q_g);
  emitGate(GateType::CNOT, {f}, a);
  // 5.
  emitGate(GateType::H, {}, f);

  if (c_g)
    emitGate(GateType::X, {}, q_g); // whole-gadget bracket (close)

  node_to_qubit_[xag_.node_to_index(node)] = f;
  return f;
}

// ── Fig 9: relative-phase CC-iωZ decomposition (3 T, 6 CNOT) ────────────
//   CX(c→a); CX(b→c); CX(a→b); T(a); T†(b); T(c);
//   CX(a→b); CX(b→c); CX(c→a)         with (a,b,c) = (q0,q1,q2)

void ProposedMethod::emitCCiwZ(uint32_t q0, uint32_t q1, uint32_t q2) {
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

// ── Fig 10: CC-iωZ† decomposition (3 T, 6 CNOT) ─────────────────────────
// Same network with T ↔ T†.

void ProposedMethod::emitCCiwZdg(uint32_t q0, uint32_t q1, uint32_t q2) {
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
