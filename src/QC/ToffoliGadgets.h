// ToffoliGadgets.h — shared relative-phase Toffoli gadget provider.
//
// Single home for the gate-level gadgets the synthesis methods emit, so a
// SPEC fix to a ladder lands in one place instead of being copied across
// ExistingMethod / ProposedMethod / PebblingMethod.
//
// Design: these functions never allocate or number qubits. The caller owns
// wire allocation and passes explicit wire indices; each function only
// appends GateOps to `out`. That is what lets a scheduling caller (Pebbling)
// and the DFS routers (Existing/Proposed) share the same primitives, and it
// makes the extraction byte-identity-preserving (the wire allocator never
// moves).
//
// The gadget family selects the primary-output core's ladder:
//   RelativePhase (ω, Proposed) — CC-iωZ / CC-iωZ† (6 T at a PO AND).
//   Exact         (Existing)    — CC-iZ  / CC-iZ†  (8 T at a PO AND).
// The intermediate both-inputs-live AND is family-independent (both expand to
// the identical 4-T string), so it takes no family argument.

#ifndef TOFFOLIGADGETS_H
#define TOFFOLIGADGETS_H

#include "QCGateList.h"
#include <cstddef>
#include <cstdint>
#include <vector>

namespace xagtdep {

/// Gadget family for the primary-output AND core.
///   RelativePhase — ω ladders (Proposed, Algorithm 2): cheaper, leaves a
///                   measurement-invisible residual phase.
///   Exact         — exact ladders (Existing, Algorithm 1): phase-exact,
///                   +2 T per PO-AND core.
enum class GadgetFamily { RelativePhase, Exact };

namespace gadgets {

/// Append one gate to `out` (the single shared emit primitive).
void emitGate(QCGateList &out, GateType type, std::vector<uint32_t> controls,
              uint32_t target);

/// Replay out.gates[begin,end) in reverse with T<->Tdg — the exact adjoint
/// used by pebbling unpebble steps and the DFS output-gadget uncompute.
void appendAdjoint(QCGateList &out, std::size_t begin, std::size_t end);

// ── Decomposition ladders (q2 is the phase-carrying wire) ─────────────────
void emitCCiwZ(QCGateList &out, uint32_t q0, uint32_t q1,
               uint32_t q2); // Fig 9  (3 T)
void emitCCiwZdg(QCGateList &out, uint32_t q0, uint32_t q1,
                 uint32_t q2); // Fig 10 (3 T)
void emitCCiZ(QCGateList &out, uint32_t q0, uint32_t q1,
              uint32_t q2); // Fig 6  (4 T)
void emitCCiZdg(QCGateList &out, uint32_t q0, uint32_t q1,
                uint32_t q2); // Fig 7  (4 T)

/// Fig 8: both-inputs-live relative-phase AND onto the |0> `target` (4 T, no
/// helper). Family-independent. Complemented inputs bracket the whole gadget:
///   [X a][X b] H(t) Tdg(t) CC-iωZ(a,b,t) H(t) [X a][X b]
void emitAndIntermediateLive(QCGateList &out, uint32_t aWire, uint32_t bWire,
                             uint32_t target, bool aComp, bool bComp);

/// Fig 1/2: both-inputs-live AND at a primary output onto the |0> `target`,
/// using a dirty-borrowed `helper` wire (restored within the emitted window).
/// `ctrl` is the ladder control, `cnotSrc` the CNOT source. The family picks
/// the ladder (ω: 6 T / exact: 8 T):
///   [X c][X n] H(t) CC(ctrl,t,h) CNOT(cnotSrc->h) CC†(ctrl,t,h) H(t)
///              CNOT(cnotSrc->h) [X c][X n]
void emitAndOutputLive(QCGateList &out, GadgetFamily family, uint32_t ctrl,
                       uint32_t cnotSrc, uint32_t target, uint32_t helper,
                       bool ctrlComp, bool cnotComp);

/// Fig 5: XOR of two live wires onto the |0> `target`. A complemented input
/// becomes an X on the target (never on the source):
///   CNOT(a->t) [X t] CNOT(b->t) [X t]
void emitXorLive(QCGateList &out, uint32_t aWire, uint32_t bWire,
                 uint32_t target, bool aComp, bool bComp);

} // namespace gadgets
} // namespace xagtdep

#endif // TOFFOLIGADGETS_H
