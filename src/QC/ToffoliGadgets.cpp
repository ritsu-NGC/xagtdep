// ToffoliGadgets.cpp — see ToffoliGadgets.h. Bodies are the pre-refactor
// gadgets, moved verbatim so the emitted GateOp sequence is unchanged.

#include "ToffoliGadgets.h"
#include <utility>

namespace xagtdep {
namespace gadgets {

void emitGate(QCGateList &out, GateType type, std::vector<uint32_t> controls,
              uint32_t target) {
  out.gates.push_back({type, std::move(controls), target});
}

void appendAdjoint(QCGateList &out, std::size_t begin, std::size_t end) {
  for (std::size_t i = end; i > begin; --i) {
    const auto &gate = out.gates[i - 1];
    GateType dag = gate.type;
    switch (gate.type) {
    case GateType::T:
      dag = GateType::Tdg;
      break;
    case GateType::Tdg:
      dag = GateType::T;
      break;
    default:
      break;
    }
    emitGate(out, dag, gate.controls, gate.target);
  }
}

// ── Fig 9: relative-phase CC-iωZ (3 T, 6 CNOT) ───────────────────────────
void emitCCiwZ(QCGateList &out, uint32_t q0, uint32_t q1, uint32_t q2) {
  emitGate(out, GateType::CNOT, {q2}, q0);
  emitGate(out, GateType::CNOT, {q1}, q2);
  emitGate(out, GateType::CNOT, {q0}, q1);
  emitGate(out, GateType::T, {}, q0);
  emitGate(out, GateType::Tdg, {}, q1);
  emitGate(out, GateType::T, {}, q2);
  emitGate(out, GateType::CNOT, {q0}, q1);
  emitGate(out, GateType::CNOT, {q1}, q2);
  emitGate(out, GateType::CNOT, {q2}, q0);
}

// ── Fig 10: CC-iωZ† — same network with T <-> T†. ────────────────────────
void emitCCiwZdg(QCGateList &out, uint32_t q0, uint32_t q1, uint32_t q2) {
  emitGate(out, GateType::CNOT, {q2}, q0);
  emitGate(out, GateType::CNOT, {q1}, q2);
  emitGate(out, GateType::CNOT, {q0}, q1);
  emitGate(out, GateType::Tdg, {}, q0);
  emitGate(out, GateType::T, {}, q1);
  emitGate(out, GateType::Tdg, {}, q2);
  emitGate(out, GateType::CNOT, {q0}, q1);
  emitGate(out, GateType::CNOT, {q1}, q2);
  emitGate(out, GateType::CNOT, {q2}, q0);
}

// ── Fig 6: exact CC-iZ = diag(1,1,1,1,1,1,i,-i) (4 T, 6 CNOT). ───────────
// A leading T†(q2) prepended to the ω ladder.
void emitCCiZ(QCGateList &out, uint32_t q0, uint32_t q1, uint32_t q2) {
  emitGate(out, GateType::Tdg, {}, q2);
  emitGate(out, GateType::CNOT, {q2}, q0);
  emitGate(out, GateType::CNOT, {q1}, q2);
  emitGate(out, GateType::CNOT, {q0}, q1);
  emitGate(out, GateType::T, {}, q0);
  emitGate(out, GateType::Tdg, {}, q1);
  emitGate(out, GateType::T, {}, q2);
  emitGate(out, GateType::CNOT, {q0}, q1);
  emitGate(out, GateType::CNOT, {q1}, q2);
  emitGate(out, GateType::CNOT, {q2}, q0);
}

// ── Fig 7: exact CC-iZ† (4 T, 6 CNOT). A trailing T(q2) after the ω† ladder.
void emitCCiZdg(QCGateList &out, uint32_t q0, uint32_t q1, uint32_t q2) {
  emitGate(out, GateType::CNOT, {q2}, q0);
  emitGate(out, GateType::CNOT, {q1}, q2);
  emitGate(out, GateType::CNOT, {q0}, q1);
  emitGate(out, GateType::Tdg, {}, q0);
  emitGate(out, GateType::T, {}, q1);
  emitGate(out, GateType::Tdg, {}, q2);
  emitGate(out, GateType::CNOT, {q0}, q1);
  emitGate(out, GateType::CNOT, {q1}, q2);
  emitGate(out, GateType::CNOT, {q2}, q0);
  emitGate(out, GateType::T, {}, q2);
}

void emitAndIntermediateLive(QCGateList &out, uint32_t aWire, uint32_t bWire,
                             uint32_t target, bool aComp, bool bComp) {
  if (aComp)
    emitGate(out, GateType::X, {}, aWire);
  if (bComp)
    emitGate(out, GateType::X, {}, bWire);

  emitGate(out, GateType::H, {}, target);
  emitGate(out, GateType::Tdg, {}, target);
  emitCCiwZ(out, aWire, bWire, target);
  emitGate(out, GateType::H, {}, target);

  if (aComp)
    emitGate(out, GateType::X, {}, aWire);
  if (bComp)
    emitGate(out, GateType::X, {}, bWire);
}

void emitAndOutputLive(QCGateList &out, GadgetFamily family, uint32_t ctrl,
                       uint32_t cnotSrc, uint32_t target, uint32_t helper,
                       bool ctrlComp, bool cnotComp) {
  if (ctrlComp)
    emitGate(out, GateType::X, {}, ctrl);
  if (cnotComp)
    emitGate(out, GateType::X, {}, cnotSrc);

  emitGate(out, GateType::H, {}, target);
  if (family == GadgetFamily::Exact) {
    emitCCiZ(out, ctrl, target, helper);
    emitGate(out, GateType::CNOT, {cnotSrc}, helper);
    emitCCiZdg(out, ctrl, target, helper);
  } else {
    emitCCiwZ(out, ctrl, target, helper);
    emitGate(out, GateType::CNOT, {cnotSrc}, helper);
    emitCCiwZdg(out, ctrl, target, helper);
  }
  emitGate(out, GateType::H, {}, target);
  emitGate(out, GateType::CNOT, {cnotSrc}, helper); // restores the helper

  if (ctrlComp)
    emitGate(out, GateType::X, {}, ctrl);
  if (cnotComp)
    emitGate(out, GateType::X, {}, cnotSrc);
}

void emitXorLive(QCGateList &out, uint32_t aWire, uint32_t bWire,
                 uint32_t target, bool aComp, bool bComp) {
  emitGate(out, GateType::CNOT, {aWire}, target);
  if (aComp)
    emitGate(out, GateType::X, {}, target);
  emitGate(out, GateType::CNOT, {bWire}, target);
  if (bComp)
    emitGate(out, GateType::X, {}, target);
}

} // namespace gadgets
} // namespace xagtdep
