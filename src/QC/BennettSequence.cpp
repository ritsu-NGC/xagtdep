// BennettSequence.cpp — see BennettSequence.h.

#include "BennettSequence.h"

#include <mockturtle/views/topo_view.hpp>
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace xagtdep {

PebblingSequence
BennettSequence::build(const mockturtle::xag_network &xag) {
  std::unordered_set<NodeIndex> po_nodes;
  xag.foreach_po([&](auto sig) {
    po_nodes.insert(xag.node_to_index(xag.get_node(sig)));
  });

  // topo_view iterates only the transitive fanin of the POs, in topological
  // order — fanins always precede their fanouts.
  mockturtle::topo_view<mockturtle::xag_network> topo{xag};
  std::vector<NodeIndex> order;
  topo.foreach_node([&](auto node) {
    if (topo.is_constant(node) || topo.is_pi(node))
      return;
    order.push_back(xag.node_to_index(node));
  });

  PebblingSequence seq;
  seq.reserve(order.size() * 2);
  std::unordered_map<NodeIndex, AncillaId> anc_of;
  AncillaId next_anc = 0;
  for (NodeIndex n : order) {
    anc_of[n] = next_anc;
    seq.push_back({PebbleAction::Pebble, n, next_anc});
    ++next_anc;
  }
  for (auto it = order.rbegin(); it != order.rend(); ++it) {
    if (po_nodes.count(*it))
      continue; // POs stay pebbled
    seq.push_back({PebbleAction::Unpebble, *it, anc_of[*it]});
  }
  return seq;
}

} // namespace xagtdep
