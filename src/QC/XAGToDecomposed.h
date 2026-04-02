// XAGToDecomposed.h — Algorithm 1 (PARSEXAG): XAG to decomposed quantum
// circuit using relative-phase Toffoli decompositions (T-gate level).
//
// Unlike XAGToGateList which emits abstract Toffoli gates, this translator
// decomposes AND nodes into Clifford+T gate sequences per the spec:
//   Fig. 6  (iZ†)  — AND, both children are PIs
//   Fig. 8  (iωZ†) — AND, one child is PI
//   Fig. 1         — AND, output is overall PO (full H/Z/ancilla circuit)

#ifndef XAGTODECOMPOSED_H
#define XAGTODECOMPOSED_H

#include "QCGateList.h"
#include "XagContext.h"
#include <unordered_map>
#include <unordered_set>

namespace xagtdep {

class XAGToDecomposed {
public:
  static QCGateList translate(const XagContext &ctx);

private:
  explicit XAGToDecomposed(const mockturtle::xag_network &xag);

  // Core recursive traversal (PARSEXAG from Algorithm 1).
  uint32_t processSignal(mockturtle::xag_network::signal sig);
  uint32_t processNode(mockturtle::xag_network::node node);
  uint32_t processXorNode(mockturtle::xag_network::node node);
  uint32_t processAndNode(mockturtle::xag_network::node node);

  // Decomposition helpers — emit gate sequences per spec figures.
  void emitFig6(uint32_t ctrl0, uint32_t ctrl1, uint32_t target);
  void emitFig8(uint32_t ctrl0, uint32_t ctrl1, uint32_t target);
  void emitFig1(uint32_t simpleCtrl, uint32_t complexCtrl, uint32_t target,
                size_t computeStart, size_t computeEnd);

  // Utility.
  bool isSimpleNode(mockturtle::xag_network::node node) const;
  bool isOutputNode(mockturtle::xag_network::node node) const;
  void emitGate(GateType type, std::vector<uint32_t> controls, uint32_t target);
  void emitUncompute(size_t startIdx, size_t endIdx);
  static GateType invertGateType(GateType t);

  const mockturtle::xag_network &xag_;
  std::unordered_map<uint32_t, uint32_t> node_to_qubit_;
  std::unordered_set<uint32_t> output_node_indices_;
  uint32_t next_qubit_ = 0;
  QCGateList result_;
};

} // namespace xagtdep

#endif // XAGTODECOMPOSED_H
