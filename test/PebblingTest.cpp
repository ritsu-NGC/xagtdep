// PebblingTest.cpp — unit tests for the sequence-driven pebbling pipeline:
// BennettSequence (order, PO-cone restriction), the PebblingMethod validator
// (one negative test per rule, rebuild/squat-and-restore acceptance),
// SequenceJson (round-trip, malformed input), PO edge cases, and the
// Milestone-2 reuse demo (fewer qubits than Bennett; emitted as a
// verification_data dir for the QCEC lane).

#include "BennettSequence.h"
#include "PebblingMethod.h"
#include "QC.h"
#include "QCVerificationCommon.h"
#include "SequenceJson.h"
#include "llvm/Support/raw_ostream.h"
#include <mockturtle/networks/xag.hpp>
#include <string>
#include <sys/stat.h>

using namespace llvm;
using namespace xagtdep;

// ── Small helpers ─────────────────────────────────────────────────────────

static PebbleStep P(NodeIndex n, AncillaId a) {
  return {PebbleAction::Pebble, n, a};
}
static PebbleStep U(NodeIndex n, AncillaId a) {
  return {PebbleAction::Unpebble, n, a};
}
static NodeIndex nidx(const mockturtle::xag_network &xag,
                      mockturtle::xag_network::signal s) {
  return xag.node_to_index(xag.get_node(s));
}
static XagContext mkCtx(const mockturtle::xag_network &xag) {
  XagContext ctx;
  ctx.xag = xag;
  ctx.optimized = true;
  return ctx;
}

/// a, b, c PIs; n1 = AND(a,b); n2 = XOR(n1,c) = PO; d1 = AND(b,c) dangling.
struct SimpleCase {
  mockturtle::xag_network xag;
  NodeIndex n1, n2, d1;
};
static SimpleCase buildSimpleCase() {
  SimpleCase sc;
  auto a = sc.xag.create_pi();
  auto b = sc.xag.create_pi();
  auto c = sc.xag.create_pi();
  auto n1 = sc.xag.create_and(a, b);
  auto n2 = sc.xag.create_xor(n1, c);
  auto d1 = sc.xag.create_and(b, c); // dangling: not in any PO cone
  sc.xag.create_po(n2);
  sc.n1 = nidx(sc.xag, n1);
  sc.n2 = nidx(sc.xag, n2);
  sc.d1 = nidx(sc.xag, d1);
  return sc;
}

/// 4-chain for the Milestone-2 reuse demo: n1=XOR(a,b); n2=AND(n1,c);
/// n3=XOR(n2,a); n4=AND(n3,b) = PO. Bennett needs 4 ancillas (+1 helper);
/// the reuse schedule frees n1's slot for n3 (squat) and REBUILDS n1 on its
/// pinned wire before unpebbling n2 — 3 ancillas (+1 helper).
struct ReuseCase {
  mockturtle::xag_network xag;
  NodeIndex n1, n2, n3, n4;
  PebblingSequence schedule;
};
static ReuseCase buildReuseCase() {
  ReuseCase rc;
  auto a = rc.xag.create_pi();
  auto b = rc.xag.create_pi();
  auto c = rc.xag.create_pi();
  auto n1 = rc.xag.create_xor(a, b);
  auto n2 = rc.xag.create_and(n1, c);
  auto n3 = rc.xag.create_xor(n2, a);
  auto n4 = rc.xag.create_and(n3, b);
  rc.xag.create_po(n4);
  rc.n1 = nidx(rc.xag, n1);
  rc.n2 = nidx(rc.xag, n2);
  rc.n3 = nidx(rc.xag, n3);
  rc.n4 = nidx(rc.xag, n4);
  rc.schedule = {
      P(rc.n1, 0), // compute n1
      P(rc.n2, 1), // n2 reads n1@anc0 (pins it for U(n2))
      U(rc.n1, 0), // free the slot early...
      P(rc.n3, 0), // ...and reuse it for n3 (squat) — literal rule (b)
                   // would forbid this; V7 semantics allow it
      P(rc.n4, 2), // PO
      U(rc.n3, 0), // n3's read wire anc1 still holds n2 — V7 ok
      P(rc.n1, 0), // REBUILD n1 on its pinned wire (D1)
      U(rc.n2, 1), // V7: anc0 holds n1 again — exact adjoint valid
      U(rc.n1, 0),
  };
  return rc;
}

/// Expect translate(ctx, seq) to throw PebblingError with the given rule tag
/// and step index.
static bool expectError(const mockturtle::xag_network &xag,
                        const PebblingSequence &seq, const char *rule,
                        size_t step, const char *label) {
  auto ctx = mkCtx(xag);
  try {
    PebblingMethod::translate(ctx, seq);
  } catch (const PebblingError &e) {
    bool ok = e.step == step &&
              std::string(e.what()).find(std::string("(") + rule + ")") !=
                  std::string::npos;
    errs() << "[PebblingTest::" << label << "] "
           << (ok ? "PASSED" : "FAILED (wrong rule/step)") << " — " << e.what()
           << "\n";
    return ok;
  }
  errs() << "[PebblingTest::" << label << "] FAILED — no error thrown\n";
  return false;
}

// ── Tests ─────────────────────────────────────────────────────────────────

/// Bennett: topo pebbles, reverse non-PO unpebbles, dangling gates skipped.
static bool testBennettBuilder() {
  auto sc = buildSimpleCase();
  auto seq = BennettSequence::build(sc.xag);

  bool pass = seq.size() == 3;
  pass = pass && seq[0].action == PebbleAction::Pebble && seq[0].node == sc.n1 &&
         seq[0].anc == 0;
  pass = pass && seq[1].action == PebbleAction::Pebble && seq[1].node == sc.n2 &&
         seq[1].anc == 1;
  pass = pass && seq[2].action == PebbleAction::Unpebble &&
         seq[2].node == sc.n1 && seq[2].anc == 0;
  for (const auto &s : seq)
    pass = pass && s.node != sc.d1; // PO-cone restriction

  errs() << "[PebblingTest::BennettBuilder] steps=" << seq.size() << " "
         << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

/// One negative test per validator rule, each checking rule tag + step index.
static bool testValidatorNegatives() {
  auto sc = buildSimpleCase();
  bool pass = true;

  pass &= expectError(sc.xag, {P(sc.n2, 0)}, "V1", 0, "V1.deadFanin");
  pass &= expectError(sc.xag, {P(sc.n1, 0), P(sc.n2, 0)}, "V2", 1,
                      "V2.targetOccupied");
  pass &= expectError(sc.xag, {P(sc.n1, 0), P(sc.n1, 1)}, "V5", 1,
                      "V5.alreadyLive");
  pass &= expectError(sc.xag, {U(sc.n1, 0)}, "V6", 0, "V6.notLive");
  pass &= expectError(sc.xag, {P(sc.n1, 0), U(sc.n1, 1)}, "V6", 1,
                      "V6.wrongAncilla");
  pass &= expectError(sc.xag,
                      {P(sc.n1, 0), P(sc.n2, 1), U(sc.n1, 0), U(sc.n2, 1)},
                      "V7", 3, "V7.inputGone");
  pass &= expectError(sc.xag, {P(sc.n1, 0)}, "end", PebblingError::kNoStep,
                      "End.nonPoLive");
  // structure: node out of range / not a gate / absurd ancilla id
  pass &= expectError(sc.xag, {P(999, 0)}, "structure", 0, "Struct.nodeRange");
  pass &= expectError(sc.xag, {P(1, 0)}, "structure", 0, "Struct.piNotGate");
  pass &= expectError(sc.xag, {P(sc.n1, 50)}, "structure", 0,
                      "Struct.ancillaCap");

  errs() << "[PebblingTest::ValidatorNegatives] "
         << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

/// The reuse schedule (squat-and-restore + rebuild) must be ACCEPTED — this
/// is the schedule shape literal rule (b) would reject — and the same
/// schedule without the rebuild must fail V7 at U(n2).
static bool testRebuildAndSquat() {
  auto rc = buildReuseCase();
  auto ctx = mkCtx(rc.xag);
  bool pass = true;

  try {
    QCGateList gl = PebblingMethod::translate(ctx, rc.schedule);
    // 3 PIs + 3 ancillas + 1 shared helper (n4 is an AND PO) = 7 wires.
    pass = gl.num_qubits == 7 && gl.num_ancillas == 4 &&
           gl.output_qubit == 3 + 2 && !gl.gates.empty();
    errs() << "[PebblingTest::RebuildAccepted] num_qubits=" << gl.num_qubits
           << " output_qubit=" << gl.output_qubit << " "
           << (pass ? "PASSED" : "FAILED") << "\n";
  } catch (const std::exception &e) {
    errs() << "[PebblingTest::RebuildAccepted] FAILED — " << e.what() << "\n";
    pass = false;
  }

  // Drop the rebuild: U(n2,1) is now step 6 and must fail V7.
  PebblingSequence broken = {
      P(rc.n1, 0), P(rc.n2, 1), U(rc.n1, 0), P(rc.n3, 0),
      P(rc.n4, 2), U(rc.n3, 0), U(rc.n2, 1), U(rc.n1, 0),
  };
  pass &= expectError(rc.xag, broken, "V7", 6, "V7.rebuildMissing");

  return pass;
}

/// Milestone-2 core assertion: the reuse schedule uses fewer qubits than
/// Bennett on the same XAG (equal function is checked by the QCEC lane on
/// the emitted demo dir).
static bool testReuseFewerQubits() {
  auto rc = buildReuseCase();
  auto ctx = mkCtx(rc.xag);

  QCGateList reuse = PebblingMethod::translate(ctx, rc.schedule);
  QCGateList bennett = PebblingMethod::translate(ctx); // empty -> Bennett

  bool pass = reuse.num_qubits == 7 && bennett.num_qubits == 8 &&
              reuse.num_qubits < bennett.num_qubits;
  errs() << "[PebblingTest::ReuseFewerQubits] reuse=" << reuse.num_qubits
         << " bennett=" << bennett.num_qubits << " "
         << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

/// SequenceJson: round-trip (string + file) and malformed-input rejection.
static bool testSequenceJson() {
  bool pass = true;

  PebblingSequence seq = {P(12, 0), P(13, 1), U(12, 0)};
  std::string json = sequenceToJSON(seq);
  PebblingSequence back = sequenceFromJSON(json);
  pass &= back.size() == seq.size();
  for (size_t i = 0; pass && i < seq.size(); ++i)
    pass &= back[i].action == seq[i].action && back[i].node == seq[i].node &&
            back[i].anc == seq[i].anc;

  const std::string path = "pebbling_test_seq.json";
  pass &= writeSequenceFile(path, seq);
  PebblingSequence fromFile = readSequenceFile(path);
  pass &= fromFile.size() == seq.size();

  auto expectBad = [&](const std::string &s, const char *label) {
    try {
      sequenceFromJSON(s);
      errs() << "[PebblingTest::Json." << label << "] FAILED — accepted\n";
      return false;
    } catch (const PebblingError &) {
      return true;
    }
  };
  pass &= expectBad("not json at all", "parse");
  pass &= expectBad("{\"steps\":[{\"action\":\"dance\",\"node\":1,\"anc\":0}]}",
                    "badAction");
  pass &= expectBad("{\"steps\":[{\"action\":\"pebble\",\"anc\":0}]}",
                    "missingNode");
  pass &= expectBad("{\"steps\":[{\"action\":\"pebble\",\"node\":-3,\"anc\":0}]}",
                    "negativeNode");
  pass &= expectBad(
      "{\"num_ancillas\":9,\"steps\":[{\"action\":\"pebble\",\"node\":4,"
      "\"anc\":0}]}",
      "numAncillasMismatch");

  // Structure dump sanity: mentions the gates and PIs.
  auto sc = buildSimpleCase();
  std::string dump = xagStructureToJSON(sc.xag);
  pass &= dump.find("\"num_pis\":3") != std::string::npos &&
          dump.find("\"and\"") != std::string::npos &&
          dump.find("\"xor\"") != std::string::npos;

  errs() << "[PebblingTest::SequenceJson] " << (pass ? "PASSED" : "FAILED")
         << "\n";
  return pass;
}

/// POs that are PIs (plain and complemented) have no pebble step; the
/// translator resolves them at PO time (X in place for a complement).
static bool testPoIsPi() {
  mockturtle::xag_network xag;
  auto a = xag.create_pi();
  auto b = xag.create_pi();
  xag.create_po(a);
  xag.create_po(xag.create_not(b));
  auto ctx = mkCtx(xag);

  QCGateList gl = PebblingMethod::translate(ctx); // Bennett = empty sequence
  bool pass = gl.num_qubits == 2 && gl.num_ancillas == 0 &&
              gl.gates.size() == 1 && gl.gates[0].type == GateType::X &&
              gl.gates[0].target == 1 && gl.output_qubit == 1;
  errs() << "[PebblingTest::PoIsPi] gates=" << gl.gates.size()
         << " output_qubit=" << gl.output_qubit << " "
         << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

/// End-to-end through QC::evaluate with the new enum value (Bennett path).
static bool testEvaluatePipeline() {
  auto sc = buildSimpleCase();
  auto ctx = mkCtx(sc.xag);

  QCGateList gl = PebblingMethod::translate(ctx);
  // 3 PIs + 2 ancillas (n1, n2), XOR PO -> no helper wire.
  bool pass = gl.num_qubits == 5 && gl.output_qubit == 4 && !gl.gates.empty();

  QC synthesizer;
  synthesizer.evaluate(ctx, SynthesisAlgorithm::PebblingMethod);
  pass &= !synthesizer.getQASM().empty();

  errs() << "[PebblingTest::EvaluatePipeline] num_qubits=" << gl.num_qubits
         << " " << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

/// Milestone-2 emission: write the reuse case (hand XAG + hand schedule) as
/// a verification_data-style dir so the Python QCEC lane can prove
/// equal-function-at-fewer-qubits. Verify with:
///   python3 test/verify_circuits.py --strict build/verification_data_pebbling_demo
static bool testEmitReuseDemo() {
  auto rc = buildReuseCase();
  const std::string dir = "verification_data_pebbling_demo";
  mkdir(dir.c_str(), 0755);

  xagtdep::test::XagCaseConfig cfg{}; // all-zero: hand-built case
  bool pass =
      xagtdep::test::runXagCase(rc.xag, cfg, 0, dir, "all", &rc.schedule);
  errs() << "[PebblingTest::EmitReuseDemo] wrote " << dir << "/ "
         << (pass ? "PASSED" : "FAILED") << "\n";
  return pass;
}

// ── Main ─────────────────────────────────────────────────────────────────

int main() {
  bool pass = true;
  pass &= testBennettBuilder();
  pass &= testValidatorNegatives();
  pass &= testRebuildAndSquat();
  pass &= testReuseFewerQubits();
  pass &= testSequenceJson();
  pass &= testPoIsPi();
  pass &= testEvaluatePipeline();
  pass &= testEmitReuseDemo();

  errs() << "\n[PebblingTest] " << (pass ? "ALL PASSED" : "FAILURES") << "\n";
  return pass ? 0 : 1;
}
