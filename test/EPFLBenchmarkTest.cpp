// EPFLBenchmarkTest.cpp — smoke-tests every EPFL combinational benchmark
// (.aig) through AIGReader and the AIGToQC pipeline.
//
// For each circuit we check:
//   1. AIGReader can parse the binary file without throwing.
//   2. The parsed AIG has at least one primary input, one primary output,
//      and at least one AND gate.
//   3. toXagNetwork() produces a non-trivial xag_network (PIs and gates
//      preserved).
//   4. AIGToQC::synthesize() returns a non-empty gate list.
//
// Large circuits (multiplier, div, log2, sqrt, hyp, mem_ctrl) are parsed
// and converted to XAG only; full QC synthesis is skipped with a note
// because caterpillar's pebbling strategy can be slow on very large graphs.

#include "AIGReader.h"
#include "AIGToQC.h"
#include "llvm/Support/raw_ostream.h"
#include <string>
#include <vector>

using namespace xagtdep;

// Circuits large enough that full QC synthesis is impractical in a test run.
static const std::vector<std::string> kSkipSynthesis = {
    "div", "hyp", "log2", "mem_ctrl", "multiplier", "sqrt"};

static bool skipSynth(const std::string &name) {
  for (const auto &s : kSkipSynthesis)
    if (name == s)
      return true;
  return false;
}

struct Circuit {
  std::string name;
  std::string path;
};

// Returns false on any failure and prints a diagnostic line.
static bool runOne(const Circuit &c) {
  bool pass = true;

  // Step 1 & 2: parse + structural checks.
  AIG aig;
  try {
    aig = AIGReader::readFromFile(c.path);
  } catch (const std::exception &e) {
    llvm::errs() << "[EPFLBenchmark] " << c.name << ": PARSE ERROR — "
                 << e.what() << "\n";
    return false;
  }

  pass &= (aig.num_inputs > 0);
  pass &= (aig.num_outputs > 0);
  pass &= (aig.num_ands > 0);

  // Step 3: XAG conversion.
  mockturtle::xag_network xag;
  try {
    xag = AIGReader::toXagNetwork(aig);
  } catch (const std::exception &e) {
    llvm::errs() << "[EPFLBenchmark] " << c.name << ": XAG CONV ERROR — "
                 << e.what() << "\n";
    return false;
  }

  pass &= (xag.num_pis() == aig.num_inputs);
  pass &= (xag.num_pos() > 0);
  pass &= (xag.num_gates() > 0);

  // Step 4: QC synthesis (skipped for very large circuits).
  if (!skipSynth(c.name)) {
    QCGateList gl;
    try {
      gl = AIGToQC::synthesize(c.path);
    } catch (const std::exception &e) {
      llvm::errs() << "[EPFLBenchmark] " << c.name << ": SYNTH ERROR — "
                   << e.what() << "\n";
      return false;
    }
    pass &= (gl.num_pis > 0 && !gl.gates.empty());
  }

  llvm::errs() << "[EPFLBenchmark] " << c.name << ": "
               << (pass ? "PASSED" : "FAILED") << " (ins=" << aig.num_inputs
               << " outs=" << aig.num_outputs << " ands=" << aig.num_ands
               << (skipSynth(c.name) ? " [synth skipped]" : "") << ")\n";
  return pass;
}

int main(int argc, char **argv) {
  // Base directory is passed as the first argument by the CMake test runner;
  // falls back to the source-tree location for manual runs.
  std::string base = (argc > 1)
                         ? std::string(argv[1])
                         : EPFL_BENCHMARK_DIR;

  const std::vector<Circuit> circuits = {
      // arithmetic
      {"adder",      base + "/arithmetic/adder.aig"},
      {"bar",        base + "/arithmetic/bar.aig"},
      {"div",        base + "/arithmetic/div.aig"},
      {"hyp",        base + "/arithmetic/hyp.aig"},
      {"log2",       base + "/arithmetic/log2.aig"},
      {"max",        base + "/arithmetic/max.aig"},
      {"multiplier", base + "/arithmetic/multiplier.aig"},
      {"sin",        base + "/arithmetic/sin.aig"},
      {"sqrt",       base + "/arithmetic/sqrt.aig"},
      {"square",     base + "/arithmetic/square.aig"},
      // random_control
      {"arbiter",    base + "/random_control/arbiter.aig"},
      {"cavlc",      base + "/random_control/cavlc.aig"},
      {"ctrl",       base + "/random_control/ctrl.aig"},
      {"dec",        base + "/random_control/dec.aig"},
      {"i2c",        base + "/random_control/i2c.aig"},
      {"int2float",  base + "/random_control/int2float.aig"},
      {"mem_ctrl",   base + "/random_control/mem_ctrl.aig"},
      {"priority",   base + "/random_control/priority.aig"},
      {"router",     base + "/random_control/router.aig"},
      {"voter",      base + "/random_control/voter.aig"},
  };

  bool all_pass = true;
  for (const auto &c : circuits)
    all_pass &= runOne(c);

  llvm::errs() << "[EPFLBenchmark] Overall: " << (all_pass ? "PASSED" : "FAILED")
               << "\n";
  return all_pass ? 0 : 1;
}
