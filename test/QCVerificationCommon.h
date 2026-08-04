#pragma once

#include "PebblingSequence.h"
#include <cstdint>
#include <mockturtle/networks/xag.hpp>
#include <string>

namespace xagtdep {
namespace test {

/// Config echoed into the meta JSON (all-zero for hand-built XAGs).
struct XagCaseConfig {
  uint64_t seed = 0;
  uint32_t pis = 0, ands = 0, xors = 0;
};

/// Run all four translators on an already-built XAG and write
/// xag_<idx>_{current,existing,proposed,pebbling}.json, the XAG structure
/// dump, and the meta JSON into dataDir. `pebbling` (optional) supplies an
/// explicit schedule for PebblingMethod; nullptr = Bennett fallback.
bool runXagCase(const mockturtle::xag_network &xag, const XagCaseConfig &cfg,
                int idx, const std::string &dataDir, const std::string &method,
                const PebblingSequence *pebbling = nullptr);

/// Generate a random XAG from (pis, ands, xors, seed) and run it as above.
bool runOneXag(uint32_t pis, uint32_t ands, uint32_t xors, uint64_t seed,
               int idx, const std::string &dataDir, const std::string &method,
               const PebblingSequence *pebbling = nullptr);

} // namespace test
} // namespace xagtdep
