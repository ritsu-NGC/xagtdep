// ProposedMethod.h — Algorithm 2 ("Proposed Method") XAG-to-quantum-circuit
// translator. Uses relative-phase Toffoli (CC-iωZ via Figs 8/9/10) and
// NO top-level uncompute.

#ifndef PROPOSEDMETHOD_H
#define PROPOSEDMETHOD_H

#include "QCGateList.h"
#include "XagContext.h"
#include <unordered_map>
#include <unordered_set>

namespace xagtdep {

class ProposedMethod {
public:
  /// Traverse ctx.xag depth-first and produce a gate list per Algorithm 2.
  static QCGateList translate(const XagContext &ctx);

private:
  explicit ProposedMethod(const mockturtle::xag_network &xag);

  // ── Recursive traversal ──────────────────────────────────────────────
  uint32_t processSignal(mockturtle::xag_network::signal sig);
  uint32_t processNode(mockturtle::xag_network::node node);

  // ── Circuit generators (one per algorithm case) ──────────────────────
  uint32_t processXorNode(mockturtle::xag_network::node node);  // Fig 5
  uint32_t processAndNode(mockturtle::xag_network::node node);  // routes to:
  uint32_t emitAndBothPI(mockturtle::xag_network::node node,    // Fig 8
                         uint32_t q_a, uint32_t q_b);
  uint32_t emitAndOutput(mockturtle::xag_network::node node,    // Fig 2
                         std::vector<mockturtle::xag_network::signal> &fanins);
  uint32_t emitAndOnePI(mockturtle::xag_network::node node,     // Fig 12
                        std::vector<mockturtle::xag_network::signal> &fanins);

  // ── Decomposition helpers (exact gate sequences from figures) ────────
  void emitCCiwZ(uint32_t q0, uint32_t q1, uint32_t q2);    // Fig 9
  void emitCCiwZdg(uint32_t q0, uint32_t q1, uint32_t q2);  // Fig 10

  // ── Utility ──────────────────────────────────────────────────────────
  bool isSimpleNode(mockturtle::xag_network::node node) const;
  bool isOutputNode(mockturtle::xag_network::node node) const;
  void appendAdjoint(size_t startIdx, size_t endIdx);
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
