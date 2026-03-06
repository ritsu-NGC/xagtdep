// XagContext.h — Shared data structure flowing between NewMethod → XAG → QC
// passes

#ifndef XAGCONTEXT_H
#define XAGCONTEXT_H

#include <caterpillar/synthesis/strategies/action.hpp>
#include <mockturtle/networks/xag.hpp>

namespace dagtdep {

using xag_steps_t =
    std::vector<std::pair<mockturtle::xag_network::node,
                          caterpillar::mapping_strategy_action>>;

/// Holds the mockturtle XAG network that flows through the pipeline.
/// Built by NewMethodAnalysis, optimized by XAGPass, consumed by QCPass.
struct XagContext {
  mockturtle::xag_network xag;
  bool optimized = false; // set to true after XAGPass runs the strategy

  // Populated by XAGPass: ordered compute/uncompute step sequence.
  // Consumed by QCPass when synthesizing the quantum circuit.
  xag_steps_t steps;
};

} // namespace dagtdep

#endif // XAGCONTEXT_H
