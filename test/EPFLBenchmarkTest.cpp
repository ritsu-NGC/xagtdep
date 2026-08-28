// EPFLBenchmarkTest.cpp — smoke-tests every EPFL combinational benchmark
// (.aig) through AIGReader and the AIGToQC pipeline, and logs T-counts.
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
//
// T-counts are written to a CSV log file (default: epfl_benchmarks.csv in
// the build directory, or overridden by EPFL_LOG_FILE env var).

#include "AIGReader.h"
#include "AIGToQC.h"
#include "QCGateList.h"
#include "llvm/Support/raw_ostream.h"
#include <cstdlib>
#include <fstream>
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

struct Result {
  std::string name;
  uint32_t num_inputs;
  uint32_t num_outputs;
  uint32_t num_ands;
  uint32_t num_qubits;
  uint32_t t_count;
  uint32_t total_gates;
  bool synth_skipped;
  bool passed;
};

// Count T-gates in a gate list.
static uint32_t countTgates(const QCGateList &gl) {
  uint32_t count = 0;
  for (const auto &gate : gl.gates) {
    if (gate.type == GateType::T || gate.type == GateType::Tdg) {
      count++;
    }
  }
  return count;
}

// Returns false on any failure and populates the result struct.
static bool runOne(const Circuit &c, Result &result) {
  result.name = c.name;
  result.passed = true;
  result.synth_skipped = false;
  result.num_qubits = 0;
  result.t_count = 0;
  result.total_gates = 0;

  // Step 1 & 2: parse + structural checks.
  AIG aig;
  try {
    aig = AIGReader::readFromFile(c.path);
  } catch (const std::exception &e) {
    llvm::errs() << "[EPFLBenchmark] " << c.name << ": PARSE ERROR — "
                 << e.what() << "\n";
    result.passed = false;
    return false;
  }

  result.num_inputs = aig.num_inputs;
  result.num_outputs = aig.num_outputs;
  result.num_ands = aig.num_ands;

  result.passed &= (aig.num_inputs > 0);
  result.passed &= (aig.num_outputs > 0);
  result.passed &= (aig.num_ands > 0);

  // Step 3: XAG conversion.
  mockturtle::xag_network xag;
  try {
    xag = AIGReader::toXagNetwork(aig);
  } catch (const std::exception &e) {
    llvm::errs() << "[EPFLBenchmark] " << c.name << ": XAG CONV ERROR — "
                 << e.what() << "\n";
    result.passed = false;
    return false;
  }

  result.passed &= (xag.num_pis() == aig.num_inputs);
  result.passed &= (xag.num_pos() > 0);
  result.passed &= (xag.num_gates() > 0);

  // Step 4: QC synthesis (skipped for very large circuits).
  if (!skipSynth(c.name)) {
    QCGateList gl;
    try {
      gl = AIGToQC::synthesize(c.path);
    } catch (const std::exception &e) {
      llvm::errs() << "[EPFLBenchmark] " << c.name << ": SYNTH ERROR — "
                   << e.what() << "\n";
      result.passed = false;
      return false;
    }
    result.num_qubits = gl.num_qubits;
    result.total_gates = gl.gates.size();
    result.t_count = countTgates(gl);
    result.passed &= (gl.num_pis > 0 && !gl.gates.empty());
  } else {
    result.synth_skipped = true;
  }

  llvm::errs() << "[EPFLBenchmark] " << c.name << ": "
               << (result.passed ? "PASSED" : "FAILED") << " (ins=" << aig.num_inputs
               << " outs=" << aig.num_outputs << " ands=" << aig.num_ands;
  if (!result.synth_skipped) {
    llvm::errs() << " | qubits=" << result.num_qubits << " T-count=" << result.t_count
                 << " total_gates=" << result.total_gates;
  } else {
    llvm::errs() << " [synth skipped]";
  }
  llvm::errs() << ")\n";
  return result.passed;
}

// Write results to CSV file.
static void writeLogFile(const std::string &logfile,
                         const std::vector<Result> &results) {
  std::ofstream out(logfile);
  if (!out) {
    llvm::errs() << "[EPFLBenchmark] WARNING: Could not open log file: "
                 << logfile << "\n";
    return;
  }

  // Header
  out << "name,num_inputs,num_outputs,num_ands,num_qubits,t_count,total_gates,"
         "synth_skipped,passed\n";

  // Data rows
  for (const auto &r : results) {
    out << r.name << "," << r.num_inputs << "," << r.num_outputs << ","
        << r.num_ands << "," << r.num_qubits << "," << r.t_count << ","
        << r.total_gates << "," << (r.synth_skipped ? "1" : "0") << ","
        << (r.passed ? "1" : "0") << "\n";
  }

  out.close();
  llvm::errs() << "[EPFLBenchmark] Wrote results to: " << logfile << "\n";
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

  std::vector<Result> results;
  bool all_pass = true;
  for (const auto &c : circuits) {
    Result r;
    all_pass &= runOne(c, r);
    results.push_back(r);
  }

  // Determine log file path: check env var, fall back to default.
  const char *logfile_env = std::getenv("EPFL_LOG_FILE");
  std::string logfile = logfile_env ? std::string(logfile_env)
                                    : "epfl_benchmarks.csv";
  writeLogFile(logfile, results);

  llvm::errs() << "[EPFLBenchmark] Overall: " << (all_pass ? "PASSED" : "FAILED")
               << "\n";
  return all_pass ? 0 : 1;
}
