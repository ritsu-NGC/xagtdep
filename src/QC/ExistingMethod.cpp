// ExistingMethod.cpp — Algorithm 1 ("Existing Method") implementation.
//
// Recursive depth-first XAG traversal producing quantum gates per:
//   XOR node  → Fig 5  (two CNOTs into a fresh output qubit)
//   AND node  → Fig 6  (H-sandwiched CC-iZ, both inputs simple)
//              → Fig 1  (AND at overall output: CC-iZ/CC-iZ† around A/A†)
//              → Fig 11 (one input PI: CC-iZ/CC-iZ† pair, F computed once)
//
// In Figs 1 and 11 the AND result lands on the H-conjugated |0> wire, not on
// the a_i/a_0 ancilla, so node_to_qubit_ maps the AND node to that wire. The
// a_i/a_0 wires are dirty ancillas: the CC-iZ/CC-iZ† pair cancels their
// initial value. Complemented fanins bracket the consuming gate(s) with X..X
// instead of flipping the shared producer wire; only a top-level PO keeps a
// trailing X on the result wire.
//
// Top-level: QC = parseXAG(xag). No top-level uncompute.

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

  // Process each primary output. processSignal applies the trailing X for a
  // complemented PO directly on the result wire. (Single PO in practice;
  // with multiple POs, output_qubit records the last one.)
  ctx.xag.foreach_po(
      [&](auto signal) { t.result_.output_qubit = t.processSignal(signal); });

  t.result_.num_qubits = t.next_qubit_;
  t.result_.num_ancillas = t.next_qubit_ - ctx.xag.num_pis();

  return t.result_;
}

// ── Recursive traversal ──────────────────────────────────────────────────

// Top-level (PO) entry: complement becomes a trailing X on the RESULT wire.
// Internal consumers must use resolveSignal() + X-brackets instead.
uint32_t ExistingMethod::processSignal(mockturtle::xag_network::signal sig) {
  auto [qubit, complemented] = resolveSignal(sig);
  if (complemented)
    emitGate(GateType::X, {}, qubit);
  return qubit;
}

// Resolve a signal WITHOUT emitting any complement gate: computes the child
// node (if not cached) and reports the complement flag to the caller.
std::pair<uint32_t, bool>
ExistingMethod::resolveSignal(mockturtle::xag_network::signal sig) {
  return {processNode(xag_.get_node(sig)), xag_.is_complemented(sig)};
}

uint32_t ExistingMethod::processNode(mockturtle::xag_network::node node) {
  uint32_t idx = xag_.node_to_index(node);

  auto it = node_to_qubit_.find(idx);
  if (it != node_to_qubit_.end())
    return it->second;

  if (idx == 0) {
    // Constant-0 node: allocate a |0⟩ qubit.
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
// Both sub-circuits CNOT their output into a fresh output qubit. A
// complemented fanin becomes an X on the FRESH target (¬v⊕w == (v⊕w)⊕1),
// never on the shared source wire.

uint32_t ExistingMethod::processXorNode(mockturtle::xag_network::node node) {
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
// Case order matches Algorithm 1: both-simple → output → one-PI → error.

uint32_t ExistingMethod::processAndNode(mockturtle::xag_network::node node) {
  std::vector<mockturtle::xag_network::signal> fanins;
  xag_.foreach_fanin(node, [&](auto sig) { fanins.push_back(sig); });

  auto child0 = xag_.get_node(fanins[0]);
  auto child1 = xag_.get_node(fanins[1]);
  bool simple0 = isSimpleNode(child0);
  bool simple1 = isSimpleNode(child1);

  if (simple0 && simple1) {
    // Case 1: both inputs already on qubits → Fig 6 (H-sandwiched CC-iZ).
    return emitAndBothPI(node, fanins);
  }

  if (isOutputNode(node)) {
    // Case 2: node output is overall output → Fig 1.
    return emitAndOutput(node, fanins);
  }

  if (simple0 || simple1) {
    // Case 3: one input simple, other is a sub-circuit → Fig 11.
    return emitAndOnePI(node, fanins);
  }

  // Case 4: both complex on a non-output node → error (per Algorithm 1).
  llvm::errs() << "[ExistingMethod] ERROR: both AND children are complex "
                  "sub-circuits (unsupported)\n";
  uint32_t q_err = next_qubit_++;
  node_to_qubit_[xag_.node_to_index(node)] = q_err;
  return q_err;
}

// ── Fig 6 (+H sandwich): AND with both inputs simple ─────────────────────
// H(f); CC-iZ(q_a, q_b → f); H(f)  ⇒  f = a∧b with relative phase i^{ab}
// (a 4-T relative-phase Toffoli). Complemented controls bracket the whole
// gadget — it is self-contained, no other wire is read inside.

uint32_t ExistingMethod::emitAndBothPI(
    mockturtle::xag_network::node node,
    std::vector<mockturtle::xag_network::signal> &fanins) {
  auto [q_a, c_a] = resolveSignal(fanins[0]);
  auto [q_b, c_b] = resolveSignal(fanins[1]);

  uint32_t f = next_qubit_++;

  // Fig 6 both-inputs-live relative-phase AND (shared with Proposed/Pebbling);
  // H;Tdg;CC-iωZ;H expands to the same gates as H;CC-iZ;H.
  gadgets::emitAndIntermediateLive(result_, q_a, q_b, f, c_a, c_b);

  node_to_qubit_[xag_.node_to_index(node)] = f;
  return f;
}

// ── Fig 1: AND at overall output ─────────────────────────────────────────
// Wires: [A sub-circuit wires], B (simple), a_i (dirty ancilla), z (|0⟩).
//
//   1. H(z)
//   2. CC-iZ(B, z → a_i)
//   3. compute A  →  q_g
//   4. CNOT(q_g → a_i)            (a_i: d → d⊕g)
//   5. CC-iZ†(B, z → a_i)         (pair kicks (−1)^{B·g·z} onto z)
//   6. H(z)                       (z now holds B∧g — the RESULT)
//   7. CNOT(q_g → a_i)            (restores a_i to d)
//   8. A† (uncompute A)
//
// The CC pair is insensitive to a_i's initial value (dirty borrow) and the
// i/−i relative phases cancel between iZ and iZ†, so the kickback is exact.
// Complemented B brackets EACH CC gate (A computes in between and may read
// B's wire); complemented A-output brackets each copy-CNOT.

uint32_t ExistingMethod::emitAndOutput(
    mockturtle::xag_network::node node,
    std::vector<mockturtle::xag_network::signal> &fanins) {
  auto child0 = xag_.get_node(fanins[0]);
  bool simple0 = isSimpleNode(child0);

  // Identify the simple child (B) and the sub-circuit child (A).
  int piIdx = simple0 ? 0 : 1;
  int complexIdx = 1 - piIdx;

  auto [q_b, c_b] = resolveSignal(fanins[piIdx]);

  uint32_t a_i = next_qubit_++;    // dirty ancilla (fresh |0⟩ is valid dirty)
  uint32_t q_zero = next_qubit_++; // z — |0⟩ kickback wire, carries the result

  // 1. H(z)
  emitGate(GateType::H, {}, q_zero);

  // 2. CC-iZ(B, z → a_i)
  if (c_b)
    emitGate(GateType::X, {}, q_b);
  gadgets::emitCCiZ(result_, q_b, q_zero, a_i);
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

  // 5. CC-iZ†(B, z → a_i)
  if (c_b)
    emitGate(GateType::X, {}, q_b);
  gadgets::emitCCiZdg(result_, q_b, q_zero, a_i);
  if (c_b)
    emitGate(GateType::X, {}, q_b);

  // 6. H(z) — z-wire now holds the AND result.
  emitGate(GateType::H, {}, q_zero);

  // 7. CNOT(A_output → a_i) — restores a_i (still before the uncompute).
  if (c_g)
    emitGate(GateType::X, {}, q_g);
  emitGate(GateType::CNOT, {q_g}, a_i);
  if (c_g)
    emitGate(GateType::X, {}, q_g);

  // 8. A† — uncompute the A window.
  gadgets::appendAdjoint(result_, computeStart, computeEnd);

  // Qubits computed inside the window are back to |0⟩ — drop their cache
  // entries so later consumers recompute instead of reading dead wires.
  std::vector<uint32_t> toErase;
  for (const auto &kv : node_to_qubit_)
    if (keysBefore.find(kv.first) == keysBefore.end())
      toErase.push_back(kv.first);
  for (uint32_t key : toErase)
    node_to_qubit_.erase(key);

  // The RESULT lives on the z-wire.
  node_to_qubit_[xag_.node_to_index(node)] = q_zero;
  return q_zero;
}

// ── Fig 11: intermediate AND with one simple input ──────────────────────
// Wires: [F sub-circuit wires], G (simple), a_0 (dirty ancilla), z (|0⟩).
//
//   0. compute F  →  q_h   (HOISTED before the gadget; F may read G's cone)
//   1. H(z)
//   2. CC-iZ(G, z → a_0)
//   3. CNOT(q_h → a_0)            (a_0: d → d⊕h)
//   4. CC-iZ†(G, z → a_0)         (pair kicks (−1)^{G·h·z} onto z)
//   5. H(z)                       (z holds G∧h — the RESULT)
//
// Per the figure there is NO final CNOT and NO F† — F's output stays as
// garbage and a_0 becomes a generated dirty ancilla (d⊕h).

uint32_t ExistingMethod::emitAndOnePI(
    mockturtle::xag_network::node node,
    std::vector<mockturtle::xag_network::signal> &fanins) {
  auto child0 = xag_.get_node(fanins[0]);
  bool simple0 = isSimpleNode(child0);

  int piIdx = simple0 ? 0 : 1;
  int complexIdx = 1 - piIdx;

  // 0. Hoist the F-compute out of the gadget.
  auto [q_h, c_h] = resolveSignal(fanins[complexIdx]);
  auto [q_g, c_g] = resolveSignal(fanins[piIdx]);

  uint32_t a_0 = next_qubit_++;    // dirty ancilla
  uint32_t q_zero = next_qubit_++; // z — kickback wire, carries the result

  // 1. H(z)
  emitGate(GateType::H, {}, q_zero);

  // 2. CC-iZ(G, z → a_0)
  if (c_g)
    emitGate(GateType::X, {}, q_g);
  gadgets::emitCCiZ(result_, q_g, q_zero, a_0);
  if (c_g)
    emitGate(GateType::X, {}, q_g);

  // 3. CNOT(F_output → a_0)
  if (c_h)
    emitGate(GateType::X, {}, q_h);
  emitGate(GateType::CNOT, {q_h}, a_0);
  if (c_h)
    emitGate(GateType::X, {}, q_h);

  // 4. CC-iZ†(G, z → a_0)
  if (c_g)
    emitGate(GateType::X, {}, q_g);
  gadgets::emitCCiZdg(result_, q_g, q_zero, a_0);
  if (c_g)
    emitGate(GateType::X, {}, q_g);

  // 5. H(z) — z-wire now holds the AND result.
  emitGate(GateType::H, {}, q_zero);

  node_to_qubit_[xag_.node_to_index(node)] = q_zero;
  return q_zero;
}

// ── Fig 6: CC-iZ decomposition (4 T, 6 CNOT) ────────────────────────────
// diag(1,1,1,1,1,1,i,−i) on |q0 q1 q2⟩.
//   T†(c); CX(c→a); CX(b→c); CX(a→b); T(a); T†(b); T(c);
//   CX(a→b); CX(b→c); CX(c→a)         with (a,b,c) = (q0,q1,q2)

// Body now provided by gadgets::emitCCiZ (ToffoliGadgets).

// ── Fig 7: exact CC-iZ† decomposition (4 T, 6 CNOT) ─────────────────────
//   CX(c→a); CX(b→c); CX(a→b); T†(a); T(b); T†(c);
//   CX(a→b); CX(b→c); CX(c→a); T(c)

// Body now provided by gadgets::emitCCiZdg (ToffoliGadgets).

// ── Utility ──────────────────────────────────────────────────────────────

bool ExistingMethod::isSimpleNode(mockturtle::xag_network::node node) const {
  if (xag_.is_pi(node) || xag_.is_constant(node))
    return true;
  return node_to_qubit_.find(xag_.node_to_index(node)) != node_to_qubit_.end();
}

bool ExistingMethod::isOutputNode(mockturtle::xag_network::node node) const {
  return po_nodes_.count(xag_.node_to_index(node)) > 0;
}

void ExistingMethod::emitGate(GateType type, std::vector<uint32_t> controls,
                              uint32_t target) {
  gadgets::emitGate(result_, type, std::move(controls), target);
}
