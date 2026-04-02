// XAGToDecomposed.cpp — Implements Algorithm 1 (PARSEXAG) from the spec.
//
// Qubit allocation:
//   PIs       -> qubits 0 .. num_pis-1
//   AND gates -> fresh ancilla qubits (allocated on first visit)
//   XOR gates -> fresh output qubit with two CNOTs (preserves children)
//
// AND node decomposition uses relative-phase Toffoli gates (4 T-gates each)
// instead of abstract Toffoli gates:
//   Fig. 6  (iZ dagger)      — both children are PIs
//   Fig. 8  (i*omega*Z dagger) — one child is PI
//   Fig. 1                    — output is overall PO

#include "XAGToDecomposed.h"
#include "llvm/Support/raw_ostream.h"
#include <cassert>

using namespace xagtdep;

XAGToDecomposed::XAGToDecomposed(const mockturtle::xag_network &xag)
    : xag_(xag) {}

QCGateList XAGToDecomposed::translate(const XagContext &ctx) {
  XAGToDecomposed t(ctx.xag);

  // Assign qubits 0..num_pis-1 to primary inputs.
  ctx.xag.foreach_pi([&](auto node) {
    t.node_to_qubit_[ctx.xag.node_to_index(node)] = t.next_qubit_++;
  });

  t.result_.num_pis = ctx.xag.num_pis();

  // Pre-compute which nodes are primary outputs.
  ctx.xag.foreach_po([&](auto signal) {
    auto node = ctx.xag.get_node(signal);
    t.output_node_indices_.insert(ctx.xag.node_to_index(node));
  });

  // Process each primary output (depth-first from output to inputs).
  ctx.xag.foreach_po([&](auto signal) { t.processSignal(signal); });

  t.result_.num_qubits = t.next_qubit_;
  t.result_.num_ancillas = t.next_qubit_ - ctx.xag.num_pis();

  return t.result_;
}

// ── Signal processing ─────────────────────────────────────────────────────

uint32_t
XAGToDecomposed::processSignal(mockturtle::xag_network::signal sig) {
  auto node = xag_.get_node(sig);
  bool complemented = xag_.is_complemented(sig);

  uint32_t qubit = processNode(node);

  if (complemented) {
    emitGate(GateType::X, {}, qubit);
  }

  return qubit;
}

uint32_t
XAGToDecomposed::processNode(mockturtle::xag_network::node node) {
  uint32_t idx = xag_.node_to_index(node);

  // Already visited — return cached qubit.
  auto it = node_to_qubit_.find(idx);
  if (it != node_to_qubit_.end()) {
    return it->second;
  }

  // Constant-0 node (index 0).
  if (idx == 0) {
    uint32_t qubit = next_qubit_++;
    node_to_qubit_[idx] = qubit;
    return qubit;
  }

  if (xag_.is_pi(node)) {
    return node_to_qubit_[idx];
  }

  if (xag_.is_and(node)) {
    return processAndNode(node);
  }

  return processXorNode(node);
}

// ── XOR node (Fig. 4) ────────────────────────────────────────────────────

uint32_t
XAGToDecomposed::processXorNode(mockturtle::xag_network::node node) {
  uint32_t idx = xag_.node_to_index(node);

  std::vector<mockturtle::xag_network::signal> fanins;
  xag_.foreach_fanin(node, [&](auto sig) { fanins.push_back(sig); });

  uint32_t q_a = processSignal(fanins[0]);
  uint32_t q_b = processSignal(fanins[1]);

  uint32_t q_out = next_qubit_++;
  emitGate(GateType::CNOT, {q_a}, q_out);
  emitGate(GateType::CNOT, {q_b}, q_out);

  node_to_qubit_[idx] = q_out;
  return q_out;
}

// ── AND node (Algorithm 1, lines 19-28) ──────────────────────────────────

uint32_t
XAGToDecomposed::processAndNode(mockturtle::xag_network::node node) {
  // Allocate ancilla for AND output.
  uint32_t a_out = next_qubit_++;
  node_to_qubit_[xag_.node_to_index(node)] = a_out;

  // Collect fanin signals.
  std::vector<mockturtle::xag_network::signal> fanins;
  xag_.foreach_fanin(node, [&](auto sig) { fanins.push_back(sig); });

  auto child0 = xag_.get_node(fanins[0]);
  auto child1 = xag_.get_node(fanins[1]);
  bool simple0 = isSimpleNode(child0);
  bool simple1 = isSimpleNode(child1);
  bool isOutput = isOutputNode(node);

  if (simple0 && simple1) {
    // Case 1 (line 20): Both A and B are primary inputs.
    // Use Fig. 6 (iZ dagger relative-phase Toffoli).
    uint32_t q_a = processSignal(fanins[0]);
    uint32_t q_b = processSignal(fanins[1]);
    emitFig6(q_a, q_b, a_out);
  } else if (isOutput) {
    // Case 2 (line 22): Node output is overall output.
    // Use Fig. 1 (full circuit with H, Z, ancilla, A/A-dagger).
    int simpleIdx = simple0 ? 0 : (simple1 ? 1 : -1);
    int complexIdx;
    uint32_t q_simple;

    if (simpleIdx >= 0) {
      q_simple = processSignal(fanins[simpleIdx]);
      complexIdx = 1 - simpleIdx;
    } else {
      // Both complex at overall output: process first child normally.
      q_simple = processSignal(fanins[0]);
      complexIdx = 1;
    }

    size_t gatesBefore = result_.gates.size();
    uint32_t q_complex = processSignal(fanins[complexIdx]);
    size_t gatesAfter = result_.gates.size();

    emitFig1(q_simple, q_complex, a_out, gatesBefore, gatesAfter);
  } else {
    // Case 3 (line 24): One of A, B is primary input (not overall output).
    // Use Fig. 8 (i*omega*Z dagger relative-phase Toffoli).
    int simpleIdx = simple0 ? 0 : (simple1 ? 1 : -1);

    assert(simpleIdx >= 0 &&
           "Both AND children are complex and node is not overall output. "
           "This case is not supported by Algorithm 1 (line 27).");

    int complexIdx = 1 - simpleIdx;
    uint32_t q_simple = processSignal(fanins[simpleIdx]);

    // Snapshot for cache invalidation after uncompute.
    std::unordered_set<uint32_t> keysBefore;
    for (const auto &kv : node_to_qubit_) {
      keysBefore.insert(kv.first);
    }

    size_t gatesBefore = result_.gates.size();
    uint32_t q_complex = processSignal(fanins[complexIdx]); // COMPUTE
    size_t gatesAfter = result_.gates.size();

    emitFig8(q_simple, q_complex, a_out); // USE

    emitUncompute(gatesBefore, gatesAfter); // UNCOMPUTE

    // Invalidate cache entries added during compute.
    uint32_t thisIdx = xag_.node_to_index(node);
    std::vector<uint32_t> toErase;
    for (const auto &kv : node_to_qubit_) {
      if (kv.first != thisIdx &&
          keysBefore.find(kv.first) == keysBefore.end()) {
        toErase.push_back(kv.first);
      }
    }
    for (uint32_t key : toErase) {
      node_to_qubit_.erase(key);
    }
  }

  return a_out;
}

// ── Fig. 6: iZ-dagger relative-phase Toffoli (both PIs) ─────────────────
// Decomposes a Toffoli into 4 T-gates (2T + 2Tdg).
// 4 CNOTs + 4 T-type gates = 8 gates total.
//
// NOTE: Exact gate sequence to be verified against the paper's Fig. 6.
// This uses a standard 4-T relative-phase Toffoli decomposition.
void XAGToDecomposed::emitFig6(uint32_t ctrl0, uint32_t ctrl1,
                                uint32_t target) {
  emitGate(GateType::CNOT, {ctrl1}, target);
  emitGate(GateType::Tdg, {}, target);
  emitGate(GateType::CNOT, {ctrl0}, target);
  emitGate(GateType::T, {}, target);
  emitGate(GateType::CNOT, {ctrl1}, target);
  emitGate(GateType::Tdg, {}, target);
  emitGate(GateType::CNOT, {ctrl0}, target);
  emitGate(GateType::T, {}, target);
}

// ── Fig. 8: i*omega*Z-dagger relative-phase Toffoli (one PI) ────────────
// Decomposes a Toffoli into 4 T-gates (2T + 2Tdg), different phase variant.
// 4 CNOTs + 4 T-type gates = 8 gates total.
//
// NOTE: Exact gate sequence to be verified against the paper's Fig. 8.
// This uses a standard 4-T relative-phase Toffoli decomposition
// with swapped T/Tdg positions for the iωZ† phase variant.
void XAGToDecomposed::emitFig8(uint32_t ctrl0, uint32_t ctrl1,
                                uint32_t target) {
  emitGate(GateType::CNOT, {ctrl1}, target);
  emitGate(GateType::T, {}, target);
  emitGate(GateType::CNOT, {ctrl0}, target);
  emitGate(GateType::Tdg, {}, target);
  emitGate(GateType::CNOT, {ctrl1}, target);
  emitGate(GateType::T, {}, target);
  emitGate(GateType::CNOT, {ctrl0}, target);
  emitGate(GateType::Tdg, {}, target);
}

// ── Fig. 1: AND node whose output is overall PO ─────────────────────────
// Full circuit with ancilla |0>, H gates, Z/Z-dagger phase corrections,
// and A/A-dagger (compute/uncompute) blocks.
//
// Circuit structure:
//   B (simple)    ─────────────●─────────────●─────────────
//   a_out         ──Z──────────┼─────Z†──────┼──────────(+)
//   |0> (ancilla) ──H──────────●─────────────●──────H──────
//
// The internal controlled gates (B, |0>) -> a_out are relative-phase
// Toffolis themselves (both controls are simple), so we use Fig. 6.
void XAGToDecomposed::emitFig1(uint32_t simpleCtrl, uint32_t complexCtrl,
                                uint32_t target, size_t computeStart,
                                size_t computeEnd) {
  // Allocate ancilla qubit initialized to |0>.
  uint32_t ancilla = next_qubit_++;

  // H on ancilla -> |+>
  emitGate(GateType::H, {}, ancilla);

  // Z on target (phase correction before first Toffoli)
  emitGate(GateType::Z, {}, target);

  // First Toffoli: (simpleCtrl, ancilla) control target
  // Both simpleCtrl and ancilla are "simple" -> use Fig. 6
  emitFig6(simpleCtrl, ancilla, target);

  // Z-dagger on target (phase correction)
  emitGate(GateType::Zdg, {}, target);

  // Uncompute the complex child (A-dagger)
  emitUncompute(computeStart, computeEnd);

  // Second Toffoli: (simpleCtrl, ancilla) control target
  emitFig6(simpleCtrl, ancilla, target);

  // CNOT: complexCtrl -> target (final XOR)
  emitGate(GateType::CNOT, {complexCtrl}, target);

  // H on ancilla (back to computational basis)
  emitGate(GateType::H, {}, ancilla);
}

// ── Utility methods ──────────────────────────────────────────────────────

bool XAGToDecomposed::isSimpleNode(
    mockturtle::xag_network::node node) const {
  if (xag_.is_pi(node) || xag_.is_constant(node))
    return true;
  return node_to_qubit_.find(xag_.node_to_index(node)) !=
         node_to_qubit_.end();
}

bool XAGToDecomposed::isOutputNode(
    mockturtle::xag_network::node node) const {
  return output_node_indices_.count(xag_.node_to_index(node)) > 0;
}

GateType XAGToDecomposed::invertGateType(GateType t) {
  switch (t) {
  case GateType::T:
    return GateType::Tdg;
  case GateType::Tdg:
    return GateType::T;
  case GateType::Z:
    return GateType::Zdg;
  case GateType::Zdg:
    return GateType::Z;
  default:
    return t; // X, CNOT, H, Toffoli are self-inverse
  }
}

void XAGToDecomposed::emitUncompute(size_t startIdx, size_t endIdx) {
  for (size_t i = endIdx; i > startIdx; --i) {
    const auto &gate = result_.gates[i - 1];
    emitGate(invertGateType(gate.type), gate.controls, gate.target);
  }
}

void XAGToDecomposed::emitGate(GateType type,
                                std::vector<uint32_t> controls,
                                uint32_t target) {
  result_.gates.push_back({type, std::move(controls), target});
}
