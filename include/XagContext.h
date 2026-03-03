// XagContext.h — Shared data structure flowing between NewMethod → XAG → QC
// passes

#ifndef XAGCONTEXT_H
#define XAGCONTEXT_H

#include <mockturtle/networks/xag.hpp>

namespace dagtdep {

/// Holds the mockturtle XAG network that flows through the pipeline.
/// Built by NewMethodAnalysis, optimized by XAGPass, consumed by QCPass.
struct XagContext {
  mockturtle::xag_network xag;
  bool optimized = false; // set to true after XAG pass runs
};

} // namespace dagtdep

#endif // XAGCONTEXT_H