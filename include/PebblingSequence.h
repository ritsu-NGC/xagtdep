// PebblingSequence.h — Explicit pebbling schedule for sequence-driven quantum
// circuit synthesis (PebblingMethod). A sequence is an ordered list of
// Pebble/Unpebble steps over XAG gate nodes and ancilla slots; position in the
// vector is execution order. Rebuild is expressed naturally: the same node may
// appear in multiple Pebble steps over time, on the same or a different slot.
//
// Kept dependency-free (std only) — David's SAT solver talks to this type via
// SequenceJson without either side needing mockturtle.

#ifndef PEBBLINGSEQUENCE_H
#define PEBBLINGSEQUENCE_H

#include <cstdint>
#include <stdexcept>
#include <string>
#include <vector>

namespace xagtdep {

using NodeIndex = uint32_t; // == xag.node_to_index(node) on ctx.xag
using AncillaId = uint32_t; // ancilla slot; qubit = num_pis + anc

enum class PebbleAction { Pebble, Unpebble };

struct PebbleStep {
  PebbleAction action;
  NodeIndex node;
  AncillaId anc;
};

using PebblingSequence = std::vector<PebbleStep>; // position = execution order

/// Ancilla count is DERIVED from the sequence (no second source of truth).
inline uint32_t sequenceNumAncillas(const PebblingSequence &seq) {
  uint32_t n = 0;
  for (const auto &s : seq)
    if (s.anc + 1 > n)
      n = s.anc + 1;
  return n;
}

/// Thrown by the sequence validator (rules V1-V7 in PebblingMethod) and by
/// SequenceJson on structurally malformed input. `step` is the index of the
/// offending PebbleStep, or kNoStep for errors not tied to one step (e.g. the
/// end-of-sequence check or JSON parse errors).
class PebblingError : public std::runtime_error {
public:
  static constexpr size_t kNoStep = static_cast<size_t>(-1);

  explicit PebblingError(const std::string &msg, size_t step_idx = kNoStep)
      : std::runtime_error(msg), step(step_idx) {}

  size_t step;
};

} // namespace xagtdep

#endif // PEBBLINGSEQUENCE_H
