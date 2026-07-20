// PebblingMethod.h — Sequence-driven XAG-to-quantum-circuit translator.
//
// Unlike the three DFS translators, this one is driven by an explicit
// PebblingSequence (ctx.pebbling, or BennettSequence::build(ctx.xag) when
// empty): every Pebble step computes one gate node onto its ancilla wire with
// a self-contained gadget, every Unpebble step replays the EXACT adjoint of
// the recorded gadget window (T<->Tdg, reversed) on the same wires. Freed
// values needed again are re-pebbled (rebuilt), never aliased — qubit slots
// are reused, contents are recomputed.
//
// Gadgets (bodies match ProposedMethod's post-conformance-fix figures):
//   XOR                → Fig 5   (two CNOTs onto the |0> ancilla)
//   AND (intermediate) → Fig 8   (H; T†; CC-iωZ ladder; H — 4 T, no helper)
//   AND (primary out)  → Fig 2's iωZ core (6 T, one shared dirty-borrowed
//                        helper wire; children come from the sequence, not
//                        inlined). Measurement-exact: leaves a residual
//                        diagonal phase ω^{-g(x)} — invisible to
//                        computational-basis measurement, same equivalence
//                        class as ProposedMethod.
//
// Phase soundness: every gadget is a permutation×diagonal unitary on the
// computational basis, and the validator's V7 rule guarantees an unpebble
// sees exactly the basis values its pebble saw — so each pebble/unpebble
// pair cancels per basis state regardless of interleaving. Only PO gadgets
// (never unpebbled) leave their diagonal residual.
//
// A malformed or invalid sequence throws PebblingError (never silently emits
// a wrong circuit).

#ifndef PEBBLINGMETHOD_H
#define PEBBLINGMETHOD_H

#include "PebblingSequence.h"
#include "QCGateList.h"
#include "XagContext.h"
#include <unordered_map>
#include <unordered_set>
#include <vector>

namespace xagtdep {

class PebblingMethod {
public:
  /// Translate using ctx.pebbling, or the Bennett schedule when it is empty.
  static QCGateList translate(const XagContext &ctx);
  /// Translate using an explicit sequence (tests / SAT-solver output).
  static QCGateList translate(const XagContext &ctx,
                              const PebblingSequence &seq);

private:
  explicit PebblingMethod(const mockturtle::xag_network &xag);

  void run(const PebblingSequence &seq);
  void validateStructure(const PebblingSequence &seq) const;
  void doPebble(const PebbleStep &step, size_t step_idx);
  void doUnpebble(const PebbleStep &step, size_t step_idx);
  void finalize();

  /// A resolved fanin: which wire holds it right now, and whether the signal
  /// is complemented. `holds` is only meaningful when from_ancilla is true —
  /// PI and constant wires are immutable at step boundaries and need no V7
  /// tracking.
  struct Fanin {
    uint32_t wire;
    bool complemented;
    bool from_ancilla;
    NodeIndex holds;
  };
  Fanin resolveFanin(mockturtle::xag_network::signal sig, size_t step_idx);

  // ── Gadget emitters (wires fully general — PI, const, or ancilla) ──────
  void emitXorPebble(uint32_t t, const Fanin &f0, const Fanin &f1);
  void emitAndIntermediate(uint32_t t, const Fanin &f0, const Fanin &f1);
  void emitAndOutputCore(uint32_t t, const Fanin &f0, const Fanin &f1);

  // ── Lazy tail-allocated extra wires (cannot collide: one counter) ──────
  uint32_t helperWire(); // shared PO-gadget helper; restored by every gadget
  uint32_t constWire();  // |0> wire backing constant fanins/POs

  // ── Decomposition helpers (copied verbatim from ProposedMethod) ────────
  void emitCCiwZ(uint32_t q0, uint32_t q1, uint32_t q2);   // Fig 9  (3 T)
  void emitCCiwZdg(uint32_t q0, uint32_t q1, uint32_t q2); // Fig 10 (3 T)
  void appendAdjoint(size_t startIdx, size_t endIdx);
  void emitGate(GateType type, std::vector<uint32_t> controls,
                uint32_t target);

  bool isOutputNode(NodeIndex idx) const { return po_nodes_.count(idx) > 0; }
  std::string describeWire(uint32_t wire) const;
  [[noreturn]] void fail(size_t step_idx, const char *rule,
                         const std::string &msg) const;

  // ── State ───────────────────────────────────────────────────────────────
  const mockturtle::xag_network &xag_;
  std::unordered_set<NodeIndex> po_nodes_;
  std::unordered_map<NodeIndex, uint32_t> pi_wire_;
  uint32_t num_pis_ = 0;
  uint32_t num_seq_ancillas_ = 0;
  uint32_t next_extra_ = 0;  // tail allocator for const/helper wires
  int64_t helper_wire_ = -1; // -1 = not yet allocated
  int64_t const_wire_ = -1;

  struct LivePebble {
    AncillaId anc;
    size_t pebbled_at; // step index, for diagnostics
    // (wire -> node it held when this pebble read it); ancilla wires only.
    std::vector<std::pair<uint32_t, NodeIndex>> read_pairs;
    size_t gate_begin, gate_end; // recorded window for the exact adjoint
  };
  std::unordered_map<NodeIndex, uint32_t> node_loc_;    // live node -> qubit
  std::unordered_map<uint32_t, NodeIndex> qubit_holds_; // qubit -> live node
  std::unordered_map<NodeIndex, LivePebble> live_;
  std::unordered_map<uint32_t, size_t> last_writer_step_; // diagnostics

  QCGateList result_;
};

} // namespace xagtdep

#endif // PEBBLINGMETHOD_H
