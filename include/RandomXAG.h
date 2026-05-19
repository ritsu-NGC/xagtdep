// RandomXAG.h — Random XAG generator with the constraint that every AND node
// has at least one primary input as a child (matching Davio decomposition).

#ifndef RANDOMXAG_H
#define RANDOMXAG_H

#include <cstdint>
#include <mockturtle/networks/xag.hpp>

namespace xagtdep {

struct RandomXAGParams {
  uint32_t num_pis = 4;
  uint32_t num_and_gates = 2;
  uint32_t num_xor_gates = 3;
  uint64_t seed = 0xcafeaffe;
};

/// Generate a random XAG network.
/// Constraint: every AND node has at least one PI (or constant) as a fanin.
/// XOR nodes may use any existing signals as fanins.
mockturtle::xag_network generate_random_xag(const RandomXAGParams &params);

/// Validate that an XAG satisfies the AND-child-is-PI constraint.
bool validate_and_constraint(const mockturtle::xag_network &xag);

} // namespace xagtdep

#endif // RANDOMXAG_H
