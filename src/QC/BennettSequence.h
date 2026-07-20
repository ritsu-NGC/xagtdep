// BennettSequence.h — Naive Bennett pebbling schedule: pebble every gate node
// in topological order on a fresh ancilla, then unpebble every non-PO gate
// node in reverse order. POs stay pebbled. This is the stand-in sequence
// producer so PebblingMethod is testable before the SAT solver exists; it is
// the honest qubit-count baseline that optimized schedules then beat.

#ifndef BENNETTSEQUENCE_H
#define BENNETTSEQUENCE_H

#include "PebblingSequence.h"
#include <mockturtle/networks/xag.hpp>

namespace xagtdep {

class BennettSequence {
public:
  /// Build the Bennett schedule for the PO cones of `xag`. Every gate node
  /// (AND and XOR alike — v1 granularity) gets its own ancilla slot; dangling
  /// gates outside every PO cone are skipped, matching what the existing
  /// DFS translators synthesize.
  static PebblingSequence build(const mockturtle::xag_network &xag);
};

} // namespace xagtdep

#endif // BENNETTSEQUENCE_H
