// XAGToGateList.cpp — Implements the SPEC's XAG2QC algorithm.
//
// Qubit allocation:
//   PIs       → qubits 0 .. num_pis-1
//   AND gates → fresh ancilla qubits (allocated on first visit)
//   XOR gates → fresh output qubit with two CNOTs (preserves children)
//
// Depth-first traversal from each PO back to PIs.

#include "XAGToGateList.h"
#include "llvm/Support/raw_ostream.h"

using namespace xagtdep;

XAGToGateList::XAGToGateList(const mockturtle::xag_network &xag) : xag_(xag) {}

QCGateList XAGToGateList::translate(const XagContext &ctx) {
  XAGToGateList t(ctx.xag);

  // Assign qubits 0..num_pis-1 to primary inputs.
  ctx.xag.foreach_pi([&](auto node) {
    t.node_to_qubit_[ctx.xag.node_to_index(node)] = t.next_qubit_++;
  });

  t.result_.num_pis = ctx.xag.num_pis();

  // Process each primary output (depth-first from output to inputs).
  // processSignal applies the trailing X for a complemented PO on the
  // result wire; output_qubit records it (last PO wins; single-PO in
  // practice).
  ctx.xag.foreach_po(
      [&](auto signal) { t.result_.output_qubit = t.processSignal(signal); });

  t.result_.num_qubits = t.next_qubit_;
  t.result_.num_ancillas = t.next_qubit_ - ctx.xag.num_pis();

  return t.result_;
}

// Top-level (PO) entry: complement becomes a trailing X on the RESULT wire.
// Internal consumers use resolveSignal() + X-brackets instead — emitting X
// on the producer's wire in place would corrupt shared signals/PIs for
// later readers.
uint32_t
XAGToGateList::processSignal(mockturtle::xag_network::signal sig) {
  auto [qubit, complemented] = resolveSignal(sig);
  if (complemented) {
    emitGate(GateType::X, {}, qubit);
  }
  return qubit;
}

// Resolve a signal WITHOUT emitting any complement gate.
std::pair<uint32_t, bool>
XAGToGateList::resolveSignal(mockturtle::xag_network::signal sig) {
  return {processNode(xag_.get_node(sig)), xag_.is_complemented(sig)};
}

uint32_t XAGToGateList::processNode(mockturtle::xag_network::node node) {
  uint32_t idx = xag_.node_to_index(node);

  // Already visited — return cached qubit.
  auto it = node_to_qubit_.find(idx);
  if (it != node_to_qubit_.end()) {
    return it->second;
  }

  // Constant-0 node (index 0): allocate a qubit initialised to |0⟩.
  if (idx == 0) {
    uint32_t qubit = next_qubit_++;
    node_to_qubit_[idx] = qubit;
    return qubit;
  }

  if (xag_.is_pi(node)) {
    // PIs should already be mapped during initialisation.
    return node_to_qubit_[idx];
  }

  if (xag_.is_and(node)) {
    return processAndNode(node);
  }

  // Must be an XOR node.
  return processXorNode(node);
}

uint32_t
XAGToGateList::processAndNode(mockturtle::xag_network::node node) {
  // Allocate ancilla for AND output (initialised to |0⟩).
  uint32_t a_out = next_qubit_++;
  node_to_qubit_[xag_.node_to_index(node)] = a_out;

  // Collect the two fanin signals.
  std::vector<mockturtle::xag_network::signal> fanins;
  xag_.foreach_fanin(node, [&](auto sig) { fanins.push_back(sig); });

  auto child0 = xag_.get_node(fanins[0]);
  auto child1 = xag_.get_node(fanins[1]);
  bool simple0 = isSimpleNode(child0);
  bool simple1 = isSimpleNode(child1);

  if (simple0 && simple1) {
    // Case 1: Both inputs already on qubits — simple Toffoli. Complemented
    // controls X-bracket the Toffoli (wires restored immediately).
    auto [q_a, c_a] = resolveSignal(fanins[0]);
    auto [q_b, c_b] = resolveSignal(fanins[1]);
    if (c_a)
      emitGate(GateType::X, {}, q_a);
    if (c_b)
      emitGate(GateType::X, {}, q_b);
    emitGate(GateType::Toffoli, {q_a, q_b}, a_out);
    if (c_a)
      emitGate(GateType::X, {}, q_a);
    if (c_b)
      emitGate(GateType::X, {}, q_b);
  } else {
    // Case 2: At least one input requires a sub-circuit (compute/uncompute).
    // Identify simple vs complex child. If both complex, process the first
    // normally to make it simple, then compute/uncompute the second.
    int simpleIdx = simple0 ? 0 : (simple1 ? 1 : -1);
    int complexIdx;

    uint32_t q_simple;
    bool c_simple;
    if (simpleIdx >= 0) {
      std::tie(q_simple, c_simple) = resolveSignal(fanins[simpleIdx]);
      complexIdx = 1 - simpleIdx;
    } else {
      // Both complex: process child 0 normally (makes it "simple").
      std::tie(q_simple, c_simple) = resolveSignal(fanins[0]);
      complexIdx = 1;
    }

    // Snapshot node_to_qubit_ keys before computing the complex child.
    std::unordered_set<uint32_t> keysBefore;
    for (const auto &kv : node_to_qubit_) {
      keysBefore.insert(kv.first);
    }

    size_t gatesBefore = result_.gates.size();
    auto [a_k, c_k] = resolveSignal(fanins[complexIdx]); // COMPUTE
    size_t gatesAfter = result_.gates.size();

    // USE — complement brackets sit OUTSIDE the uncompute window.
    if (c_simple)
      emitGate(GateType::X, {}, q_simple);
    if (c_k)
      emitGate(GateType::X, {}, a_k);
    emitGate(GateType::Toffoli, {q_simple, a_k}, a_out);
    if (c_simple)
      emitGate(GateType::X, {}, q_simple);
    if (c_k)
      emitGate(GateType::X, {}, a_k);

    emitUncompute(gatesBefore, gatesAfter); // UNCOMPUTE

    // Invalidate node_to_qubit_ entries added during compute — those
    // ancilla qubits are back to |0⟩ after uncomputation.
    uint32_t thisIdx = xag_.node_to_index(node);
    std::vector<uint32_t> toErase;
    for (const auto &kv : node_to_qubit_) {
      if (kv.first != thisIdx && keysBefore.find(kv.first) == keysBefore.end()) {
        toErase.push_back(kv.first);
      }
    }
    for (uint32_t key : toErase) {
      node_to_qubit_.erase(key);
    }
  }

  return a_out;
}

uint32_t
XAGToGateList::processXorNode(mockturtle::xag_network::node node) {
  uint32_t idx = xag_.node_to_index(node);

  // Collect the two fanin signals.
  std::vector<mockturtle::xag_network::signal> fanins;
  xag_.foreach_fanin(node, [&](auto sig) { fanins.push_back(sig); });

  // Process both children.
  auto [q_a, c_a] = resolveSignal(fanins[0]);
  auto [q_b, c_b] = resolveSignal(fanins[1]);

  // Allocate a fresh output qubit to preserve both children's qubits.
  // A complemented fanin becomes an X on the FRESH target (¬v⊕w == v⊕w⊕1),
  // never on the shared source wire.
  uint32_t q_out = next_qubit_++;
  emitGate(GateType::CNOT, {q_a}, q_out); // q_out ^= A
  if (c_a)
    emitGate(GateType::X, {}, q_out);
  emitGate(GateType::CNOT, {q_b}, q_out); // q_out ^= B  →  q_out = A XOR B
  if (c_b)
    emitGate(GateType::X, {}, q_out);

  node_to_qubit_[idx] = q_out;
  return q_out;
}

bool XAGToGateList::isSimpleNode(mockturtle::xag_network::node node) const {
  if (xag_.is_pi(node) || xag_.is_constant(node))
    return true;
  return node_to_qubit_.find(xag_.node_to_index(node)) != node_to_qubit_.end();
}

void XAGToGateList::emitUncompute(size_t startIdx, size_t endIdx) {
  for (size_t i = endIdx; i > startIdx; --i) {
    const auto &gate = result_.gates[i - 1];
    emitGate(gate.type, gate.controls, gate.target);
  }
}

void XAGToGateList::emitGate(GateType type, std::vector<uint32_t> controls,
                             uint32_t target) {
  result_.gates.push_back({type, std::move(controls), target});
}
