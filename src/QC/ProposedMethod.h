// ProposedMethod.h — Algorithm 2 ("Proposed Method") XAG-to-quantum-circuit
// translator. AND gadgets use the 3-T relative-phase CC-iωZ/CC-iωZ† ladders
// (Figs 9/10), the 4-T relative-phase Toffoli (Fig 8) and the 4-T
// dirty-ancilla AND (Fig 12); NO top-level uncompute. The AND result
// materialises on the H-conjugated |0⟩ wire (see ProposedMethod.cpp header
// comment for the full semantics).

#ifndef PROPOSEDMETHOD_H
#define PROPOSEDMETHOD_H

#include "QCGateList.h"
#include "ToffoliGadgets.h"
#include "XagContext.h"
#include <unordered_map>
#include <unordered_set>
#include <utility>

namespace xagtdep {

class ProposedMethod {
public:
  /// Traverse ctx.xag depth-first and produce a gate list per Algorithm 2.
  static QCGateList translate(const XagContext &ctx);

private:
  explicit ProposedMethod(const mockturtle::xag_network &xag);

  // ── Recursive traversal ──────────────────────────────────────────────
  /// Top-level (PO) entry: applies a trailing X for complemented POs.
  uint32_t processSignal(mockturtle::xag_network::signal sig);
  /// Internal entry: returns {qubit, complemented} WITHOUT emitting the
  /// complement — call sites bracket the consuming gate(s) with X…X.
  std::pair<uint32_t, bool>
  resolveSignal(mockturtle::xag_network::signal sig);
  uint32_t processNode(mockturtle::xag_network::node node);

  // ── Circuit generators (one per algorithm case) ──────────────────────
  uint32_t processXorNode(mockturtle::xag_network::node node);  // Fig 5
  uint32_t processAndNode(mockturtle::xag_network::node node);  // routes to:
  uint32_t emitAndBothPI(mockturtle::xag_network::node node,    // Fig 8
                         std::vector<mockturtle::xag_network::signal> &fanins);
  uint32_t emitAndOutput(mockturtle::xag_network::node node,    // Fig 2
                         std::vector<mockturtle::xag_network::signal> &fanins);
  uint32_t emitAndOnePI(mockturtle::xag_network::node node,     // Fig 12
                        std::vector<mockturtle::xag_network::signal> &fanins);

  // ── Utility ──────────────────────────────────────────────────────────
  bool isSimpleNode(mockturtle::xag_network::node node) const;
  bool isOutputNode(mockturtle::xag_network::node node) const;
  // Thin forwarder to gadgets::emitGate — the shared gate/ladder/adjoint
  // primitives live in ToffoliGadgets.
  void emitGate(GateType type, std::vector<uint32_t> controls, uint32_t target);

  // ── State ────────────────────────────────────────────────────────────
  const mockturtle::xag_network &xag_;
  std::unordered_set<uint32_t> po_nodes_;
  std::unordered_map<uint32_t, uint32_t> node_to_qubit_;
  uint32_t next_qubit_ = 0;
  QCGateList result_;
};

} // namespace xagtdep

#endif // PROPOSEDMETHOD_H
