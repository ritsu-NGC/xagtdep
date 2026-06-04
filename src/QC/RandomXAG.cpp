// RandomXAG.cpp — Random XAG generator implementation.
//
// Builds XAGs by:
//   1. Creating num_pis primary inputs
//   2. Adding XOR gates (both fanins from any existing signal)
//   3. Adding AND gates (one fanin must be a PI, other from any signal)
//   4. Creating a PO from the last gate output

#include "RandomXAG.h"
#include <cstdint>
#include <limits>
#include <random>
#include <utility>
#include <vector>

namespace xagtdep {

namespace {

// Draw a uniformly-distributed integer in [0, n) directly from the engine.
//
// std::uniform_int_distribution is implementation-defined: libstdc++ (gcc) and
// libc++ (clang) use different mapping algorithms, so the same mt19937_64 seed
// yields different values — and therefore different XAGs — across platforms.
// This unbiased rejection method has fixed behaviour everywhere, restoring
// cross-platform reproducibility for a given seed.
uint64_t bounded(std::mt19937_64 &rng, uint64_t n) {
  // n is always >= 1 at the call sites; guard defensively anyway.
  if (n <= 1)
    return 0;
  const uint64_t max = std::numeric_limits<uint64_t>::max();
  const uint64_t limit = max - (max % n); // largest multiple of n (exclusive).
  uint64_t r;
  do {
    r = rng();
  } while (r >= limit);
  return r % n;
}

} // namespace

mockturtle::xag_network generate_random_xag(const RandomXAGParams &params) {
  mockturtle::xag_network xag;
  std::mt19937_64 rng(params.seed);

  // Pool of all available signals and PI-only signals.
  std::vector<mockturtle::xag_network::signal> all_signals;
  std::vector<mockturtle::xag_network::signal> pi_signals;

  // Create primary inputs.
  for (uint32_t i = 0; i < params.num_pis; ++i) {
    auto pi = xag.create_pi();
    all_signals.push_back(pi);
    pi_signals.push_back(pi);
  }

  auto pick = [&](const std::vector<mockturtle::xag_network::signal> &pool) {
    auto sig = pool[bounded(rng, pool.size())];
    // Randomly complement with 25% probability.
    if (bounded(rng, 4) == 0)
      sig = !sig;
    return sig;
  };

  // Interleave XOR and AND gate creation for variety.
  // Build a schedule: 'x' for XOR, 'a' for AND.
  std::vector<char> schedule;
  for (uint32_t i = 0; i < params.num_xor_gates; ++i)
    schedule.push_back('x');
  for (uint32_t i = 0; i < params.num_and_gates; ++i)
    schedule.push_back('a');
  // Fisher–Yates shuffle. std::shuffle is implementation-defined and would
  // otherwise reintroduce the cross-platform divergence; bounded() keeps this
  // deterministic for a given seed.
  for (size_t i = schedule.size(); i > 1; --i) {
    size_t j = bounded(rng, i);
    std::swap(schedule[i - 1], schedule[j]);
  }

  for (char op : schedule) {
    if (all_signals.size() < 2)
      break;

    if (op == 'x') {
      // XOR: both fanins from any signal.
      auto a = pick(all_signals);
      auto b = pick(all_signals);
      // Avoid trivial XOR of a signal with itself.
      if (xag.get_node(a) == xag.get_node(b) && all_signals.size() >= 3) {
        for (int retry = 0; retry < 5; ++retry) {
          b = pick(all_signals);
          if (xag.get_node(a) != xag.get_node(b))
            break;
        }
      }
      auto out = xag.create_xor(a, b);
      all_signals.push_back(out);
    } else {
      // AND: one fanin must be a PI, other from any signal.
      auto pi_fanin = pick(pi_signals);
      auto other_fanin = pick(all_signals);
      // Avoid AND of a signal with itself.
      if (xag.get_node(pi_fanin) == xag.get_node(other_fanin) &&
          all_signals.size() >= 2) {
        for (int retry = 0; retry < 5; ++retry) {
          other_fanin = pick(all_signals);
          if (xag.get_node(pi_fanin) != xag.get_node(other_fanin))
            break;
        }
      }
      auto out = xag.create_and(pi_fanin, other_fanin);
      all_signals.push_back(out);
    }
  }

  // Create primary output from the last signal (or last gate output).
  if (!all_signals.empty()) {
    xag.create_po(all_signals.back());
  }

  return xag;
}

bool validate_and_constraint(const mockturtle::xag_network &xag) {
  bool valid = true;
  xag.foreach_gate([&](auto node) {
    if (xag.is_and(node)) {
      bool has_pi_child = false;
      xag.foreach_fanin(node, [&](auto sig) {
        auto child = xag.get_node(sig);
        if (xag.is_pi(child) || xag.is_constant(child))
          has_pi_child = true;
      });
      if (!has_pi_child)
        valid = false;
    }
  });
  return valid;
}

} // namespace xagtdep
