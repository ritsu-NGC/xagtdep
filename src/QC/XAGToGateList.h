// XAGToGateList.h — Converts XAG network to quantum gate list via depth-first
// traversal per the SPEC's XAG2QC algorithm.

#ifndef XAGTOGATELIST_H
#define XAGTOGATELIST_H

#include "QCGateList.h"
#include "XagContext.h"
#include <unordered_map>
#include <unordered_set>

namespace xagtdep {

class XAGToGateList {
public:
  /// Traverse ctx.xag depth-first (outputs → inputs) and produce a gate list.
  static QCGateList translate(const XagContext &ctx);

private:
  explicit XAGToGateList(const mockturtle::xag_network &xag);

  /// Process a signal (node + possible complement). Returns the qubit holding
  /// the result; emits X gate if the signal is complemented.
  uint32_t processSignal(mockturtle::xag_network::signal sig);

  /// Process a node. Returns the qubit holding the node's output value.
  uint32_t processNode(mockturtle::xag_network::node node);

  /// AND node: allocate ancilla, emit Toffoli.
  uint32_t processAndNode(mockturtle::xag_network::node node);

  /// XOR node: allocate fresh output qubit, CNOT both children into it.
  uint32_t processXorNode(mockturtle::xag_network::node node);

  /// Returns true if the node is already on a qubit (PI, constant, or visited).
  bool isSimpleNode(mockturtle::xag_network::node node) const;

  /// Replay gates[startIdx..endIdx-1] in reverse order (uncomputation).
  void emitUncompute(size_t startIdx, size_t endIdx);

  void emitGate(GateType type, std::vector<uint32_t> controls, uint32_t target);

  const mockturtle::xag_network &xag_;
  std::unordered_map<uint32_t, uint32_t> node_to_qubit_;
  uint32_t next_qubit_ = 0;
  QCGateList result_;
};

} // namespace xagtdep

#endif // XAGTOGATELIST_H
